# 배포 전 GCP 실물 검사. 버킷·DB·코퍼스는 만들지 않는다(존재만 본다).
# 예외 두 가지만 고친다 - 둘 다 "없으면 배포가 조용히 헛도는" 것들이다.
#   1) 기본 컴퓨팅 SA 가 없으면 물어보고 compute.googleapis.com 을 켠다
#      (SA 를 직접 만들 수는 없다 - API 를 켜야 Google 이 만든다)
#   2) 공유드라이브에 그 SA 가 없으면 물어보고 뷰어로 초대한다(대화형일 때만)
# 자동 조치를 끄려면 $env:PREFLIGHT_NO_FIX = "1"
#
# 사용: .\scripts\preflight.ps1
# deploy.ps1 이 API enable 뒤에 호출한다. 건너뛰려면 $env:SKIP_PREFLIGHT = "1"
#
# _load_env.ps1 의 Require-FullDeployEnv 는 config 문자열만 본다.
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

function Test-PreflightCanFix {
  <#
    .SYNOPSIS
      자동 조치를 해도 되는 상황인지. CI·비대화형에서는 묻지 않고 실패시킨다.
  #>
  if ((Get-EnvOr PREFLIGHT_NO_FIX "") -eq "1") { return $false }
  if ($env:CI) { return $false }
  return [Environment]::UserInteractive
}

function Confirm-PreflightAction {
  # 상태·결과 출력은 개조식, 사용자에게 묻는 문장만 경어체로 쓴다.
  param([string]$Question)
  if (-not (Test-PreflightCanFix)) { return $false }
  $ans = Read-Host "$Question [y/N]"
  return $ans -match '^(y|yes)$'
}

function Add-DriveMember {
  <#
    .SYNOPSIS
      공유드라이브에 계정을 멤버로 추가한다.
    .NOTES
      서비스 계정은 알림 메일을 못 받으므로 sendNotificationEmail=false 가 필수다.
  #>
  param([string]$DriveId, [string]$Email, [string]$Role, [string]$Token)

  $uri = "https://www.googleapis.com/drive/v3/files/$DriveId/permissions" +
         "?supportsAllDrives=true&sendNotificationEmail=false"
  $body = (@{ type = "user"; role = $Role; emailAddress = $Email } | ConvertTo-Json -Compress)
  try {
    $res = Invoke-RestMethod -Method Post -Uri $uri `
      -Headers @{ Authorization = "Bearer $Token" } `
      -ContentType "application/json; charset=utf-8" -Body $body
    return @{ Ok = $true; Body = $res }
  } catch {
    $code = 0
    if ($_.Exception.Response -and $null -ne $_.Exception.Response.StatusCode) {
      $code = [int]$_.Exception.Response.StatusCode
    }
    return @{ Ok = $false; Code = $code; Error = $_.Exception.Message }
  }
}

function Invoke-JsonPost {
  param([string]$Uri, [hashtable]$Headers, [string]$Body)
  try {
    $res = Invoke-RestMethod -Method Post -Uri $Uri -Headers $Headers `
      -ContentType "application/json; charset=utf-8" -Body $Body
    return @{ Ok = $true; Body = $res }
  } catch {
    $code = 0
    if ($_.Exception.Response -and $null -ne $_.Exception.Response.StatusCode) {
      $code = [int]$_.Exception.Response.StatusCode
    }
    return @{ Ok = $false; Code = $code; Error = $_.Exception.Message }
  }
}

function New-ImpersonatedToken {
  <#
    .SYNOPSIS
      SA 를 가장한 액세스 토큰을 발급한다. 스코프를 지정할 수 있는 게 핵심이다.
    .NOTES
      호출자는 cloud-platform 스코프면 충분하다 - Drive 스코프는 발급받는 토큰에
      붙는다. 대신 호출자에게 그 SA 의 roles/iam.serviceAccountTokenCreator 가
      있어야 한다. 부여 직후에는 IAM 전파에 수십 초가 걸린다.
  #>
  param([string]$Sa, [string[]]$Scopes, [string]$CallerToken)

  $body = (@{ scope = $Scopes; lifetime = "300s" } | ConvertTo-Json -Compress)
  $uri = "https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/${Sa}:generateAccessToken"
  $r = Invoke-JsonPost -Uri $uri -Headers @{ Authorization = "Bearer $CallerToken" } -Body $body
  if (-not $r.Ok) { return @{ Ok = $false; Code = $r.Code; Error = $r.Error } }
  return @{ Ok = $true; Token = [string]$r.Body.accessToken }
}

function Test-SaDriveAccess {
  <#
    .SYNOPSIS
      SA 가 실제로 그 공유드라이브를 읽는지 본다. 멤버 목록 조회보다 정확하다 -
      "봇이 읽을 수 있나" 를 런타임 신원으로 직접 답한다.
    .NOTES
      startPageToken 까지 보는 이유: DRIVE_IDS 에 폴더 ID 를 넣으면 파일 조회는
      되는데 델타 진입점만 실패한다. 백필은 되고 증분만 조용히 죽는 형태라
      배포 후에는 알아채기 어렵다.
  #>
  param([string]$Sa, [string]$DriveId, [string]$CallerToken)

  $tok = New-ImpersonatedToken -Sa $Sa -CallerToken $CallerToken `
    -Scopes @("https://www.googleapis.com/auth/drive.readonly")
  if (-not $tok.Ok) {
    return @{ Ok = $false; Stage = "impersonate"; Code = $tok.Code; Detail = $tok.Error }
  }
  $h = @{ Authorization = "Bearer $($tok.Token)" }

  $drive = Get-JsonUri -Uri "https://www.googleapis.com/drive/v3/drives/${DriveId}?fields=id,name" -Headers $h
  if (-not $drive.Ok) {
    return @{ Ok = $false; Stage = "access"; Code = $drive.Code; Detail = $drive.Error }
  }
  $delta = Get-JsonUri -Headers $h `
    -Uri "https://www.googleapis.com/drive/v3/changes/startPageToken?driveId=${DriveId}&supportsAllDrives=true"
  if (-not $delta.Ok) {
    return @{ Ok = $false; Stage = "delta"; Code = $delta.Code; Detail = $delta.Error; Name = $drive.Body.name }
  }
  return @{ Ok = $true; Name = $drive.Body.name }
}

function Restore-DefaultComputeSa {
  <#
    .SYNOPSIS
      기본 컴퓨팅 SA 를 되살린다. 직접 만들 수는 없고 Compute Engine API 를 켜면
      Google 이 만들어 준다. 생성까지 시차가 있어 잠시 기다린다.
  #>
  param([string]$Project, [string]$Email)

  Write-Host "     compute.googleapis.com 을 켜는 중 (기본 SA 생성)"
  $r = Get-GcloudText -GcloudArgs @("services", "enable", "compute.googleapis.com", "--project=$Project")
  if (-not $r.Ok) {
    Write-Host "     API enable 실패: $($r.Text)"
    return $false
  }
  for ($i = 1; $i -le 6; $i++) {
    Start-Sleep -Seconds 5
    if (Test-ServiceAccountExists -Project $Project -Email $Email) { return $true }
    Write-Host "     대기 중... ($i/6)"
  }
  return $false
}

function Test-ServiceAccountExists {
  param([string]$Project, [string]$Email)
  $r = Get-GcloudText -GcloudArgs @(
    "iam", "service-accounts", "describe", $Email, "--project=$Project", "--format=value(email)"
  )
  return $r.Ok
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

function Test-RagCorpusUsable {
  <#
    .SYNOPSIS
      코퍼스가 색인을 받을 수 있는 상태인지 본다.
    .NOTES
      Test-RagCorpusExists 는 존재만 본다. state 가 ACTIVE 가 아니면 색인이
      전부 실패하는데 워크플로는 그래도 SUCCEEDED 로 끝나 조용히 빈 코퍼스로
      남는다 - 배포·백필 전에 여기서 막는다.
      corpusStatus 가 없는 응답(구 API)은 통과로 본다. 못 읽는 것과
      비정상인 것을 섞으면 정상 코퍼스까지 배포가 막힌다.
  #>
  param([string]$Name, [string]$Token)
  $loc = Get-RagCorpusLocation $Name
  if (-not $loc) {
    return @{ Ok = $false; State = ""; Detail = "코퍼스 경로 형식이 아니다: $Name" }
  }
  $headers = @{ Authorization = "Bearer $Token" }
  $last = ""
  foreach ($ver in @("v1", "v1beta1")) {
    $got = Get-JsonUri -Uri "https://${loc}-aiplatform.googleapis.com/${ver}/${Name}" -Headers $headers
    if (-not $got.Ok) { $last = "HTTP $($got.Code)"; continue }
    $state = [string]$got.Body.corpusStatus.state
    if (-not $state) { return @{ Ok = $true; State = "UNKNOWN"; Detail = "" } }
    if ($state -eq "ACTIVE") { return @{ Ok = $true; State = $state; Detail = "" } }
    $why = [string]$got.Body.corpusStatus.errorStatus
    return @{ Ok = $false; State = $state; Detail = ("state=$state $why").Trim() }
  }
  return @{ Ok = $false; State = ""; Detail = "조회 실패 ($last)" }
}

function Get-RagCorpusFileCount {
  <#
    .SYNOPSIS
      코퍼스에 실린 파일 수. 조회 실패는 -1 - 0 과 섞으면 멀쩡한 코퍼스에
      전체 백필을 다시 걸게 된다.
    .NOTES
      용도는 "비었는가" 판정이라 상한($Max)까지만 센다.
  #>
  param([string]$Name, [string]$Token, [int]$Max = 2000)
  $loc = Get-RagCorpusLocation $Name
  if (-not $loc) { return -1 }
  $headers = @{ Authorization = "Bearer $Token" }
  foreach ($ver in @("v1", "v1beta1")) {
    $count = 0
    $page = ""
    $ok = $true
    while ($count -lt $Max) {
      $uri = "https://${loc}-aiplatform.googleapis.com/${ver}/${Name}/ragFiles?pageSize=100"
      if ($page) { $uri = "$uri&pageToken=$page" }
      $got = Get-JsonUri -Uri $uri -Headers $headers
      if (-not $got.Ok) { $ok = $false; break }
      $count += @($got.Body.ragFiles).Where({ $null -ne $_ }).Count
      $page = [string]$got.Body.nextPageToken
      if ([string]::IsNullOrWhiteSpace($page)) { break }
    }
    if ($ok) { return $count }
  }
  return -1
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
    throw "gcloud not on PATH - https://cloud.google.com/sdk/docs/install"
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
      "missing $corpus - Vertex AI RAG 콘솔에서 코퍼스를 만든 뒤 RAG_CORPUS_NAME 에 경로를 넣을 것"
    if ($student) {
      Add-PreflightResult $errs (Test-RagCorpusExists -Name $student -Token $token) `
        "RAG corpus (student)" `
        "missing $student"
    }
  } else {
    $errs.Add("gcloud auth print-access-token failed - gcloud auth login")
  }

  $numR = Get-GcloudText -GcloudArgs @("projects", "describe", $project, "--format=value(projectNumber)")
  $sa = ""
  if ($numR.Ok -and $numR.Text) {
    $sa = "$($numR.Text.Trim())-compute@developer.gserviceaccount.com"
    # 주소를 조립만 하고 ok 를 찍던 자리다. 기본 컴퓨팅 SA 는 Compute Engine API 를
    # 켤 때 생기므로, 안 켠 프로젝트에는 **계정 자체가 없다**. 그런데도 ok 로 넘어가면
    # 다음 Drive 단계에서 "Google 계정이 없는 이메일 주소" 라는 엉뚱한 메시지를 만나
    # 공유 정책 문제로 오해하게 된다 - 실제로는 없는 계정을 공유하려 한 것이다.
    $saOk = Test-ServiceAccountExists -Project $project -Email $sa
    if (-not $saOk) {
      Write-Host "MISS Cloud Run SA $sa - 없다"
      if (Confirm-PreflightAction "     생성하시겠습니까? (compute.googleapis.com 활성화)") {
        $saOk = Restore-DefaultComputeSa -Project $project -Email $sa
      }
    }
    Add-PreflightResult $errs $saOk `
      "Cloud Run SA $sa" `
      ("service account missing. 수동: gcloud services enable compute.googleapis.com --project=$project " +
       "- 켠 뒤에도 안 생기면 GCP 콘솔 IAM 및 관리자 > 서비스 계정 에서 확인할 것")
  } else {
    $errs.Add("project number: cannot resolve default compute SA")
  }

  $driveIds = @((Get-EnvOr DRIVE_IDS "") -split "," | ForEach-Object { $_.Trim() } | Where-Object { $_ })
  # Drive 호출은 토큰을 따로 잡는다. `auth print-access-token` 은 cloud-platform
  # 스코프뿐이라 Drive API 가 403 을 낸다. Drive 스코프는 ADC 쪽에만 붙일 수 있어
  # (`auth application-default login --scopes=...`) 그쪽을 우선 쓴다 - 이걸 안 하면
  # 실패 안내문대로 재로그인해도 preflight 는 계속 같은 403 을 낸다.
  $adcR = Get-GcloudText -GcloudArgs @("auth", "application-default", "print-access-token")
  $driveToken = if ($adcR.Ok -and $adcR.Text) { $adcR.Text.Split("`n")[0].Trim() } else { $token }

  if ($driveToken -and $sa -and $driveIds.Count -gt 0) {
    foreach ($did in $driveIds) {
      $hit = $false
      $conclusive = $false   # 판정을 신뢰할 수 있는가 (아니면 WARN 으로 남긴다)

      # 1순위: SA 를 가장해 실제로 읽어 본다. 사람 토큰에 Drive 스코프가 없어도 되고,
      # 멤버 목록 조회와 달리 "봇이 읽을 수 있나" 를 직접 답한다.
      $probe = Test-SaDriveAccess -Sa $sa -DriveId $did -CallerToken $token
      if ($probe.Ok) {
        Write-Host "     SA 실접근 확인: $($probe.Name)"
        $hit = $true; $conclusive = $true
      } elseif ($probe.Stage -eq "delta") {
        # 드라이브는 읽히는데 델타 진입점만 실패 = 공유드라이브가 아니라 폴더 ID.
        Write-Host "FAIL Drive $did : 파일은 읽히는데 델타 시작점이 없다 (HTTP $($probe.Code))"
        Write-Host "     DRIVE_IDS 가 공유드라이브가 아니라 폴더 ID 일 가능성이 높다"
        Write-Host "     폴더는 SYNC_FOLDER_IDS 로 옮기고 DRIVE_IDS 에는 0A... 형태를 넣을 것"
        $errs.Add("Drive ${did}: 델타 시작점 없음 - 공유드라이브 ID 가 맞는지 확인")
        continue
      } elseif ($probe.Stage -eq "access") {
        Write-Host "MISS Drive $did : SA 가 접근하지 못한다 (HTTP $($probe.Code))"
        $conclusive = $true
      } else {
        # 가장 자체가 안 됨(Token Creator 없음 등). 멤버 목록 조회로 물러난다.
        $perm = Get-DrivePermissions -DriveId $did -Token $driveToken
        if ($perm.Ok) {
          foreach ($em in $perm.Emails) {
            if ($em.Equals($sa, [StringComparison]::OrdinalIgnoreCase)) { $hit = $true; break }
          }
          $conclusive = $true
          if (-not $hit) { Write-Host "MISS Drive $did : $sa 가 멤버가 아니다" }
        } else {
          Write-Host "WARN Drive $did : 확인 경로 둘 다 막힘 (가장 $($probe.Code) / 목록 $($perm.Code))"
          Write-Host "     SA 가장을 쓰려면: gcloud iam service-accounts add-iam-policy-binding $sa --member=user:<본인> --role=roles/iam.serviceAccountTokenCreator"
        }
      }

      if (-not $hit) {
        if (Confirm-PreflightAction "     뷰어로 초대하시겠습니까?") {
          $add = Add-DriveMember -DriveId $did -Email $sa -Role "reader" -Token $driveToken
          if ($add.Ok) {
            Write-Host "     초대 완료 - SA 실접근으로 다시 확인한다"
            $again = Test-SaDriveAccess -Sa $sa -DriveId $did -CallerToken $token
            if ($again.Ok) {
              $hit = $true; $conclusive = $true
            } else {
              # 가장이 막힌 환경이면 초대 성공 자체를 근거로 삼는다.
              $hit = $true
            }
          } else {
            Write-Host "     초대 실패 HTTP $($add.Code): $($add.Error)"
            if ($add.Code -eq 401 -or $add.Code -eq 403) {
              Write-Host "     Drive 스코프 토큰이 필요하다:"
              Write-Host "       gcloud auth application-default login --scopes=https://www.googleapis.com/auth/cloud-platform,https://www.googleapis.com/auth/drive"
            }
          }
        }
      }
      if ($hit -or $conclusive) {
        Add-PreflightResult $errs $hit `
          "Drive $did share($sa)" `
          "Cloud Run SA 가 접근하지 못함. .\scripts\share_drive.ps1 또는 공유드라이브 관리에서 $sa 를 뷰어 이상으로 초대"
      } else {
        # 확인 경로가 둘 다 막혔다. 여기서 실패로 처리하면 Token Creator 도 Drive
        # 스코프도 없는 정상 환경(예: CI)까지 배포가 막힌다 - 경고로 남긴다.
        Write-Host "WARN Drive $did share($sa) : 확인 불가. 콘솔에서 $sa 를 뷰어 이상으로 초대할 것"
      }
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
  Set-BaseDeployConfig | Out-Null
  Require-FullDeployEnv
  Assert-GcpPrereqs
}
