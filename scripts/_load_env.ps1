# .env 로더 + 배포 필수값 검사. deploy.ps1 / deploy_mcp.ps1 이 dot-source 한다.
# 셸에 이미 있는 값은 건드리지 않는다 (일회성 오버라이드 허용).
# 규칙 변경 시 scripts/_load_env.sh (pytest) 도 같이 맞출 것.

function Load-Dotenv {
  param([string]$Path = ".env")
  if (-not (Test-Path -LiteralPath $Path)) { return }
  Get-Content -LiteralPath $Path -Encoding utf8 | ForEach-Object {
    $line = $_ -replace "`r$", ""
    if ($line -match '^\s*(#|$)' -or $line -notmatch "=") { return }
    $key, $val = $line.Split("=", 2)
    $key = $key.Trim()
    if ($key.StartsWith("export ")) { $key = $key.Substring(7).Trim() }
    if ($key -notmatch '^[A-Za-z_][A-Za-z0-9_]*$') { return }
    if (Test-Path -LiteralPath "Env:$key") { return }
    $val = $val.Trim()
    if ($val.Length -ge 2) {
      $q = $val[0]
      if (($q -eq '"' -or $q -eq "'") -and $val[-1] -eq $q) {
        $val = $val.Substring(1, $val.Length - 2)
      }
    }
    Set-Item -LiteralPath "Env:$key" -Value $val
  }
}

function Get-EnvOr {
  param([string]$Name, [string]$Default = "")
  $v = [Environment]::GetEnvironmentVariable($Name)
  if ([string]::IsNullOrWhiteSpace($v)) { return $Default }
  return $v
}

function Get-McpStaffServiceName {
  return Get-EnvOr MCP_SERVICE_NAME_STAFF "rag-mcp"
}

function Get-McpStudentServiceName {
  return Get-EnvOr MCP_SERVICE_NAME_STUDENT "rag-mcp-student"
}

# 이번 실행 타깃. MCP_SERVICE_NAME 은 세션 오버라이드(.env 에 두지 말 것).
function Get-McpDeployServiceName {
  if (-not [string]::IsNullOrWhiteSpace($env:MCP_SERVICE_NAME)) {
    return $env:MCP_SERVICE_NAME
  }
  if ($env:MCP_AUDIENCE -like "*student*") {
    return Get-McpStudentServiceName
  }
  return Get-McpStaffServiceName
}

function Test-McpStudentTarget {
  param([string]$Service)
  if ($env:MCP_AUDIENCE -like "*student*") { return $true }
  $student = Get-McpStudentServiceName
  if ($Service -eq $student) { return $true }
  if ($Service -like "*student*") { return $true }
  return $false
}

function Test-PlaceholderValue {
  param([string]$Value)
  if ([string]::IsNullOrWhiteSpace($Value)) { return $true }
  if ($Value.Contains("{")) { return $true }
  $examples = @(
    "your-project-id",
    "change-me-to-a-long-random-secret",
    "shared-drive-id-1",
    "shared-drive-id-2",
    "shared-drive-id-1,shared-drive-id-2"
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
  Write-Host "== .env check failed =="
  foreach ($e in $Errs) { Write-Host "- $e" }
  throw "fix .env and retry"
}

# parser/sync/mcp + Scheduler. 버킷·Drive 가 비면 색인이 빈 채로 돈다.
function Require-FullDeployEnv {
  $errs = [System.Collections.Generic.List[string]]::new()
  Add-RequiredEnv $errs GCP_PROJECT_ID
  Add-RequiredEnv $errs GCS_RAW_BUCKET
  Add-RequiredEnv $errs GCS_NORMALIZED_BUCKET
  Add-RequiredEnv $errs RAG_CORPUS_NAME "Vertex RAG corpus path"
  Add-RequiredEnv $errs DRIVE_IDS "shared drive id"
  Add-RequiredEnv $errs MCP_API_KEY "set MCP_API_KEY_STAFF"

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
  if ($env:MCP_API_KEY_STUDENT -and $env:MCP_API_KEY -and $env:MCP_API_KEY_STUDENT -eq $env:MCP_API_KEY) {
    $errs.Add("MCP_API_KEY_STUDENT: must differ from MCP_API_KEY_STAFF")
  }
  Assert-EnvErrors $errs
}

# MCP 만. 버킷은 unused 기본값 허용.
function Require-McpDeployEnv {
  $errs = [System.Collections.Generic.List[string]]::new()
  Add-RequiredEnv $errs GCP_PROJECT_ID
  Add-RequiredEnv $errs RAG_CORPUS_NAME "Vertex RAG corpus path"
  Add-RequiredEnv $errs MCP_API_KEY "set MCP_API_KEY_STAFF (or MCP_API_KEY for student)"

  $service = Get-McpDeployServiceName
  if (Test-McpStudentTarget $service) {
    if ($env:MCP_API_KEY_STAFF -and $env:MCP_API_KEY -eq $env:MCP_API_KEY_STAFF) {
      $errs.Add("MCP_API_KEY: student service must not reuse MCP_API_KEY_STAFF")
    }
    if ($env:RAG_CORPUS_NAME_STUDENT -and $env:RAG_CORPUS_NAME -ne $env:RAG_CORPUS_NAME_STUDENT) {
      $errs.Add("RAG_CORPUS_NAME: student deploy must use RAG_CORPUS_NAME_STUDENT")
    }
  } elseif ($env:RAG_CORPUS_NAME_STUDENT -and $env:RAG_CORPUS_NAME -eq $env:RAG_CORPUS_NAME_STUDENT) {
    $errs.Add("MCP_AUDIENCE: student corpus on staff service $service — set MCP_AUDIENCE=student")
  }
  Assert-EnvErrors $errs
}
