from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _find_pwsh() -> str | None:
    for name in ("pwsh", "powershell"):
        found = shutil.which(name)
        if found:
            return found
    windir = os.environ.get("WINDIR", r"C:\Windows")
    sys32 = Path(windir) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    if sys32.is_file():
        return str(sys32)
    return None


PWSH = _find_pwsh()
pytestmark = pytest.mark.skipif(not PWSH, reason="PowerShell 없음")

LOADER = ROOT / "scripts" / "_load_env.ps1"


def test_loader_forces_utf8_for_gui_deploy_logs() -> None:
    source = LOADER.read_text(encoding="utf-8")
    assert "[Console]::OutputEncoding = $Utf8NoBom" in source
    assert '$env:PYTHONIOENCODING = "utf-8"' in source


PREFLIGHT = ROOT / "scripts" / "preflight.ps1"

VALID = {
    "GCP_PROJECT_ID": "my-proj",
    "GCS_HWP_ORIGINAL_BUCKET": "rag-hwp-original-my-proj",
    "GCS_SOURCE_BUCKET": "rag-source-my-proj",
    "RAG_CORPUS_NAME": "projects/my-proj/locations/asia-northeast3/ragCorpora/abc",
    "DRIVE_IDS": "0ABrealDriveId",
    "SYNC_FOLDER_IDS": "1ABrealFolderId",
    "MCP_API_KEY": "a-long-random-staff-secret",
}


def _run(
    exports: dict[str, str],
    fn: str,
    *,
    source_preflight: bool = False,
) -> subprocess.CompletedProcess[str]:
    assigns = "; ".join(f"$env:{k} = '{v}'" for k, v in exports.items())
    extra = f". '{PREFLIGHT}'; " if source_preflight else ""
    script = (
        "$ErrorActionPreference = 'Stop'; "
        f"{assigns}; "
        f". '{LOADER}'; "
        f"{extra}"
        f"{fn}"
    )
    env = {
        "PATH": os.environ.get("PATH", ""),
        "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
        "WINDIR": os.environ.get("WINDIR", ""),
        "PSModulePath": os.environ.get("PSModulePath", ""),
    }
    return subprocess.run(
        [PWSH, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
        cwd=str(ROOT),
        env=env,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )


def test_preflight_autofix_is_off_when_not_interactive() -> None:
    for guard in ({"PREFLIGHT_NO_FIX": "1"}, {"CI": "true"}):
        p = _run(guard, "if (Test-PreflightCanFix) { exit 1 } else { exit 0 }", source_preflight=True)
        assert p.returncode == 0, f"{guard} 에서 자동 조치가 켜져 있다: {p.stderr or p.stdout}"


def test_preflight_confirm_returns_false_without_prompt() -> None:
    p = _run(
        {"PREFLIGHT_NO_FIX": "1"},
        "if (Confirm-PreflightAction 'x') { exit 1 } else { exit 0 }",
        source_preflight=True,
    )
    assert p.returncode == 0, p.stderr or p.stdout


def test_student_split_requires_student_key() -> None:
    env = dict(VALID)
    env["SYNC_FOLDER_IDS"] = "f1"
    env["RAG_CORPUS_NAME_STUDENT"] = "projects/my-proj/locations/asia-northeast3/ragCorpora/stu"
    env["STUDENT_FOLDER_IDS"] = "f1"
    p = _run(env, "Require-FullDeployEnv")
    assert p.returncode != 0
    assert "MCP_API_KEY_STUDENT" in (p.stderr + p.stdout)


def _parse_engines() -> list[str]:
    engines: list[str] = []
    seen: set[str] = set()
    windir = os.environ.get("WINDIR", r"C:\Windows")
    winps = Path(windir) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    for candidate in ((str(winps) if winps.is_file() else None), PWSH):
        if not candidate:
            continue
        key = os.path.normcase(os.path.abspath(candidate))
        if key in seen:
            continue
        seen.add(key)
        engines.append(candidate)
    return engines


def test_ps1_scripts_parse() -> None:
    engines = _parse_engines()
    assert engines, "PowerShell 없음"
    for name in (
        "deploy.ps1",
        "deploy_mcp.ps1",
        "_load_env.ps1",
        "preflight.ps1",
        "setup_alerts.ps1",
        "share_drive.ps1",
        "backfill.ps1",
    ):
        path = ROOT / "scripts" / name
        script = (
            "$e = $null; $t = $null; "
            "[void][System.Management.Automation.Language.Parser]::ParseFile("
            f"'{path}', [ref]$t, [ref]$e); "
            "if ($e) { $e | ForEach-Object { $_.ToString() }; exit 1 }"
        )
        for engine in engines:
            p = subprocess.run(
                [engine, "-NoProfile", "-Command", script],
                cwd=str(ROOT),
                text=True,
                capture_output=True,
                check=False,
                encoding="utf-8",
                errors="replace",
            )
            assert p.returncode == 0, f"{name} via {engine}: {p.stdout}{p.stderr}"


def test_mcp_deploy_attaches_cloud_management_metadata() -> None:
    source = (ROOT / "scripts" / "deploy_mcp.ps1").read_text(encoding="utf-8")
    assert "gcp-rag.dev/department-metadata" in source
    assert "--update-labels=$managementLabels" in source
    assert "--update-annotations=$managementAnnotation" in source
    assert "$env:DEPLOYMENT_METADATA_B64" in source
    assert "gcp-rag-schema=v2" in source


def test_full_deploy_accepts_real_values() -> None:
    p = _run(VALID, "Require-FullDeployEnv")
    assert p.returncode == 0, p.stderr or p.stdout


def test_full_deploy_rejects_empty_sync_folder_ids() -> None:
    env = dict(VALID)
    env["SYNC_FOLDER_IDS"] = ""
    p = _run(env, "Require-FullDeployEnv")
    assert p.returncode != 0
    assert "SYNC_FOLDER_IDS" in (p.stderr + p.stdout)


def test_full_deploy_rejects_example_env() -> None:
    p = _run(
        {
            "GCP_PROJECT_ID": "your-project-id",
            "GCS_HWP_ORIGINAL_BUCKET": "rag-hwp-original-{project}",
            "GCS_SOURCE_BUCKET": "rag-source-{project}",
            "RAG_CORPUS_NAME": "projects/{project}/locations/asia-northeast3/ragCorpora/{corpus-id}",
            "DRIVE_IDS": "shared-drive-id-1,shared-drive-id-2",
            "MCP_API_KEY": "change-me-to-a-long-random-secret",
        },
        "Require-FullDeployEnv",
    )
    assert p.returncode != 0
    err = p.stderr + p.stdout
    assert "GCP_PROJECT_ID" in err
    assert "example value" in err


def test_cs_staff_name_is_not_student() -> None:
    p = _run(
        {
            "GCP_PROJECT_ID": "my-proj",
            "RAG_CORPUS_NAME": VALID["RAG_CORPUS_NAME"],
            "MCP_API_KEY": VALID["MCP_API_KEY"],
            "MCP_SERVICE_NAME_STAFF": "rag-mcp-cs-staff",
            "MCP_SERVICE_NAME_STUDENT": "rag-mcp-cs-student",
        },
        "Require-McpDeployEnv",
    )
    assert p.returncode == 0, p.stderr or p.stdout


def test_student_mcp_deploy_requires_student_corpus() -> None:
    p = _run(
        {
            "GCP_PROJECT_ID": "my-proj",
            "RAG_CORPUS_NAME": VALID["RAG_CORPUS_NAME"],
            "MCP_API_KEY": "a-long-random-student-secret",
            "MCP_API_KEY_STAFF": VALID["MCP_API_KEY"],
            "MCP_SERVICE_NAME_STUDENT": "rag-mcp-cs-student",
            "MCP_AUDIENCE": "student",
            "RAG_CORPUS_NAME_STUDENT": "",
        },
        "Require-McpDeployEnv",
    )
    assert p.returncode != 0
    assert "RAG_CORPUS_NAME_STUDENT" in (p.stderr + p.stdout)


def test_student_mcp_deploy_rejects_staff_corpus() -> None:
    p = _run(
        {
            "GCP_PROJECT_ID": "my-proj",
            "RAG_CORPUS_NAME": VALID["RAG_CORPUS_NAME"],
            "MCP_API_KEY": "a-long-random-student-secret",
            "MCP_API_KEY_STAFF": VALID["MCP_API_KEY"],
            "MCP_SERVICE_NAME_STUDENT": "rag-mcp-cs-student",
            "MCP_AUDIENCE": "student",
            "RAG_CORPUS_NAME_STUDENT": "projects/my-proj/locations/asia-northeast3/ragCorpora/stu",
        },
        "Require-McpDeployEnv",
    )
    assert p.returncode != 0
    assert "must use RAG_CORPUS_NAME_STUDENT" in (p.stderr + p.stdout)


def test_student_mcp_deploy_accepts_swapped_corpus() -> None:
    student = "projects/my-proj/locations/asia-northeast3/ragCorpora/stu"
    p = _run(
        {
            "GCP_PROJECT_ID": "my-proj",
            "RAG_CORPUS_NAME": student,
            "MCP_API_KEY": "a-long-random-student-secret",
            "MCP_API_KEY_STAFF": VALID["MCP_API_KEY"],
            "MCP_SERVICE_NAME_STUDENT": "rag-mcp-cs-student",
            "MCP_AUDIENCE": "student",
            "RAG_CORPUS_NAME_STUDENT": student,
        },
        "Require-McpDeployEnv",
    )
    assert p.returncode == 0, p.stderr or p.stdout


def test_student_split_requires_both() -> None:
    env = dict(VALID)
    env["RAG_CORPUS_NAME_STUDENT"] = "projects/my-proj/locations/asia-northeast3/ragCorpora/stu"
    p = _run(env, "Require-FullDeployEnv")
    assert p.returncode != 0
    assert "STUDENT_FOLDER_IDS" in (p.stderr + p.stdout)


def test_preflight_config_accepts_valid() -> None:
    p = _run(VALID, "Assert-PreflightConfig", source_preflight=True)
    assert p.returncode == 0, p.stderr or p.stdout


def test_preflight_rejects_same_buckets() -> None:
    env = dict(VALID)
    env["GCS_SOURCE_BUCKET"] = env["GCS_HWP_ORIGINAL_BUCKET"]
    p = _run(env, "Assert-PreflightConfig", source_preflight=True)
    assert p.returncode != 0
    assert "must differ" in (p.stderr + p.stdout)


def test_preflight_rejects_bad_corpus_path() -> None:
    env = dict(VALID)
    env["RAG_CORPUS_NAME"] = "not-a-corpus-path"
    p = _run(env, "Assert-PreflightConfig", source_preflight=True)
    assert p.returncode != 0
    assert "RAG_CORPUS_NAME" in (p.stderr + p.stdout)


def test_preflight_fallback_requires_processor() -> None:
    env = dict(VALID)
    env["QG_MODE"] = "fallback"
    p = _run(env, "Assert-PreflightConfig", source_preflight=True)
    assert p.returncode != 0
    assert "DOCAI_PROCESSOR_ID" in (p.stderr + p.stdout)


@pytest.mark.parametrize(
    ("allow_unauth", "flag"),
    [("true", "--allow-unauthenticated"), ("false", "--no-allow-unauthenticated")],
)
def test_mcp_auth_arg_splats_as_one_whole_flag(allow_unauth: str, flag: str) -> None:
    lines = [
        ln.strip()
        for ln in (ROOT / "scripts" / "deploy_mcp.ps1").read_text(encoding="utf-8").splitlines()
        if ln.strip().startswith("$authArgs") or ln.strip().startswith("if ($ALLOW_UNAUTH")
    ]
    assert lines, "deploy_mcp.ps1 에서 $authArgs 를 못 찾았다"
    script = (
        "$ErrorActionPreference = 'Stop'; "
        f"$ALLOW_UNAUTH = '{allow_unauth}'; "
        + "; ".join(lines)
        + "; function Show-Args { $args -join '|' }; "
        "Write-Output (Show-Args --region=x @authArgs --memory=1Gi)"
    )
    p = subprocess.run(
        [PWSH, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
        cwd=str(ROOT),
        text=True,
        capture_output=True,
        check=False,
    )
    assert p.returncode == 0, p.stderr or p.stdout
    assert p.stdout.strip() == f"--region=x|{flag}|--memory=1Gi"


def test_no_script_reads_dotenv() -> None:
    for path in sorted((ROOT / "scripts").glob("*.ps1")):
        text = path.read_text(encoding="utf-8")
        assert "Load-Dotenv" not in text, path.name
    for path in sorted((ROOT / "scripts").glob("*.py")):
        text = path.read_text(encoding="utf-8")
        assert "load_dotenv" not in text, path.name
    assert not (ROOT / ".env.example").exists(), ".env.example 이 남아 있다"


def test_base_config_returns_a_real_department_code() -> None:
    script = (
        "$ErrorActionPreference = 'Stop'; "
        f". '{LOADER}'; "
        "function Get-DepartmentCodes { $c = @('cs'); return $c }; "
        "function Set-DeptConfig { param($DeptCode, $AudienceName) "
        "  if ($DeptCode -ne 'cs') { throw \"학과 코드가 잘렸다: $DeptCode\" } }; "
        "function Get-AllDriveIds { return @('D1') }; "
        "$codes = @(Set-BaseDeployConfig); "
        "Write-Output \"$($codes[0])|$($env:DRIVE_IDS)\""
    )
    p = subprocess.run(
        [PWSH, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
        cwd=str(ROOT),
        text=True,
        capture_output=True,
        check=False,
    )
    assert p.returncode == 0, p.stderr or p.stdout
    assert p.stdout.strip().endswith("D1")
    assert p.stdout.strip().startswith("cs"), p.stdout


def test_deploy_passes_switches_to_deploy_mcp_by_name() -> None:
    text = (ROOT / "scripts" / "deploy.ps1").read_text(encoding="utf-8")
    lines = [
        ln.strip()
        for ln in text.splitlines()
        if "$mcpArgs" in ln and not ln.strip().startswith("&")
    ]
    assert lines, "deploy.ps1 에서 $mcpArgs 를 못 찾았다"

    script = (
        "$ErrorActionPreference = 'Stop'; "
        "$ShowKeys = $true; "
        + "; ".join(lines)
        + "; "
        "function Fake-DeployMcp { param("
        "  [string]$Dept,"
        "  [ValidateSet('staff','student')][string]$Audience = 'staff',"
        "  [switch]$All, [switch]$SkipBuild, [switch]$ShowKeys"
        ") Write-Output \"dept=[$Dept] aud=[$Audience] all=$All skip=$SkipBuild show=$ShowKeys\" }; "
        "Fake-DeployMcp @mcpArgs"
    )
    p = subprocess.run(
        [PWSH, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
        cwd=str(ROOT),
        text=True,
        capture_output=True,
        check=False,
    )
    assert p.returncode == 0, p.stderr or p.stdout
    out = p.stdout.strip()
    assert out == "dept=[] aud=[staff] all=True skip=True show=True", out


def test_reuse_images_skips_build_only_for_existing_image() -> None:
    text = (ROOT / "scripts" / "deploy.ps1").read_text(encoding="utf-8")
    start = text.index("function Ensure-Image {")
    end = text.index("Ensure-Image -Name", start)
    body = text[start:end]

    script = (
        "[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false); "
        "$ErrorActionPreference = 'Stop'; "
        "$IMAGE_BASE = 'reg/base'; $ReuseExisting = $true; $script:calls = @(); "
        "function gcloud { $script:calls += ($args -join ' '); "
        "  if ($args[0] -eq 'artifacts') { "
        "    if ($args -like '*parser*') { $global:LASTEXITCODE = 0; 'sha256:abc' } "
        "    else { $global:LASTEXITCODE = 1; '' } "
        "  } else { $global:LASTEXITCODE = 0 } }; "
        "function Assert-LastExit { if ($LASTEXITCODE -ne 0) { throw \"gcloud exit\" } }; "
        + body
        + "; Ensure-Image -Name 'parser' -Config 'cloudbuild.parser.yaml'"
        "; Ensure-Image -Name 'sync' -Config 'cloudbuild.sync.yaml'"
        "; $script:calls | ForEach-Object { Write-Output \"CALL $_\" }"
    )
    p = subprocess.run(
        [PWSH, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
        cwd=str(ROOT),
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )
    assert p.returncode == 0, p.stderr or p.stdout
    builds = [
        ln for ln in p.stdout.splitlines() if ln.startswith("CALL ") and "builds submit" in ln
    ]
    assert len(builds) == 1, p.stdout
    assert "cloudbuild.sync.yaml" in builds[0], builds[0]
    assert "reg/base/sync:latest" in builds[0], builds[0]


def test_deploy_ps1_builds_everything_without_reuse_switch() -> None:
    text = (ROOT / "scripts" / "deploy.ps1").read_text(encoding="utf-8")
    start = text.index("function Ensure-Image {")
    end = text.index("Ensure-Image -Name", start)
    body = text[start:end]

    script = (
        "$ErrorActionPreference = 'Stop'; "
        "$IMAGE_BASE = 'reg/base'; $script:calls = @(); "
        "function gcloud { $script:calls += ($args -join ' '); $global:LASTEXITCODE = 0 }; "
        "function Assert-LastExit { if ($LASTEXITCODE -ne 0) { throw \"gcloud exit\" } }; "
        + body
        + "; Ensure-Image -Name 'parser' -Config 'cloudbuild.parser.yaml'"
        "; $script:calls | ForEach-Object { Write-Output \"CALL $_\" }"
    )
    p = subprocess.run(
        [PWSH, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
        cwd=str(ROOT),
        text=True,
        capture_output=True,
        check=False,
    )
    assert p.returncode == 0, p.stderr or p.stdout
    calls = [ln for ln in p.stdout.splitlines() if ln.startswith("CALL ")]
    assert len(calls) == 1, p.stdout
    assert "builds submit" in calls[0], calls[0]


@pytest.mark.parametrize(
    ("exists", "expected"),
    [(True, "True"), (False, "False")],
)
def test_reuse_existing_skips_only_already_deployed_services(exists: bool, expected: str) -> None:
    text = (ROOT / "scripts" / "deploy.ps1").read_text(encoding="utf-8")
    start = text.index("function Test-SkipService {")
    end = text.index('if (-not (Test-SkipService', start)
    body = text[start:end]

    script = (
        "[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false); "
        "$ErrorActionPreference = 'Stop'; "
        "$REGION = 'asia-northeast3'; $PROJECT_ID = 'p'; $ReuseExisting = $true; "
        f"function gcloud {{ $global:LASTEXITCODE = {0 if exists else 1}; 'https://x' }}; "
        + body
        + "; Write-Output \"SKIP=$(Test-SkipService -Name 'rag-parser')\""
    )
    p = subprocess.run(
        [PWSH, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
        cwd=str(ROOT),
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )
    assert p.returncode == 0, p.stderr or p.stdout
    assert f"SKIP={expected}" in p.stdout, p.stdout


def test_without_reuse_switch_services_always_deploy() -> None:
    text = (ROOT / "scripts" / "deploy.ps1").read_text(encoding="utf-8")
    start = text.index("function Test-SkipService {")
    end = text.index('if (-not (Test-SkipService', start)
    body = text[start:end]

    script = (
        "$ErrorActionPreference = 'Stop'; "
        "$REGION = 'r'; $PROJECT_ID = 'p'; $script:calls = 0; "
        "function gcloud { $script:calls++; $global:LASTEXITCODE = 0 }; "
        + body
        + "; Write-Output \"SKIP=$(Test-SkipService -Name 'rag-sync') CALLS=$script:calls\""
    )
    p = subprocess.run(
        [PWSH, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
        cwd=str(ROOT),
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )
    assert p.returncode == 0, p.stderr or p.stdout
    assert "SKIP=False CALLS=0" in p.stdout, p.stdout


def test_wait_for_gcloud_success_retries_until_propagation() -> None:
    text = (ROOT / "scripts" / "deploy.ps1").read_text(encoding="utf-8")
    start = text.index("function Wait-ForGcloudSuccess {")
    end = text.index("$PROJECT_ID = $env:GCP_PROJECT_ID", start)
    body = text[start:end]

    script = (
        "[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false); "
        "$ErrorActionPreference = 'Stop'; "
        + body
        + "; $script:n = 0; "
        "$flaky = { $script:n++; $global:LASTEXITCODE = $(if ($script:n -ge 3) { 0 } else { 1 }) }; "
        "$ok = Wait-ForGcloudSuccess -Label 'x' -Waits @(0,0,0,0) -Action $flaky; "
        "Write-Output \"FLAKY ok=$ok tries=$script:n\"; "
        "$script:m = 0; "
        "$always = { $script:m++; $global:LASTEXITCODE = 1 }; "
        "$bad = Wait-ForGcloudSuccess -Label 'y' -Waits @(0,0) -Action $always; "
        "Write-Output \"ALWAYS ok=$bad tries=$script:m\"; "
        "$script:k = 0; "
        "$good = { $script:k++; $global:LASTEXITCODE = 0 }; "
        "$fast = Wait-ForGcloudSuccess -Label 'z' -Waits @(0,0,0) -Action $good; "
        "Write-Output \"GOOD ok=$fast tries=$script:k\""
    )
    p = subprocess.run(
        [PWSH, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
        cwd=str(ROOT),
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )
    assert p.returncode == 0, p.stderr or p.stdout
    assert "FLAKY ok=True tries=3" in p.stdout, p.stdout
    assert "ALWAYS ok=False tries=2" in p.stdout, p.stdout
    assert "GOOD ok=True tries=1" in p.stdout, p.stdout


def test_scheduler_sa_binding_is_retried_not_asserted_once() -> None:
    text = (ROOT / "scripts" / "deploy.ps1").read_text(encoding="utf-8")
    start = text.index("== Ensure Scheduler SA / App Engine ==")
    section = text[start:text.index("== Cloud Scheduler (00:00 Asia/Seoul) ==", start)]
    assert "Wait-ForGcloudSuccess -Label \"Scheduler SA\"" in section
    assert "Wait-ForGcloudSuccess -Label \"Scheduler SA IAM 바인딩\"" in section
    binding = section[section.index("add-iam-policy-binding"):]
    assert "Assert-LastExit" not in binding.split("if (-not $saBound)")[0]


def _deploy_targets_script(call: str) -> str:
    text = (ROOT / "scripts" / "deploy_mcp.ps1").read_text(encoding="utf-8")
    start = text.index("function Get-DeployTargets {")
    end = text.index("$targets = @(Get-DeployTargets", start)
    return (
        "[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false); "
        "$ErrorActionPreference = 'Stop'; "
        "function Get-DepartmentCodes { @('cs', 'ee') }; "
        "function Get-DepartmentAudiences { param([string]$DeptCode) "
        "  if ($DeptCode -eq 'cs') { @('staff', 'student') } else { @('staff') } }; "
        + text[start:end]
        + "; " + call
    )


def _run_targets(call: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [PWSH, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", _deploy_targets_script(call)],
        cwd=str(ROOT),
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )


def test_dept_without_audience_deploys_every_configured_scope() -> None:
    p = _run_targets(
        "(Get-DeployTargets -Dept 'cs' -Audience 'staff' -All $false -AudienceExplicit $false)"
        " | ForEach-Object { Write-Output \"T $($_.Dept)/$($_.Audience)\" }"
    )
    assert p.returncode == 0, p.stderr or p.stdout
    assert [ln for ln in p.stdout.splitlines() if ln.startswith("T ")] == [
        "T cs/staff",
        "T cs/student",
    ], p.stdout


def test_explicit_audience_deploys_only_that_one() -> None:
    p = _run_targets(
        "(Get-DeployTargets -Dept 'cs' -Audience 'student' -All $false -AudienceExplicit $true)"
        " | ForEach-Object { Write-Output \"T $($_.Dept)/$($_.Audience)\" }"
    )
    assert p.returncode == 0, p.stderr or p.stdout
    assert [ln for ln in p.stdout.splitlines() if ln.startswith("T ")] == ["T cs/student"], p.stdout


def test_all_covers_every_department_and_scope() -> None:
    p = _run_targets(
        "(Get-DeployTargets -Dept '' -Audience 'staff' -All $true -AudienceExplicit $false)"
        " | ForEach-Object { Write-Output \"T $($_.Dept)/$($_.Audience)\" }"
    )
    assert p.returncode == 0, p.stderr or p.stdout
    assert [ln for ln in p.stdout.splitlines() if ln.startswith("T ")] == [
        "T cs/staff",
        "T cs/student",
        "T ee/staff",
    ], p.stdout


def test_no_target_argument_is_rejected() -> None:
    p = _run_targets(
        "try { Get-DeployTargets -Dept '' -Audience 'staff' -All $false -AudienceExplicit $false }"
        " catch { Write-Output 'THROWN' }"
    )
    assert p.returncode == 0, p.stderr or p.stdout
    assert "THROWN" in p.stdout, p.stdout
