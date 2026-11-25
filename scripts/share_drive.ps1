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
Set-BaseDeployConfig | Out-Null

$errs = [System.Collections.Generic.List[string]]::new()
Add-RequiredEnv $errs GCP_PROJECT_ID
Add-RequiredEnv $errs DRIVE_IDS "shared drive id"
Assert-EnvErrors $errs

if (-not (Get-Command gcloud -ErrorAction SilentlyContinue)) {
  throw "gcloud not on PATH - https://cloud.google.com/sdk/docs/install"
}

$PROJECT_ID = Get-EnvOr GCP_PROJECT_ID ""
$driveIds = @((Get-EnvOr DRIVE_IDS "") -split "," | ForEach-Object { $_.Trim() } | Where-Object { $_ })

$numR = Get-GcloudText -GcloudArgs @("projects", "describe", $PROJECT_ID, "--format=value(projectNumber)")
if (-not $numR.Ok -or -not $numR.Text) {
  throw "프로젝트 번호 조회 실패 - Cloud Run SA 를 특정할 수 없다: $($numR.Text)"
}
$SA = "$($numR.Text.Trim())-compute@developer.gserviceaccount.com"

$adc = Get-GcloudText -GcloudArgs @("auth", "application-default", "print-access-token")
if ($adc.Ok -and $adc.Text) {
  $TOKEN = $adc.Text.Trim()
  $tokenSource = "application-default"
} else {
  $plain = Get-GcloudText -GcloudArgs @("auth", "print-access-token")
  if (-not $plain.Ok) { throw "액세스 토큰을 못 얻었다 - gcloud auth login" }
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
    Write-Host "warn $did  멤버 목록을 못 읽었다 (HTTP $($perm.Code)) - 초대는 시도한다"
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
