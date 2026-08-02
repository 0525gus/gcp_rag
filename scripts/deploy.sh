#!/usr/bin/env bash
# Cloud Run / Workflows / Scheduler 배포 헬퍼
set -euo pipefail

PROJECT_ID="${GCP_PROJECT_ID:?set GCP_PROJECT_ID}"
REGION="${GCP_REGION:-asia-northeast3}"
REPO="${ARTIFACT_REPO:-rag-mcp}"

gcloud config set project "${PROJECT_ID}"

echo "== Enable APIs =="
gcloud services enable \
  run.googleapis.com \
  workflows.googleapis.com \
  cloudscheduler.googleapis.com \
  drive.googleapis.com \
  documentai.googleapis.com \
  aiplatform.googleapis.com \
  firestore.googleapis.com \
  storage.googleapis.com \
  artifactregistry.googleapis.com \
  secretmanager.googleapis.com \
  cloudbuild.googleapis.com \
  --project="${PROJECT_ID}"

echo "== Artifact Registry =="
gcloud artifacts repositories describe "${REPO}" --location="${REGION}" >/dev/null 2>&1 \
  || gcloud artifacts repositories create "${REPO}" \
       --repository-format=docker \
       --location="${REGION}"

IMAGE_BASE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPO}"

echo "== Build & push images =="
gcloud builds submit --config=cloudbuild.parser.yaml \
  --substitutions="_IMAGE=${IMAGE_BASE}/parser:latest"
gcloud builds submit --config=cloudbuild.sync.yaml \
  --substitutions="_IMAGE=${IMAGE_BASE}/sync:latest"
gcloud builds submit --config=cloudbuild.mcp.yaml \
  --substitutions="_IMAGE=${IMAGE_BASE}/mcp:latest"

echo "== Deploy Cloud Run =="
# 콤마가 값에 포함될 수 있어(DRIVE_IDS/SYNC_FOLDER_IDS) 구분자를 | 로 지정 (^|^...)
# --timeout 은 sync 가 파서를 기다리는 httpx 타임아웃(600s) 이하여야 한다.
# 더 길면 sync 가 포기한 뒤에도 파서가 계속 돌며 아무도 읽지 않을 MD 를 GCS 에 쓴다.
gcloud run deploy rag-parser \
  --image="${IMAGE_BASE}/parser:latest" \
  --region="${REGION}" \
  --no-allow-unauthenticated \
  --set-env-vars="^|^GCP_PROJECT_ID=${PROJECT_ID}|GCP_REGION=${REGION}|GCS_RAW_BUCKET=${GCS_RAW_BUCKET}|GCS_NORMALIZED_BUCKET=${GCS_NORMALIZED_BUCKET}|RAG_CORPUS_NAME=${RAG_CORPUS_NAME}|DOCAI_PROCESSOR_ID=${DOCAI_PROCESSOR_ID:-}|QG_MODE=${QG_MODE:-log}|FIRESTORE_DATABASE=${FIRESTORE_DATABASE:-doc-state}|FIRESTORE_COLLECTION=${FIRESTORE_COLLECTION:-doc_state}" \
  --memory=2Gi --cpu=2 --timeout=600

# --timeout 은 워크플로우가 sync 에 거는 가장 긴 스텝(backfill-run/retry-failed
# = 1800s) 이하여야 한다. 서버가 더 길면 워크플로우가 포기한 뒤에도 요청이 살아
# 있고, 그 위에 재시도가 새 요청을 얹어 같은 드라이브에 백필이 두 개 돈다.
gcloud run deploy rag-sync \
  --image="${IMAGE_BASE}/sync:latest" \
  --region="${REGION}" \
  --no-allow-unauthenticated \
  --set-env-vars="^|^GCP_PROJECT_ID=${PROJECT_ID}|GCP_REGION=${REGION}|GCS_RAW_BUCKET=${GCS_RAW_BUCKET}|GCS_NORMALIZED_BUCKET=${GCS_NORMALIZED_BUCKET}|RAG_CORPUS_NAME=${RAG_CORPUS_NAME}|DRIVE_IDS=${DRIVE_IDS}|SYNC_FOLDER_IDS=${SYNC_FOLDER_IDS:-}|FIRESTORE_COLLECTION=${FIRESTORE_COLLECTION:-doc_state}|FIRESTORE_DATABASE=${FIRESTORE_DATABASE:-doc-state}|QG_MODE=${QG_MODE:-log}|RAW_UPLOAD_CONCURRENCY=${RAW_UPLOAD_CONCURRENCY:-8}" \
  --memory=2Gi --cpu=2 --timeout=1800

# Cursor 등 IAM ID 토큰용. FactChat 커넥터는 scripts/deploy_mcp.ps1 (공개 URL + MCP_API_KEY) 사용.
gcloud run deploy rag-mcp \
  --image="${IMAGE_BASE}/mcp:latest" \
  --region="${REGION}" \
  --no-allow-unauthenticated \
  --set-env-vars="^|^GCP_PROJECT_ID=${PROJECT_ID}|GCP_REGION=${REGION}|RAG_CORPUS_NAME=${RAG_CORPUS_NAME}|GCS_RAW_BUCKET=${GCS_RAW_BUCKET:-unused}|GCS_NORMALIZED_BUCKET=${GCS_NORMALIZED_BUCKET:-unused}|FIRESTORE_DATABASE=${FIRESTORE_DATABASE:-doc-state}|FIRESTORE_COLLECTION=${FIRESTORE_COLLECTION:-doc_state}|MCP_ALLOW_NO_AUTH=true" \
  --memory=1Gi --cpu=1 --timeout=60

PARSER_URL=$(gcloud run services describe rag-parser --region="${REGION}" --format='value(status.url)')
SYNC_URL=$(gcloud run services describe rag-sync --region="${REGION}" --format='value(status.url)')
MCP_URL=$(gcloud run services describe rag-mcp --region="${REGION}" --format='value(status.url)')

echo "PARSER_URL=${PARSER_URL}"
echo "SYNC_URL=${SYNC_URL}"
echo "MCP_URL=${MCP_URL}"

# Workflows용 driveIds JSON 배열 생성
IFS=',' read -ra DRIVES <<< "${DRIVE_IDS}"
DRIVE_JSON="["
for i in "${!DRIVES[@]}"; do
  [[ $i -gt 0 ]] && DRIVE_JSON+=","
  DRIVE_JSON+="\"${DRIVES[$i]}\""
done
DRIVE_JSON+="]"

echo "== Deploy Workflow =="
gcloud workflows deploy rag-daily-sync \
  --location="${REGION}" \
  --source=workflows/daily_sync.yaml

SCHEDULER_SA="${SCHEDULER_SA:-scheduler@${PROJECT_ID}.iam.gserviceaccount.com}"
echo "== Ensure Scheduler SA / App Engine =="
gcloud iam service-accounts describe "${SCHEDULER_SA}" --project="${PROJECT_ID}" >/dev/null 2>&1 \
  || gcloud iam service-accounts create scheduler \
       --display-name="RAG daily sync scheduler" \
       --project="${PROJECT_ID}"
gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
  --member="serviceAccount:${SCHEDULER_SA}" \
  --role="roles/workflows.invoker" \
  --condition=None >/dev/null
gcloud app describe --project="${PROJECT_ID}" >/dev/null 2>&1 \
  || gcloud app create --region="${REGION}" --project="${PROJECT_ID}"

echo "== Cloud Scheduler (00:00 Asia/Seoul) =="
BODY_FILE="$(mktemp)"
printf '%s' "{\"argument\":\"{\\\"syncUrl\\\":\\\"${SYNC_URL}\\\",\\\"parserUrl\\\":\\\"${PARSER_URL}\\\",\\\"driveIds\\\":${DRIVE_JSON}}\"}" > "${BODY_FILE}"
gcloud scheduler jobs describe rag-daily-sync --location="${REGION}" >/dev/null 2>&1 \
  && gcloud scheduler jobs update http rag-daily-sync \
       --location="${REGION}" \
       --schedule="0 0 * * *" \
       --time-zone="Asia/Seoul" \
       --uri="https://workflowexecutions.googleapis.com/v1/projects/${PROJECT_ID}/locations/${REGION}/workflows/rag-daily-sync/executions" \
       --http-method=POST \
       --oauth-service-account-email="${SCHEDULER_SA}" \
       --message-body-from-file="${BODY_FILE}" \
  || gcloud scheduler jobs create http rag-daily-sync \
       --location="${REGION}" \
       --schedule="0 0 * * *" \
       --time-zone="Asia/Seoul" \
       --uri="https://workflowexecutions.googleapis.com/v1/projects/${PROJECT_ID}/locations/${REGION}/workflows/rag-daily-sync/executions" \
       --http-method=POST \
       --oauth-service-account-email="${SCHEDULER_SA}" \
       --message-body-from-file="${BODY_FILE}"
rm -f "${BODY_FILE}"

echo "Done."
echo "PARSER_URL=${PARSER_URL}"
echo "SYNC_URL=${SYNC_URL}"
echo "MCP_URL=${MCP_URL}"
