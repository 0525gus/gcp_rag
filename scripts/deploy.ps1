# Cloud Run / Workflows / Scheduler 배포
# 사용: .\scripts\deploy.ps1
#       .\scripts\deploy.ps1 -SkipMcp       parser/sync/Workflows/Scheduler 만
#       .\scripts\deploy.ps1 -ShowKeys      MCP 요약표에 키 노출
#
# 올리는 것: rag-parser 1개, rag-sync 1개, 전 학과 MCP 2N개(deploy_mcp.ps1 위임),
#            Workflows 1벌, Scheduler 1벌, 그 SA·IAM.
#
# **-Dept 인자는 없다.** config/departments 의 학과 목록이 곧 배포 대상이다.
# 설정 원본도 config/ 하나뿐이다 (.env 는 없앴다 — docs/ENV_MIGRATION.md).
#
# --set-env-vars 는 Cloud Run env 를 통째로 치환한다. 안 넘긴 값은 사라진다.
# DRIVE_IDS / SYNC_FOLDER_IDS 에 콤마가 있어 구분자는 | (^|^...).
#
# ---- 1. config 로드 · 필수값 검증 (Set-BaseDeployConfig, Require-FullDeployEnv) ----
# ---- 2. gcloud 프로젝트 설정 · API enable ----
# ---- 3. Artifact Registry 확인/생성 ----
# ---- 4. 이미지 빌드 · 푸시 (parser → sync → mcp) ----
# ---- 5. Cloud Run 배포 (rag-parser → rag-sync) ----
# ---- 6. MCP 전 학과 배포 (deploy_mcp.ps1 -All -SkipBuild) ----
# ---- 7. 서비스 URL 조회 ----
# ---- 8. Workflow 배포 (rag-daily-sync) ----
# ---- 9. Scheduler SA · App Engine 준비 ----
# ---- 10. Cloud Scheduler job 등록/갱신 (00:00 Asia/Seoul) ----
# ---- 11. 학과별 코퍼스 적재량 확인 · 비어있으면 첫 백필 여부 확인 ----

[CmdletBinding()]
param(
  # MCP 를 건너뛴다. parser/sync 만 고쳤을 때 학과 수만큼의 배포를 아낀다.
  [switch]$SkipMcp,
  # MCP 요약표에 키를 평문으로 찍는다 (deploy_mcp.ps1 로 그대로 넘긴다).
  [switch]$ShowKeys
)

# ---- 1. config 로드 · 필수값 검증 ----
$ErrorActionPreference = "Stop"
# describe 실패를 throw 로 올리지 않는다 (없으면 create).
if ($PSVersionTable.PSVersion.Major -ge 7) {
  $PSNativeCommandUseErrorActionPreference = $false
}

Set-Location (Split-Path -Parent $PSScriptRoot)
. (Join-Path $PSScriptRoot "_load_env.ps1")
. (Join-Path $PSScriptRoot "preflight.ps1")

# 첫 학과 값 + 전 학과 드라이브 union. 근거는 _load_env.ps1 의 주석.
# @() 필수 — 학과가 하나면 결과가 문자열이라 [0] 이 첫 글자가 된다.
$DEPT_CODES = @(Set-BaseDeployConfig)
$BASE_DEPT = $DEPT_CODES[0]
Require-FullDeployEnv

# rag-sync 학과 맵. **이 값이 sync 라우팅의 스위치다** — 비면 전 학과가 아래
# 기본 코퍼스 하나로 들어간다. 시크릿은 안 들어간다(sync 는 MCP 키를 안 쓴다).
$DEPARTMENTS_JSON = Get-DepartmentsJson
$DEPT_MAP = $DEPARTMENTS_JSON | ConvertFrom-Json

function Assert-LastExit {
  if ($LASTEXITCODE -ne 0) { throw "gcloud exit $LASTEXITCODE" }
}

$PROJECT_ID = $env:GCP_PROJECT_ID
$REGION = Get-EnvOr GCP_REGION "asia-northeast3"
$REPO = Get-EnvOr ARTIFACT_REPO "rag-mcp"
$FS_DB = Get-EnvOr FIRESTORE_DATABASE "rag-sync-state"
$FS_COL = Get-EnvOr DOC_STATE_COLLECTION "doc_state"
$QG_MODE = Get-EnvOr QG_MODE "log"
$PARSER_TIMEOUT = Get-EnvOr PARSER_TIMEOUT "540"
$PARSER_CONCURRENCY = Get-EnvOr PARSER_CONCURRENCY "4"
$PARSER_MAX_INSTANCES = Get-EnvOr PARSER_MAX_INSTANCES "10"
$SYNC_CONCURRENCY = Get-EnvOr SYNC_CONCURRENCY "4"
# MCP 공개 여부. deploy_mcp.ps1 이 같은 스위치를 본다 — 아래 안내문에만 쓴다.
# 기본 true: FactChat 커넥터가 정적 헤더만 보내므로 Cloud Run IAM 을 열어야 한다.
# 경계는 앱 계층 키(MCP_API_KEY)뿐이다 — 키가 새면 코퍼스 전량이 열린다.
$ALLOW_UNAUTH = Get-EnvOr ALLOW_UNAUTH "true"
$INGEST_CONC = Get-EnvOr INGEST_CONCURRENCY "8"
$RAG_DEL_PACE = Get-EnvOr RAG_DELETE_PACING_SECONDS "1.1"
$RAG_DEL_CONC = Get-EnvOr RAG_DELETE_CONCURRENCY "1"
$DOCAI = Get-EnvOr DOCAI_PROCESSOR_ID ""
$SYNC_FOLDERS = Get-EnvOr SYNC_FOLDER_IDS ""
$STUDENT_CORPUS = Get-EnvOr RAG_CORPUS_NAME_STUDENT ""
$STUDENT_FOLDERS = Get-EnvOr STUDENT_FOLDER_IDS ""
$GCS_HWP_ORIG = $env:GCS_HWP_ORIGINAL_BUCKET
$GCS_SOURCE = $env:GCS_SOURCE_BUCKET
$CORPUS = $env:RAG_CORPUS_NAME
$DRIVE_IDS = $env:DRIVE_IDS

Write-Host "== 학과 $($DEPT_CODES.Count) 개: $($DEPT_CODES -join ', ') =="
Write-Host "   parser/sync 기본값은 '$BASE_DEPT' · 드라이브는 전 학과 union"

# ---- 2. gcloud 프로젝트 설정 · API enable ----
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
if (-not $SkipMcp) {
  # 여기서 한 번만 빌드하고 deploy_mcp.ps1 -SkipBuild 가 그 digest 를 전 학과에
  # 쓴다. 학과마다 빌드하면 학과별로 다른 코드가 도는 것을 아무도 모른다.
  gcloud builds submit --config=cloudbuild.mcp.yaml --substitutions="_IMAGE=$IMAGE_BASE/mcp:latest"
  Assert-LastExit
}

# ---- 5. Cloud Run 배포 (rag-parser → rag-sync) ----
Write-Host "== Deploy Cloud Run =="
# parser timeout 540 < sync httpx 600. 서버가 먼저 포기해야 sync 가 오류를 받는다.
# concurrency 4: 요청당 메모리 한계. 넘치는 요청은 새 인스턴스로.
# min-instances=0 은 기본값과 같지만 명시한다 — 콜드스타트를 감수하겠다는 의도
# 표시(docs/DEV_SPEC.md 운영 체크리스트). MCP 응답 지연이 문제되면 학과 yaml 의
# minInstances 를 1로, sync/parser 는 배치라 0 유지.
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
# DEPARTMENTS_JSON 이 학과 라우팅 스위치다: 이 값이 비거나 깨지면 sync 는 폴백해서
# 전 학과 문서를 아래 RAG_CORPUS_NAME 하나에 몰아넣는다(단일 학과 동작).
# 값에 공백이 없어야 명령줄에서 안전하다 — dept_config.py 가 그렇게 낸다.
$syncEnv = "^|^GCP_PROJECT_ID=$PROJECT_ID|GCP_REGION=$REGION|GCS_HWP_ORIGINAL_BUCKET=$GCS_HWP_ORIG|GCS_SOURCE_BUCKET=$GCS_SOURCE|RAG_CORPUS_NAME=$CORPUS|DRIVE_IDS=$DRIVE_IDS|SYNC_FOLDER_IDS=$SYNC_FOLDERS|DOC_STATE_COLLECTION=$FS_COL|FIRESTORE_DATABASE=$FS_DB|QG_MODE=$QG_MODE|INGEST_CONCURRENCY=$INGEST_CONC|RAG_DELETE_PACING_SECONDS=$RAG_DEL_PACE|RAG_DELETE_CONCURRENCY=$RAG_DEL_CONC|RAG_CORPUS_NAME_STUDENT=$STUDENT_CORPUS|STUDENT_FOLDER_IDS=$STUDENT_FOLDERS|DEPARTMENTS_JSON=$DEPARTMENTS_JSON"
gcloud run deploy rag-sync `
  --image="$IMAGE_BASE/sync:latest" `
  --region=$REGION `
  --no-allow-unauthenticated `
  --set-env-vars=$syncEnv `
  --memory=2Gi --cpu=2 --timeout=3600 `
  --concurrency=$SYNC_CONCURRENCY `
  --min-instances=0
Assert-LastExit

# ---- 6. MCP 전 학과 배포 ----
# 여기서 루프를 복사하지 않고 deploy_mcp.ps1 에 위임한다. 키 중복 사전 검사,
# digest 고정, 요약표가 거기 한 벌만 있어야 두 경로가 갈라지지 않는다.
if (-not $SkipMcp) {
  Write-Host ""
  Write-Host "== MCP 전 학과 배포 (deploy_mcp.ps1 -All) =="
  # **해시테이블 splat 이어야 한다.** 배열 splat 은 요소를 이름이 아니라
  # 위치 인자로 넘긴다 — @("-All","-SkipBuild") 는 $Dept="-All",
  # $Audience="-SkipBuild" 로 박혀 ValidateSet 에서 죽었다(실측, 실배포 중단).
  # gcloud 쪽 @authArgs 는 배열이 맞다 — 네이티브 명령은 위치 인자를 받는다.
  $mcpArgs = @{ All = $true; SkipBuild = $true }
  if ($ShowKeys) { $mcpArgs["ShowKeys"] = $true }
  # 자식이 실패하면 throw 가 그대로 올라온다($ErrorActionPreference=Stop, 그리고
  # deploy_mcp.ps1 은 gcloud 마다 Assert-LastExit 를 건다). $LASTEXITCODE 는
  # 자식이 마지막에 부른 gcloud 것이라 여기서 성패 판정에 쓸 수 없다.
  & (Join-Path $PSScriptRoot "deploy_mcp.ps1") @mcpArgs
  # 자식 스크립트가 학과 env 를 마지막 학과 값으로 남긴다. 아래 Scheduler·코퍼스
  # 확인이 그걸 물려받지 않도록 기준 학과로 되돌린다.
  Set-BaseDeployConfig | Out-Null
}

# ---- 7. 서비스 URL 조회 ----
$PARSER_URL = gcloud run services describe rag-parser --region=$REGION --format="value(status.url)"
Assert-LastExit
$SYNC_URL = gcloud run services describe rag-sync --region=$REGION --format="value(status.url)"
Assert-LastExit

Write-Host "PARSER_URL=$PARSER_URL"
Write-Host "SYNC_URL=$SYNC_URL"
Write-Host ""
if ($SkipMcp) {
  Write-Host "MCP 는 건너뛰었다 (-SkipMcp). 올리려면: .\scripts\deploy_mcp.ps1 -All"
} elseif ($ALLOW_UNAUTH -eq "true") {
  Write-Host "MCP 는 공개(--allow-unauthenticated). 경계는 API 키뿐이다."
  Write-Host "FactChat 커넥터: {URL}/mcp · Streamable HTTP · Authorization: Bearer {키}"
} else {
  Write-Host "MCP 는 IAM 전용(ALLOW_UNAUTH=$ALLOW_UNAUTH). FactChat 은 붙지 못한다."
}

# Scheduler job 본문. **학과를 늘리고 Cloud Run 만 재배포하면 워크플로가 새
# 드라이브를 영영 못 본다** — driveIds 는 Cloud Run env 가 아니라 이 본문으로
# 간다(Workflows args → for_each_drive). 그래서 여기는 전 학과 union 이다.
$driveIds = @($DRIVE_IDS -split "," | ForEach-Object { $_.Trim() } | Where-Object { $_ })
$driveJson = "[" + (($driveIds | ForEach-Object { '"' + $_ + '"' }) -join ",") + "]"
$innerJson = '{"syncUrl":"' + $SYNC_URL.Trim() + '","parserUrl":"' + $PARSER_URL.Trim() + '","driveIds":' + $driveJson + "}"
$escaped = $innerJson.Replace("\", "\\").Replace('"', '\"')
$bodyJson = '{"argument":"' + $escaped + '"}'

# ---- 8. Workflow 배포 (rag-daily-sync) ----
Write-Host "== Deploy Workflow =="
gcloud workflows deploy rag-daily-sync --location=$REGION --source=workflows/daily_sync.yaml
Assert-LastExit

# ---- 9. Scheduler SA · App Engine 준비 ----
$SCHEDULER_SA = "scheduler@${PROJECT_ID}.iam.gserviceaccount.com"
Write-Host "== Ensure Scheduler SA / App Engine =="
# 프로젝트에서 파생한다 — 저장하지 않는다. 예전에는 이 값을 설정 파일에 뒀는데
# 다른 프로젝트를 가리키면 조용히 어긋났다: describe 는 "없다" 하고 create 는
# ALREADY_EXISTS 로 터진다(실측 — .env 에 예시값이 남아 있던 경우).
$SCHEDULER_SA_ID = ($SCHEDULER_SA -split "@")[0]
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

# ---- 10. Cloud Scheduler job 등록/갱신 (00:00 Asia/Seoul) ----
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

# ---- 11. 학과별 코퍼스 적재량 확인 · 비어있으면 첫 백필 여부 확인 ----
# 배포가 끝났으니 코퍼스가 실제로 찼는지 본다. 학과마다 코퍼스가 다르므로 전부
# 훑는다 — 한 학과만 보면 새로 붙인 학과가 빈 채로 남아도 조용하다.
# ACTIVE 가 아니면 색인이 통째로 실패하는데 워크플로는 그래도 SUCCEEDED 로 끝난다.
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
      # -1 = 조회 실패. 0 과 섞으면 멀쩡한 코퍼스에 전체 백필을 다시 건다.
      if ($files -lt 0) {
        Write-Host "     $who : 적재량 확인 실패 — 백필 판단에서 뺀다"
      } else {
        Write-Host "     $who : $files 건"
        if ($files -eq 0 -and $pair.Label -eq "staff") { $emptyDepts += $p.Name }
      }
    }
  }
  if ($emptyDepts.Count -gt 0) {
    # **빈 학과의 드라이브만** 넘긴다. 인자 없이 부르면 backfill.ps1 이 전 학과
    # union 을 대상으로 잡아 **이미 찬 학과까지 전량 재적재**한다 — 학과를 하나
    # 붙일 때마다 기존 학과 전체를 다시 넣는 비용이 붙는다.
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
