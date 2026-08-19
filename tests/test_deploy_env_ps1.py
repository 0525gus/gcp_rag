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


def test_ps1_scripts_parse() -> None:
    for name in (
        "deploy.ps1",
        "deploy_mcp.ps1",
        "_load_env.ps1",
        "preflight.ps1",
        "setup_alerts.ps1",
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
