# Cloud Run / Workflows / Scheduler 배포
# 사용: .\scripts\deploy.ps1
#
# 올리는 것: rag-parser, rag-sync, 교직원 MCP, (분리가 켜져 있으면) 학생 MCP,
#            Workflows, Scheduler, 그 SA·IAM.
# MCP 공개 여부는 ALLOW_UNAUTH (기본 true = 공개). deploy_mcp.ps1 과 같은 스위치.
#
# --set-env-vars 는 Cloud Run env 를 통째로 치환한다. 안 넘긴 값은 사라진다.
# DRIVE_IDS / SYNC_FOLDER_IDS 에 콤마가 있어 구분자는 | (^|^...).
#
# ---- 1. 환경 로드 · 필수값 검증 (Load-Dotenv, Require-FullDeployEnv) ----
# ---- 2. gcloud 프로젝트 설정 · API enable ----
# ---- 3. Artifact Registry 확인/생성 ----
# ---- 4. 이미지 빌드 · 푸시 (parser → sync → mcp) ----
# ---- 5. Cloud Run 배포 (rag-parser → rag-sync → MCP 교직원 → (분리 시) MCP 학생) ----
# ---- 6. 서비스 URL 조회 ----
# ---- 7. Workflow 배포 (rag-daily-sync) ----
# ---- 8. Scheduler SA · App Engine 준비 ----
# ---- 9. Cloud Scheduler job 등록/갱신 (00:00 Asia/Seoul) ----
# ---- 10. 코퍼스 적재량 확인 · 비어있으면 첫 백필 여부 확인 ----

# ---- 1. 환경 로드 · 필수값 검증 ----
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
# deploy_mcp.ps1 과 같은 값이어야 한다. 한쪽만 넘기면 그쪽으로 재배포할 때마다
# 동시성이 Cloud Run 기본값으로 되돌아간다.
$MCP_CONCURRENCY = Get-EnvOr MCP_CONCURRENCY "40"
# MCP 공개 여부. deploy_mcp.ps1 과 같은 스위치를 쓴다 — 두 스크립트가 다르면
# 어느 쪽으로 재배포했느냐에 따라 공개 상태가 조용히 뒤집힌다.
# 기본 true: FactChat 커넥터가 정적 헤더만 보내므로 Cloud Run IAM 을 열어야 한다.
# 경계는 앱 계층 키(MCP_API_KEY)뿐이다 — 키가 새면 코퍼스 전량이 열린다.
$ALLOW_UNAUTH = Get-EnvOr ALLOW_UNAUTH "true"
# @(...) 로 감싼다. if 결과를 그냥 담으면 1개짜리 배열이 문자열로 풀리고,
# 아래 @mcpAuthArgs 스플랫이 그 문자열을 글자 단위로 넘긴다 —
# gcloud 가 "unrecognized arguments: - a l o w ..." 로 죽었다(실측).
$mcpAuthArgs = @(if ($ALLOW_UNAUTH -eq "true") { "--allow-unauthenticated" } else { "--no-allow-unauthenticated" })
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

# ---- 2. gcloud 프로젝트 설정 · API enable ----
gcloud config set project $PROJECT_ID
Assert-LastExit

Write-Host "== Enable APIs =="
gcloud services enable `
  run.googleapis.com `
  compute.googleapis.com `
  workflows.googleapis.com `
  cloudscheduler.googleapis.com `
  appengine.googleapis.com `
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

# ---- 3. Artifact Registry 확인/생성 ----
Write-Host "== Artifact Registry =="
gcloud artifacts repositories describe $REPO --location=$REGION 2>$null
if ($LASTEXITCODE -ne 0) {
  gcloud artifacts repositories create $REPO `
    --repository-format=docker `
    --location=$REGION
  Assert-LastExit
}

$IMAGE_BASE = "$REGION-docker.pkg.dev/$PROJECT_ID/$REPO"

# ---- 4. 이미지 빌드 · 푸시 (parser → sync → mcp) ----
Write-Host "== Build & push images =="
gcloud builds submit --config=cloudbuild.parser.yaml --substitutions="_IMAGE=$IMAGE_BASE/parser:latest"
Assert-LastExit
gcloud builds submit --config=cloudbuild.sync.yaml --substitutions="_IMAGE=$IMAGE_BASE/sync:latest"
Assert-LastExit
gcloud builds submit --config=cloudbuild.mcp.yaml --substitutions="_IMAGE=$IMAGE_BASE/mcp:latest"
Assert-LastExit

# ---- 5. Cloud Run 배포 (rag-parser → rag-sync → MCP 교직원 → (분리 시) MCP 학생) ----
Write-Host "== Deploy Cloud Run =="
# parser timeout 540 < sync httpx 600. 서버가 먼저 포기해야 sync 가 오류를 받는다.
# concurrency 4: 요청당 메모리 한계. 넘치는 요청은 새 인스턴스로.
# min-instances=0 은 기본값과 같지만 명시한다 — 콜드스타트를 감수하겠다는 의도
# 표시(docs/OPS_DEFERRED.md #7). MCP 응답 지연이 문제되면 MCP 쪽만 1로 올릴 것,
# sync/parser 는 배치라 0 유지.
$parserEnv = "^|^GCP_PROJECT_ID=$PROJECT_ID|GCP_REGION=$REGION|GCS_HWP_ORIGINAL_BUCKET=$GCS_HWP_ORIG|GCS_SOURCE_BUCKET=$GCS_SOURCE|RAG_CORPUS_NAME=$CORPUS|DOCAI_PROCESSOR_ID=$DOCAI|QG_MODE=$QG_MODE|FIRESTORE_DATABASE=$FS_DB|DOC_STATE_COLLECTION=$FS_COL"
gcloud run deploy rag-parser `
  --image="$IMAGE_BASE/parser:latest" `
  --region=$REGION `
  --no-allow-unauthenticated `
  --set-env-vars=$parserEnv `
  --memory=2Gi --cpu=2 --timeout=$PARSER_TIMEOUT `
  --concurrency=$PARSER_CONCURRENCY `
  --max-instances=$PARSER_MAX_INSTANCES `
  --min-instances=0
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
  --concurrency=$SYNC_CONCURRENCY `
  --min-instances=0
Assert-LastExit

# ALLOW_UNAUTH=true(기본)면 공개 URL 로 올라가 FactChat 이 바로 붙는다.
$MCP_SERVICE = Get-McpStaffServiceName
$mcpEnv = "^|^GCP_PROJECT_ID=$PROJECT_ID|GCP_REGION=$REGION|RAG_CORPUS_NAME=$CORPUS|GCS_HWP_ORIGINAL_BUCKET=$GCS_HWP_ORIG|GCS_SOURCE_BUCKET=$GCS_SOURCE|FIRESTORE_DATABASE=$FS_DB|DOC_STATE_COLLECTION=$FS_COL|MCP_API_KEY=$MCP_API_KEY|TOP_K_DEFAULT=$TOP_K|SEARCH_FETCH_MULTIPLIER=$FETCH_MULT|SEARCH_FETCH_MAX=$FETCH_MAX"
gcloud run deploy $MCP_SERVICE `
  --image="$IMAGE_BASE/mcp:latest" `
  --region=$REGION `
  @mcpAuthArgs `
  --set-env-vars=$mcpEnv `
  --memory=1Gi --cpu=1 --timeout=60 --concurrency=$MCP_CONCURRENCY `
  --min-instances=0
Assert-LastExit

# 학생 분리가 켜져 있으면 학생 MCP 도 같이 올린다. 코퍼스와 키를 학생 값으로
# 갈아끼우는 게 핵심 — 비우고 배포하면 학생 서비스가 교직원 전량을 검색한다.
# 스위치는 .env 두 값이며 config.py 의 audience_split_enabled 와 같은 조건이다.
$STUDENT_MCP_SERVICE = ""
if ($STUDENT_CORPUS -and $STUDENT_FOLDERS) {
  $STUDENT_MCP_SERVICE = Get-McpStudentServiceName
  $STUDENT_KEY = Get-EnvOr MCP_API_KEY_STUDENT ""
  if (-not $STUDENT_KEY) {
    throw "MCP_API_KEY_STUDENT: 학생 분리가 켜져 있으면 필요하다 (교직원 키와 다른 값)"
  }
  Write-Host "== 학생 MCP ($STUDENT_MCP_SERVICE) =="
  $studentMcpEnv = "^|^GCP_PROJECT_ID=$PROJECT_ID|GCP_REGION=$REGION|RAG_CORPUS_NAME=$STUDENT_CORPUS|GCS_HWP_ORIGINAL_BUCKET=$GCS_HWP_ORIG|GCS_SOURCE_BUCKET=$GCS_SOURCE|FIRESTORE_DATABASE=$FS_DB|DOC_STATE_COLLECTION=$FS_COL|MCP_API_KEY=$STUDENT_KEY|TOP_K_DEFAULT=$TOP_K|SEARCH_FETCH_MULTIPLIER=$FETCH_MULT|SEARCH_FETCH_MAX=$FETCH_MAX"
  gcloud run deploy $STUDENT_MCP_SERVICE `
    --image="$IMAGE_BASE/mcp:latest" `
    --region=$REGION `
    @mcpAuthArgs `
    --set-env-vars=$studentMcpEnv `
    --memory=1Gi --cpu=1 --timeout=60 --concurrency=$MCP_CONCURRENCY `
    --min-instances=0
  Assert-LastExit
}

# ---- 6. 서비스 URL 조회 ----
$PARSER_URL = gcloud run services describe rag-parser --region=$REGION --format="value(status.url)"
Assert-LastExit
$SYNC_URL = gcloud run services describe rag-sync --region=$REGION --format="value(status.url)"
Assert-LastExit
$MCP_URL = gcloud run services describe $MCP_SERVICE --region=$REGION --format="value(status.url)"
Assert-LastExit

Write-Host "PARSER_URL=$PARSER_URL"
Write-Host "SYNC_URL=$SYNC_URL"
Write-Host "MCP_URL=$MCP_URL"
if ($STUDENT_MCP_SERVICE) {
  $STUDENT_MCP_URL = gcloud run services describe $STUDENT_MCP_SERVICE --region=$REGION --format="value(status.url)"
  Assert-LastExit
  Write-Host "STUDENT_MCP_URL=$STUDENT_MCP_URL"
}
Write-Host ""
if ($ALLOW_UNAUTH -eq "true") {
  Write-Host "MCP 는 공개(--allow-unauthenticated). 경계는 API 키뿐이다."
  Write-Host "FactChat 커넥터: {URL}/mcp · Streamable HTTP · Authorization: Bearer {키}"
} else {
  Write-Host "MCP 는 IAM 전용(ALLOW_UNAUTH=$ALLOW_UNAUTH). FactChat 은 붙지 못한다."
}

$driveIds = @($DRIVE_IDS -split "," | ForEach-Object { $_.Trim() } | Where-Object { $_ })
$driveJson = "[" + (($driveIds | ForEach-Object { '"' + $_ + '"' }) -join ",") + "]"
$innerJson = '{"syncUrl":"' + $SYNC_URL.Trim() + '","parserUrl":"' + $PARSER_URL.Trim() + '","driveIds":' + $driveJson + "}"
$escaped = $innerJson.Replace("\", "\\").Replace('"', '\"')
$bodyJson = '{"argument":"' + $escaped + '"}'

# ---- 7. Workflow 배포 (rag-daily-sync) ----
Write-Host "== Deploy Workflow =="
gcloud workflows deploy rag-daily-sync --location=$REGION --source=workflows/daily_sync.yaml
Assert-LastExit

# ---- 8. Scheduler SA · App Engine 준비 ----
$SCHEDULER_SA = Get-EnvOr SCHEDULER_SA "scheduler@${PROJECT_ID}.iam.gserviceaccount.com"
Write-Host "== Ensure Scheduler SA / App Engine =="
# 이메일이 다른 프로젝트를 가리키면 조용히 어긋난다: describe 는 "없다" 하고
# create 는 ALREADY_EXISTS 로 터진다(실측 — .env 에 예시값이 남아 있던 경우).
# 예전에는 describe 가 전체 이메일을, create 가 하드코딩 "scheduler" 를 봐서
# 확인 대상과 생성 대상이 아예 달랐다. 계정 ID 는 이메일에서 뽑아 하나로 쓴다.
# 이 스크립트는 SA 를 $PROJECT_ID 안에만 만들므로 도메인도 거기로 맞춘다.
$SCHEDULER_SA_ID = ($SCHEDULER_SA -split "@")[0]
$SCHEDULER_SA_EXPECTED = "${SCHEDULER_SA_ID}@${PROJECT_ID}.iam.gserviceaccount.com"
if ($SCHEDULER_SA -ne $SCHEDULER_SA_EXPECTED) {
  Write-Host "!! SCHEDULER_SA 가 $PROJECT_ID 밖을 가리킨다 ($SCHEDULER_SA)"
  Write-Host "   $SCHEDULER_SA_EXPECTED 로 진행한다 — .env 를 고칠 것"
  $SCHEDULER_SA = $SCHEDULER_SA_EXPECTED
}
gcloud iam service-accounts describe $SCHEDULER_SA --project=$PROJECT_ID 2>$null
if ($LASTEXITCODE -ne 0) {
  gcloud iam service-accounts create $SCHEDULER_SA_ID `
    --display-name="RAG daily sync scheduler" `
    --project=$PROJECT_ID
  Assert-LastExit
}
gcloud projects add-iam-policy-binding $PROJECT_ID `
  --member="serviceAccount:${SCHEDULER_SA}" `
  --role="roles/workflows.invoker" `
  --condition=None | Out-Null
Assert-LastExit

# Cloud Scheduler 는 프로젝트에 App Engine 앱이 있어야 잡을 만든다.
# appengine.googleapis.com 이 enable 목록에 없으면 여기 create 가 실패하고,
# Cloud Run·Workflows 까지 다 올라간 뒤 마지막에 죽는다(실측).
gcloud app describe --project=$PROJECT_ID 2>$null
if ($LASTEXITCODE -ne 0) {
  gcloud app create --region=$REGION --project=$PROJECT_ID
  Assert-LastExit
}

# ---- 9. Cloud Scheduler job 등록/갱신 (00:00 Asia/Seoul) ----
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

# ---- 10. 코퍼스 적재량 확인 · 비어있으면 첫 백필 여부 확인 ----
# 배포가 끝났으니 코퍼스가 실제로 찼는지 본다. 비어 있으면 첫 백필을 물어본다.
# ACTIVE 가 아니면 색인이 통째로 실패하므로 그 사실을 먼저 알린다 —
# 워크플로는 그래도 SUCCEEDED 로 끝나서 조용히 빈 코퍼스로 남는다.
$corpusToken = (Get-GcloudText -GcloudArgs @("auth", "print-access-token")).Text.Split("`n")[0].Trim()
if ($corpusToken) {
  $usable = Test-RagCorpusUsable -Name $CORPUS -Token $corpusToken
  if (-not $usable.Ok) {
    Write-Host ""
    Write-Host "WARN 코퍼스를 쓸 수 없다: $($usable.Detail)"
    Write-Host "     이 상태로는 색인이 전부 실패한다. 백필을 걸지 않는다."
  } else {
    $files = Get-RagCorpusFileCount -Name $CORPUS -Token $corpusToken
    Write-Host ""
    # -1 = 조회 실패. 0 과 섞으면 멀쩡한 코퍼스에 전체 백필을 다시 건다.
    if ($files -lt 0) {
      Write-Host "코퍼스 적재량을 확인하지 못했다 — 백필은 걸지 않는다."
    } else {
      Write-Host "코퍼스 적재: $files 건"
    }
    if ($files -eq 0) {
      Write-Host "비어 있다 — 첫 적재가 필요하다."
      if (Confirm-PreflightAction "지금 전체 백필을 실행하시겠습니까? (수 분~수십 분 소요)") {
        & (Join-Path $PSScriptRoot "backfill.ps1")
      } else {
        Write-Host "건너뜀. 나중에: scripts\backfill.ps1"
      }
    }
  }
}
