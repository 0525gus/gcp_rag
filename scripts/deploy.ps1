[CmdletBinding()]
param(
  [switch]$SkipMcp,
  [switch]$ReuseExisting,
  [switch]$EnvOnly
)

$ErrorActionPreference = "Stop"
if ($PSVersionTable.PSVersion.Major -ge 7) {
  $PSNativeCommandUseErrorActionPreference = $false
}

Set-Location (Split-Path -Parent $PSScriptRoot)
. (Join-Path $PSScriptRoot "_load_env.ps1")
. (Join-Path $PSScriptRoot "preflight.ps1")

# @() 필수 — 학과가 하나면 결과가 문자열이라 [0] 이 첫 글자가 된다.
$DEPT_CODES = @(Set-BaseDeployConfig)
$BASE_DEPT = $DEPT_CODES[0]
Require-FullDeployEnv

$DEPARTMENTS_JSON = Get-DepartmentsJson
$DEPT_MAP = $DEPARTMENTS_JSON | ConvertFrom-Json

function Assert-LastExit {
  if ($LASTEXITCODE -ne 0) { throw "gcloud exit $LASTEXITCODE" }
}

# GCP 는 만든 직후를 모른다. API enable 뒤의 서비스 에이전트, SA 생성 뒤의 IAM
# 이 그렇다 — 만들기는 성공했는데 다음 명령이 "does not exist" 로 죽는다(실측,
# 신규 프로젝트 첫 배포에서 두 번 다 터졌다). 전파를 기다렸다 다시 친다.
function Wait-ForGcloudSuccess {
  param([ScriptBlock]$Action, [int[]]$Waits, [string]$Label)
  foreach ($wait in $Waits) {
    if ($wait -gt 0) {
      Write-Host "-- $Label 전파 대기 ${wait}s 후 재시도"
      Start-Sleep -Seconds $wait
    }
    & $Action | Out-Null
    if ($LASTEXITCODE -eq 0) { return $true }
  }
  return $false
}

$PROJECT_ID = $env:GCP_PROJECT_ID
$REGION = $env:GCP_REGION
$REPO = $env:ARTIFACT_REPO
$FS_DB = $env:FIRESTORE_DATABASE
$FS_COL = $env:DOC_STATE_COLLECTION
$QG_MODE = $env:QG_MODE
$PARSER_TIMEOUT = $env:PARSER_TIMEOUT
$PARSER_CONCURRENCY = $env:PARSER_CONCURRENCY
$PARSER_MAX_INSTANCES = $env:PARSER_MAX_INSTANCES
$SYNC_CONCURRENCY = $env:SYNC_CONCURRENCY
$ALLOW_UNAUTH = $env:ALLOW_UNAUTH
$INGEST_CONC = $env:INGEST_CONCURRENCY
$RAG_DEL_PACE = $env:RAG_DELETE_PACING_SECONDS
$RAG_DEL_CONC = $env:RAG_DELETE_CONCURRENCY
$RAG_METADATA_BUCKET = $env:RAG_METADATA_BUCKET
$RAG_MAPPING_WRITE = $env:RAG_MAPPING_WRITE_ENABLED
$RAG_MAPPING_READ = $env:RAG_MAPPING_READ_ENABLED
$RAG_MAPPING_FALLBACK = $env:RAG_MAPPING_FALLBACK_SCAN_ENABLED
$CLOUD_TASKS_ENABLED = $env:CLOUD_TASKS_ENABLED
$TASK_QUEUE_LOCATION = $env:TASK_QUEUE_LOCATION
$TASK_QUEUE_FACULTY = $env:TASK_QUEUE_FACULTY
$TASK_QUEUE_STUDENT = $env:TASK_QUEUE_STUDENT
$TASK_SERVICE_ACCOUNT = $env:TASK_SERVICE_ACCOUNT
$SYNC_TASK_BASE_URL = $env:SYNC_TASK_BASE_URL
$INDEX_JOB_TIMEOUT = $env:INDEX_JOB_TIMEOUT_SECONDS
$DOCAI = $env:DOCAI_PROCESSOR_ID
$DOCAI_LOCATION = $env:DOCAI_LOCATION
$ENABLE_DOCAI_FALLBACK = $env:ENABLE_DOCAI_FALLBACK
$DOCAI_OCR_PROCESSOR_ID = $env:DOCAI_OCR_PROCESSOR_ID
$DOCAI_OCR_LOCATION = $env:DOCAI_OCR_LOCATION
$SYNC_FOLDERS = $env:SYNC_FOLDER_IDS
$STUDENT_CORPUS = $env:RAG_CORPUS_NAME_STUDENT
$STUDENT_FOLDERS = $env:STUDENT_FOLDER_IDS
$GCS_HWP_ORIG = $env:GCS_HWP_ORIGINAL_BUCKET
$GCS_SOURCE = $env:GCS_SOURCE_BUCKET
$CORPUS = $env:RAG_CORPUS_NAME
$DRIVE_IDS = $env:DRIVE_IDS

if (-not $TASK_SERVICE_ACCOUNT) {
  $PROJECT_NUMBER = (gcloud projects describe $PROJECT_ID --format="value(projectNumber)").Trim()
  if (-not $PROJECT_NUMBER) { throw "프로젝트 번호를 조회하지 못했다: $PROJECT_ID" }
  $TASK_SERVICE_ACCOUNT = "${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"
}
if (-not $SYNC_TASK_BASE_URL) {
  $SYNC_TASK_BASE_URL = (gcloud run services describe rag-sync --region=$REGION --project=$PROJECT_ID --format="value(status.url)" 2>$null).Trim()
}

# --set-env-vars 는 env 를 통째로 치환한다. 여기 없는 값은 서비스에서 사라진다.
# DRIVE_IDS / SYNC_FOLDER_IDS 에 콤마가 있어 구분자는 | (^|^...).
$parserEnv = "^|^GCP_PROJECT_ID=$PROJECT_ID|GCP_REGION=$REGION|GCS_HWP_ORIGINAL_BUCKET=$GCS_HWP_ORIG|GCS_SOURCE_BUCKET=$GCS_SOURCE|RAG_CORPUS_NAME=$CORPUS|DOCAI_PROCESSOR_ID=$DOCAI|QG_MODE=$QG_MODE|FIRESTORE_DATABASE=$FS_DB|DOC_STATE_COLLECTION=$FS_COL"
$parserEnv += "|DOCAI_LOCATION=$DOCAI_LOCATION|ENABLE_DOCAI_FALLBACK=$ENABLE_DOCAI_FALLBACK"
$parserEnv += "|DOCAI_OCR_PROCESSOR_ID=$DOCAI_OCR_PROCESSOR_ID|DOCAI_OCR_LOCATION=$DOCAI_OCR_LOCATION"
$syncEnv = "^|^GCP_PROJECT_ID=$PROJECT_ID|GCP_REGION=$REGION|GCS_HWP_ORIGINAL_BUCKET=$GCS_HWP_ORIG|GCS_SOURCE_BUCKET=$GCS_SOURCE|RAG_CORPUS_NAME=$CORPUS|DRIVE_IDS=$DRIVE_IDS|SYNC_FOLDER_IDS=$SYNC_FOLDERS|DOC_STATE_COLLECTION=$FS_COL|FIRESTORE_DATABASE=$FS_DB|QG_MODE=$QG_MODE|INGEST_CONCURRENCY=$INGEST_CONC|RAG_DELETE_PACING_SECONDS=$RAG_DEL_PACE|RAG_DELETE_CONCURRENCY=$RAG_DEL_CONC|RAG_METADATA_BUCKET=$RAG_METADATA_BUCKET|RAG_MAPPING_WRITE_ENABLED=$RAG_MAPPING_WRITE|RAG_MAPPING_READ_ENABLED=$RAG_MAPPING_READ|RAG_MAPPING_FALLBACK_SCAN_ENABLED=$RAG_MAPPING_FALLBACK|CLOUD_TASKS_ENABLED=$CLOUD_TASKS_ENABLED|TASK_QUEUE_LOCATION=$TASK_QUEUE_LOCATION|TASK_QUEUE_FACULTY=$TASK_QUEUE_FACULTY|TASK_QUEUE_STUDENT=$TASK_QUEUE_STUDENT|TASK_SERVICE_ACCOUNT=$TASK_SERVICE_ACCOUNT|SYNC_TASK_BASE_URL=$SYNC_TASK_BASE_URL|INDEX_JOB_TIMEOUT_SECONDS=$INDEX_JOB_TIMEOUT|RAG_CORPUS_NAME_STUDENT=$STUDENT_CORPUS|STUDENT_FOLDER_IDS=$STUDENT_FOLDERS|DEPARTMENTS_JSON=$DEPARTMENTS_JSON|DEPARTMENTS_REQUIRED=true"

Write-Host "== 학과 $($DEPT_CODES.Count) 개: $($DEPT_CODES -join ', ') =="
Write-Host "   parser/sync 기본값은 '$BASE_DEPT' · 드라이브는 전 학과 union"

if ($EnvOnly) {
  Write-Host "== Refresh Cloud Run env =="
  gcloud config set project $PROJECT_ID
  Assert-LastExit
  gcloud run services update rag-parser --region=$REGION --project=$PROJECT_ID --set-env-vars=$parserEnv
  Assert-LastExit
  gcloud run services update rag-sync --region=$REGION --project=$PROJECT_ID --set-env-vars=$syncEnv
  Assert-LastExit
  Write-Host "== Cloud Run env 갱신 완료 =="
  exit 0
}

gcloud config set project $PROJECT_ID
Assert-LastExit

Write-Host "== Enable APIs =="
gcloud services enable `
  run.googleapis.com `
  compute.googleapis.com `
  workflows.googleapis.com `
  workflowexecutions.googleapis.com `
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
  cloudtasks.googleapis.com `
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

function Ensure-Image {
  param([string]$Name, [string]$Config)
  $tag = "$IMAGE_BASE/${Name}:latest"
  if ($ReuseExisting) {
    $digest = gcloud artifacts docker images describe $tag --format="value(image_summary.digest)" 2>$null
    if ($LASTEXITCODE -eq 0 -and -not [string]::IsNullOrWhiteSpace($digest)) {
      Write-Host "-- $Name 이미지 재사용: $digest"
      return
    }
    Write-Host "-- $Name 이미지 없음 → 빌드"
  }
  gcloud builds submit --config=$Config --substitutions="_IMAGE=$tag"
  Assert-LastExit
}

Ensure-Image -Name "parser" -Config "cloudbuild.parser.yaml"
Ensure-Image -Name "sync" -Config "cloudbuild.sync.yaml"
if (-not $SkipMcp) {
  Ensure-Image -Name "mcp" -Config "cloudbuild.mcp.yaml"
}

Write-Host "== Deploy Cloud Run =="

function Test-SkipService {
  param([string]$Name)
  if (-not $ReuseExisting) { return $false }
  gcloud run services describe $Name --region=$REGION --project=$PROJECT_ID --format="value(status.url)" 2>$null | Out-Null
  if ($LASTEXITCODE -eq 0) {
    Write-Host "-- $Name 이미 배포됨 → 배포 생략"
    return $true
  }
  Write-Host "-- $Name 없음 → 배포"
  return $false
}
# parser timeout 540 < sync httpx 600. 서버가 먼저 포기해야 sync 가 오류를 받는다.
# concurrency 4: 요청당 메모리 한계. 넘치는 요청은 새 인스턴스로.
if (-not (Test-SkipService -Name "rag-parser")) {
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
}

# sync timeout 3600. 워크플로우 스텝(1800s)에 맞추면 안 됨 — backfill 이 토큰을 못 남긴다.
if (-not (Test-SkipService -Name "rag-sync")) {
  gcloud run deploy rag-sync `
    --image="$IMAGE_BASE/sync:latest" `
    --region=$REGION `
    --no-allow-unauthenticated `
    --set-env-vars=$syncEnv `
    --memory=2Gi --cpu=2 --timeout=3600 `
    --concurrency=$SYNC_CONCURRENCY `
    --min-instances=0
  Assert-LastExit
}

if (-not $SkipMcp) {
  Write-Host ""
  Write-Host "== 공통 MCP 배포 (deploy_mcp.ps1) =="
  & (Join-Path $PSScriptRoot "deploy_mcp.ps1") -SkipBuild
  Set-BaseDeployConfig | Out-Null
}

$PARSER_URL = gcloud run services describe rag-parser --region=$REGION --format="value(status.url)"
Assert-LastExit
$SYNC_URL = gcloud run services describe rag-sync --region=$REGION --format="value(status.url)"
Assert-LastExit

if ($SYNC_TASK_BASE_URL -ne $SYNC_URL.Trim()) {
  gcloud run services update rag-sync --region=$REGION --project=$PROJECT_ID `
    --update-env-vars="SYNC_TASK_BASE_URL=$($SYNC_URL.Trim())"
  Assert-LastExit
  $SYNC_TASK_BASE_URL = $SYNC_URL.Trim()
}

# 한 코퍼스는 동시에 import/delete를 받을 수 없으므로 queue별 동시 실행은 1이다.
# 0.2/s는 시작 간격의 상한이고, 실제 처리(약 10~20초)가 더 느려 자연스럽게
# 분당 3~6회 수준이 된다. 교직원/학생은 서로 다른 코퍼스라 두 큐가 병렬 가능하다.
Write-Host "== Cloud Tasks queues =="
$PROJECT_NUMBER = (gcloud projects describe $PROJECT_ID --format="value(projectNumber)").Trim()
$TASKS_AGENT = "service-${PROJECT_NUMBER}@gcp-sa-cloudtasks.iam.gserviceaccount.com"
gcloud projects add-iam-policy-binding $PROJECT_ID `
  --member="serviceAccount:${TASK_SERVICE_ACCOUNT}" `
  --role="roles/cloudtasks.enqueuer" --condition=None | Out-Null
Assert-LastExit
gcloud iam service-accounts add-iam-policy-binding $TASK_SERVICE_ACCOUNT `
  --project=$PROJECT_ID `
  --member="serviceAccount:${TASKS_AGENT}" `
  --role="roles/iam.serviceAccountTokenCreator" | Out-Null
Assert-LastExit
gcloud run services add-iam-policy-binding rag-sync `
  --region=$REGION --project=$PROJECT_ID `
  --member="serviceAccount:${TASK_SERVICE_ACCOUNT}" `
  --role="roles/run.invoker" | Out-Null
Assert-LastExit

foreach ($queue in @($TASK_QUEUE_FACULTY, $TASK_QUEUE_STUDENT)) {
  gcloud tasks queues describe $queue --location=$TASK_QUEUE_LOCATION --project=$PROJECT_ID 2>$null
  if ($LASTEXITCODE -eq 0) {
    gcloud tasks queues update $queue `
      --location=$TASK_QUEUE_LOCATION --project=$PROJECT_ID `
      --max-dispatches-per-second=0.2 --max-concurrent-dispatches=1 `
      --max-attempts=5 --max-retry-duration=900s `
      --min-backoff=2s --max-backoff=60s --max-doublings=5 `
      --log-sampling-ratio=1.0
  } else {
    gcloud tasks queues create $queue `
      --location=$TASK_QUEUE_LOCATION --project=$PROJECT_ID `
      --max-dispatches-per-second=0.2 --max-concurrent-dispatches=1 `
      --max-attempts=5 --max-retry-duration=900s `
      --min-backoff=2s --max-backoff=60s --max-doublings=5 `
      --log-sampling-ratio=1.0
  }
  Assert-LastExit
}

Write-Host "PARSER_URL=$PARSER_URL"
Write-Host "SYNC_URL=$SYNC_URL"
Write-Host ""
if ($SkipMcp) {
  Write-Host "MCP 는 건너뛰었다 (-SkipMcp). 올리려면: .\scripts\deploy_mcp.ps1"
} elseif ($ALLOW_UNAUTH -eq "true") {
  Write-Host "MCP 는 공개(--allow-unauthenticated). 경계는 API 키뿐이다."
  Write-Host "FactChat 커넥터: {URL}/mcp / Streamable HTTP / Authorization: Bearer {키}"
} else {
  Write-Host "MCP 는 IAM 전용(ALLOW_UNAUTH=$ALLOW_UNAUTH). FactChat 은 붙지 못한다."
}

$driveIds = @($DRIVE_IDS -split "," | ForEach-Object { $_.Trim() } | Where-Object { $_ })
$driveJson = "[" + (($driveIds | ForEach-Object { '"' + $_ + '"' }) -join ",") + "]"
$innerJson = '{"syncUrl":"' + $SYNC_URL.Trim() + '","parserUrl":"' + $PARSER_URL.Trim() + '","driveIds":' + $driveJson + "}"
$escaped = $innerJson.Replace("\", "\\").Replace('"', '\"')
$bodyJson = '{"argument":"' + $escaped + '"}'

Write-Host "== Deploy Workflow =="
# 새 프로젝트에서 workflows.googleapis.com 을 막 켜면 서비스 에이전트
# (service-<번호>@gcp-sa-workflows.iam.gserviceaccount.com) 가 아직 없어서 첫
# 배포가 "FAILED_PRECONDITION: Workflows service agent does not exist" 로 죽는다
# (실측 — 신규 프로젝트 첫 배포). 에이전트는 enable 뒤 **비동기로** 생기므로
# 생성을 먼저 요청하고(이미 있으면 무해), 그래도 늦으면 기다렸다 재시도한다.
# `gcloud beta services identity create` 는 beta 컴포넌트를 요구해서 못 쓴다 —
# 비대화 모드에서는 컴포넌트 설치가 막힌다. 그래서 REST 를 직접 부른다.
$identityUri = "https://serviceusage.googleapis.com/v1beta1/projects/$PROJECT_ID/services/workflows.googleapis.com:generateServiceIdentity"
try {
  $suToken = (Get-GcloudText -GcloudArgs @("auth", "print-access-token")).Text.Split("`n")[0].Trim()
  Invoke-RestMethod -Method Post -Uri $identityUri `
    -Headers @{ Authorization = "Bearer $suToken" } `
    -ContentType "application/json" -Body "{}" | Out-Null
} catch {
  Write-Host "-- 서비스 에이전트 생성 요청 실패(재시도로 확인): $($_.Exception.Message)"
}
$workflowDeployed = Wait-ForGcloudSuccess -Label "Workflows 서비스 에이전트" -Waits @(0, 10, 20, 30) -Action {
  gcloud workflows deploy rag-daily-sync --location=$REGION --source=workflows/daily_sync.yaml
}
if (-not $workflowDeployed) {
  throw "Workflow 배포 실패 — Workflows 서비스 에이전트가 아직 없다. 몇 분 뒤 다시 실행할 것"
}

$SCHEDULER_SA = "scheduler@${PROJECT_ID}.iam.gserviceaccount.com"
Write-Host "== Ensure Scheduler SA / App Engine =="
$SCHEDULER_SA_ID = ($SCHEDULER_SA -split "@")[0]
gcloud iam service-accounts describe $SCHEDULER_SA --project=$PROJECT_ID 2>$null
if ($LASTEXITCODE -ne 0) {
  gcloud iam service-accounts create $SCHEDULER_SA_ID `
    --display-name="RAG daily sync scheduler" `
    --project=$PROJECT_ID
  Assert-LastExit
  # create 가 성공해도 IAM 은 이 SA 를 아직 모른다. 바로 바인딩하면
  # "Service account ... does not exist" 로 죽는다(실측). describe 가 통할
  # 때까지 기다린다.
  $saVisible = Wait-ForGcloudSuccess -Label "Scheduler SA" -Waits @(0, 5, 10, 15, 30) -Action {
    gcloud iam service-accounts describe $SCHEDULER_SA --project=$PROJECT_ID 2>$null
  }
  if (-not $saVisible) { throw "Scheduler SA 생성이 아직 반영되지 않았다 — 몇 분 뒤 다시 실행할 것" }
}
# describe 가 통해도 정책 쪽 반영은 조금 더 늦을 수 있다. 바인딩도 재시도한다.
$saBound = Wait-ForGcloudSuccess -Label "Scheduler SA IAM 바인딩" -Waits @(0, 5, 10, 20, 30) -Action {
  gcloud projects add-iam-policy-binding $PROJECT_ID `
    --member="serviceAccount:${SCHEDULER_SA}" `
    --role="roles/workflows.invoker" `
    --condition=None
}
if (-not $saBound) {
  throw "Scheduler SA 에 roles/workflows.invoker 를 붙이지 못했다 — 몇 분 뒤 다시 실행할 것"
}

# Cloud Scheduler 는 프로젝트에 App Engine 앱이 있어야 잡을 만든다.
# appengine.googleapis.com 이 enable 목록에 없으면 여기 create 가 실패하고,
# Cloud Run·Workflows 까지 다 올라간 뒤 마지막에 죽는다(실측).
gcloud app describe --project=$PROJECT_ID 2>$null
if ($LASTEXITCODE -ne 0) {
  gcloud app create --region=$REGION --project=$PROJECT_ID
  Assert-LastExit
}

Write-Host "== Cloud Scheduler (00:00 Asia/Seoul) =="
Write-Host "   drives: $($driveIds -join ', ')"
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

$corpusToken = (Get-GcloudText -GcloudArgs @("auth", "print-access-token")).Text.Split("`n")[0].Trim()
if ($corpusToken) {
  Write-Host ""
  Write-Host "== 코퍼스 적재량 =="
  $emptyDepts = @()
  foreach ($p in $DEPT_MAP.PSObject.Properties) {
    foreach ($pair in @(
        @{ Label = "staff"; Name = $p.Value.staffCorpus },
        @{ Label = "student"; Name = $p.Value.studentCorpus }
      )) {
      if (-not $pair.Name) { continue }
      $who = "$($p.Name)/$($pair.Label)"
      $usable = Test-RagCorpusUsable -Name $pair.Name -Token $corpusToken
      if (-not $usable.Ok) {
        Write-Host "WARN $who : 쓸 수 없다 — $($usable.Detail)"
        Write-Host "     이 상태로는 색인이 전부 실패한다."
        continue
      }
      $files = Get-RagCorpusFileCount -Name $pair.Name -Token $corpusToken
      if ($files -lt 0) {
        Write-Host "     $who : 적재량 확인 실패 — 백필 판단에서 뺀다"
      } else {
        Write-Host "     $who : $files 건"
        if ($files -eq 0 -and $pair.Label -eq "staff") { $emptyDepts += $p.Name }
      }
    }
  }
  if ($emptyDepts.Count -gt 0) {
    $emptyDrives = @()
    foreach ($code in $emptyDepts) {
      foreach ($d in $DEPT_MAP.$code.driveIds) {
        if ($emptyDrives -notcontains $d) { $emptyDrives += $d }
      }
    }
    $driveArg = $emptyDrives -join ","
    Write-Host ""
    Write-Host "비어 있다: $($emptyDepts -join ', ') — 첫 적재가 필요하다."
    Write-Host "   대상 드라이브: $driveArg"
    if (Confirm-PreflightAction "이 학과만 백필을 실행하시겠습니까? (수 분~수십 분 소요)") {
      & (Join-Path $PSScriptRoot "backfill.ps1") -DriveIds $driveArg
    } else {
      Write-Host "건너뜀. 나중에: scripts\backfill.ps1 -DriveIds $driveArg"
    }
  }
}
