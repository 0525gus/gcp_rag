"""배포 스크립트 필수값 검사 (PowerShell). GCP 호출 없음."""

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
    """CI·비대화형에서는 자동 조치를 하지 않는다.

    deploy.ps1 이 preflight 를 dot-source 하므로, 여기서 Read-Host 가 걸리면
    파이프라인이 멈춘다. PREFLIGHT_NO_FIX / CI 둘 다 차단 스위치여야 한다.
    """
    for guard in ({"PREFLIGHT_NO_FIX": "1"}, {"CI": "true"}):
        p = _run(guard, "if (Test-PreflightCanFix) { exit 1 } else { exit 0 }", source_preflight=True)
        assert p.returncode == 0, f"{guard} 에서 자동 조치가 켜져 있다: {p.stderr or p.stdout}"


def test_preflight_confirm_returns_false_without_prompt() -> None:
    """자동 조치가 꺼져 있으면 묻지 않고 바로 False (블로킹 금지)."""
    p = _run(
        {"PREFLIGHT_NO_FIX": "1"},
        "if (Confirm-PreflightAction 'x') { exit 1 } else { exit 0 }",
        source_preflight=True,
    )
    assert p.returncode == 0, p.stderr or p.stdout


def test_student_split_requires_student_key() -> None:
    """분리가 켜지면 학생 키가 필수다.

    deploy.ps1 이 학생 MCP 까지 올리는데, 키가 없으면 배포 도중 멈춘다.
    이미지 3개를 빌드한 뒤에 죽는 것보다 .env 검사에서 잡는 편이 싸다.
    """
    env = dict(VALID)
    env["SYNC_FOLDER_IDS"] = "f1"
    env["RAG_CORPUS_NAME_STUDENT"] = "projects/my-proj/locations/asia-northeast3/ragCorpora/stu"
    env["STUDENT_FOLDER_IDS"] = "f1"
    p = _run(env, "Require-FullDeployEnv")
    assert p.returncode != 0
    assert "MCP_API_KEY_STUDENT" in (p.stderr + p.stdout)


def test_ps1_scripts_parse() -> None:
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
        p = subprocess.run(
            [PWSH, "-NoProfile", "-Command", script],
            cwd=str(ROOT),
            text=True,
            capture_output=True,
            check=False,
        )
        assert p.returncode == 0, f"{name}: {p.stdout}{p.stderr}"


def test_mcp_deploy_attaches_cloud_management_metadata() -> None:
    source = (ROOT / "scripts" / "deploy_mcp.ps1").read_text(encoding="utf-8")
    assert "gcp-rag.dev/department-metadata" in source
    assert "--update-labels=$managementLabels" in source
    assert "--update-annotations=$managementAnnotation" in source
    assert "$env:DEPLOYMENT_METADATA_B64" in source


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
    """학생 코퍼스가 비면 거부해야 한다.

    deploy_mcp.ps1 은 `if ($env:RAG_CORPUS_NAME_STUDENT)` 로만 코퍼스를 갈아끼운다.
    비어 있으면 그 교체가 통째로 건너뛰어져 RAG_CORPUS_NAME 이 교직원 값 그대로
    남는데, 예전 가드도 같은 -and 단축평가라 검사를 통째로 건너뛰었다 —
    **학생 서비스가 교직원 전량을 검색하는 채로 배포가 통과**했다.
    """
    p = _run(
        {
            "GCP_PROJECT_ID": "my-proj",
            "RAG_CORPUS_NAME": VALID["RAG_CORPUS_NAME"],  # 교직원 코퍼스 그대로
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
    """학생 코퍼스가 있어도 RAG_CORPUS_NAME 이 교직원이면 거부."""
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
    """deploy_mcp.ps1 이 교체를 마친 정상 상태는 통과."""
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
    """MCP 인증 스위치가 통짜 플래그 하나로 넘어가야 한다.

    if 결과를 그냥 담으면 1개짜리 배열이 문자열로 풀리고, @authArgs 스플랫이
    그 문자열을 글자 단위로 넘긴다 — gcloud 가
    "unrecognized arguments: - a l o w ..." 로 죽었다(실측, rag-sync 배포 직후).
    MCP 배포는 deploy_mcp.ps1 한 곳뿐이다(deploy.ps1 이 위임한다).
    """
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
    """설정 원본은 config/ 하나다.

    로더가 남아 있으면 누군가 .env 를 되살려 두 원본이 생긴다 — 그때 어느 쪽이
    이겼는지는 스크립트마다 달라서, 배포된 값과 파일이 조용히 어긋난다.
    """
    for path in sorted((ROOT / "scripts").glob("*.ps1")):
        text = path.read_text(encoding="utf-8")
        assert "Load-Dotenv" not in text, path.name
    for path in sorted((ROOT / "scripts").glob("*.py")):
        text = path.read_text(encoding="utf-8")
        assert "load_dotenv" not in text, path.name
    assert not (ROOT / ".env.example").exists(), ".env.example 이 남아 있다"


def test_base_config_returns_a_real_department_code() -> None:
    """학과가 하나뿐일 때 코드가 첫 글자로 잘리지 않아야 한다.

    PowerShell 의 `return` 은 배열을 풀어서 내보낸다. 학과가 하나면 결과가
    문자열이 되고, `$codes[0]` 은 "cs" 가 아니라 "c" 다 — 그대로 배포하면
    `config/departments/c.yaml` 을 찾다 죽는다. @() 로 감싸는 것이 유일한 방어라
    누가 지우기 쉽다. `@(...)` 표기를 검사하지 않고 **실제 동작**으로 잡는다.
    """
    # 스텁은 dot-source **뒤에** 덮어써야 한다 (로더가 같은 이름을 정의한다).
    # 그 위에서 로더의 진짜 Set-BaseDeployConfig 를 돌린다 — python·config 불필요.
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
    """deploy.ps1 → deploy_mcp.ps1 인자가 **이름으로** 바인딩돼야 한다.

    배열 splat 은 요소를 위치 인자로 넘긴다. `@("-All","-SkipBuild")` 는
    $Dept="-All" / $Audience="-SkipBuild" 가 되어 ValidateSet 에서 죽는다 —
    실배포 중간(parser·sync 배포 후 MCP 직전)에 실제로 터졌다. 스위치를
    넘기려면 해시테이블 splat 이어야 한다.

    표기를 검사하지 않고 **바인딩 결과**로 잡는다. deploy_mcp.ps1 의 param 을
    그대로 흉내낸 함수에 splat 해서 무엇이 어디에 들어갔는지 본다.
    """
    text = (ROOT / "scripts" / "deploy.ps1").read_text(encoding="utf-8")
    # 조립에 관여하는 줄 전부. `if ($ShowKeys) { $mcpArgs[...] }` 처럼 $mcpArgs 로
    # 시작하지 않는 줄이 있어서 startswith 로 거르면 조용히 빠진다.
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
        # deploy_mcp.ps1 의 param 블록과 같은 모양.
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
    # $Dept 가 비어 있어야 한다 — 여기 "-All" 이 들어가면 그게 그 버그다.
    assert out == "dept=[] aud=[staff] all=True skip=True show=True", out


def test_reuse_images_skips_build_only_for_existing_image() -> None:
    """-ReuseExisting: 레지스트리에 있는 이미지는 빌드를 건너뛰고, 없으면 빈다.

    GUI 공통 런타임 배포가 이 스위치로 돈다. 여기가 뒤집히면 (a) 매번 몇 분씩
    다시 굽거나 (b) 이미지가 없는데도 빌드를 건너뛰어 Cloud Run 배포가 죽는다.

    표기가 아니라 **동작**으로 잡는다 — deploy.ps1 의 Ensure-Image 를 그대로
    떼어다 가짜 gcloud 로 돌린다.
    """
    text = (ROOT / "scripts" / "deploy.ps1").read_text(encoding="utf-8")
    start = text.index("function Ensure-Image {")
    end = text.index("Ensure-Image -Name", start)
    body = text[start:end]

    script = (
        # Write-Host 에 한글이 있다. 콘솔 코드페이지를 UTF-8 로 고정하지 않으면
        # 파이썬 쪽 디코딩이 cp949 로 죽는다.
        "[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false); "
        "$ErrorActionPreference = 'Stop'; "
        "$IMAGE_BASE = 'reg/base'; $ReuseExisting = $true; $script:calls = @(); "
        # parser 는 digest 가 있고(재사용), sync 는 조회 실패(빌드).
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
    """스위치 없이 돌리면 조회 없이 그냥 빌드한다(코드 변경 반영 경로)."""
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
    """-ReuseExisting: 이미 떠 있는 Cloud Run 서비스는 다시 배포하지 않는다.

    뒤집히면 (a) 매번 리비전을 새로 만들거나 (b) 서비스가 없는데도 건너뛰어
    Workflow·Scheduler 가 없는 URL 을 부르게 된다.
    """
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
    """스위치가 없으면 조회조차 하지 않는다 — 코드·설정 변경 반영 경로."""
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
    """만든 직후를 GCP 가 모르는 구간을 넘기는 재시도 프리미티브.

    신규 프로젝트 첫 배포에서 두 번 터졌다 — Workflows 서비스 에이전트,
    Scheduler SA IAM 바인딩. 한 번에 성공하면 재시도하지 않고, 계속 실패하면
    거짓 성공을 내지 않아야 한다.
    """
    text = (ROOT / "scripts" / "deploy.ps1").read_text(encoding="utf-8")
    start = text.index("function Wait-ForGcloudSuccess {")
    end = text.index("$PROJECT_ID = $env:GCP_PROJECT_ID", start)
    body = text[start:end]

    script = (
        "[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false); "
        "$ErrorActionPreference = 'Stop'; "
        + body
        + "; $script:n = 0; "
        # 3번째 호출에서야 성공하는 명령.
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
    # 첫 번에 되면 더 부르지 않는다 — 재시도가 배포를 늘리면 안 된다.
    assert "GOOD ok=True tries=1" in p.stdout, p.stdout


def test_scheduler_sa_binding_is_retried_not_asserted_once() -> None:
    """SA 생성 직후의 바인딩은 단발 Assert-LastExit 이면 안 된다.

    create 성공 → 바로 add-iam-policy-binding → "does not exist" 로 죽었다.
    """
    text = (ROOT / "scripts" / "deploy.ps1").read_text(encoding="utf-8")
    start = text.index("== Ensure Scheduler SA / App Engine ==")
    section = text[start:text.index("# ---- 10.", start)]
    assert "Wait-ForGcloudSuccess -Label \"Scheduler SA\"" in section
    assert "Wait-ForGcloudSuccess -Label \"Scheduler SA IAM 바인딩\"" in section
    binding = section[section.index("add-iam-policy-binding"):]
    assert "Assert-LastExit" not in binding.split("if (-not $saBound)")[0]


def _deploy_targets_script(call: str) -> str:
    """deploy_mcp.ps1 의 대상 선택 함수만 떼어내 가짜 설정으로 돌린다."""
    text = (ROOT / "scripts" / "deploy_mcp.ps1").read_text(encoding="utf-8")
    start = text.index("function Get-DeployTargets {")
    end = text.index("$targets = @(Get-DeployTargets", start)
    return (
        "[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false); "
        "$ErrorActionPreference = 'Stop'; "
        "function Get-DepartmentCodes { @('cs', 'ee') }; "
        # cs 는 학생 분리, ee 는 교직원만.
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
    """-Dept 만 주면 그 학과의 MCP 를 전부 올려야 한다.

    staff 하나만 올리던 때, 콘솔은 학생 서비스를 기대해 Ready 확인에서 걸렸고
    ("Ready 상태가 아닌 서비스: rag-mcp-cs-student") 손으로 돌린 사람은 학생
    서비스가 낡은 채 도는 것을 몰랐다.
    """
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

