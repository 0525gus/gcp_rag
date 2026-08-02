#!/usr/bin/env bash
# MCP 서버만 Cloud Run에 배포 (FactChat MCP 커넥터용)
set -euo pipefail

PROJECT_ID="${GCP_PROJECT_ID:?set GCP_PROJECT_ID}"
REGION="${GCP_REGION:-asia-northeast3}"
REPO="${ARTIFACT_REPO:-rag-mcp}"
SERVICE="${MCP_SERVICE_NAME:-rag-mcp}"
MCP_API_KEY="${MCP_API_KEY:?set MCP_API_KEY (FactChat 커넥터 Authorization에 사용)}"

# FactChat은 브라우저/서버에서 공개 HTTPS를 호출하므로 allow-unauthenticated + API 키
ALLOW_UNAUTH="${ALLOW_UNAUTH:-true}"

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
  --set-env-vars="GCP_PROJECT_ID=${PROJECT_ID},GCP_REGION=${REGION},RAG_CORPUS_NAME=${RAG_CORPUS_NAME:?set RAG_CORPUS_NAME},GCS_RAW_BUCKET=${GCS_RAW_BUCKET:-unused},GCS_NORMALIZED_BUCKET=${GCS_NORMALIZED_BUCKET:-unused},MCP_API_KEY=${MCP_API_KEY},TOP_K_DEFAULT=${TOP_K_DEFAULT:-5}" \
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
