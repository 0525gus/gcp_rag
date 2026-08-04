#!/usr/bin/env bash
# Cloud Run / Workflows / Scheduler 배포 헬퍼
set -euo pipefail

PROJECT_ID="${GCP_PROJECT_ID:?set GCP_PROJECT_ID}"
REGION="${GCP_REGION:-asia-northeast3}"
REPO="${ARTIFACT_REPO:-rag-mcp}"
# rag-mcp 도 함께 배포하므로 여기서도 필수다. --set-env-vars 는 기존 env 를
# 통째로 치환하니, 안 넘기면 운영 중인 키가 조용히 사라진다. 실제로 리비전
# 00005~00013(7/25~7/28) 8개가 키 없이 떠서 코퍼스가 무인증 공개됐다
# (docs/OPS_AUDIT.md Ⅱ.1). 빌드 전에 먼저 막는다.
MCP_API_KEY="${MCP_API_KEY:?set MCP_API_KEY (rag-mcp 인증 키 — 누락 시 무인증 공개)}"

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
  --memory=2Gi --cpu=2 --timeout=600 \
  --concurrency="${PARSER_CONCURRENCY:-8}"
# concurrency 를 낮게 두는 이유: 파서는 HWP 원본을 통째로 메모리에 올리고
# 네이티브 확장(rhwp)으로 파싱한다. 2Gi 에 동시 요청이 몰리면 OOM 이다.
# 실효 동시성은 어차피 RAW_UPLOAD_CONCURRENCY(=8) 로 묶여 있으니 그에 맞춘다.
# (운영에 손으로 160 이 들어가 있었다 — 여태 안 터진 건 워크플로가 순차라서다)

# --timeout 을 워크플로우 스텝(1800s)에 맞추면 안 된다. backfill-run 은 끝에서
# 스스로 pageToken 을 커밋하므로, 워크플로우가 1800s 에 포기해도 서버가 2500s 에
# 끝내면 그 작업은 유효하게 남는다. 서버를 1800s 로 깎으면 30분을 넘기는 드라이브는
# 매번 중간에 죽어 토큰을 못 남기고 — 다음 실행도 같은 지점에서 죽어 영영 못 끝낸다.
# 중복 실행은 타임아웃 정렬이 아니라 backfill-run 의 단일 실행 잠금이 막는다.
gcloud run deploy rag-sync \
  --image="${IMAGE_BASE}/sync:latest" \
  --region="${REGION}" \
  --no-allow-unauthenticated \
  --set-env-vars="^|^GCP_PROJECT_ID=${PROJECT_ID}|GCP_REGION=${REGION}|GCS_RAW_BUCKET=${GCS_RAW_BUCKET}|GCS_NORMALIZED_BUCKET=${GCS_NORMALIZED_BUCKET}|RAG_CORPUS_NAME=${RAG_CORPUS_NAME}|DRIVE_IDS=${DRIVE_IDS}|SYNC_FOLDER_IDS=${SYNC_FOLDER_IDS:-}|FIRESTORE_COLLECTION=${FIRESTORE_COLLECTION:-doc_state}|FIRESTORE_DATABASE=${FIRESTORE_DATABASE:-doc-state}|QG_MODE=${QG_MODE:-log}|RAW_UPLOAD_CONCURRENCY=${RAW_UPLOAD_CONCURRENCY:-8}|RAG_DELETE_PACING_SECONDS=${RAG_DELETE_PACING_SECONDS:-1.1}|RAG_DELETE_CONCURRENCY=${RAG_DELETE_CONCURRENCY:-1}|RAG_CORPUS_NAME_STUDENT=${RAG_CORPUS_NAME_STUDENT:-}|STUDENT_FOLDER_IDS=${STUDENT_FOLDER_IDS:-}" \
  --memory=2Gi --cpu=2 --timeout=3600
# RAG_CORPUS_NAME_STUDENT / STUDENT_FOLDER_IDS 는 학생용 코퍼스 분리 스위치다.
# 둘 중 하나라도 비면 분리가 꺼지고 단일 코퍼스로 동작한다(config.audience_split_enabled).
# 여기서 넘기지 않으면 --set-env-vars 치환으로 조용히 꺼지므로 반드시 등록해 둘 것.

# Cursor 등 IAM ID 토큰용. FactChat 커넥터는 scripts/deploy_mcp.ps1 (공개 URL + MCP_API_KEY) 사용.
gcloud run deploy rag-mcp \
  --image="${IMAGE_BASE}/mcp:latest" \
  --region="${REGION}" \
  --no-allow-unauthenticated \
  --set-env-vars="^|^GCP_PROJECT_ID=${PROJECT_ID}|GCP_REGION=${REGION}|RAG_CORPUS_NAME=${RAG_CORPUS_NAME}|GCS_RAW_BUCKET=${GCS_RAW_BUCKET:-unused}|GCS_NORMALIZED_BUCKET=${GCS_NORMALIZED_BUCKET:-unused}|FIRESTORE_DATABASE=${FIRESTORE_DATABASE:-doc-state}|FIRESTORE_COLLECTION=${FIRESTORE_COLLECTION:-doc_state}|MCP_API_KEY=${MCP_API_KEY}|SEARCH_FETCH_MULTIPLIER=${SEARCH_FETCH_MULTIPLIER:-3}|SEARCH_FETCH_MAX=${SEARCH_FETCH_MAX:-60}" \
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
