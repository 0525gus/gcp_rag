# MCP 서버만 Cloud Run 배포 (FactChat MCP 커넥터용)
# 사용: .\scripts\deploy_mcp.ps1
#
# .env 를 올리되 셸에 이미 있는 값은 건드리지 않는다. 학생용은
#   $env:RAG_CORPUS_NAME = $env:RAG_CORPUS_NAME_STUDENT
#   $env:MCP_API_KEY = $env:MCP_API_KEY_STUDENT
#   $env:MCP_SERVICE_NAME = "rag-mcp-student"
#   .\scripts\deploy_mcp.ps1
# 처럼 앞에 값을 두고 돈다. .env 가 이기면 학생 서비스에 교직원 코퍼스가 실린다.

$ErrorActionPreference = "Stop"

Set-Location (Split-Path -Parent $PSScriptRoot)

function Load-Dotenv {
  param([string]$Path = ".env")
  if (-not (Test-Path -LiteralPath $Path)) { return }
  Get-Content -LiteralPath $Path -Encoding utf8 | ForEach-Object {
    $line = $_ -replace "`r$", ""
    if ($line -match '^\s*(#|$)' -or $line -notmatch "=") { return }
    $key, $val = $line.Split("=", 2)
    $key = $key.Trim()
    if ($key.StartsWith("export ")) { $key = $key.Substring(7).Trim() }
    if ($key -notmatch '^[A-Za-z_][A-Za-z0-9_]*$') { return }
    if (Test-Path -LiteralPath "Env:$key") { return }
    $val = $val.Trim()
    if ($val.Length -ge 2) {
      $q = $val[0]
      if (($q -eq '"' -or $q -eq "'") -and $val[-1] -eq $q) {
        $val = $val.Substring(1, $val.Length - 2)
      }
    }
    Set-Item -LiteralPath "Env:$key" -Value $val
  }
}

Load-Dotenv

if (-not $env:MCP_API_KEY) { $env:MCP_API_KEY = $env:MCP_API_KEY_STAFF }

if (-not $env:GCP_PROJECT_ID) { throw "set GCP_PROJECT_ID" }
if (-not $env:RAG_CORPUS_NAME) { throw "set RAG_CORPUS_NAME" }
if (-not $env:MCP_API_KEY) { throw "set MCP_API_KEY_STAFF (FactChat Authorization Bearer)" }

$PROJECT_ID = $env:GCP_PROJECT_ID
$REGION = if ($env:GCP_REGION) { $env:GCP_REGION } else { "asia-northeast3" }
$REPO = if ($env:ARTIFACT_REPO) { $env:ARTIFACT_REPO } else { "rag-mcp" }
$SERVICE = if ($env:MCP_SERVICE_NAME) { $env:MCP_SERVICE_NAME } else { "rag-mcp" }
$MCP_API_KEY = $env:MCP_API_KEY
$ALLOW_UNAUTH = if ($env:ALLOW_UNAUTH) { $env:ALLOW_UNAUTH } else { "true" }
$GCS_RAW = if ($env:GCS_RAW_BUCKET) { $env:GCS_RAW_BUCKET } else { "unused" }
$GCS_NORM = if ($env:GCS_NORMALIZED_BUCKET) { $env:GCS_NORMALIZED_BUCKET } else { "unused" }
$TOP_K = if ($env:TOP_K_DEFAULT) { $env:TOP_K_DEFAULT } else { "5" }
# --set-env-vars 는 기존 env 를 통째로 치환한다. FIRESTORE_* 를 빼면 (default)
# Datastore 모드를 보게 되어 검색 결과의 파일명·경로 메타가 조용히 null 이 된다
# (deploy_mcp.sh 와 동일하게 반드시 넘긴다).
$FS_DB = if ($env:FIRESTORE_DATABASE) { $env:FIRESTORE_DATABASE } else { "doc-state" }
$FS_COL = if ($env:FIRESTORE_COLLECTION) { $env:FIRESTORE_COLLECTION } else { "doc_state" }
$FETCH_MULT = if ($env:SEARCH_FETCH_MULTIPLIER) { $env:SEARCH_FETCH_MULTIPLIER } else { "3" }
$FETCH_MAX = if ($env:SEARCH_FETCH_MAX) { $env:SEARCH_FETCH_MAX } else { "60" }

gcloud config set project $PROJECT_ID
gcloud services enable run.googleapis.com aiplatform.googleapis.com `
  artifactregistry.googleapis.com firestore.googleapis.com --project=$PROJECT_ID

$desc = gcloud artifacts repositories describe $REPO --location=$REGION 2>$null
if (-not $desc) {
  gcloud artifacts repositories create $REPO `
    --repository-format=docker --location=$REGION
}

$IMAGE = "$REGION-docker.pkg.dev/$PROJECT_ID/$REPO/mcp:latest"
gcloud builds submit --config=cloudbuild.mcp.yaml --substitutions="_IMAGE=$IMAGE"

$authArgs = @("--allow-unauthenticated")
if ($ALLOW_UNAUTH -ne "true") {
  $authArgs = @("--no-allow-unauthenticated")
}

# 값에 콤마가 없을 때도 안전하게 | 구분자 사용 (.sh 의 --set-env-vars 목록과 동기)
$envVars = "^|^GCP_PROJECT_ID=$PROJECT_ID|GCP_REGION=$REGION|RAG_CORPUS_NAME=$($env:RAG_CORPUS_NAME)|GCS_RAW_BUCKET=$GCS_RAW|GCS_NORMALIZED_BUCKET=$GCS_NORM|FIRESTORE_DATABASE=$FS_DB|FIRESTORE_COLLECTION=$FS_COL|MCP_API_KEY=$MCP_API_KEY|TOP_K_DEFAULT=$TOP_K|SEARCH_FETCH_MULTIPLIER=$FETCH_MULT|SEARCH_FETCH_MAX=$FETCH_MAX"

gcloud run deploy $SERVICE `
  --image=$IMAGE `
  --region=$REGION `
  @authArgs `
  --set-env-vars=$envVars `
  --memory=1Gi --cpu=1 --timeout=60 --concurrency=40

$MCP_URL = gcloud run services describe $SERVICE --region=$REGION --format="value(status.url)"
Write-Host ""
Write-Host "=== FactChat MCP 커넥터 설정 ==="
Write-Host "Server URL : $MCP_URL/mcp"
Write-Host "Transport  : Streamable HTTP (또는 HTTP)"
Write-Host "Header     : Authorization: Bearer $MCP_API_KEY"
Write-Host "  (또는)   : X-API-Key: $MCP_API_KEY"
Write-Host ""
Write-Host "Health: curl -s $MCP_URL/health"
