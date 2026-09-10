# 알림 일괄 설정 (멱등) - 예산 1건 + 운영 정책 3건
# 사용:
#   $env:ALERT_EMAIL = "ops@example.com"
#   .\scripts\setup_alerts.ps1
#   $env:BUDGET_AMOUNT = "200USD"; .\scripts\setup_alerts.ps1
#
# 예산 알림과 운영 알림은 서로를 못 잡는다.
#   - 예산은 '돈'을 본다. 스케줄러가 멈추면 비용은 줄어들어 조용하다
#   - 운영은 '동작'을 본다. 백필이 정상인데 비용만 폭증하면 조용하다
# 전제: gcloud 인증. 예산은 결제 계정 권한이 따로 필요 (없으면 예산만 건너뜀)

$ErrorActionPreference = "Stop"
if ($PSVersionTable.PSVersion.Major -ge 7) {
  $PSNativeCommandUseErrorActionPreference = $false
}

Set-Location (Split-Path -Parent $PSScriptRoot)
. (Join-Path $PSScriptRoot "_load_env.ps1")
# GCP_PROJECT_ID 만 쓴다 — 학과 무관이라 어느 학과로 깔든 같은 값이다(common.yaml).
Set-BaseDeployConfig | Out-Null

function Assert-LastExit {
  if ($LASTEXITCODE -ne 0) { throw "gcloud exit $LASTEXITCODE" }
}

function Get-GcloudFirstValue {
  param([string[]]$Args)
  $out = & gcloud @Args 2>$null
  if ($LASTEXITCODE -ne 0) { return "" }
  $line = @($out) | Where-Object { $_ -and $_.Trim() } | Select-Object -First 1
  if ($null -eq $line) { return "" }
  return [string]$line.Trim()
}

$PROJECT_ID = $env:GCP_PROJECT_ID
if ([string]::IsNullOrWhiteSpace($PROJECT_ID)) {
  throw "set GCP_PROJECT_ID"
}
$ALERT_EMAIL = $env:ALERT_EMAIL
if ([string]::IsNullOrWhiteSpace($ALERT_EMAIL)) {
  throw "set ALERT_EMAIL (알림 받을 주소)"
}
$BUDGET_AMOUNT = Get-EnvOr BUDGET_AMOUNT "100USD"
$WORKFLOW_NAME = Get-EnvOr WORKFLOW_NAME "rag-daily-sync"

gcloud config set project $PROJECT_ID
Assert-LastExit

Write-Host "== API 활성화 =="
gcloud services enable monitoring.googleapis.com billingbudgets.googleapis.com --project=$PROJECT_ID
Assert-LastExit

Write-Host "== 알림 채널 =="
$CHANNEL = Get-GcloudFirstValue @(
  "beta", "monitoring", "channels", "list",
  "--project=$PROJECT_ID",
  "--filter=type='email' AND labels.email_address='$ALERT_EMAIL'",
  "--format=value(name)"
)
if (-not $CHANNEL) {
  $CHANNEL = Get-GcloudFirstValue @(
    "beta", "monitoring", "channels", "create",
    "--project=$PROJECT_ID",
    "--display-name=rag ops ($ALERT_EMAIL)",
    "--type=email",
    "--channel-labels=email_address=$ALERT_EMAIL",
    "--format=value(name)"
  )
  if (-not $CHANNEL) { throw "notification channel create failed" }
  Write-Host "  생성: $CHANNEL"
} else {
  Write-Host "  기존 사용: $CHANNEL"
}

$policyDir = Join-Path ([System.IO.Path]::GetTempPath()) ("rag-alerts-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $policyDir | Out-Null
$utf8 = New-Object System.Text.UTF8Encoding $false
try {
  # 부재 조건은 성공 실행이 한 번이라도 있어야 동작한다. 첫 동기화 뒤에 의미가 있다.
  $noSuccess = @"
{
  "displayName": "rag: 일일 동기화 24시간 무성공",
  "combiner": "OR",
  "conditions": [
    {
      "displayName": "workflow 성공 실행 부재 24h",
      "conditionAbsent": {
        "filter": "resource.type=\`"workflows.googleapis.com/Workflow\`" AND resource.label.\`"workflow_id\`"=\`"$WORKFLOW_NAME\`" AND metric.type=\`"workflows.googleapis.com/finished_execution_count\`" AND metric.label.\`"status\`"=\`"SUCCEEDED\`"",
        "duration": "86400s",
        "aggregations": [
          {"alignmentPeriod": "3600s", "perSeriesAligner": "ALIGN_SUM"}
        ]
      }
    }
  ],
  "notificationChannels": ["$CHANNEL"],
  "alertStrategy": {"autoClose": "604800s"}
}
"@

  $workflowError = @"
{
  "displayName": "rag: 워크플로 실행 실패",
  "combiner": "OR",
  "conditions": [
    {
      "displayName": "workflow ERROR 로그",
      "conditionMatchedLog": {
        "filter": "resource.type=\`"workflows.googleapis.com/Workflow\`" AND severity>=ERROR"
      }
    }
  ],
  "notificationChannels": ["$CHANNEL"],
  "alertStrategy": {
    "notificationRateLimit": {"period": "3600s"},
    "autoClose": "604800s"
  }
}
"@

  $syncStuck = @"
{
  "displayName": "rag: 동기화 정체 / 학생 코퍼스 정리 실패",
  "combiner": "OR",
  "conditions": [
    {
      "displayName": "sync 정체 신호",
      "conditionMatchedLog": {
        "filter": "resource.type=\`"cloud_run_revision\`" AND resource.labels.service_name=\`"rag-sync\`" AND (textPayload:\`"pageToken NOT committed\`" OR textPayload:\`"학생 코퍼스\`" OR textPayload:\`"cleanup failed\`")"
      }
    }
  ],
  "notificationChannels": ["$CHANNEL"],
  "alertStrategy": {
    "notificationRateLimit": {"period": "3600s"},
    "autoClose": "604800s"
  }
}
"@

  $files = @{
    "no-success.json" = $noSuccess
    "workflow-error.json" = $workflowError
    "sync-stuck.json" = $syncStuck
  }
  foreach ($kv in $files.GetEnumerator()) {
    [System.IO.File]::WriteAllText((Join-Path $policyDir $kv.Key), $kv.Value.Trim(), $utf8)
  }

  Write-Host "== 알림 정책 =="
  Get-ChildItem -LiteralPath $policyDir -Filter *.json | ForEach-Object {
    $policy = Get-Content -LiteralPath $_.FullName -Encoding utf8 -Raw | ConvertFrom-Json
    $name = $policy.displayName
    $existing = Get-GcloudFirstValue @(
      "alpha", "monitoring", "policies", "list",
      "--project=$PROJECT_ID",
      "--filter=displayName='$name'",
      "--format=value(name)"
    )
    if ($existing) {
      Write-Host "  건너뜀(이미 있음): $name"
      return
    }
    gcloud alpha monitoring policies create --project=$PROJECT_ID --policy-from-file="$($_.FullName)" | Out-Null
    Assert-LastExit
    Write-Host "  생성: $name"
  }
} finally {
  Remove-Item -LiteralPath $policyDir -Recurse -Force -ErrorAction SilentlyContinue
}

Write-Host "== 예산 =="
$billing = Get-EnvOr BILLING_ACCOUNT ""
if (-not $billing) {
  $raw = Get-GcloudFirstValue @(
    "billing", "projects", "describe", $PROJECT_ID,
    "--format=value(billingAccountName)"
  )
  $billing = $raw -replace "^billingAccounts/", ""
}
if (-not $billing) {
  Write-Host "  건너뜀: 결제 계정을 못 읽었다 (권한 또는 미연결). BILLING_ACCOUNT=... 로 지정 가능"
} else {
  $BUDGET_NAME = "rag $PROJECT_ID 월 예산"
  $existingBudget = Get-GcloudFirstValue @(
    "billing", "budgets", "list",
    "--billing-account=$billing",
    "--filter=displayName='$BUDGET_NAME'",
    "--format=value(name)"
  )
  if ($existingBudget) {
    Write-Host "  건너뜀(이미 있음): $BUDGET_NAME"
  } else {
    gcloud billing budgets create `
      --billing-account=$billing `
      --display-name="$BUDGET_NAME" `
      --budget-amount=$BUDGET_AMOUNT `
      --threshold-rule=percent=0.5 `
      --threshold-rule=percent=0.9 `
      --threshold-rule=percent=1.0 `
      --filter-projects="projects/${PROJECT_ID}"
    Assert-LastExit
    Write-Host "  생성: $BUDGET_NAME ($BUDGET_AMOUNT)"
  }
}

Write-Host ""
Write-Host "Done."
Write-Host "  채널 : $ALERT_EMAIL"
Write-Host "  정책 : 24시간 무성공 / 워크플로 실패 / 동기화 정체"
Write-Host "  예산 : $BUDGET_AMOUNT (50/90/100%)"
Write-Host ""
Write-Host "확인: gcloud alpha monitoring policies list --project=$PROJECT_ID --format=`"table(displayName,enabled)`""
