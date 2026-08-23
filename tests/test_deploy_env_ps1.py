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
