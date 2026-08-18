"""배포 스크립트 필수값 검사 (bash). GCP 호출 없음."""

from __future__ import annotations

import os
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _find_bash() -> str | None:
    git = Path(r"C:\Program Files\Git\bin\bash.exe")
    if git.is_file():
        return str(git)
    return shutil.which("bash")


BASH = _find_bash()
pytestmark = pytest.mark.skipif(not BASH, reason="bash 없음 — 배포 env 검사는 Git Bash/CI에서 돈다")

LOADER = ROOT / "scripts" / "_load_env.sh"

VALID = {
    "GCP_PROJECT_ID": "my-proj",
    "GCS_RAW_BUCKET": "rag-raw-my-proj",
    "GCS_NORMALIZED_BUCKET": "rag-normalized-my-proj",
    "RAG_CORPUS_NAME": "projects/my-proj/locations/asia-northeast3/ragCorpora/abc",
    "DRIVE_IDS": "0ABrealDriveId",
    "MCP_API_KEY": "a-long-random-staff-secret",
}


def _run(exports: dict[str, str], fn: str) -> subprocess.CompletedProcess[str]:
    # 값은 따옴표로 감싼다. 공백·특수문자 방지.
    body = "\n".join(f"export {k}='{v}'" for k, v in exports.items())
    script = textwrap.dedent(
        f"""
        set -euo pipefail
        {body}
        . '{LOADER.as_posix()}'
        {fn}
        """
    )
    env = {
        "PATH": os.environ.get("PATH", ""),
        "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
        "WINDIR": os.environ.get("WINDIR", ""),
    }
    return subprocess.run(
        [BASH, "-c", script],
        cwd=str(ROOT),
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def test_full_deploy_accepts_real_values() -> None:
    p = _run(VALID, "require_full_deploy_env")
    assert p.returncode == 0, p.stderr


def test_full_deploy_rejects_example_env() -> None:
    p = _run(
        {
            "GCP_PROJECT_ID": "your-project-id",
            "GCS_RAW_BUCKET": "rag-raw-{project}",
            "GCS_NORMALIZED_BUCKET": "rag-normalized-{project}",
            "RAG_CORPUS_NAME": "projects/{project}/locations/asia-northeast3/ragCorpora/{corpus-id}",
            "DRIVE_IDS": "shared-drive-id-1,shared-drive-id-2",
            "MCP_API_KEY": "change-me-to-a-long-random-secret",
        },
        "require_full_deploy_env",
    )
    assert p.returncode != 0
    err = p.stderr
    assert "GCP_PROJECT_ID" in err
    assert "GCS_RAW_BUCKET" in err
    assert "RAG_CORPUS_NAME" in err
    assert "DRIVE_IDS" in err
    assert "MCP_API_KEY" in err
    assert "example value" in err


def test_full_deploy_lists_all_missing() -> None:
    p = _run({}, "require_full_deploy_env")
    assert p.returncode != 0
    for name in (
        "GCP_PROJECT_ID",
        "GCS_RAW_BUCKET",
        "GCS_NORMALIZED_BUCKET",
        "RAG_CORPUS_NAME",
        "DRIVE_IDS",
        "MCP_API_KEY",
    ):
        assert name in p.stderr


def test_student_split_requires_both() -> None:
    env = dict(VALID)
    env["RAG_CORPUS_NAME_STUDENT"] = "projects/my-proj/locations/asia-northeast3/ragCorpora/stu"
    p = _run(env, "require_full_deploy_env")
    assert p.returncode != 0
    assert "STUDENT_FOLDER_IDS" in p.stderr


def test_student_keys_must_differ() -> None:
    env = dict(VALID)
    env["RAG_CORPUS_NAME_STUDENT"] = "projects/my-proj/locations/asia-northeast3/ragCorpora/stu"
    env["STUDENT_FOLDER_IDS"] = "folderStudent"
    env["MCP_API_KEY_STUDENT"] = env["MCP_API_KEY"]
    p = _run(env, "require_full_deploy_env")
    assert p.returncode != 0
    assert "MCP_API_KEY_STUDENT" in p.stderr


def test_mcp_deploy_accepts_without_buckets() -> None:
    p = _run(
        {
            "GCP_PROJECT_ID": "my-proj",
            "RAG_CORPUS_NAME": VALID["RAG_CORPUS_NAME"],
            "MCP_API_KEY": VALID["MCP_API_KEY"],
        },
        "require_mcp_deploy_env",
    )
    assert p.returncode == 0, p.stderr


def test_student_mcp_rejects_staff_key() -> None:
    p = _run(
        {
            "GCP_PROJECT_ID": "my-proj",
            "RAG_CORPUS_NAME": VALID["RAG_CORPUS_NAME"],
            "MCP_API_KEY": VALID["MCP_API_KEY"],
            "MCP_API_KEY_STAFF": VALID["MCP_API_KEY"],
            "MCP_SERVICE_NAME": "rag-mcp-student",
        },
        "require_mcp_deploy_env",
    )
    assert p.returncode != 0
    assert "MCP_API_KEY_STAFF" in p.stderr


def test_audience_student_uses_named_service() -> None:
    p = _run(
        {
            "GCP_PROJECT_ID": "my-proj",
            "RAG_CORPUS_NAME": VALID["RAG_CORPUS_NAME"],
            "MCP_API_KEY": VALID["MCP_API_KEY"],
            "MCP_API_KEY_STAFF": VALID["MCP_API_KEY"],
            "MCP_SERVICE_NAME_STAFF": "rag-mcp-cs-staff",
            "MCP_SERVICE_NAME_STUDENT": "rag-mcp-cs-student",
            "MCP_AUDIENCE": "student",
        },
        "require_mcp_deploy_env",
    )
    assert p.returncode != 0
    assert "MCP_API_KEY_STAFF" in p.stderr


def test_cs_staff_name_is_not_student() -> None:
    p = _run(
        {
            "GCP_PROJECT_ID": "my-proj",
            "RAG_CORPUS_NAME": VALID["RAG_CORPUS_NAME"],
            "MCP_API_KEY": VALID["MCP_API_KEY"],
            "MCP_SERVICE_NAME_STAFF": "rag-mcp-cs-staff",
            "MCP_SERVICE_NAME_STUDENT": "rag-mcp-cs-student",
        },
        "require_mcp_deploy_env",
    )
    assert p.returncode == 0, p.stderr
