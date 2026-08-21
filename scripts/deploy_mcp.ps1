# MCP 서버 Cloud Run 배포 (FactChat MCP 커넥터용)
#
# deploy.ps1 도 MCP 를 올리지만 --no-allow-unauthenticated (IAM 전용)라 FactChat 이
# 못 붙는다. 공개 URL 은 여기서만 나온다(ALLOW_UNAUTH, 기본 true).
#
# 사용:
#   .\scripts\deploy_mcp.ps1                          # 레거시 — .env 만 보고 1개 배포
#   .\scripts\deploy_mcp.ps1 -Dept cs                 # 학과 교직원
#   .\scripts\deploy_mcp.ps1 -Dept cs -Audience student
#   .\scripts\deploy_mcp.ps1 -All                     # 전 학과 x 교직원/학생
#
# 설정 원본은 config/common.yaml + config/departments/<학과>.yaml 이다.
# 학과 yaml 은 코퍼스·키를 함께 담으므로 **커밋되지 않는다**(.gitignore).
# 커밋되는 것은 dept.yaml.example 템플릿뿐이다.
# -Dept 를 쓰면 .env 는 읽지 않는다 — .env 는 레거시 경로 전용이다.
#
# 이미지는 학과와 무관하게 동일하다 — Dockerfile 에 학과별 값이 하나도 안 들어가고
# 코퍼스·키는 전부 런타임 env 다. 그래서 **빌드는 한 번만** 하고 그 digest 를 전
# 학과에 배포한다. 학과마다 빌드하면 requirements 가 범위 지정이라 학과별로 다른
# 의존성 버전이 잡힐 수 있고, :latest 태그라 그게 무증상으로 배포된다
# (requirements-mcp.txt 주석에 적힌 그 사고가 학과 수만큼 반복된다).

param(
  [string]$Dept,
  [ValidateSet("staff", "student")][string]$Audience = "staff",
  [switch]$All,
  [switch]$SkipBuild,
  # 요약표에 키를 평문으로 찍는다. 기본은 가린다 — 터미널 스크롤백·화면공유·
  # CI 로그가 키가 새는 가장 흔한 경로이고, -All 이면 그게 학과 수만큼 쌓인다.
  # 커넥터에 실제로 넣어야 할 때만 켤 것.
  [switch]$ShowKeys
)

$ErrorActionPreference = "Stop"
if ($PSVersionTable.PSVersion.Major -ge 7) {
  $PSNativeCommandUseErrorActionPreference = $false
}

Set-Location (Split-Path -Parent $PSScriptRoot)
. (Join-Path $PSScriptRoot "_load_env.ps1")

function Assert-LastExit {
  if ($LASTEXITCODE -ne 0) { throw "gcloud exit $LASTEXITCODE" }
}

function Get-PythonExe {
  $venv = Join-Path (Get-Location) ".venv\Scripts\python.exe"
  if (Test-Path -LiteralPath $venv) { return $venv }
  return "python"
}

# 학과 yaml 이 채우는 키. 반복 배포에서 앞 학과 값이 남지 않게 매번 비운다 —
# 안 비우면 -All 로 여러 학과를 돌 때 앞 학과 값이 남아 조용히 섞인다.
$ConfigKeys = @(
  "GCP_PROJECT_ID", "GCP_REGION", "ARTIFACT_REPO",
  "GCS_HWP_ORIGINAL_BUCKET", "GCS_SOURCE_BUCKET",
  "FIRESTORE_DATABASE", "DOC_STATE_COLLECTION",
  "TOP_K_DEFAULT", "SEARCH_FETCH_MULTIPLIER", "SEARCH_FETCH_MAX",
  "MCP_CONCURRENCY", "ALLOW_UNAUTH",
  "RAG_CORPUS_NAME", "RAG_CORPUS_NAME_STUDENT",
  "DRIVE_IDS", "SYNC_FOLDER_IDS", "STUDENT_FOLDER_IDS",
  "MCP_MIN_INSTANCES", "MCP_SERVICE_NAME",
  "MCP_AUDIENCE", "DEPT_CODE", "DEPT_NAME",
  "MCP_API_KEY", "MCP_API_KEY_STAFF", "MCP_API_KEY_STUDENT"
)

# 학과 yaml -> 환경변수. 코퍼스도 키도 여기서만 온다(.env 는 안 본다).
function Set-DeptConfig {
  param([string]$DeptCode, [string]$AudienceName)

  foreach ($k in $ConfigKeys) { Set-Item -LiteralPath "Env:$k" -Value "" }

  $py = Get-PythonExe
  $lines = & $py scripts/dept_config.py --dept $DeptCode --audience $AudienceName
  Assert-LastExit
  foreach ($line in $lines) {
    if (-not $line -or $line -notmatch "=") { continue }
    $k, $v = $line.Split("=", 2)
    Set-Item -LiteralPath "Env:$k" -Value $v
  }

  # 학과 모드에서는 .env 를 안 본다 — 코퍼스도 키도 학과 yaml 이 원본이다.
  # (.env 는 -Dept 없이 도는 레거시 경로 전용으로 남는다)
  if ([string]::IsNullOrWhiteSpace($env:MCP_API_KEY)) {
    throw "$DeptCode/$AudienceName : keys 를 못 읽었다"
  }
  # MCP_API_KEY_STAFF 는 dept_config 가 같이 내보낸다
  # (Require-McpDeployEnv 의 '학생이 교직원 키를 재사용했나' 검사가 쓴다).
}

# ---- 배포 대상 정하기 ----
$targets = @()
if ($All) {
  $py = Get-PythonExe
  $codes = & $py scripts/dept_config.py --list
  Assert-LastExit
  if (-not $codes) { throw "config/departments 에 학과 yaml 이 없다" }
  foreach ($c in $codes) {
    if (-not $c) { continue }
    foreach ($a in @("staff", "student")) {
      $targets += [pscustomobject]@{ Dept = $c.Trim(); Audience = $a }
    }
  }
} elseif ($Dept) {
  $targets += [pscustomobject]@{ Dept = $Dept; Audience = $Audience }
} else {
  # 레거시 경로: 학과 개념 없이 .env 만 본다. 기존 사용법을 깨지 않는다.
  $targets += [pscustomobject]@{ Dept = ""; Audience = "" }
}

$deptMode = [bool]$targets[0].Dept

# ---- 키 중복 사전 검사 ----
# 배포를 시작한 뒤에 걸리면 절반만 올라간 채로 멈춘다. 먼저 전부 본다.
# 학과가 늘수록 복붙으로 키가 겹치기 쉽고, 겹치면 한 키로 남의 코퍼스가 열린다.
if ($deptMode) {
  $seen = @{}
  foreach ($t in $targets) {
    Set-DeptConfig -DeptCode $t.Dept -AudienceName $t.Audience
    $k = $env:MCP_API_KEY
    $who = "$($t.Dept)/$($t.Audience)"
    if ($seen.ContainsKey($k)) {
      throw "MCP 키 중복: $who 와 $($seen[$k]) 가 같은 키를 쓴다"
    }
    $seen[$k] = $who
  }
  Write-Host "== 대상 $($targets.Count) 개 · 키 중복 없음 =="
}

# ---- 공통값 확보 ----
if ($deptMode) {
  Set-DeptConfig -DeptCode $targets[0].Dept -AudienceName $targets[0].Audience
} else {
  Load-Dotenv
  if (-not $env:MCP_API_KEY) { $env:MCP_API_KEY = $env:MCP_API_KEY_STAFF }
  $svc = Get-McpDeployServiceName
  if (Test-McpStudentTarget $svc) {
    if ($env:RAG_CORPUS_NAME_STUDENT) { $env:RAG_CORPUS_NAME = $env:RAG_CORPUS_NAME_STUDENT }
    if ($env:MCP_API_KEY_STUDENT) { $env:MCP_API_KEY = $env:MCP_API_KEY_STUDENT }
  }
}

$PROJECT_ID = $env:GCP_PROJECT_ID
$REGION = Get-EnvOr GCP_REGION "asia-northeast3"
$REPO = Get-EnvOr ARTIFACT_REPO "rag-mcp"

gcloud config set project $PROJECT_ID
Assert-LastExit
gcloud services enable run.googleapis.com aiplatform.googleapis.com `
  artifactregistry.googleapis.com firestore.googleapis.com --project=$PROJECT_ID
Assert-LastExit

gcloud artifacts repositories describe $REPO --location=$REGION 2>$null
if ($LASTEXITCODE -ne 0) {
  gcloud artifacts repositories create $REPO `
    --repository-format=docker --location=$REGION
  Assert-LastExit
}

# ---- 빌드 1회 ----
$TAG = "$REGION-docker.pkg.dev/$PROJECT_ID/$REPO/mcp:latest"
if (-not $SkipBuild) {
  Write-Host "== 이미지 빌드 (1회, 전 학과 공용) =="
  gcloud builds submit --config=cloudbuild.mcp.yaml --substitutions="_IMAGE=$TAG"
  Assert-LastExit
}

# 태그가 아니라 digest 로 배포한다. :latest 는 다음 빌드에서 다른 이미지를 가리키게
# 되고, 그러면 학과마다 배포 시점이 달라 서로 다른 코드가 돌아도 아무도 모른다.
$DIGEST = gcloud artifacts docker images describe $TAG --format="value(image_summary.digest)"
Assert-LastExit
if ([string]::IsNullOrWhiteSpace($DIGEST)) { throw "이미지 digest 조회 실패: $TAG" }
$IMAGE = "$REGION-docker.pkg.dev/$PROJECT_ID/$REPO/mcp@$DIGEST"
Write-Host "이미지: $IMAGE"

# ---- 배포 ----
$results = @()
foreach ($t in $targets) {
  if ($t.Dept) { Set-DeptConfig -DeptCode $t.Dept -AudienceName $t.Audience }

  Require-McpDeployEnv

  $SERVICE = Get-McpDeployServiceName
  $MCP_API_KEY = $env:MCP_API_KEY
  $ALLOW_UNAUTH = Get-EnvOr ALLOW_UNAUTH "true"
  # --set-env-vars 는 기존 env 를 통째로 치환한다. FIRESTORE_* 를 빼면 (default)
  # Datastore 모드를 보게 되어 검색 결과의 파일명·경로 메타가 조용히 null 이 된다.
  $GCS_HWP_ORIG = Get-EnvOr GCS_HWP_ORIGINAL_BUCKET "unused"
  $GCS_SOURCE = Get-EnvOr GCS_SOURCE_BUCKET "unused"
  $FS_DB = Get-EnvOr FIRESTORE_DATABASE "rag-sync-state"
  $FS_COL = Get-EnvOr DOC_STATE_COLLECTION "doc_state"
  $TOP_K = Get-EnvOr TOP_K_DEFAULT "5"
  $FETCH_MULT = Get-EnvOr SEARCH_FETCH_MULTIPLIER "3"
  $FETCH_MAX = Get-EnvOr SEARCH_FETCH_MAX "60"
  # deploy.ps1 과 같은 기본값을 쓴다. 한쪽만 넘기면 그쪽 재배포마다 조용히 뒤집힌다.
  $MCP_CONCURRENCY = Get-EnvOr MCP_CONCURRENCY "40"
  # 학과가 늘면 이게 곧 청구서다 — 1 은 24시간 상주를 뜻한다.
  $MIN_INSTANCES = Get-EnvOr MCP_MIN_INSTANCES "0"

  Write-Host ""
  Write-Host "== 배포: $SERVICE (min-instances=$MIN_INSTANCES) =="

  $authArgs = @("--allow-unauthenticated")
  if ($ALLOW_UNAUTH -ne "true") { $authArgs = @("--no-allow-unauthenticated") }

  $envVars = "^|^GCP_PROJECT_ID=$PROJECT_ID|GCP_REGION=$REGION|RAG_CORPUS_NAME=$($env:RAG_CORPUS_NAME)|GCS_HWP_ORIGINAL_BUCKET=$GCS_HWP_ORIG|GCS_SOURCE_BUCKET=$GCS_SOURCE|FIRESTORE_DATABASE=$FS_DB|DOC_STATE_COLLECTION=$FS_COL|MCP_API_KEY=$MCP_API_KEY|TOP_K_DEFAULT=$TOP_K|SEARCH_FETCH_MULTIPLIER=$FETCH_MULT|SEARCH_FETCH_MAX=$FETCH_MAX"

  gcloud run deploy $SERVICE `
    --image=$IMAGE `
    --region=$REGION `
    @authArgs `
    --set-env-vars=$envVars `
    --memory=1Gi --cpu=1 --timeout=60 --concurrency=$MCP_CONCURRENCY `
    --min-instances=$MIN_INSTANCES
  Assert-LastExit

  $MCP_URL = gcloud run services describe $SERVICE --region=$REGION --format="value(status.url)"
  Assert-LastExit

  $results += [pscustomobject]@{
    # 학과 코드만 찍으면 20개 표에서 어느 학과인지 못 알아본다.
    # DEPT_NAME 은 학과 yaml 의 name — 표시 전용이고 Cloud Run 엔 안 넘어간다.
    Dept    = if ($env:DEPT_NAME) { $env:DEPT_NAME } else { $env:DEPT_CODE }
    Service = $SERVICE
    Url     = "$MCP_URL/mcp"
    Key     = if ($ShowKeys) { $MCP_API_KEY } else { "***($($MCP_API_KEY.Length)자) -ShowKeys" }
  }
}

# ---- 요약 ----
Write-Host ""
Write-Host "=== FactChat MCP 커넥터 설정 ==="
Write-Host "Transport : Streamable HTTP (또는 HTTP)"
Write-Host "Header    : Authorization: Bearer <Key>  (또는 X-API-Key: <Key>)"
Write-Host ""
$results | Format-Table -AutoSize
Write-Host "Health: <URL 에서 /mcp 를 뺀 주소>/health"
