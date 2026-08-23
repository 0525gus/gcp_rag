# 공유드라이브에 Cloud Run SA 를 멤버로 초대한다. 이미 멤버면 건너뛴다(멱등).
# 사용: .\scripts\share_drive.ps1
#       .\scripts\share_drive.ps1 -DryRun          누구를 어디에 넣을지만 출력
#       .\scripts\share_drive.ps1 -Role writer     기본은 reader
#
# preflight.ps1 도 대화형이면 물어보고 초대한다. 이 스크립트는 비대화형·일괄
# 처리용이고, 역할(-Role)을 바꾸거나 여러 드라이브를 한 번에 돌릴 때 쓴다.
# 초대 함수(Add-DriveMember)는 preflight.ps1 것을 재사용한다.
#
# 토큰에 Drive 스코프가 있어야 한다. 평소 쓰는 gcloud auth print-access-token 은
# cloud-platform 스코프뿐이라 403 이 난다. 먼저 한 번:
#   gcloud auth application-default login `
#     --scopes=https://www.googleapis.com/auth/cloud-platform,https://www.googleapis.com/auth/drive
# 그리고 호출하는 본인이 그 공유드라이브의 관리자여야 한다.

[CmdletBinding()]
param(
  [ValidateSet("reader", "commenter", "writer", "fileOrganizer", "organizer")]
  [string]$Role = "reader",
  [switch]$DryRun
)

$ErrorActionPreference = "Stop"
if ($PSVersionTable.PSVersion.Major -ge 7) {
  $PSNativeCommandUseErrorActionPreference = $false
}

Set-Location (Split-Path -Parent $PSScriptRoot)
. (Join-Path $PSScriptRoot "_load_env.ps1")
. (Join-Path $PSScriptRoot "preflight.ps1")
# DRIVE_IDS 는 전 학과 union 이 된다 — SA 는 모든 공유드라이브에 들어가야 한다.
Set-BaseDeployConfig | Out-Null

# Add-DriveMember 는 preflight.ps1 이 제공한다 (초대 로직 단일화).

$errs = [System.Collections.Generic.List[string]]::new()
Add-RequiredEnv $errs GCP_PROJECT_ID
Add-RequiredEnv $errs DRIVE_IDS "shared drive id"
Assert-EnvErrors $errs

if (-not (Get-Command gcloud -ErrorAction SilentlyContinue)) {
  throw "gcloud not on PATH — https://cloud.google.com/sdk/docs/install"
}

$PROJECT_ID = Get-EnvOr GCP_PROJECT_ID ""
$driveIds = @((Get-EnvOr DRIVE_IDS "") -split "," | ForEach-Object { $_.Trim() } | Where-Object { $_ })

# Cloud Run 기본 컴퓨팅 SA. preflight 와 같은 방식으로 프로젝트 번호에서 만든다.
$numR = Get-GcloudText -GcloudArgs @("projects", "describe", $PROJECT_ID, "--format=value(projectNumber)")
if (-not $numR.Ok -or -not $numR.Text) {
  throw "프로젝트 번호 조회 실패 — Cloud Run SA 를 특정할 수 없다: $($numR.Text)"
}
$SA = "$($numR.Text.Trim())-compute@developer.gserviceaccount.com"

# ADC 쪽이 Drive 스코프를 갖고 있을 가능성이 높다. 없으면 일반 토큰으로 시도하고
# 403 이면 아래에서 재로그인 방법을 안내한다.
$adc = Get-GcloudText -GcloudArgs @("auth", "application-default", "print-access-token")
if ($adc.Ok -and $adc.Text) {
  $TOKEN = $adc.Text.Trim()
  $tokenSource = "application-default"
} else {
  $plain = Get-GcloudText -GcloudArgs @("auth", "print-access-token")
  if (-not $plain.Ok) { throw "액세스 토큰을 못 얻었다 — gcloud auth login" }
  $TOKEN = $plain.Text.Trim()
  $tokenSource = "user (Drive 스코프 없을 수 있음)"
}

Write-Host "== share drive =="
Write-Host "project : $PROJECT_ID"
Write-Host "SA      : $SA"
Write-Host "role    : $Role"
Write-Host "token   : $tokenSource"
if ($DryRun) { Write-Host "(-DryRun: 초대하지 않는다)" }

$scopeHint = @(
  "  Drive 스코프가 없는 토큰이다. 한 번만 다시 로그인하면 된다:",
  "    gcloud auth application-default login --scopes=https://www.googleapis.com/auth/cloud-platform,https://www.googleapis.com/auth/drive",
  "  그래도 안 되면 본인이 해당 공유드라이브의 관리자인지 확인할 것.",
  "  마지막 수단: Drive 웹에서 '멤버 관리' 로 $SA 를 초대"
)

$failed = 0
foreach ($did in $driveIds) {
  $perm = Get-DrivePermissions -DriveId $did -Token $TOKEN
  if ($perm.Ok) {
    $already = $false
    foreach ($em in $perm.Emails) {
      if ($em.Equals($SA, [StringComparison]::OrdinalIgnoreCase)) { $already = $true; break }
    }
    if ($already) {
      Write-Host "skip $did  (이미 멤버)"
      continue
    }
  } else {
    # 멤버 목록을 못 읽어도 초대는 될 수 있다(권한 범위가 다름). 시도는 해 본다.
    Write-Host "warn $did  멤버 목록을 못 읽었다 (HTTP $($perm.Code)) — 초대는 시도한다"
  }

  if ($DryRun) {
    Write-Host "plan $did  <- $SA ($Role)"
    continue
  }

  $res = Add-DriveMember -DriveId $did -Email $SA -Role $Role -Token $TOKEN
  if ($res.Ok) {
    Write-Host "made $did  <- $SA ($Role)"
  } else {
    $failed++
    Write-Host "FAIL $did  HTTP $($res.Code)"
    Write-Host "  $($res.Error)"
    if ($res.Code -eq 401 -or $res.Code -eq 403) { $scopeHint | ForEach-Object { Write-Host $_ } }
  }
}

Write-Host ""
if ($failed -gt 0) {
  throw "$failed 개 드라이브 초대 실패"
}
Write-Host "다음: .\scripts\preflight.ps1"
