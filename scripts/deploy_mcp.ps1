param(
  [string]$Dept,
  [ValidateSet("staff", "student")][string]$Audience = "staff",
  [switch]$All,
  [switch]$SkipBuild,
  [switch]$ShowKeys
)

$ErrorActionPreference = "Stop"
if ($PSVersionTable.PSVersion.Major -ge 7) {
  $PSNativeCommandUseErrorActionPreference = $false
}

Set-Location (Split-Path -Parent $PSScriptRoot)
. (Join-Path $PSScriptRoot "_load_env.ps1")

$py = Get-PythonExe
$unifiedMode = & $py -c "from scripts.dept_gui import _unified_mcp_enabled; print('1' if _unified_mcp_enabled() else '0')"
if ($LASTEXITCODE -ne 0) { throw "MCP routing mode lookup failed" }
if ($unifiedMode.Trim() -eq "1") {
  Write-Host "== Unified MCP: deploy the shared rag-mcp runtime; routes remain in Cloud =="
  $unifiedArgs = @((Join-Path $PSScriptRoot "deploy_unified_mcp.py"))
  if ($SkipBuild) { $unifiedArgs += "--skip-build" }
  & $py @unifiedArgs
  if ($LASTEXITCODE -ne 0) { throw "Unified MCP deployment failed" }
  return
}

function Assert-LastExit {
  if ($LASTEXITCODE -ne 0) { throw "gcloud exit $LASTEXITCODE" }
}

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
    throw "-Dept <학과> 또는 -All 이 필요하다 (대상은 Cloud 학과 등록부에서 조회)"
  }
  return $found
}

$targets = @(Get-DeployTargets -Dept $Dept -Audience $Audience -All $All.IsPresent `
  -AudienceExplicit $PSBoundParameters.ContainsKey("Audience"))

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

Set-DeptConfig -DeptCode $targets[0].Dept -AudienceName $targets[0].Audience

$PROJECT_ID = $env:GCP_PROJECT_ID
$REGION = $env:GCP_REGION
$REPO = $env:ARTIFACT_REPO

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

$results = @()
foreach ($t in $targets) {
  Set-DeptConfig -DeptCode $t.Dept -AudienceName $t.Audience

  Require-McpDeployEnv

  $SERVICE = Get-McpDeployServiceName
  $MCP_API_KEY = $env:MCP_API_KEY
  $ALLOW_UNAUTH = $env:ALLOW_UNAUTH
  # --set-env-vars 는 기존 env 를 통째로 치환한다. FIRESTORE_* 를 빼면 (default)
  # Datastore 모드를 보게 되어 검색 결과의 파일명·경로 메타가 조용히 null 이 된다.
  $GCS_HWP_ORIG = Get-EnvOr GCS_HWP_ORIGINAL_BUCKET "unused"
  $GCS_SOURCE = Get-EnvOr GCS_SOURCE_BUCKET "unused"
  $FS_DB = $env:FIRESTORE_DATABASE
  $FS_COL = $env:DOC_STATE_COLLECTION
  $TOP_K = $env:TOP_K_DEFAULT
  $FETCH_MULT = $env:SEARCH_FETCH_MULTIPLIER
  $FETCH_MAX = $env:SEARCH_FETCH_MAX
  $MCP_CONCURRENCY = $env:MCP_CONCURRENCY
  # 학과가 늘면 이게 곧 청구서다 — 1 은 24시간 상주를 뜻한다.
  $MIN_INSTANCES = Get-EnvOr MCP_MIN_INSTANCES "0"

  Write-Host ""
  Write-Host "== 배포: $SERVICE (min-instances=$MIN_INSTANCES) =="

  $authArgs = @("--allow-unauthenticated")
  if ($ALLOW_UNAUTH -ne "true") { $authArgs = @("--no-allow-unauthenticated") }

  # 관리 메타데이터는 이전 Cloud Run 배포와의 복구 호환성을 위해 유지한다.
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
    Dept    = if ($env:DEPT_NAME) { $env:DEPT_NAME } else { $env:DEPT_CODE }
    Service = $SERVICE
    Url     = "$MCP_URL/mcp"
    Key     = if ($ShowKeys) { $MCP_API_KEY } else { "***($($MCP_API_KEY.Length)자) -ShowKeys" }
  }
}

Write-Host ""
Write-Host "=== FactChat MCP 커넥터 설정 ==="
Write-Host "Transport : Streamable HTTP (또는 HTTP)"
Write-Host "Header    : Authorization: Bearer <Key>  (또는 X-API-Key: <Key>)"
Write-Host ""
$results | Format-Table -AutoSize
Write-Host "Health: <URL 에서 /mcp 를 뺀 주소>/health"
