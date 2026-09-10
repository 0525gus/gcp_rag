# Drive → GCS → RAG 색인을 수동으로 돌린다 (rag-daily-sync 워크플로 실행).
# 사용: .\scripts\backfill.ps1               전체 백필 (첫 적재)
#       .\scripts\backfill.ps1 -Delta        델타만 (스케줄러와 같은 동작)
#       .\scripts\backfill.ps1 -NoWait       실행만 걸고 빠진다
#
# 스케줄러는 00:00 KST 에 돈다. 그걸 기다리지 않을 때 쓴다.
#
# 코퍼스가 ACTIVE 가 아니면 시작하지 않는다 - 색인이 통째로 실패하는데
# 워크플로는 SUCCEEDED 로 끝나서 알아채기 어렵다(실측).

[CmdletBinding()]
param(
  # 델타 모드. 기본은 전체 백필.
  [switch]$Delta,
  # 실행을 걸고 완료를 기다리지 않는다.
  [switch]$NoWait,
  # 전 학과 드라이브 대신 쓸 값 (쉼표 구분). 한 학과만 다시 적재할 때.
  [string]$DriveIds = "",
  [int]$MaxChanges = 0,
  [int]$IndexBatchSize = 0
)

$ErrorActionPreference = "Stop"
if ($PSVersionTable.PSVersion.Major -ge 7) {
  $PSNativeCommandUseErrorActionPreference = $false
}

Set-Location (Split-Path -Parent $PSScriptRoot)
. (Join-Path $PSScriptRoot "_load_env.ps1")
. (Join-Path $PSScriptRoot "preflight.ps1")
# DRIVE_IDS 는 전 학과 union 이 된다 — 기본 동작이 "전 학과 백필" 이다.
Set-BaseDeployConfig | Out-Null
$DEPT_MAP = Get-DepartmentMap

$errs = [System.Collections.Generic.List[string]]::new()
Add-RequiredEnv $errs GCP_PROJECT_ID
Add-RequiredEnv $errs DRIVE_IDS "shared drive id"
Add-RequiredEnv $errs RAG_CORPUS_NAME "Vertex RAG corpus path"
Assert-EnvErrors $errs

if (-not (Get-Command gcloud -ErrorAction SilentlyContinue)) {
  throw "gcloud not on PATH - https://cloud.google.com/sdk/docs/install"
}

$PROJECT_ID = Get-EnvOr GCP_PROJECT_ID ""
$REGION = Get-EnvOr GCP_REGION "asia-northeast3"
$rawIds = if ($DriveIds) { $DriveIds } else { Get-EnvOr DRIVE_IDS "" }
$ids = @($rawIds -split "," | ForEach-Object { $_.Trim() } | Where-Object { $_ })
if ($ids.Count -eq 0) { throw "DRIVE_IDS 가 비어 있다" }

# 코퍼스가 못 쓰는 상태면 색인이 전부 실패한다. 워크플로는 그래도 SUCCEEDED 로
# 끝나므로 여기서 먼저 막는다.
$tokenR = Get-GcloudText -GcloudArgs @("auth", "print-access-token")
if (-not $tokenR.Ok) { throw "gcloud auth print-access-token 실패 - gcloud auth login" }
$token = $tokenR.Text.Split("`n")[0].Trim()

# 대상 드라이브가 속한 학과의 코퍼스만 본다 — 한 학과를 다시 적재하는데 남의
# 학과 코퍼스 상태로 막히면 안 된다.
$owners = @()
foreach ($p in $DEPT_MAP.PSObject.Properties) {
  if (@($p.Value.driveIds | Where-Object { $ids -contains $_ }).Count -gt 0) {
    $owners += $p
  }
}
# 맵에 없는 드라이브를 넘기면 sync 가 그 문서를 전부 건너뛴다(UnknownDriveError).
# 워크플로는 그래도 SUCCEEDED 로 끝나므로 여기서 먼저 막는다.
$known = @($owners | ForEach-Object { $_.Value.driveIds } )
$unknown = @($ids | Where-Object { $known -notcontains $_ })
if ($unknown.Count -gt 0) {
  throw "학과 맵에 없는 드라이브: $($unknown -join ', ') — config/departments 를 볼 것"
}

foreach ($p in $owners) {
  foreach ($pair in @(
      @{ Key = "$($p.Name)/staff"; Name = $p.Value.staffCorpus },
      @{ Key = "$($p.Name)/student"; Name = $p.Value.studentCorpus }
    )) {
    if (-not $pair.Name) { continue }
    $u = Test-RagCorpusUsable -Name $pair.Name -Token $token
    if (-not $u.Ok) {
      throw "$($pair.Key) 을 쓸 수 없다: $($u.Detail)"
    }
    Write-Host "ok   $($pair.Key) state=$($u.State)"
  }
}

$SYNC_URL = (Get-GcloudText -GcloudArgs @(
    "run", "services", "describe", "rag-sync", "--region=$REGION", "--project=$PROJECT_ID",
    "--format=value(status.url)")).Text.Trim()
$PARSER_URL = (Get-GcloudText -GcloudArgs @(
    "run", "services", "describe", "rag-parser", "--region=$REGION", "--project=$PROJECT_ID",
    "--format=value(status.url)")).Text.Trim()
if (-not $SYNC_URL -or -not $PARSER_URL) {
  throw "rag-sync / rag-parser URL 을 못 얻었다 - 배포부터 할 것"
}

$payload = [ordered]@{
  syncUrl   = $SYNC_URL
  parserUrl = $PARSER_URL
  driveIds  = @($ids)
  backfill  = (-not $Delta)
}
if ($MaxChanges -gt 0) { $payload["maxChanges"] = $MaxChanges }
if ($IndexBatchSize -gt 0) { $payload["indexBatchSize"] = $IndexBatchSize }
$data = ($payload | ConvertTo-Json -Compress)

Write-Host ""
Write-Host "== rag-daily-sync ($(if ($Delta) { 'delta' } else { 'backfill' })) =="
Write-Host "drives : $($ids -join ', ')"
Write-Host "sync   : $SYNC_URL"

$runArgs = @(
  "workflows", "run", "rag-daily-sync",
  "--location=$REGION", "--project=$PROJECT_ID", "--data=$data"
)
if ($NoWait) {
  # run 은 완료까지 기다린다. 기다리지 않으려면 executions create 를 쓴다.
  $runArgs = @(
    "workflows", "executions", "create", "rag-daily-sync",
    "--location=$REGION", "--project=$PROJECT_ID", "--data=$data"
  )
}

$r = Get-GcloudText -GcloudArgs $runArgs
Write-Host $r.Text
if (-not $r.Ok) { throw "워크플로 실행 실패" }

Write-Host ""
Write-Host "확인:"
Write-Host "  gcloud workflows executions list --workflow=rag-daily-sync --location=$REGION --project=$PROJECT_ID --limit=3"
Write-Host "  gcloud logging read 'resource.labels.service_name=\"rag-sync\"' --project=$PROJECT_ID --limit=50"
