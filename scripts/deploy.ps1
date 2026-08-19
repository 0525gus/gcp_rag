# Cloud Run / Workflows / Scheduler 배포
# 사용: .\scripts\deploy.ps1
#
# --set-env-vars 는 Cloud Run env 를 통째로 치환한다. 안 넘긴 값은 사라진다.
# DRIVE_IDS / SYNC_FOLDER_IDS 에 콤마가 있어 구분자는 | (^|^...).

$ErrorActionPreference = "Stop"
# describe 실패를 throw 로 올리지 않는다 (없으면 create).
if ($PSVersionTable.PSVersion.Major -ge 7) {
  $PSNativeCommandUseErrorActionPreference = $false
}

Set-Location (Split-Path -Parent $PSScriptRoot)
. (Join-Path $PSScriptRoot "_load_env.ps1")
. (Join-Path $PSScriptRoot "preflight.ps1")
Load-Dotenv

# .env 출처 이름(_STAFF) → 서비스가 읽는 이름(MCP_API_KEY).
if (-not $env:MCP_API_KEY) { $env:MCP_API_KEY = $env:MCP_API_KEY_STAFF }
Require-FullDeployEnv

function Assert-LastExit {
  if ($LASTEXITCODE -ne 0) { throw "gcloud exit $LASTEXITCODE" }
}

$PROJECT_ID = $env:GCP_PROJECT_ID
$REGION = Get-EnvOr GCP_REGION "asia-northeast3"
$REPO = Get-EnvOr ARTIFACT_REPO "rag-mcp"
$MCP_API_KEY = $env:MCP_API_KEY
$FS_DB = Get-EnvOr FIRESTORE_DATABASE "rag-sync-state"
$FS_COL = Get-EnvOr DOC_STATE_COLLECTION "doc_state"
$QG_MODE = Get-EnvOr QG_MODE "log"
$PARSER_TIMEOUT = Get-EnvOr PARSER_TIMEOUT "540"
$PARSER_CONCURRENCY = Get-EnvOr PARSER_CONCURRENCY "4"
$PARSER_MAX_INSTANCES = Get-EnvOr PARSER_MAX_INSTANCES "10"
$SYNC_CONCURRENCY = Get-EnvOr SYNC_CONCURRENCY "4"
$INGEST_CONC = Get-EnvOr INGEST_CONCURRENCY "8"
$RAG_DEL_PACE = Get-EnvOr RAG_DELETE_PACING_SECONDS "1.1"
$RAG_DEL_CONC = Get-EnvOr RAG_DELETE_CONCURRENCY "1"
$TOP_K = Get-EnvOr TOP_K_DEFAULT "5"
$FETCH_MULT = Get-EnvOr SEARCH_FETCH_MULTIPLIER "3"
$FETCH_MAX = Get-EnvOr SEARCH_FETCH_MAX "60"
$DOCAI = Get-EnvOr DOCAI_PROCESSOR_ID ""
$SYNC_FOLDERS = Get-EnvOr SYNC_FOLDER_IDS ""
$STUDENT_CORPUS = Get-EnvOr RAG_CORPUS_NAME_STUDENT ""
$STUDENT_FOLDERS = Get-EnvOr STUDENT_FOLDER_IDS ""
$GCS_HWP_ORIG = $env:GCS_HWP_ORIGINAL_BUCKET
$GCS_SOURCE = $env:GCS_SOURCE_BUCKET
$CORPUS = $env:RAG_CORPUS_NAME
$DRIVE_IDS = $env:DRIVE_IDS

gcloud config set project $PROJECT_ID
Assert-LastExit

Write-Host "== Enable APIs =="
gcloud services enable `
  run.googleapis.com `
  workflows.googleapis.com `
  cloudscheduler.googleapis.com `
  drive.googleapis.com `
  documentai.googleapis.com `
  aiplatform.googleapis.com `
  firestore.googleapis.com `
  storage.googleapis.com `
  artifactregistry.googleapis.com `
  secretmanager.googleapis.com `
  cloudbuild.googleapis.com `
  --project=$PROJECT_ID
Assert-LastExit

Assert-GcpPrereqs

Write-Host "== Artifact Registry =="
gcloud artifacts repositories describe $REPO --location=$REGION 2>$null
if ($LASTEXITCODE -ne 0) {
  gcloud artifacts repositories create $REPO `
    --repository-format=docker `
    --location=$REGION
  Assert-LastExit
}

$IMAGE_BASE = "$REGION-docker.pkg.dev/$PROJECT_ID/$REPO"

Write-Host "== Build & push images =="
gcloud builds submit --config=cloudbuild.parser.yaml --substitutions="_IMAGE=$IMAGE_BASE/parser:latest"
Assert-LastExit
gcloud builds submit --config=cloudbuild.sync.yaml --substitutions="_IMAGE=$IMAGE_BASE/sync:latest"
Assert-LastExit
gcloud builds submit --config=cloudbuild.mcp.yaml --substitutions="_IMAGE=$IMAGE_BASE/mcp:latest"
Assert-LastExit

Write-Host "== Deploy Cloud Run =="
# parser timeout 540 < sync httpx 600. 서버가 먼저 포기해야 sync 가 오류를 받는다.
# concurrency 4: 요청당 메모리 한계. 넘치는 요청은 새 인스턴스로.
$parserEnv = "^|^GCP_PROJECT_ID=$PROJECT_ID|GCP_REGION=$REGION|GCS_HWP_ORIGINAL_BUCKET=$GCS_HWP_ORIG|GCS_SOURCE_BUCKET=$GCS_SOURCE|RAG_CORPUS_NAME=$CORPUS|DOCAI_PROCESSOR_ID=$DOCAI|QG_MODE=$QG_MODE|FIRESTORE_DATABASE=$FS_DB|DOC_STATE_COLLECTION=$FS_COL"
gcloud run deploy rag-parser `
  --image="$IMAGE_BASE/parser:latest" `
  --region=$REGION `
  --no-allow-unauthenticated `
  --set-env-vars=$parserEnv `
  --memory=2Gi --cpu=2 --timeout=$PARSER_TIMEOUT `
  --concurrency=$PARSER_CONCURRENCY `
  --max-instances=$PARSER_MAX_INSTANCES
Assert-LastExit

# sync timeout 3600. 워크플로우 스텝(1800s)에 맞추면 안 됨 — backfill 이 토큰을 못 남긴다.
# RAG_CORPUS_NAME_STUDENT / STUDENT_FOLDER_IDS 를 안 넘기면 분리가 꺼진다.
$syncEnv = "^|^GCP_PROJECT_ID=$PROJECT_ID|GCP_REGION=$REGION|GCS_HWP_ORIGINAL_BUCKET=$GCS_HWP_ORIG|GCS_SOURCE_BUCKET=$GCS_SOURCE|RAG_CORPUS_NAME=$CORPUS|DRIVE_IDS=$DRIVE_IDS|SYNC_FOLDER_IDS=$SYNC_FOLDERS|DOC_STATE_COLLECTION=$FS_COL|FIRESTORE_DATABASE=$FS_DB|QG_MODE=$QG_MODE|INGEST_CONCURRENCY=$INGEST_CONC|RAG_DELETE_PACING_SECONDS=$RAG_DEL_PACE|RAG_DELETE_CONCURRENCY=$RAG_DEL_CONC|RAG_CORPUS_NAME_STUDENT=$STUDENT_CORPUS|STUDENT_FOLDER_IDS=$STUDENT_FOLDERS"
gcloud run deploy rag-sync `
  --image="$IMAGE_BASE/sync:latest" `
  --region=$REGION `
  --no-allow-unauthenticated `
  --set-env-vars=$syncEnv `
  --memory=2Gi --cpu=2 --timeout=3600 `
  --concurrency=$SYNC_CONCURRENCY
Assert-LastExit

# IAM ID 토큰용. FactChat 공개 URL 은 deploy_mcp.ps1.
$MCP_SERVICE = Get-McpStaffServiceName
$mcpEnv = "^|^GCP_PROJECT_ID=$PROJECT_ID|GCP_REGION=$REGION|RAG_CORPUS_NAME=$CORPUS|GCS_HWP_ORIGINAL_BUCKET=$GCS_HWP_ORIG|GCS_SOURCE_BUCKET=$GCS_SOURCE|FIRESTORE_DATABASE=$FS_DB|DOC_STATE_COLLECTION=$FS_COL|MCP_API_KEY=$MCP_API_KEY|TOP_K_DEFAULT=$TOP_K|SEARCH_FETCH_MULTIPLIER=$FETCH_MULT|SEARCH_FETCH_MAX=$FETCH_MAX"
gcloud run deploy $MCP_SERVICE `
  --image="$IMAGE_BASE/mcp:latest" `
  --region=$REGION `
  --no-allow-unauthenticated `
  --set-env-vars=$mcpEnv `
  --memory=1Gi --cpu=1 --timeout=60
Assert-LastExit

$PARSER_URL = gcloud run services describe rag-parser --region=$REGION --format="value(status.url)"
Assert-LastExit
$SYNC_URL = gcloud run services describe rag-sync --region=$REGION --format="value(status.url)"
Assert-LastExit
$MCP_URL = gcloud run services describe $MCP_SERVICE --region=$REGION --format="value(status.url)"
Assert-LastExit

Write-Host "PARSER_URL=$PARSER_URL"
Write-Host "SYNC_URL=$SYNC_URL"
Write-Host "MCP_URL=$MCP_URL"

$driveIds = @($DRIVE_IDS -split "," | ForEach-Object { $_.Trim() } | Where-Object { $_ })
$driveJson = "[" + (($driveIds | ForEach-Object { '"' + $_ + '"' }) -join ",") + "]"
$innerJson = '{"syncUrl":"' + $SYNC_URL.Trim() + '","parserUrl":"' + $PARSER_URL.Trim() + '","driveIds":' + $driveJson + "}"
$escaped = $innerJson.Replace("\", "\\").Replace('"', '\"')
$bodyJson = '{"argument":"' + $escaped + '"}'

Write-Host "== Deploy Workflow =="
gcloud workflows deploy rag-daily-sync --location=$REGION --source=workflows/daily_sync.yaml
Assert-LastExit

$SCHEDULER_SA = Get-EnvOr SCHEDULER_SA "scheduler@${PROJECT_ID}.iam.gserviceaccount.com"
Write-Host "== Ensure Scheduler SA / App Engine =="
gcloud iam service-accounts describe $SCHEDULER_SA --project=$PROJECT_ID 2>$null
if ($LASTEXITCODE -ne 0) {
  gcloud iam service-accounts create scheduler `
    --display-name="RAG daily sync scheduler" `
    --project=$PROJECT_ID
  Assert-LastExit
}
gcloud projects add-iam-policy-binding $PROJECT_ID `
  --member="serviceAccount:${SCHEDULER_SA}" `
  --role="roles/workflows.invoker" `
  --condition=None | Out-Null
Assert-LastExit

gcloud app describe --project=$PROJECT_ID 2>$null
if ($LASTEXITCODE -ne 0) {
  gcloud app create --region=$REGION --project=$PROJECT_ID
  Assert-LastExit
}

Write-Host "== Cloud Scheduler (00:00 Asia/Seoul) =="
$bodyFile = [System.IO.Path]::GetTempFileName()
try {
  $utf8 = New-Object System.Text.UTF8Encoding $false
  [System.IO.File]::WriteAllText($bodyFile, $bodyJson, $utf8)
  $schedUri = "https://workflowexecutions.googleapis.com/v1/projects/${PROJECT_ID}/locations/${REGION}/workflows/rag-daily-sync/executions"
  gcloud scheduler jobs describe rag-daily-sync --location=$REGION 2>$null
  if ($LASTEXITCODE -eq 0) {
    gcloud scheduler jobs update http rag-daily-sync `
      --location=$REGION `
      --schedule="0 0 * * *" `
      --time-zone="Asia/Seoul" `
      --uri=$schedUri `
      --http-method=POST `
      --oauth-service-account-email=$SCHEDULER_SA `
      --message-body-from-file=$bodyFile
  } else {
    gcloud scheduler jobs create http rag-daily-sync `
      --location=$REGION `
      --schedule="0 0 * * *" `
      --time-zone="Asia/Seoul" `
      --uri=$schedUri `
      --http-method=POST `
      --oauth-service-account-email=$SCHEDULER_SA `
      --message-body-from-file=$bodyFile
  }
  Assert-LastExit
} finally {
  Remove-Item -LiteralPath $bodyFile -ErrorAction SilentlyContinue
}

Write-Host "Done."
Write-Host "PARSER_URL=$PARSER_URL"
Write-Host "SYNC_URL=$SYNC_URL"
Write-Host "MCP_URL=$MCP_URL"
