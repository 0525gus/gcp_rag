# GUI 배포 화면은 이 스크립트들의 stdout을 UTF-8로 읽는다. Windows의 기본
# 코드페이지(CP949)가 섞이면 한글 진행/실패 로그가 ���로 깨지므로 PowerShell과
# 하위 gcloud/Python 프로세스의 텍스트 출력을 한 인코딩으로 고정한다.
$Utf8NoBom = [System.Text.UTF8Encoding]::new($false)
[Console]::InputEncoding = $Utf8NoBom
[Console]::OutputEncoding = $Utf8NoBom
$OutputEncoding = $Utf8NoBom
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

function Get-PythonExe {
  $venv = Join-Path (Split-Path -Parent $PSScriptRoot) ".venv\Scripts\python.exe"
  if (Test-Path -LiteralPath $venv) { return $venv }
  return "python"
}

function Invoke-DeptConfig {
  # stdout 은 KEY=VALUE 전용이고 경고는 stderr 로 나온다(dept_config.py).
  # 여기서 2>&1 로 합치면 경고가 값으로 파싱된다 — 합치지 말 것.
  $py = Get-PythonExe
  $out = & $py (Join-Path $PSScriptRoot "dept_config.py") @args
  if ($LASTEXITCODE -ne 0) { throw "dept_config.py $($args -join ' ') 실패" }
  return $out
}

function Set-DeptConfig {
  param([string]$DeptCode, [string]$AudienceName)

  foreach ($line in (Invoke-DeptConfig --dept $DeptCode --audience $AudienceName)) {
    if (-not $line -or $line -notmatch "=") { continue }
    $k, $v = $line.Split("=", 2)
    Set-Item -LiteralPath "Env:$k" -Value $v
  }

  if ([string]::IsNullOrWhiteSpace($env:MCP_API_KEY)) {
    throw "$DeptCode/$AudienceName : keys 를 못 읽었다"
  }
}

function Get-DepartmentCodes {
  $codes = @(Invoke-DeptConfig --list | ForEach-Object { $_.Trim() } | Where-Object { $_ })
  if (-not $codes) { throw "Cloud 등록부에 학과 설정이 없다" }
  return $codes
}


function Get-DepartmentsJson {
  $json = (Invoke-DeptConfig --departments-json) -join ""
  if ([string]::IsNullOrWhiteSpace($json)) { throw "DEPARTMENTS_JSON 생성 실패" }
  return $json.Trim()
}

function Get-DepartmentMap {
  return (Get-DepartmentsJson | ConvertFrom-Json)
}

function Get-AllDriveIds {
  param($Map = $null)
  if ($null -eq $Map) { $Map = Get-DepartmentMap }
  $ids = [System.Collections.Generic.List[string]]::new()
  foreach ($p in $Map.PSObject.Properties) {
    foreach ($d in $p.Value.driveIds) {
      if (-not $ids.Contains($d)) { $ids.Add($d) }
    }
  }
  return $ids
}

function Set-BaseDeployConfig {
  $codes = @(Get-DepartmentCodes)
  Set-DeptConfig -DeptCode $codes[0] -AudienceName "staff"
  $env:DRIVE_IDS = (Get-AllDriveIds) -join ","
  return $codes
}

function Get-EnvOr {
  param([string]$Name, [string]$Default = "")
  $v = [Environment]::GetEnvironmentVariable($Name)
  if ([string]::IsNullOrWhiteSpace($v)) { return $Default }
  return $v
}





function Test-PlaceholderValue {
  param([string]$Value)
  if ([string]::IsNullOrWhiteSpace($Value)) { return $true }
  if ($Value.Contains("{")) { return $true }
  $examples = @(
    "your-project-id",
    "CHANGE_ME",
    "change-me-to-a-long-random-secret",
    "SHARED_DRIVE_ID",
    "FOLDER_ID"
  )
  return $examples -contains $Value
}

function Add-RequiredEnv {
  param(
    [System.Collections.Generic.List[string]]$Errs,
    [string]$Name,
    [string]$Hint = ""
  )
  $val = [Environment]::GetEnvironmentVariable($Name)
  if ([string]::IsNullOrWhiteSpace($val)) {
    $suffix = if ($Hint) { " ($Hint)" } else { "" }
    $Errs.Add("${Name}: empty${suffix}")
  } elseif (Test-PlaceholderValue $val) {
    $Errs.Add("${Name}: example value ($val)")
  }
}

function Assert-EnvErrors {
  param([System.Collections.Generic.List[string]]$Errs)
  if ($Errs.Count -eq 0) { return }
  Write-Host "== config check failed =="
  foreach ($e in $Errs) { Write-Host "- $e" }
  throw "fix config/ and retry"
}

function Require-FullDeployEnv {
  $errs = [System.Collections.Generic.List[string]]::new()
  Add-RequiredEnv $errs GCP_PROJECT_ID
  Add-RequiredEnv $errs GCS_HWP_ORIGINAL_BUCKET
  Add-RequiredEnv $errs GCS_SOURCE_BUCKET
  Add-RequiredEnv $errs RAG_CORPUS_NAME "Vertex RAG corpus path"
  Add-RequiredEnv $errs DRIVE_IDS "shared drive id"
  Add-RequiredEnv $errs SYNC_FOLDER_IDS "folder id from Drive URL folders/"
  Add-RequiredEnv $errs MCP_API_KEY "Cloud 학과 등록부 keys.staff"

  $studentCorpus = $env:RAG_CORPUS_NAME_STUDENT
  $studentFolders = $env:STUDENT_FOLDER_IDS
  $hasCorpus = -not [string]::IsNullOrWhiteSpace($studentCorpus)
  $hasFolders = -not [string]::IsNullOrWhiteSpace($studentFolders)
  if ($hasCorpus -and -not $hasFolders) {
    $errs.Add("STUDENT_FOLDER_IDS: required when RAG_CORPUS_NAME_STUDENT is set")
  } elseif (-not $hasCorpus -and $hasFolders) {
    $errs.Add("RAG_CORPUS_NAME_STUDENT: required when STUDENT_FOLDER_IDS is set")
  }
  if ($hasCorpus -and (Test-PlaceholderValue $studentCorpus)) {
    $errs.Add("RAG_CORPUS_NAME_STUDENT: example value ($studentCorpus)")
  }
  if ($hasCorpus -and $hasFolders -and [string]::IsNullOrWhiteSpace($env:MCP_API_KEY_STUDENT)) {
    $errs.Add("MCP_API_KEY_STUDENT: empty (student split is on — set a key different from keys.staff)")
  }
  if ($env:MCP_API_KEY_STUDENT -and $env:MCP_API_KEY -and $env:MCP_API_KEY_STUDENT -eq $env:MCP_API_KEY) {
    $errs.Add("MCP_API_KEY_STUDENT: must differ from keys.staff")
  }
  Assert-EnvErrors $errs
}
