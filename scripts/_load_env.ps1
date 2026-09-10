# config/ 로더 + 배포 필수값 검사.
# deploy.ps1 / deploy_mcp.ps1 / preflight.ps1 / share_drive.ps1 / backfill.ps1 /
# setup_alerts.ps1 이 dot-source 한다.
#
# 설정 원본은 config/common.yaml + config/departments/<학과>.yaml **하나뿐이다**
# (.env 는 없앴다 — docs/ENV_MIGRATION.md). PS 에는 YAML 파서가 없으므로 파싱은
# scripts/dept_config.py 가 하고, 여기서는 그 KEY=VALUE 출력을 프로세스 환경변수로
# 옮기기만 한다. 그래서 preflight 처럼 $env: 를 읽는 코드는 손대지 않아도 된다.
#
# 규칙 변경 시 tests/test_deploy_env_ps1.py 도 맞출 것.

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

# 학과 yaml + common.yaml 이 채우는 키.
# **반복 배포에서 앞 학과 값이 남지 않게 매번 비운다** — 안 비우면 -All 로 여러
# 학과를 돌 때 앞 학과 코퍼스·키가 남아 조용히 섞인다. 이 목록에서 빠진 키는
# 학과 사이로 새어 나가므로, dept_config.py 가 내보내는 이름을 전부 적을 것.
$ConfigKeys = @(
  "GCP_PROJECT_ID", "GCP_REGION", "ARTIFACT_REPO",
  "GCS_HWP_ORIGINAL_BUCKET", "GCS_SOURCE_BUCKET",
  "FIRESTORE_DATABASE", "DOC_STATE_COLLECTION",
  "QG_MODE", "DOCAI_PROCESSOR_ID",
  "INGEST_CONCURRENCY", "RAG_DELETE_CONCURRENCY", "RAG_DELETE_PACING_SECONDS",
  "RAG_METADATA_BUCKET", "RAG_MAPPING_WRITE_ENABLED", "RAG_MAPPING_READ_ENABLED",
  "RAG_MAPPING_FALLBACK_SCAN_ENABLED",
  "CLOUD_TASKS_ENABLED", "TASK_QUEUE_LOCATION", "TASK_QUEUE_FACULTY",
  "TASK_QUEUE_STUDENT", "TASK_SERVICE_ACCOUNT", "SYNC_TASK_BASE_URL",
  "INDEX_JOB_TIMEOUT_SECONDS",
  "PARSER_TIMEOUT", "PARSER_CONCURRENCY", "PARSER_MAX_INSTANCES", "SYNC_CONCURRENCY",
  "TOP_K_DEFAULT", "SEARCH_FETCH_MULTIPLIER", "SEARCH_FETCH_MAX",
  "MCP_CONCURRENCY", "ALLOW_UNAUTH",
  "RAG_CORPUS_NAME", "RAG_CORPUS_NAME_STUDENT",
  "DRIVE_IDS", "SYNC_FOLDER_IDS", "STUDENT_FOLDER_IDS",
  "MCP_MIN_INSTANCES", "MCP_SERVICE_NAME",
  "MCP_AUDIENCE", "DEPT_CODE", "DEPT_NAME", "DEPLOYMENT_METADATA_B64",
  "MCP_API_KEY", "MCP_API_KEY_STAFF", "MCP_API_KEY_STUDENT"
)

function Invoke-DeptConfig {
  # stdout 은 KEY=VALUE 전용이고 경고는 stderr 로 나온다(dept_config.py).
  # 여기서 2>&1 로 합치면 경고가 값으로 파싱된다 — 합치지 말 것.
  $py = Get-PythonExe
  $out = & $py (Join-Path $PSScriptRoot "dept_config.py") @args
  if ($LASTEXITCODE -ne 0) { throw "dept_config.py $($args -join ' ') 실패" }
  return $out
}

# 학과 yaml -> 환경변수. 코퍼스도 키도 여기서만 온다.
function Set-DeptConfig {
  param([string]$DeptCode, [string]$AudienceName)

  foreach ($k in $ConfigKeys) { Set-Item -LiteralPath "Env:$k" -Value "" }

  foreach ($line in (Invoke-DeptConfig --dept $DeptCode --audience $AudienceName)) {
    if (-not $line -or $line -notmatch "=") { continue }
    $k, $v = $line.Split("=", 2)
    Set-Item -LiteralPath "Env:$k" -Value $v
  }

  if ([string]::IsNullOrWhiteSpace($env:MCP_API_KEY)) {
    throw "$DeptCode/$AudienceName : keys 를 못 읽었다"
  }
  # MCP_API_KEY_STAFF 는 dept_config 가 같이 내보낸다
  # (Require-McpDeployEnv 의 '학생이 교직원 키를 재사용했나' 검사가 쓴다).
}

function Get-DepartmentCodes {
  $codes = @(Invoke-DeptConfig --list | ForEach-Object { $_.Trim() } | Where-Object { $_ })
  if (-not $codes) { throw "config/departments 에 학과 yaml 이 없다" }
  return $codes
}

function Get-DepartmentAudiences {
  param([string]$DeptCode)
  $audiences = @(Invoke-DeptConfig --dept $DeptCode --audiences | ForEach-Object { $_.Trim() } | Where-Object { $_ })
  if (-not $audiences) { throw "$DeptCode : 배포할 MCP 범위를 찾지 못했다" }
  return $audiences
}

# rag-sync 가 읽는 학과 맵(한 줄 JSON). 시크릿은 안 들어간다.
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

# 학과 단위가 아닌 스크립트(deploy·backfill·share_drive·setup_alerts)의 출발점.
#
#   - 값 대부분은 **첫 학과**(코드 알파벳 순) 것을 깐다. parser·sync 는 학과마다
#     뜨지 않으므로 기본값이 필요한데, 여기에 전 학과 union 을 깔면 학과 맵이
#     깨졌을 때(`DEPARTMENTS_JSON` 파싱 실패 → 단일 학과 폴백) 그 한 벌이 **남의
#     폴더까지 훑는다.** 한 학과로 좁혀 두면 그때 다른 학과가 멈출 뿐 섞이지는
#     않는다 — 섞인 코퍼스는 파일을 골라 지워야 하므로 그쪽이 훨씬 비싼 실패다.
#   - DRIVE_IDS 만 전 학과 union 이다. 서비스 코드가 읽지 않는 값이고(실측),
#     Scheduler·backfill·share_drive 가 "대상 드라이브 전체" 라는 뜻으로 쓴다.
#
# @() 를 빼지 말 것: `return` 은 배열을 풀어서 내보내므로 **학과가 하나면 결과가
# 문자열**이 된다. 그러면 $codes[0] 이 학과 코드가 아니라 첫 글자다("cs" -> "c").
# 받는 쪽도 @(Set-BaseDeployConfig) 로 감싸야 같은 이유로 안전하다.
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

function Get-McpStaffServiceName {
  return Get-EnvOr MCP_SERVICE_NAME_STAFF "rag-mcp-cs-staff"
}

function Get-McpStudentServiceName {
  return Get-EnvOr MCP_SERVICE_NAME_STUDENT "rag-mcp-cs-student"
}

# 이번 실행 타깃. 학과 모드에서는 dept_config 가 MCP_SERVICE_NAME 을 직접 준다.
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
  # dept.yaml.example 을 복사만 하고 안 채운 경우. dept_config.py 도 같은 것을
  # 막지만(PLACEHOLDER_KEYS), 셸에서 직접 넣은 값은 그쪽을 거치지 않는다.
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

# parser/sync/mcp + Scheduler. 버킷·Drive 가 비면 색인이 빈 채로 돈다.
function Require-FullDeployEnv {
  $errs = [System.Collections.Generic.List[string]]::new()
  Add-RequiredEnv $errs GCP_PROJECT_ID
  Add-RequiredEnv $errs GCS_HWP_ORIGINAL_BUCKET
  Add-RequiredEnv $errs GCS_SOURCE_BUCKET
  Add-RequiredEnv $errs RAG_CORPUS_NAME "Vertex RAG corpus path"
  Add-RequiredEnv $errs DRIVE_IDS "shared drive id"
  Add-RequiredEnv $errs SYNC_FOLDER_IDS "folder id from Drive URL folders/"
  Add-RequiredEnv $errs MCP_API_KEY "config/departments/<dept>.yaml keys.staff"

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
  # 분리가 켜지면 학생 MCP 도 올라간다 — 키가 없으면 거기서 멈추므로 먼저 잡는다.
  if ($hasCorpus -and $hasFolders -and [string]::IsNullOrWhiteSpace($env:MCP_API_KEY_STUDENT)) {
    $errs.Add("MCP_API_KEY_STUDENT: empty (student split is on — set a key different from keys.staff)")
  }
  if ($env:MCP_API_KEY_STUDENT -and $env:MCP_API_KEY -and $env:MCP_API_KEY_STUDENT -eq $env:MCP_API_KEY) {
    $errs.Add("MCP_API_KEY_STUDENT: must differ from keys.staff")
  }
  Assert-EnvErrors $errs
}

# MCP 만. 버킷은 unused 기본값 허용.
function Require-McpDeployEnv {
  $errs = [System.Collections.Generic.List[string]]::new()
  Add-RequiredEnv $errs GCP_PROJECT_ID
  Add-RequiredEnv $errs RAG_CORPUS_NAME "Vertex RAG corpus path"
  Add-RequiredEnv $errs MCP_API_KEY "config/departments/<dept>.yaml keys.<audience>"

  $service = Get-McpDeployServiceName
  if (Test-McpStudentTarget $service) {
    if ($env:MCP_API_KEY_STAFF -and $env:MCP_API_KEY -eq $env:MCP_API_KEY_STAFF) {
      $errs.Add("MCP_API_KEY: student service must not reuse MCP_API_KEY_STAFF")
    }
    # 학생 배포에는 학생 코퍼스가 무조건 있어야 한다. 비어 있으면 코퍼스 교체가
    # 통째로 건너뛰어져 RAG_CORPUS_NAME 이 교직원 값 그대로 남는다
    # — 학생 서비스가 교직원 전량을 검색하게 되므로 조용히 통과시키면 안 된다.
    Add-RequiredEnv $errs RAG_CORPUS_NAME_STUDENT "student deploy needs its own corpus"
    if ($env:RAG_CORPUS_NAME -ne $env:RAG_CORPUS_NAME_STUDENT) {
      $errs.Add("RAG_CORPUS_NAME: student deploy must use RAG_CORPUS_NAME_STUDENT")
    }
  } elseif ($env:RAG_CORPUS_NAME_STUDENT -and $env:RAG_CORPUS_NAME -eq $env:RAG_CORPUS_NAME_STUDENT) {
    $errs.Add("MCP_AUDIENCE: student corpus on staff service $service - set MCP_AUDIENCE=student")
  }
  Assert-EnvErrors $errs
}
