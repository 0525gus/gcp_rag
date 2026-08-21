"""학과 배포 설정(config/*.yaml) 해석. GCP 호출 없음.

여기서 지키는 것은 두 가지다.
  1. yaml 이 내는 환경변수가 서비스가 읽는 이름·형식과 맞는가
  2. 커밋되는 설정에 시크릿이 섞이지 않는가

특히 2번은 규칙을 사람 기억이 아니라 테스트에 맡기기 위한 것이다 —
config/ 는 커밋되므로 키가 한 번 들어가면 이력에 영구히 남는다.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import dept_config
from scripts.dept_config import _fmt, build_env, list_departments

# --- 형식 ---------------------------------------------------------------

def test_bool_is_lowercase():
    """ALLOW_UNAUTH 가 "True" 로 나가면 Get-EnvOr 비교(-ne "true")가 뒤집힌다.

    그 순간 MCP 가 --no-allow-unauthenticated 로 올라가 FactChat 커넥터가
    조용히 못 붙는다.
    """
    assert _fmt(True) == "true"
    assert _fmt(False) == "false"


def test_list_is_comma_joined():
    # SYNC_FOLDER_IDS 등은 저장소 관례가 쉼표 구분이다.
    assert _fmt(["a", "b"]) == "a,b"
    assert _fmt(["a", "  ", "b"]) == "a,b"


# --- 대상별 코퍼스 선택 ---------------------------------------------------

def test_audience_selects_corpus():
    staff = build_env("cs", "staff")
    student = build_env("cs", "student")

    assert staff["RAG_CORPUS_NAME"] != student["RAG_CORPUS_NAME"]
    assert student["RAG_CORPUS_NAME"] == student["RAG_CORPUS_NAME_STUDENT"]
    # 교직원 대상에도 학생 코퍼스를 실어 보낸다 — Require-McpDeployEnv 가
    # "학생 코퍼스가 교직원 서비스에 실렸다" 를 이 값으로 대조한다.
    assert staff["RAG_CORPUS_NAME_STUDENT"] == student["RAG_CORPUS_NAME"]
    assert staff["RAG_CORPUS_NAME"] != staff["RAG_CORPUS_NAME_STUDENT"]


def test_derived_names_follow_convention():
    env = build_env("cs", "student")
    assert env["MCP_SERVICE_NAME"] == "rag-mcp-cs-student"
    assert env["MCP_AUDIENCE"] == "student"
    assert env["DEPT_CODE"] == "cs"


def test_common_values_are_present():
    env = build_env("cs", "staff")
    # --set-env-vars 가 env 를 통째로 치환하므로 이 중 하나라도 빠지면
    # 배포된 서비스에서 그 값이 사라진다.
    for key in (
        "GCP_PROJECT_ID",
        "GCP_REGION",
        "FIRESTORE_DATABASE",
        "DOC_STATE_COLLECTION",
        "TOP_K_DEFAULT",
        "ALLOW_UNAUTH",
    ):
        assert env.get(key), f"{key} 가 비었다"


# --- 잘못된 설정 거부 -----------------------------------------------------

def _write_dept(tmp_path: Path, monkeypatch, body: dict) -> None:
    body.setdefault("keys", {"staff": "S" * 30, "student": "T" * 30})
    dept_dir = tmp_path / "departments"
    dept_dir.mkdir()
    (dept_dir / "x.yaml").write_text(
        yaml.safe_dump(body, allow_unicode=True), encoding="utf-8"
    )
    (tmp_path / "common.yaml").write_text("GCP_PROJECT_ID: p\n", encoding="utf-8")
    monkeypatch.setattr(dept_config, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(dept_config, "DEPT_DIR", dept_dir)


def test_identical_corpora_rejected(tmp_path, monkeypatch):
    """staff 와 student 코퍼스가 같으면 학생이 교직원 전량을 검색하게 된다."""
    _write_dept(tmp_path, monkeypatch, {"corpora": {"staff": "c/1", "student": "c/1"}})
    with pytest.raises(SystemExit, match="같다"):
        build_env("x", "student")


def test_missing_corpus_rejected(tmp_path, monkeypatch):
    _write_dept(tmp_path, monkeypatch, {"corpora": {"staff": "c/1"}})
    with pytest.raises(SystemExit, match="corpora"):
        build_env("x", "staff")


def test_unknown_audience_rejected():
    with pytest.raises(SystemExit):
        build_env("cs", "faculty")


# --- 전 학과 회귀 ---------------------------------------------------------

def test_every_department_builds():
    """학과 yaml 이 깨졌으면 배포 때가 아니라 여기서 걸린다."""
    codes = list_departments()
    assert codes, "config/departments 에 학과 yaml 이 없다"
    for code in codes:
        for audience in ("staff", "student"):
            env = build_env(code, audience)
            assert env["MCP_SERVICE_NAME"] == f"rag-mcp-{code}-{audience}"


def test_all_departments_use_distinct_corpora():
    """학과끼리 코퍼스를 공유하면 한 학과가 남의 자료를 검색해준다."""
    seen: dict[str, str] = {}
    for code in list_departments():
        for audience in ("staff", "student"):
            corpus = build_env(code, audience)["RAG_CORPUS_NAME"]
            owner = f"{code}/{audience}"
            assert corpus not in seen, f"코퍼스 중복: {owner} 와 {seen[corpus]}"
            seen[corpus] = owner


# --- 시크릿 유출 방지 -----------------------------------------------------

def test_real_department_files_are_gitignored():
    """학과 yaml 은 코퍼스 ID 와 MCP 키를 담는다. 커밋되면 회전해도 이력에 남는다.

    .gitignore 규칙이 사라지면 다음 커밋에 키가 실려 나가므로 여기서 막는다.
    """
    rules = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "config/departments/*.yaml" in [r.strip() for r in rules]


def test_department_buckets_override_common():
    """학과 버킷을 적으면 common.yaml 의 공용 버킷을 덮는다."""
    env = build_env("cs", "staff")
    assert env["GCS_HWP_ORIGINAL_BUCKET"]
    assert env["GCS_SOURCE_BUCKET"]
    assert env["GCS_HWP_ORIGINAL_BUCKET"] != env["GCS_SOURCE_BUCKET"]


def test_half_configured_buckets_rejected(tmp_path, monkeypatch):
    """한쪽만 적으면 원본은 학과 버킷, 산출물은 공용으로 갈라진다."""
    _write_dept(
        tmp_path,
        monkeypatch,
        {
            "corpora": {"staff": "c/1", "student": "c/2"},
            "buckets": {"source": "only-one"},
        },
    )
    with pytest.raises(SystemExit, match="짝이어야"):
        build_env("x", "staff")


def test_omitted_buckets_inherit_common(tmp_path, monkeypatch):
    """생략하면 common.yaml 값을 그대로 쓴다(기존 학과 이관용)."""
    _write_dept(tmp_path, monkeypatch, {"corpora": {"staff": "c/1", "student": "c/2"}})
    (tmp_path / "common.yaml").write_text(
        yaml.safe_dump({"GCP_PROJECT_ID": "p", "GCS_SOURCE_BUCKET": "shared-src"}),
        encoding="utf-8",
    )
    assert build_env("x", "staff")["GCS_SOURCE_BUCKET"] == "shared-src"


def test_template_carries_no_real_values():
    """커밋되는 것은 템플릿뿐이고, 거기엔 실값이 없어야 한다."""
    # 파일명에 묶지 않는다 — cs.yaml.example / dept.yaml.example 무엇이든 된다.
    tpls = sorted((ROOT / "config" / "departments").glob("*.yaml.example"))
    assert tpls, "템플릿이 없으면 새 학과를 만들 방법이 사라진다"
    assert len(tpls) == 1, f"템플릿이 여러 개다: {[t.name for t in tpls]}"
    data = yaml.safe_load(tpls[0].read_text(encoding="utf-8")) or {}
    for audience in ("staff", "student"):
        assert data["keys"][audience] in dept_config.PLACEHOLDER_KEYS
        assert "CHANGE_ME" in data["corpora"][audience]
    # 버킷도 플레이스홀더여야 한다 — 실이름이 템플릿에 남으면 새 학과가
    # 남의 버킷을 그대로 물려받는다.
    for key in ("hwpOriginal", "source"):
        assert data["buckets"][key].isupper() or "DEPT" in data["buckets"][key]


def test_placeholder_key_is_rejected(tmp_path, monkeypatch):
    """템플릿을 복사만 하고 안 채운 채 배포하면 키가 공개된 것과 같다."""
    _write_dept(
        tmp_path,
        monkeypatch,
        {
            "corpora": {"staff": "c/1", "student": "c/2"},
            "keys": {"staff": "CHANGE_ME", "student": "x" * 30},
        },
    )
    with pytest.raises(SystemExit, match="템플릿"):
        build_env("x", "staff")


def test_shared_key_between_audiences_is_rejected(tmp_path, monkeypatch):
    """같으면 학생 키로 교직원 코퍼스가 열린다."""
    _write_dept(
        tmp_path,
        monkeypatch,
        {
            "corpora": {"staff": "c/1", "student": "c/2"},
            "keys": {"staff": "z" * 30, "student": "z" * 30},
        },
    )
    with pytest.raises(SystemExit, match="키가 같다"):
        build_env("x", "student")


def test_weak_key_warns_on_stderr(tmp_path, monkeypatch, capsys):
    """막지는 않는다 — 판단은 사람이 한다. 다만 조용히 넘어가지도 않는다.

    stdout 은 KEY=VALUE 전용이라 경고가 거기 섞이면 배포 스크립트가 파싱한다.
    """
    _write_dept(
        tmp_path,
        monkeypatch,
        {
            "corpora": {"staff": "c/1", "student": "c/2"},
            "keys": {"staff": "TEST_STAFF", "student": "y" * 30},
        },
    )
    env = build_env("x", "staff")
    captured = capsys.readouterr()
    assert env["MCP_API_KEY"] == "TEST_STAFF"
    assert "약하다" in captured.err
    assert "약하다" not in captured.out


def test_all_departments_use_distinct_keys():
    """학과끼리 키가 겹치면 한 키로 남의 코퍼스가 열린다."""
    seen: dict[str, str] = {}
    for code in list_departments():
        for audience in ("staff", "student"):
            key = build_env(code, audience)["MCP_API_KEY"]
            owner = f"{code}/{audience}"
            assert key not in seen, f"키 중복: {owner} 와 {seen[key]}"
            seen[key] = owner
