#!/usr/bin/env bash
# MCP 서버만 Cloud Run에 배포 (FactChat MCP 커넥터용)
set -euo pipefail

cd "$(dirname "$0")/.."

# .env 를 올리되 **셸에 이미 있는 값은 건드리지 않는다.** 학생용 배포가
#   RAG_CORPUS_NAME="${RAG_CORPUS_NAME_STUDENT}" ... ./scripts/deploy_mcp.sh
# 처럼 앞에 값을 붙여 도는 구조라, 여기서 .env 가 이기면 학생 서비스에
# 교직원 코퍼스가 실린다.
# shellcheck source=scripts/_load_env.sh
. "$(dirname "$0")/_load_env.sh"
load_dotenv

PROJECT_ID="${GCP_PROJECT_ID:?set GCP_PROJECT_ID}"
REGION="${GCP_REGION:-asia-northeast3}"
REPO="${ARTIFACT_REPO:-rag-mcp}"
SERVICE="${MCP_SERVICE_NAME:-rag-mcp}"
# .env 의 출처 이름(_STAFF) → 서비스가 읽는 이름(MCP_API_KEY). deploy.sh 와 동일.
# 학생용은 앞에 MCP_API_KEY="${MCP_API_KEY_STUDENT}" 를 붙여 도므로 그쪽이 이긴다.
MCP_API_KEY="${MCP_API_KEY:-${MCP_API_KEY_STAFF:-}}"
: "${MCP_API_KEY:?set MCP_API_KEY_STAFF (FactChat 커넥터 Authorization에 사용)}"

# FactChat은 브라우저/서버에서 공개 HTTPS를 호출하므로 allow-unauthenticated + API 키
ALLOW_UNAUTH="${ALLOW_UNAUTH:-true}"

# --set-env-vars 는 기존 env 를 통째로 치환한다. 여기서 안 넘기는 변수는
# 배포 순간 사라지므로, 운영에서 손으로 켜둔 값은 반드시 이 스크립트에 등록할 것.
# (FIRESTORE_DATABASE 가 빠지면 (default) = Datastore 모드를 보게 되어
#  검색 결과의 파일명·경로 메타가 조용히 전부 null 이 된다)

gcloud config set project "${PROJECT_ID}"
gcloud services enable run.googleapis.com aiplatform.googleapis.com \
  artifactregistry.googleapis.com firestore.googleapis.com --project="${PROJECT_ID}"

gcloud artifacts repositories describe "${REPO}" --location="${REGION}" >/dev/null 2>&1 \
  || gcloud artifacts repositories create "${REPO}" \
       --repository-format=docker --location="${REGION}"

IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPO}/mcp:latest"
gcloud builds submit --config=cloudbuild.mcp.yaml \
  --substitutions="_IMAGE=${IMAGE}"

AUTH_FLAG="--allow-unauthenticated"
if [[ "${ALLOW_UNAUTH}" != "true" ]]; then
  AUTH_FLAG="--no-allow-unauthenticated"
fi

gcloud run deploy "${SERVICE}" \
  --image="${IMAGE}" \
  --region="${REGION}" \
  ${AUTH_FLAG} \
  --set-env-vars="GCP_PROJECT_ID=${PROJECT_ID},GCP_REGION=${REGION},RAG_CORPUS_NAME=${RAG_CORPUS_NAME:?set RAG_CORPUS_NAME},GCS_RAW_BUCKET=${GCS_RAW_BUCKET:-unused},GCS_NORMALIZED_BUCKET=${GCS_NORMALIZED_BUCKET:-unused},FIRESTORE_DATABASE=${FIRESTORE_DATABASE:-doc-state},FIRESTORE_COLLECTION=${FIRESTORE_COLLECTION:-doc_state},MCP_API_KEY=${MCP_API_KEY},TOP_K_DEFAULT=${TOP_K_DEFAULT:-5},SEARCH_FETCH_MULTIPLIER=${SEARCH_FETCH_MULTIPLIER:-3},SEARCH_FETCH_MAX=${SEARCH_FETCH_MAX:-60}" \
  --memory=1Gi --cpu=1 --timeout=60 --concurrency=40

MCP_URL=$(gcloud run services describe "${SERVICE}" --region="${REGION}" --format='value(status.url)')
echo ""
echo "=== FactChat MCP 커넥터 설정 ==="
echo "Server URL : ${MCP_URL}/mcp"
echo "Transport  : Streamable HTTP (또는 HTTP)"
echo "Header     : Authorization: Bearer ${MCP_API_KEY}"
echo "  (또는)   : X-API-Key: ${MCP_API_KEY}"
echo ""
echo "Health check: curl -s ${MCP_URL}/health"
