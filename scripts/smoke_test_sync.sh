#!/usr/bin/env bash
# =============================================================================
# sync 수정(#1~#5) 실배포 스모크 테스트
#
#   전제: gcloud SDK + 프로젝트 인증 (없으면 Google Cloud Shell 사용 권장)
#         저장소 루트에서 실행. .env 에 배포 변수 존재.
#
#   검증 핵심(#1): 델타 경로에서 2-URI 파일(PDF/Google Docs/Sheets)이 섞여도
#     pageToken 이 커밋되고 reconcile 이 ok=true 인지.
#     → 따라서 대상 드라이브에 '변경된 비-HWP 파일'이 1건 이상 있어야 의미가 있음.
#       (없으면 드라이브에서 PDF/Docs 하나를 수정/추가한 뒤 실행)
#
#   사용:
#     bash scripts/smoke_test_sync.sh              # sync+workflow 배포 후 델타 1회
#     SKIP_DEPLOY=1 bash scripts/smoke_test_sync.sh  # 배포 생략, 실행+검증만
#     BACKFILL=1   bash scripts/smoke_test_sync.sh  # 델타 대신 backfill (전체 재적재)
#     DRIVE=<id>   bash scripts/smoke_test_sync.sh  # 특정 드라이브만 (기본: 첫 DRIVE_ID)
# =============================================================================
set -euo pipefail
cd "$(dirname "$0")/.."

# --- 환경 로드 ---
# 배포 스크립트와 같은 로더를 쓴다. `source .env` 는 (1) 값을 bash 로 평가해
# `$(...)` 가 실행되고 (2) .env 가 셸을 이겨서 `GCP_PROJECT_ID=... bash 스크립트`
# 식의 일회성 오버라이드가 안 먹었다. 공백 트림도 여기 한 곳에만 있다.
# shellcheck source=scripts/_load_env.sh
. "$(dirname "$0")/_load_env.sh"
load_dotenv
REGION="${GCP_REGION:-asia-northeast3}"
REPO="${ARTIFACT_REPO:-rag-mcp}"
IMAGE_BASE="${REGION}-docker.pkg.dev/${GCP_PROJECT_ID}/${REPO}"
DRIVE="${DRIVE:-$(echo "${DRIVE_IDS}" | cut -d, -f1)}"
: "${GCP_PROJECT_ID:?GCP_PROJECT_ID 필요}"; : "${DRIVE:?DRIVE_IDS 필요}"

gcloud config set project "${GCP_PROJECT_ID}" >/dev/null

if [[ "${SKIP_DEPLOY:-0}" != "1" ]]; then
  echo "== [1/3] sync 이미지 빌드 & 배포 =="
  gcloud builds submit --config=cloudbuild.sync.yaml \
    --substitutions="_IMAGE=${IMAGE_BASE}/sync:latest"
  # env 목록은 deploy.sh 의 rag-sync 와 **같아야 한다.** 여기는 운영 서비스를
  # 그대로 재배포하고 --set-env-vars 는 통째 치환이라, 빠진 변수는 스모크
  # 한 번으로 운영에서 사라진다. 특히 RAG_CORPUS_NAME_STUDENT/STUDENT_FOLDER_IDS
  # 가 빠지면 audience_split_enabled 가 False 로 떨어져 학생 분리가 조용히 꺼진다.
  gcloud run deploy rag-sync \
    --image="${IMAGE_BASE}/sync:latest" --region="${REGION}" --no-allow-unauthenticated \
    --set-env-vars="^|^GCP_PROJECT_ID=${GCP_PROJECT_ID}|GCP_REGION=${REGION}|GCS_RAW_BUCKET=${GCS_RAW_BUCKET}|GCS_NORMALIZED_BUCKET=${GCS_NORMALIZED_BUCKET}|RAG_CORPUS_NAME=${RAG_CORPUS_NAME}|DRIVE_IDS=${DRIVE_IDS}|SYNC_FOLDER_IDS=${SYNC_FOLDER_IDS:-}|FIRESTORE_COLLECTION=${FIRESTORE_COLLECTION:-doc_state}|FIRESTORE_DATABASE=${FIRESTORE_DATABASE:-doc-state}|QG_MODE=${QG_MODE:-log}|RAW_UPLOAD_CONCURRENCY=${RAW_UPLOAD_CONCURRENCY:-8}|RAG_DELETE_PACING_SECONDS=${RAG_DELETE_PACING_SECONDS:-1.1}|RAG_DELETE_CONCURRENCY=${RAG_DELETE_CONCURRENCY:-1}|RAG_CORPUS_NAME_STUDENT=${RAG_CORPUS_NAME_STUDENT:-}|STUDENT_FOLDER_IDS=${STUDENT_FOLDER_IDS:-}" \
    --memory=2Gi --cpu=2 --timeout=3600

  echo "== [1/3] 워크플로우 배포 (#1 YAML) =="
  gcloud workflows deploy rag-daily-sync --location="${REGION}" --source=workflows/daily_sync.yaml
fi

PARSER_URL=$(gcloud run services describe rag-parser --region="${REGION}" --format='value(status.url)' 2>/dev/null || echo "")
SYNC_URL=$(gcloud run services describe rag-sync --region="${REGION}" --format='value(status.url)')
echo "SYNC_URL=${SYNC_URL}"

# BACKFILL=1 은 드라이브 전체 재적재다 — 스모크 테스트 이름 아래 켜지기엔 무겁다.
if [[ "${BACKFILL:-0}" == "1" && "${CONFIRM_BACKFILL:-}" != "yes" ]]; then
  echo "BACKFILL=1 은 드라이브 전체를 다시 적재합니다." >&2
  echo "계속하려면 CONFIRM_BACKFILL=yes 를 함께 지정하세요." >&2
  exit 1
fi

echo "== [2/3] 워크플로우 실행 (drive=${DRIVE} backfill=${BACKFILL:-0}) =="
ARG="{\"syncUrl\":\"${SYNC_URL}\",\"parserUrl\":\"${PARSER_URL}\",\"driveIds\":[\"${DRIVE}\"]"
[[ "${BACKFILL:-0}" == "1" ]] && ARG="${ARG},\"backfill\":true"
ARG="${ARG}}"
RESULT=$(gcloud workflows run rag-daily-sync --location="${REGION}" --data="${ARG}" \
           --format='value(result)')
echo "--- workflow result.totals ---"
echo "${RESULT}"

echo "== [3/3] reconcile / commit 로그 (최근 1시간) =="
LOGS=$(gcloud logging read \
  'resource.type=cloud_run_revision AND resource.labels.service_name="rag-sync" AND (textPayload:"Reconciliation" OR textPayload:"pageToken NOT committed")' \
  --limit=30 --freshness=1h --format='value(timestamp,textPayload)' || true)
echo "${LOGS}"

echo "== 판정 =="
FAIL=0
if echo "${LOGS}" | grep -q "pageToken NOT committed"; then
  echo "  ❌ #1 회귀: 'pageToken NOT committed' 발견 → 커밋 게이트 여전히 막힘"; FAIL=1
fi
if echo "${LOGS}" | grep -q "Reconciliation mismatch"; then
  echo "  ❌ reconcile mismatch (ok=false) 발견"; FAIL=1
fi
if echo "${RESULT}" | grep -q '"failed": 0' 2>/dev/null; then
  echo "  ✅ totals.failed == 0"
fi
if [[ "${FAIL}" == "0" ]]; then
  echo "  ✅ PASS: 커밋 미보류 + reconcile mismatch 없음"
  echo "     (단, 이번 실행에 2-URI 파일이 실제 처리됐는지 result.totals 의 uris>gcsUploaded 로 확인)"
else
  echo "  ⚠️  위 항목 점검 필요"
fi
