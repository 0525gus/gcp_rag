# MCP 서버만 Cloud Run 배포 (FactChat MCP 커넥터용)
#
# deploy.ps1 도 MCP 를 올리지만 --no-allow-unauthenticated (IAM 전용)라 FactChat 이
# 못 붙는다. 공개 URL 은 여기서만 나온다(ALLOW_UNAUTH, 기본 true).
# 이미지도 mcp 하나만 빌드하므로 MCP 설정만 바꿨을 때 재배포용으로도 쓴다.
#
# 사용: .\scripts\deploy_mcp.ps1
# 학생: $env:MCP_AUDIENCE = "student"; .\scripts\deploy_mcp.ps1
# 이름은 .env 의 MCP_SERVICE_NAME_STAFF / MCP_SERVICE_NAME_STUDENT

$ErrorActionPreference = "Stop"
if ($PSVersionTable.PSVersion.Major -ge 7) {
  $PSNativeCommandUseErrorActionPreference = $false
}

Set-Location (Split-Path -Parent $PSScriptRoot)
. (Join-Path $PSScriptRoot "_load_env.ps1")
Load-Dotenv

$SERVICE = Get-McpDeployServiceName

if (-not $env:MCP_API_KEY) { $env:MCP_API_KEY = $env:MCP_API_KEY_STAFF }

if (Test-McpStudentTarget $SERVICE) {
  if ($env:RAG_CORPUS_NAME_STUDENT) { $env:RAG_CORPUS_NAME = $env:RAG_CORPUS_NAME_STUDENT }
  if ($env:MCP_API_KEY_STUDENT) { $env:MCP_API_KEY = $env:MCP_API_KEY_STUDENT }
}

Require-McpDeployEnv

function Assert-LastExit {
  if ($LASTEXITCODE -ne 0) { throw "gcloud exit $LASTEXITCODE" }
}

$PROJECT_ID = $env:GCP_PROJECT_ID
$REGION = Get-EnvOr GCP_REGION "asia-northeast3"
$REPO = Get-EnvOr ARTIFACT_REPO "rag-mcp"
$MCP_API_KEY = $env:MCP_API_KEY
$ALLOW_UNAUTH = Get-EnvOr ALLOW_UNAUTH "true"
$GCS_HWP_ORIG = Get-EnvOr GCS_HWP_ORIGINAL_BUCKET "unused"
$GCS_SOURCE = Get-EnvOr GCS_SOURCE_BUCKET "unused"
$TOP_K = Get-EnvOr TOP_K_DEFAULT "5"
# --set-env-vars 는 기존 env 를 통째로 치환한다. FIRESTORE_* 를 빼면 (default)
# Datastore 모드를 보게 되어 검색 결과의 파일명·경로 메타가 조용히 null 이 된다.
$FS_DB = Get-EnvOr FIRESTORE_DATABASE "rag-sync-state"
$FS_COL = Get-EnvOr DOC_STATE_COLLECTION "doc_state"
$FETCH_MULT = Get-EnvOr SEARCH_FETCH_MULTIPLIER "3"
$FETCH_MAX = Get-EnvOr SEARCH_FETCH_MAX "60"
# deploy.ps1 과 같은 기본값을 쓴다.
$MCP_CONCURRENCY = Get-EnvOr MCP_CONCURRENCY "40"

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

$IMAGE = "$REGION-docker.pkg.dev/$PROJECT_ID/$REPO/mcp:latest"
gcloud builds submit --config=cloudbuild.mcp.yaml --substitutions="_IMAGE=$IMAGE"
Assert-LastExit

$authArgs = @("--allow-unauthenticated")
if ($ALLOW_UNAUTH -ne "true") {
  $authArgs = @("--no-allow-unauthenticated")
}

$envVars = "^|^GCP_PROJECT_ID=$PROJECT_ID|GCP_REGION=$REGION|RAG_CORPUS_NAME=$($env:RAG_CORPUS_NAME)|GCS_HWP_ORIGINAL_BUCKET=$GCS_HWP_ORIG|GCS_SOURCE_BUCKET=$GCS_SOURCE|FIRESTORE_DATABASE=$FS_DB|DOC_STATE_COLLECTION=$FS_COL|MCP_API_KEY=$MCP_API_KEY|TOP_K_DEFAULT=$TOP_K|SEARCH_FETCH_MULTIPLIER=$FETCH_MULT|SEARCH_FETCH_MAX=$FETCH_MAX"

# min-instances=0 은 deploy.ps1 과 같은 값 — 어긋나면 재배포 쪽으로 조용히 뒤집힌다.
gcloud run deploy $SERVICE `
  --image=$IMAGE `
  --region=$REGION `
  @authArgs `
  --set-env-vars=$envVars `
  --memory=1Gi --cpu=1 --timeout=60 --concurrency=$MCP_CONCURRENCY `
  --min-instances=0
Assert-LastExit

$MCP_URL = gcloud run services describe $SERVICE --region=$REGION --format="value(status.url)"
Assert-LastExit
Write-Host ""
Write-Host "=== FactChat MCP 커넥터 설정 ==="
Write-Host "Server URL : $MCP_URL/mcp"
Write-Host "Transport  : Streamable HTTP (또는 HTTP)"
Write-Host "Header     : Authorization: Bearer $MCP_API_KEY"
Write-Host "  (또는)   : X-API-Key: $MCP_API_KEY"
Write-Host ""
Write-Host "Health: curl -s $MCP_URL/health"
