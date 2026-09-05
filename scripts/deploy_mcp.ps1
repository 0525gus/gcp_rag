# MCP 서버 Cloud Run 배포 (FactChat MCP 커넥터용)
#
# deploy.ps1 도 MCP 를 올리지만 --no-allow-unauthenticated (IAM 전용)라 FactChat 이
# 못 붙는다. 공개 URL 은 여기서만 나온다(ALLOW_UNAUTH, 기본 true).
#
# 사용:
#   .\scripts\deploy_mcp.ps1 -Dept cs                 # 그 학과에 설정된 범위 전부
#   .\scripts\deploy_mcp.ps1 -Dept cs -Audience student  # 하나만
#   .\scripts\deploy_mcp.ps1 -All                     # 전 학과 x 교직원/학생
#
# 설정 원본은 config/common.yaml + config/departments/<학과>.yaml 이다.
# 학과 yaml 은 코퍼스·키를 함께 담으므로 **커밋되지 않는다**(.gitignore).
# 커밋되는 것은 dept.yaml.example 템플릿뿐이다.
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

# ---- 배포 대상 정하기 ----
# -Dept 만 주면 **그 학과에 설정된 범위 전부**다. 예전에는 staff 하나만 올라갔고
# 학생 MCP 는 아무 말 없이 빠졌다 — 콘솔은 둘을 기대해 Ready 확인에서 걸렸고
# ("Ready 상태가 아닌 서비스: rag-mcp-cs-student"), 손으로 돌린 사람은 학생
# 서비스가 낡은 채로 도는 것을 몰랐다(실측).
# 하나만 올리려면 -Audience 를 명시한다.
function Get-DeployTargets {
  param(
    [string]$Dept,
    [string]$Audience,
    [bool]$All,
    [bool]$AudienceExplicit
  )
  $found = @()
  if ($All) {
    foreach ($c in (Get-DepartmentCodes)) {
      foreach ($a in @(Get-DepartmentAudiences -DeptCode $c)) {
        $found += [pscustomobject]@{ Dept = $c; Audience = $a }
      }
    }
  } elseif ($Dept) {
    $audiences = if ($AudienceExplicit) { @($Audience) } else { @(Get-DepartmentAudiences -DeptCode $Dept) }
    foreach ($a in $audiences) {
      $found += [pscustomobject]@{ Dept = $Dept; Audience = $a }
    }
  } else {
    throw "-Dept <학과> 또는 -All 이 필요하다 (설정 원본은 config/departments/)"
  }
  return $found
}

$targets = @(Get-DeployTargets -Dept $Dept -Audience $Audience -All $All.IsPresent `
  -AudienceExplicit $PSBoundParameters.ContainsKey("Audience"))

# ---- 키 중복 사전 검사 ----
# 배포를 시작한 뒤에 걸리면 절반만 올라간 채로 멈춘다. 먼저 전부 본다.
# 학과가 늘수록 복붙으로 키가 겹치기 쉽고, 겹치면 한 키로 남의 코퍼스가 열린다.
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

# ---- 공통값 확보 ----
Set-DeptConfig -DeptCode $targets[0].Dept -AudienceName $targets[0].Audience

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
  Set-DeptConfig -DeptCode $t.Dept -AudienceName $t.Audience

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

  # 다른 운영 PC가 로컬 YAML 없이도 Cloud Run에서 학과 설정을 복원한다.
  # annotation 값은 학과 YAML 전체를 담은 JSON의 base64url이고 쉼표가 없어
  # gcloud 인자에 안전하다. keys도 포함되므로 Cloud Run 조회 권한을 제한해야 한다.
  if ([string]::IsNullOrWhiteSpace($env:DEPT_CODE) -or
      [string]::IsNullOrWhiteSpace($env:MCP_AUDIENCE) -or
      [string]::IsNullOrWhiteSpace($env:DEPLOYMENT_METADATA_B64)) {
    throw "$SERVICE : Cloud Run 관리 메타데이터를 만들지 못했다"
  }
  $managementLabels = "gcp-rag-managed=true,gcp-rag-dept=$($env:DEPT_CODE),gcp-rag-audience=$($env:MCP_AUDIENCE),gcp-rag-schema=v2"
  $managementAnnotation = "gcp-rag.dev/department-metadata=$($env:DEPLOYMENT_METADATA_B64)"

  $envVars = "^|^GCP_PROJECT_ID=$PROJECT_ID|GCP_REGION=$REGION|RAG_CORPUS_NAME=$($env:RAG_CORPUS_NAME)|GCS_HWP_ORIGINAL_BUCKET=$GCS_HWP_ORIG|GCS_SOURCE_BUCKET=$GCS_SOURCE|FIRESTORE_DATABASE=$FS_DB|DOC_STATE_COLLECTION=$FS_COL|MCP_API_KEY=$MCP_API_KEY|TOP_K_DEFAULT=$TOP_K|SEARCH_FETCH_MULTIPLIER=$FETCH_MULT|SEARCH_FETCH_MAX=$FETCH_MAX"

  gcloud run deploy $SERVICE `
    --image=$IMAGE `
    --region=$REGION `
    @authArgs `
    --update-labels=$managementLabels `
    --update-annotations=$managementAnnotation `
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
