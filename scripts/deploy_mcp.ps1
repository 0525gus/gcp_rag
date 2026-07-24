# MCP 서버만 Cloud Run 배포 (FactChat MCP 커넥터용)
# 사용: .\scripts\deploy_mcp.ps1

$ErrorActionPreference = "Stop"

if (-not $env:GCP_PROJECT_ID) { throw "set GCP_PROJECT_ID" }
if (-not $env:RAG_CORPUS_NAME) { throw "set RAG_CORPUS_NAME" }
if (-not $env:MCP_API_KEY) { throw "set MCP_API_KEY (FactChat Authorization Bearer)" }

$PROJECT_ID = $env:GCP_PROJECT_ID
$REGION = if ($env:GCP_REGION) { $env:GCP_REGION } else { "asia-northeast3" }
$REPO = if ($env:ARTIFACT_REPO) { $env:ARTIFACT_REPO } else { "rag-mcp" }
$SERVICE = if ($env:MCP_SERVICE_NAME) { $env:MCP_SERVICE_NAME } else { "rag-mcp" }
$MCP_API_KEY = $env:MCP_API_KEY
$ALLOW_UNAUTH = if ($env:ALLOW_UNAUTH) { $env:ALLOW_UNAUTH } else { "true" }
$GCS_RAW = if ($env:GCS_RAW_BUCKET) { $env:GCS_RAW_BUCKET } else { "unused" }
$GCS_NORM = if ($env:GCS_NORMALIZED_BUCKET) { $env:GCS_NORMALIZED_BUCKET } else { "unused" }
$TOP_K = if ($env:TOP_K_DEFAULT) { $env:TOP_K_DEFAULT } else { "5" }

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

# 값에 콤마가 없을 때도 안전하게 | 구분자 사용
$envVars = "^|^GCP_PROJECT_ID=$PROJECT_ID|GCP_REGION=$REGION|RAG_CORPUS_NAME=$($env:RAG_CORPUS_NAME)|GCS_RAW_BUCKET=$GCS_RAW|GCS_NORMALIZED_BUCKET=$GCS_NORM|MCP_API_KEY=$MCP_API_KEY|MCP_TRANSPORT=streamable-http|TOP_K_DEFAULT=$TOP_K"

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
