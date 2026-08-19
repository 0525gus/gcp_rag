# 배포 전 GCP 실물 검사. 리소스를 만들지 않는다.
# 사용: .\scripts\preflight.ps1
# deploy.ps1 이 API enable 뒤에 호출한다. 건너뛰려면 $env:SKIP_PREFLIGHT = "1"
#
# _load_env.ps1 의 Require-FullDeployEnv 는 .env 문자열만 본다.
# 여기가 버킷·DB·코퍼스·(가능하면) Drive 공유의 실존을 본다.

function Test-RagCorpusNameFormat {
  param([string]$Name)
  if ([string]::IsNullOrWhiteSpace($Name)) { return $false }
  return $Name -match '^projects/[^/]+/locations/[^/]+/ragCorpora/[^/]+$'
}

function Get-RagCorpusLocation {
  param([string]$Name)
  if ($Name -match '^projects/[^/]+/locations/([^/]+)/ragCorpora/') {
    return $Matches[1]
  }
  return ""
}

function Get-PreflightConfigErrors {
  $errs = [System.Collections.Generic.List[string]]::new()
  $hwp = Get-EnvOr GCS_HWP_ORIGINAL_BUCKET ""
  $source = Get-EnvOr GCS_SOURCE_BUCKET ""
  if ($hwp -and $source -and ($hwp -eq $source)) {
    $errs.Add("GCS buckets: HWP original and source must differ ($hwp)")
  }

  $corpus = Get-EnvOr RAG_CORPUS_NAME ""
  if ($corpus -and -not (Test-RagCorpusNameFormat $corpus)) {
    $errs.Add("RAG_CORPUS_NAME: expected projects/.../locations/.../ragCorpora/... (got $corpus)")
  }
  $student = Get-EnvOr RAG_CORPUS_NAME_STUDENT ""
  if ($student -and -not (Test-RagCorpusNameFormat $student)) {
    $errs.Add("RAG_CORPUS_NAME_STUDENT: expected projects/.../locations/.../ragCorpora/... (got $student)")
  }

  $qg = (Get-EnvOr QG_MODE "log").ToLower()
  if ($qg -eq "fallback") {
    $proc = Get-EnvOr DOCAI_PROCESSOR_ID ""
    if ([string]::IsNullOrWhiteSpace($proc) -or (Test-PlaceholderValue $proc)) {
      $errs.Add("DOCAI_PROCESSOR_ID: required when QG_MODE=fallback")
    }
  }
  return $errs
}

function Assert-PreflightConfig {
  Assert-EnvErrors (Get-PreflightConfigErrors)
}

function Get-GcloudText {
  param([string[]]$GcloudArgs)
  $prev = $ErrorActionPreference
  $ErrorActionPreference = "Continue"
  try {
    $out = & gcloud @GcloudArgs 2>&1
    $code = $LASTEXITCODE
  } finally {
    $ErrorActionPreference = $prev
  }
  $stdout = @(
    $out | Where-Object { $_ -isnot [System.Management.Automation.ErrorRecord] }
  ) -join "`n"
  $all = ($out | Out-String).Trim()
  $text = if ($code -eq 0) { $stdout.Trim() } else { $all }
  return @{ Ok = ($code -eq 0); Text = $text; Code = $code }
}

function Get-JsonUri {
  param([string]$Uri, [hashtable]$Headers)
  try {
    return @{ Ok = $true; Body = (Invoke-RestMethod -Method Get -Uri $Uri -Headers $Headers) }
  } catch {
    $code = 0
    if ($_.Exception.Response -and $null -ne $_.Exception.Response.StatusCode) {
      $code = [int]$_.Exception.Response.StatusCode
    }
    return @{ Ok = $false; Code = $code; Error = $_.Exception.Message }
  }
}

function Add-PreflightResult {
  param(
    [System.Collections.Generic.List[string]]$Errs,
    [bool]$Ok,
    [string]$Name,
    [string]$FailDetail
  )
  if ($Ok) {
    Write-Host "ok   $Name"
  } else {
    Write-Host "FAIL $Name"
    $Errs.Add("${Name}: $FailDetail")
  }
}

function Test-GcsBucketExists {
  param([string]$Bucket)
  $r = Get-GcloudText -GcloudArgs @("storage", "buckets", "describe", "gs://$Bucket", "--format=value(name)")
  return $r.Ok
}

function Test-FirestoreNative {
  param([string]$Project, [string]$Database)
  $r = Get-GcloudText -GcloudArgs @("firestore", "databases", "describe", "--database=$Database", "--project=$Project", "--format=value(type)")
  if (-not $r.Ok) { return @{ Ok = $false; Type = ""; Text = $r.Text } }
  $type = $r.Text.Trim()
  return @{ Ok = ($type -eq "FIRESTORE_NATIVE"); Type = $type; Text = $r.Text }
}

function Test-RagCorpusExists {
  param([string]$Name, [string]$Token)
  $loc = Get-RagCorpusLocation $Name
  if (-not $loc) { return $false }
  $headers = @{ Authorization = "Bearer $Token" }
  foreach ($ver in @("v1", "v1beta1")) {
    $uri = "https://${loc}-aiplatform.googleapis.com/${ver}/${Name}"
    $got = Get-JsonUri -Uri $uri -Headers $headers
    if ($got.Ok) { return $true }
  }
  return $false
}

function Test-DocAiProcessor {
  param([string]$Project, [string]$Location, [string]$ProcessorId)
  $id = $ProcessorId
  if ($ProcessorId -match '/processors/([^/]+)$') { $id = $Matches[1] }
  $r = Get-GcloudText -GcloudArgs @("documentai", "processors", "describe", $id, "--location=$Location", "--project=$Project", "--format=value(name)")
  return $r.Ok
}

function Get-DrivePermissions {
  param([string]$DriveId, [string]$Token)
  $headers = @{ Authorization = "Bearer $Token" }
  $emails = New-Object System.Collections.Generic.List[string]
  $page = ""
  for ($i = 0; $i -lt 10; $i++) {
    $uri = "https://www.googleapis.com/drive/v3/files/${DriveId}/permissions?supportsAllDrives=true&fields=nextPageToken,permissions(emailAddress,role,type)"
    if ($page) { $uri = "$uri&pageToken=$page" }
    $got = Get-JsonUri -Uri $uri -Headers $headers
    if (-not $got.Ok) {
      return @{ Ok = $false; Code = $got.Code; Emails = @() }
    }
    foreach ($p in @($got.Body.permissions)) {
      if ($p.emailAddress) { $emails.Add([string]$p.emailAddress) }
    }
    $page = [string]$got.Body.nextPageToken
    if ([string]::IsNullOrWhiteSpace($page)) { break }
  }
  return @{ Ok = $true; Code = 200; Emails = $emails }
}

function Assert-GcpPrereqs {
  if ((Get-EnvOr SKIP_PREFLIGHT "") -eq "1") {
    Write-Host "== preflight skipped (SKIP_PREFLIGHT=1) =="
    return
  }

  Assert-PreflightConfig

  Write-Host "== preflight (GCP resources) =="
  if (-not (Get-Command gcloud -ErrorAction SilentlyContinue)) {
    throw "gcloud not on PATH — https://cloud.google.com/sdk/docs/install"
  }

  $errs = [System.Collections.Generic.List[string]]::new()
  $project = Get-EnvOr GCP_PROJECT_ID ""
  $region = Get-EnvOr GCP_REGION "asia-northeast3"
  $hwp = Get-EnvOr GCS_HWP_ORIGINAL_BUCKET ""
  $source = Get-EnvOr GCS_SOURCE_BUCKET ""
  $fsDb = Get-EnvOr FIRESTORE_DATABASE "rag-sync-state"
  $corpus = Get-EnvOr RAG_CORPUS_NAME ""
  $student = Get-EnvOr RAG_CORPUS_NAME_STUDENT ""
  $qg = (Get-EnvOr QG_MODE "log").ToLower()
  $docai = Get-EnvOr DOCAI_PROCESSOR_ID ""
  $docaiLoc = Get-EnvOr DOCAI_LOCATION $region

  $cfg = Get-GcloudText -GcloudArgs @("config", "set", "project", $project)
  if (-not $cfg.Ok) {
    $errs.Add("gcloud config set project ${project}: $($cfg.Text)")
    Assert-EnvErrors $errs
    return
  }

  Add-PreflightResult $errs (Test-GcsBucketExists $hwp) `
    "GCS $hwp (HWP original)" `
    "bucket missing. create: gcloud storage buckets create gs://$hwp --location=$region --project=$project"

  Add-PreflightResult $errs (Test-GcsBucketExists $source) `
    "GCS $source (RAG source)" `
    "bucket missing. create: gcloud storage buckets create gs://$source --location=$region --project=$project"

  $fs = Test-FirestoreNative -Project $project -Database $fsDb
  $fsHint = "create Native DB: gcloud firestore databases create --database=$fsDb --location=$region --type=firestore-native --project=$project"
  if ($fs.Ok) {
    Add-PreflightResult $errs $true "Firestore $fsDb (NATIVE)" ""
  } elseif ($fs.Type) {
    Add-PreflightResult $errs $false "Firestore $fsDb" "type=$($fs.Type), need FIRESTORE_NATIVE. (default) Datastore 불가. $fsHint"
  } else {
    Add-PreflightResult $errs $false "Firestore $fsDb" "database missing. $fsHint"
  }

  $tokenR = Get-GcloudText -GcloudArgs @("auth", "print-access-token")
  $token = ""
  if ($tokenR.Ok) { $token = $tokenR.Text.Split("`n")[0].Trim() }

  if ($token) {
    Add-PreflightResult $errs (Test-RagCorpusExists -Name $corpus -Token $token) `
      "RAG corpus" `
      "missing $corpus — Vertex AI RAG 콘솔에서 코퍼스를 만든 뒤 RAG_CORPUS_NAME 에 경로를 넣을 것"
    if ($student) {
      Add-PreflightResult $errs (Test-RagCorpusExists -Name $student -Token $token) `
        "RAG corpus (student)" `
        "missing $student"
    }
  } else {
    $errs.Add("gcloud auth print-access-token failed — gcloud auth login")
  }

  $numR = Get-GcloudText -GcloudArgs @("projects", "describe", $project, "--format=value(projectNumber)")
  $sa = ""
  if ($numR.Ok -and $numR.Text) {
    $sa = "$($numR.Text.Trim())-compute@developer.gserviceaccount.com"
    Write-Host "ok   Cloud Run SA $sa"
  } else {
    $errs.Add("project number: cannot resolve default compute SA")
  }

  $driveIds = @((Get-EnvOr DRIVE_IDS "") -split "," | ForEach-Object { $_.Trim() } | Where-Object { $_ })
  if ($token -and $sa -and $driveIds.Count -gt 0) {
    foreach ($did in $driveIds) {
      $perm = Get-DrivePermissions -DriveId $did -Token $token
      if (-not $perm.Ok) {
        Write-Host "WARN Drive $did : SA 멤버십을 확인하지 못함 (Drive 스코프 없는 토큰이면 정상). 콘솔에서 $sa 를 공유드라이브 멤버(뷰어 이상)로 초대할 것"
        continue
      }
      $hit = $false
      foreach ($em in $perm.Emails) {
        if ($em.Equals($sa, [StringComparison]::OrdinalIgnoreCase)) { $hit = $true; break }
      }
      Add-PreflightResult $errs $hit `
        "Drive $did share($sa)" `
        "Cloud Run SA 가 멤버가 아님. 공유드라이브 관리에서 $sa 를 뷰어 이상으로 초대"
    }
  }

  if ($qg -eq "fallback") {
    Write-Host "WARN QG_MODE=fallback : parser 이미지에 LibreOffice 가 없어 런타임에 FALLBACK_FAILED 가 난다. docs/PARSER_DOCAI_FALLBACK.md"
    Add-PreflightResult $errs (Test-DocAiProcessor -Project $project -Location $docaiLoc -ProcessorId $docai) `
      "Document AI processor" `
      "processor missing. QG_MODE=log 이거나 DOCAI_PROCESSOR_ID 를 실존 ID 로"
  }

  Assert-EnvErrors $errs
  Write-Host "== preflight done =="
}

# 직접 실행일 때만 돈다. deploy.ps1 은 dotsource 후 Assert-GcpPrereqs 를 호출.
if ($MyInvocation.InvocationName -ne '.') {
  $ErrorActionPreference = "Stop"
  if ($PSVersionTable.PSVersion.Major -ge 7) {
    $PSNativeCommandUseErrorActionPreference = $false
  }
  Set-Location (Split-Path -Parent $PSScriptRoot)
  . (Join-Path $PSScriptRoot "_load_env.ps1")
  Load-Dotenv
  if (-not $env:MCP_API_KEY) { $env:MCP_API_KEY = $env:MCP_API_KEY_STAFF }
  Require-FullDeployEnv
  Assert-GcpPrereqs
}
