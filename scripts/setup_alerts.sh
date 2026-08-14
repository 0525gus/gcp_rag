#!/usr/bin/env bash
# =============================================================================
# 알림 일괄 설정 (멱등) — 예산 1건 + 운영 정책 3건
#
# 예산 알림과 운영 알림은 서로를 못 잡는다.
#   - 예산은 '돈'을 본다. 스케줄러가 멈추면 비용은 **줄어들어** 조용하다
#   - 운영은 '동작'을 본다. 백필이 정상인데 비용만 폭증하면 조용하다
# 그래서 둘 다 건다.
#
#   사용:
#     ALERT_EMAIL=ops@example.com bash scripts/setup_alerts.sh
#     BUDGET_AMOUNT=200USD ALERT_EMAIL=... bash scripts/setup_alerts.sh
#
#   전제: gcloud 인증 + 프로젝트 권한. 예산은 결제 계정 권한이 따로 필요하다
#         (없으면 예산만 건너뛰고 운영 알림은 그대로 걸린다)
# =============================================================================
set -euo pipefail
cd "$(dirname "$0")/.."

# shellcheck source=scripts/_load_env.sh
. "$(dirname "$0")/_load_env.sh"
load_dotenv

PROJECT_ID="${GCP_PROJECT_ID:?set GCP_PROJECT_ID}"
: "${ALERT_EMAIL:?set ALERT_EMAIL (알림 받을 주소)}"
BUDGET_AMOUNT="${BUDGET_AMOUNT:-100USD}"
WORKFLOW_NAME="${WORKFLOW_NAME:-rag-daily-sync}"

echo "== API 활성화 =="
gcloud services enable monitoring.googleapis.com billingbudgets.googleapis.com \
  --project="${PROJECT_ID}"

# --- 1) 알림 채널 (이메일) --------------------------------------------------
# 채널이 없으면 정책을 걸어도 아무 데도 안 간다. 먼저 만든다.
echo "== 알림 채널 =="
CHANNEL=$(gcloud beta monitoring channels list \
  --project="${PROJECT_ID}" \
  --filter="type='email' AND labels.email_address='${ALERT_EMAIL}'" \
  --format="value(name)" | head -1)
if [[ -z "${CHANNEL}" ]]; then
  CHANNEL=$(gcloud beta monitoring channels create \
    --project="${PROJECT_ID}" \
    --display-name="rag ops (${ALERT_EMAIL})" \
    --type=email \
    --channel-labels="email_address=${ALERT_EMAIL}" \
    --format="value(name)")
  echo "  생성: ${CHANNEL}"
else
  echo "  기존 사용: ${CHANNEL}"
fi

# --- 2) 운영 알림 정책 -------------------------------------------------------
POLICY_DIR="$(mktemp -d)"
trap 'rm -rf "${POLICY_DIR}"' EXIT

# ① 하루 넘게 '성공한' 동기화가 없다
#    실패 알림으로는 못 잡는 사고를 잡는 유일한 조건이다 — 스케줄러가 아예
#    안 돌면 실패 로그조차 안 남는다(실제로 3일 중단을 늦게 발견한 사례).
#    주의: 부재 조건은 **한 번이라도 데이터가 있었어야** 동작한다. 첫 실행을
#    한 번 돌린 뒤에 이 정책이 의미를 갖는다.
cat > "${POLICY_DIR}/no-success.json" <<JSON
{
  "displayName": "rag: 일일 동기화 24시간 무성공",
  "combiner": "OR",
  "conditions": [
    {
      "displayName": "workflow 성공 실행 부재 24h",
      "conditionAbsent": {
        "filter": "resource.type=\"workflows.googleapis.com/Workflow\" AND resource.label.\"workflow_id\"=\"${WORKFLOW_NAME}\" AND metric.type=\"workflows.googleapis.com/finished_execution_count\" AND metric.label.\"status\"=\"SUCCEEDED\"",
        "duration": "86400s",
        "aggregations": [
          {"alignmentPeriod": "3600s", "perSeriesAligner": "ALIGN_SUM"}
        ]
      }
    }
  ],
  "notificationChannels": ["${CHANNEL}"],
  "alertStrategy": {"autoClose": "604800s"}
}
JSON

# ② 워크플로 실행 자체가 실패
cat > "${POLICY_DIR}/workflow-error.json" <<JSON
{
  "displayName": "rag: 워크플로 실행 실패",
  "combiner": "OR",
  "conditions": [
    {
      "displayName": "workflow ERROR 로그",
      "conditionMatchedLog": {
        "filter": "resource.type=\"workflows.googleapis.com/Workflow\" AND severity>=ERROR"
      }
    }
  ],
  "notificationChannels": ["${CHANNEL}"],
  "alertStrategy": {
    "notificationRateLimit": {"period": "3600s"},
    "autoClose": "604800s"
  }
}
JSON

# ③ 조용히 굳는 상태 — 델타 정체와 학생 코퍼스 정리 실패
#    문서 1건 실패는 워크플로가 흡수하므로 severity>=ERROR 전체를 걸면 시끄럽다.
#    '반복되면 데이터가 영구히 어긋나는' 두 신호만 고른다.
#      - pageToken 미커밋: 같은 페이지를 매일 재생하고 뒷 페이지는 영영 처리 안 됨
#      - 학생 코퍼스 정리 실패: 내려야 할 자료가 학생에게 계속 노출됨
cat > "${POLICY_DIR}/sync-stuck.json" <<JSON
{
  "displayName": "rag: 동기화 정체 / 학생 코퍼스 정리 실패",
  "combiner": "OR",
  "conditions": [
    {
      "displayName": "sync 정체 신호",
      "conditionMatchedLog": {
        "filter": "resource.type=\"cloud_run_revision\" AND resource.labels.service_name=\"rag-sync\" AND (textPayload:\"pageToken NOT committed\" OR textPayload:\"학생 코퍼스\" OR textPayload:\"cleanup failed\")"
      }
    }
  ],
  "notificationChannels": ["${CHANNEL}"],
  "alertStrategy": {
    "notificationRateLimit": {"period": "3600s"},
    "autoClose": "604800s"
  }
}
JSON

echo "== 알림 정책 =="
for f in no-success workflow-error sync-stuck; do
  name=$(python -c "import json,sys;print(json.load(open(sys.argv[1]))['displayName'])" \
    "${POLICY_DIR}/${f}.json")
  existing=$(gcloud alpha monitoring policies list \
    --project="${PROJECT_ID}" \
    --filter="displayName='${name}'" \
    --format="value(name)" | head -1)
  if [[ -n "${existing}" ]]; then
    echo "  건너뜀(이미 있음): ${name}"
    continue
  fi
  gcloud alpha monitoring policies create \
    --project="${PROJECT_ID}" \
    --policy-from-file="${POLICY_DIR}/${f}.json" >/dev/null
  echo "  생성: ${name}"
done

# --- 3) 예산 알림 -----------------------------------------------------------
# 예산은 **알림만** 한다. 임계를 넘어도 서비스는 그대로 돈다(자동 차단 아님).
echo "== 예산 =="
BILLING_ACCOUNT="${BILLING_ACCOUNT:-$(gcloud billing projects describe "${PROJECT_ID}" \
  --format='value(billingAccountName)' 2>/dev/null || echo "")}"
BILLING_ACCOUNT="${BILLING_ACCOUNT#billingAccounts/}"
if [[ -z "${BILLING_ACCOUNT}" ]]; then
  echo "  건너뜀: 결제 계정을 못 읽었다 (권한 또는 미연결). BILLING_ACCOUNT=... 로 지정 가능"
else
  BUDGET_NAME="rag ${PROJECT_ID} 월 예산"
  existing=$(gcloud billing budgets list \
    --billing-account="${BILLING_ACCOUNT}" \
    --filter="displayName='${BUDGET_NAME}'" \
    --format="value(name)" 2>/dev/null | head -1)
  if [[ -n "${existing}" ]]; then
    echo "  건너뜀(이미 있음): ${BUDGET_NAME}"
  else
    gcloud billing budgets create \
      --billing-account="${BILLING_ACCOUNT}" \
      --display-name="${BUDGET_NAME}" \
      --budget-amount="${BUDGET_AMOUNT}" \
      --threshold-rule=percent=0.5 \
      --threshold-rule=percent=0.9 \
      --threshold-rule=percent=1.0 \
      --filter-projects="projects/${PROJECT_ID}" >/dev/null
    echo "  생성: ${BUDGET_NAME} (${BUDGET_AMOUNT})"
  fi
fi

echo
echo "Done."
echo "  채널 : ${ALERT_EMAIL}"
echo "  정책 : 24시간 무성공 / 워크플로 실패 / 동기화 정체"
echo "  예산 : ${BUDGET_AMOUNT} (50%·90%·100%)"
echo
echo "확인: gcloud alpha monitoring policies list --project=${PROJECT_ID} --format='table(displayName,enabled)'"
