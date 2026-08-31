"""학과 배포 설정(config/*.yaml) 해석. GCP 호출 없음.

여기서 지키는 것은 두 가지다.
  1. yaml 이 내는 환경변수가 서비스가 읽는 이름·형식과 맞는가
  2. 커밋되는 설정에 시크릿이 섞이지 않는가

특히 2번은 규칙을 사람 기억이 아니라 테스트에 맡기기 위한 것이다 —
config/ 는 커밋되므로 키가 한 번 들어가면 이력에 영구히 남는다.
"""

from __future__ import annotations

import base64
import json
import re
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import dept_config
from scripts.dept_config import _fmt, build_env, list_departments

# 학과 yaml 은 **커밋되지 않는다**(키가 들어간다). 그래서 새 클론과 CI 에는
# 원래 없다 — 실파일이 있어야만 도는 검사는 건너뛴다. 있으면 엄격히 본다.
requires_dept_files = pytest.mark.skipif(
    not list_departments(),
    reason="config/departments/*.yaml 없음 (gitignore 대상)",
)


def _any_dept() -> str:
    """실재하는 학과 하나. 학과 코드를 테스트에 박아 두지 않는다."""
    return list_departments()[0]

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

@requires_dept_files
def test_audience_selects_corpus():
    dept = _any_dept()
    staff = build_env(dept, "staff")
    student = build_env(dept, "student")

    assert staff["RAG_CORPUS_NAME"] != student["RAG_CORPUS_NAME"]
    assert student["RAG_CORPUS_NAME"] == student["RAG_CORPUS_NAME_STUDENT"]
    # 교직원 대상에도 학생 코퍼스를 실어 보낸다 — Require-McpDeployEnv 가
    # "학생 코퍼스가 교직원 서비스에 실렸다" 를 이 값으로 대조한다.
    assert staff["RAG_CORPUS_NAME_STUDENT"] == student["RAG_CORPUS_NAME"]
    assert staff["RAG_CORPUS_NAME"] != staff["RAG_CORPUS_NAME_STUDENT"]


@requires_dept_files
def test_derived_names_follow_convention():
    dept = _any_dept()
    env = build_env(dept, "student")
    assert env["MCP_SERVICE_NAME"] == f"rag-mcp-{dept}-student"
    assert env["MCP_AUDIENCE"] == "student"
    assert env["DEPT_CODE"] == dept


@requires_dept_files
def test_common_values_are_present():
    env = build_env(_any_dept(), "staff")
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
    split_enabled = bool((body.get("corpora") or {}).get("student"))
    default_keys = {"staff": "S" * 30}
    if split_enabled:
        default_keys["student"] = "T" * 30
        body.setdefault("drive", {}).setdefault("studentFolderIds", ["F_STUDENT"])
    body.setdefault("keys", default_keys)
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
    _write_dept(tmp_path, monkeypatch, {"corpora": {"student": "c/2"}})
    with pytest.raises(SystemExit, match="corpora"):
        build_env("x", "staff")


@requires_dept_files
def test_unknown_audience_rejected():
    with pytest.raises(SystemExit):
        build_env(_any_dept(), "faculty")


# --- 전 학과 회귀 ---------------------------------------------------------

@requires_dept_files
def test_every_department_builds():
    """학과 yaml 이 깨졌으면 배포 때가 아니라 여기서 걸린다."""
    for code in list_departments():
        for audience in dept_config.configured_audiences(code):
            env = build_env(code, audience)
            assert env["MCP_SERVICE_NAME"] == f"rag-mcp-{code}-{audience}"


def test_all_departments_use_distinct_corpora():
    """학과끼리 코퍼스를 공유하면 한 학과가 남의 자료를 검색해준다."""
    seen: dict[str, str] = {}
    for code in list_departments():
        for audience in dept_config.configured_audiences(code):
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


def test_real_department_files_are_excluded_from_cloud_build():
    """Git 제외만으로는 부족하다. gcloud submit도 비밀 YAML을 업로드하면 안 된다."""
    rules = (ROOT / ".gcloudignore").read_text(encoding="utf-8").splitlines()
    assert "config/departments/*.yaml" in [r.strip() for r in rules]


@requires_dept_files
def test_department_buckets_override_common():
    """학과 버킷을 적으면 common.yaml 의 공용 버킷을 덮는다."""
    env = build_env(_any_dept(), "staff")
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
        for audience in dept_config.configured_audiences(code):
            key = build_env(code, audience)["MCP_API_KEY"]
            owner = f"{code}/{audience}"
            assert key not in seen, f"키 중복: {owner} 와 {seen[key]}"
            seen[key] = owner


# --- rag-sync 학과 맵 (DEPARTMENTS_JSON) ----------------------------------
#
# 맵을 켜기 전까지 sync 의 라우팅 코드는 아무 일도 안 한다(`for_drive` 가 자기
# 자신을 반환). 켠 뒤에는 즉시 활성화되므로, **여기서 틀리면 그날 배포에서
# 남의 학과 코퍼스로 문서가 들어간다.** 그래서 값을 확인하는 것으로 끝내지
# 않고 sync 가 실제로 쓰는 파서로 되읽어 대조한다.

from shared.config import Settings, _departments_from_json


def _write_depts(tmp_path: Path, monkeypatch, depts: dict[str, dict]) -> None:
    dept_dir = tmp_path / "departments"
    dept_dir.mkdir()
    for i, (code, body) in enumerate(depts.items()):
        body.setdefault("keys", {"staff": f"S{i}" * 20, "student": f"T{i}" * 20})
        (dept_dir / f"{code}.yaml").write_text(
            yaml.safe_dump(body, allow_unicode=True), encoding="utf-8"
        )
    (tmp_path / "common.yaml").write_text("GCP_PROJECT_ID: p\n", encoding="utf-8")
    monkeypatch.setattr(dept_config, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(dept_config, "DEPT_DIR", dept_dir)


def _dept_body(code: str, **over) -> dict:
    body = {
        "corpora": {"staff": f"c-{code}-staff", "student": f"c-{code}-student"},
        "buckets": {"hwpOriginal": f"b-{code}-hwp", "source": f"b-{code}-src"},
        "drive": {
            "driveIds": [f"D_{code.upper()}"],
            "syncFolderIds": [f"F_{code.upper()}_A", f"F_{code.upper()}_B"],
            "studentFolderIds": [f"F_{code.upper()}_A"],
        },
    }
    body.update(over)
    return body


def test_map_round_trips_through_the_parser_sync_uses(tmp_path, monkeypatch):
    """맵 생성 결과를 sync 의 파서로 되읽어 학과 수·코퍼스·드라이브를 대조한다.

    `_departments_from_json` 은 모르는 키를 조용히 무시하고 깨진 JSON 도
    빈 맵으로 폴백한다 — 즉 **오타는 예외가 아니라 무동작으로 나타난다.**
    그래서 파싱이 됐는지가 아니라 값이 실제로 실렸는지를 본다.
    """
    _write_depts(tmp_path, monkeypatch, {"cs": _dept_body("cs"), "ee": _dept_body("ee")})

    depts = _departments_from_json(dept_config.departments_json())

    assert [d.code for d in depts] == ["cs", "ee"]
    cs = {d.code: d for d in depts}["cs"]
    assert cs.drive_ids == ("D_CS",)
    assert cs.staff_corpus == "c-cs-staff"
    assert cs.student_corpus == "c-cs-student"
    assert cs.hwp_bucket == "b-cs-hwp"
    assert cs.source_bucket == "b-cs-src"
    assert cs.sync_folder_ids == ("F_CS_A", "F_CS_B")
    assert cs.student_folder_ids == ("F_CS_A",)


def test_map_routes_each_drive_to_its_own_corpus(tmp_path, monkeypatch):
    """맵을 실은 Settings 가 드라이브별로 다른 코퍼스를 준다 — 켜는 목적 그 자체."""
    _write_depts(tmp_path, monkeypatch, {"cs": _dept_body("cs"), "ee": _dept_body("ee")})
    s = Settings(
        gcp_project_id="p",
        rag_corpus_name="corpus-base",
        departments=_departments_from_json(dept_config.departments_json()),
    )
    assert s.for_drive("D_CS").rag_corpus_name == "c-cs-staff"
    assert s.for_drive("D_EE").rag_corpus_name == "c-ee-staff"


def test_map_carries_no_secret(tmp_path, monkeypatch):
    """맵은 Cloud Run env 로 나간다 — describe 권한이 있으면 누구나 본다.

    build_env 는 MCP 키를 함께 내보내므로, 필드 표가 늘어날 때 키가 딸려 들어갈
    여지가 상시 존재한다. 값으로도 대조한다(필드명만 보면 못 잡는다).
    """
    _write_depts(tmp_path, monkeypatch, {"cs": _dept_body("cs")})
    raw = dept_config.departments_json()
    key = build_env("cs", "staff")["MCP_API_KEY"]
    assert key and key not in raw
    assert build_env("cs", "student")["MCP_API_KEY"] not in raw
    for name in ("MCP_API_KEY", "key", "Key"):
        assert name not in raw


def test_map_is_one_line_without_spaces(tmp_path, monkeypatch):
    """`--set-env-vars` 인자에 실려 명령줄로 나간다.

    공백이나 줄바꿈이 섞이면 값의 안전이 셸의 인자 분리 규칙에 걸리게 된다.
    """
    _write_depts(tmp_path, monkeypatch, {"cs": _dept_body("cs", name="컴퓨터 공학과")})
    raw = dept_config.departments_json()
    assert not any(c.isspace() for c in raw), raw
    assert raw.isascii(), raw


def test_map_rejects_department_without_drive_ids(tmp_path, monkeypatch):
    """맵이 비지 않은 이상 맵에 없는 드라이브는 통째로 건너뛴다.

    driveIds 를 빠뜨리면 그 학과 문서가 영영 처리되지 않는다 — 조용히.
    """
    body = _dept_body("cs")
    body["drive"].pop("driveIds")
    _write_depts(tmp_path, monkeypatch, {"cs": body})
    with pytest.raises(SystemExit, match="driveIds"):
        dept_config.departments_json()


def test_map_rejects_department_without_sync_folders(tmp_path, monkeypatch):
    """빠뜨리면 for_drive 가 공용 기본값을 남겨 그 학과가 남의 폴더를 훑는다."""
    body = _dept_body("cs")
    body["drive"].pop("syncFolderIds")
    _write_depts(tmp_path, monkeypatch, {"cs": body})
    with pytest.raises(SystemExit, match="syncFolderIds"):
        dept_config.departments_json()


def test_map_rejects_drive_shared_by_two_departments(tmp_path, monkeypatch):
    """department_for_drive 는 첫 일치를 준다 — 겹치면 뒤 학과가 앞 학과 코퍼스로 간다."""
    ee = _dept_body("ee")
    ee["drive"]["driveIds"] = ["D_CS"]
    _write_depts(tmp_path, monkeypatch, {"cs": _dept_body("cs"), "ee": ee})
    with pytest.raises(SystemExit, match="driveId 중복"):
        dept_config.departments_json()


def test_map_field_names_are_the_ones_sync_reads():
    """필드명을 바꾸면 파싱은 그대로 성공하고 그 값만 사라진다.

    `_departments_from_json` 은 모르는 키를 무시하므로 오타가 예외로 드러나지
    않는다. 이름 표를 파서 쪽 소스와 직접 대조해 둔다.
    """
    src = (ROOT / "shared" / "config.py").read_text(encoding="utf-8")
    for field, _env_key, _is_list in dept_config._MAP_FIELDS:
        assert f'd.get("{field}")' in src, f"sync 가 안 읽는 필드: {field}"


# --- 배포 스크립트와의 계약 ------------------------------------------------

def test_powershell_clears_every_key_dept_config_emits(tmp_path, monkeypatch):
    """`$ConfigKeys` 는 dept_config 가 내보내는 이름을 전부 덮어야 한다.

    배포 스크립트는 학과를 순회하며 이 목록만 비우고 다시 채운다. 여기서 빠진
    키는 **앞 학과 값이 그대로 남는다** — `-All` 로 20개를 돌리면 코퍼스나 키가
    조용히 섞이고, 배포는 성공한 것처럼 끝난다.

    `.env` 시절의 '빈 셸 변수 vs 파일' 우선순위 규칙이 있던 자리다. 원본이
    하나가 된 지금 남은 위험은 우선순위가 아니라 **학과 간 잔류**다.
    """
    ps = (ROOT / "scripts" / "_load_env.ps1").read_text(encoding="utf-8")
    block = ps[ps.index("$ConfigKeys = @("):]
    block = block[: block.index(")\n")]
    cleared = set(re.findall(r'"([A-Z_][A-Z0-9_]*)"', block))

    # 학과 yaml 의 모든 갈래를 채운 최대 집합 + 실제 common.yaml 키.
    _write_depts(tmp_path, monkeypatch, {"x": _dept_body("x", name="이름")})
    emitted = set(build_env("x", "staff")) | set(build_env("x", "student"))
    common = yaml.safe_load(
        (ROOT / "config" / "common.yaml").read_text(encoding="utf-8")
    )
    emitted |= set(common)

    missing = sorted(emitted - cleared)
    assert not missing, f"$ConfigKeys 에서 빠졌다 — 학과 사이로 샌다: {missing}"


def test_both_audience_keys_are_exported(tmp_path, monkeypatch):
    """대상이 아닌 쪽 키도 나와야 한다. 배포 검사가 **짝을 대조**하기 때문이다.

    `Require-McpDeployEnv` 는 STAFF 를, `Require-FullDeployEnv` 는 STUDENT 를 본다.
    한쪽만 내보내면 그 검사가 멀쩡한 설정을 거부한다 — 실제로 STUDENT 를
    빠뜨려서 `deploy.ps1` 이 첫 검증에서 죽었다(preflight 가 잡았다).
    """
    _write_depts(tmp_path, monkeypatch, {"x": _dept_body("x")})
    for audience in ("staff", "student"):
        env = build_env("x", audience)
        assert env["MCP_API_KEY_STAFF"], f"{audience} 배포에 STAFF 키가 없다"
        assert env["MCP_API_KEY_STUDENT"], f"{audience} 배포에 STUDENT 키가 없다"
        assert env["MCP_API_KEY_STAFF"] != env["MCP_API_KEY_STUDENT"]
        # 이번 대상 키는 그 대상 것이어야 한다.
        assert env["MCP_API_KEY"] == env[f"MCP_API_KEY_{audience.upper()}"]


def test_deployment_metadata_is_complete_and_contains_no_secret(tmp_path, monkeypatch):
    """Cloud Run 주석은 YAML 대체용 설정만 담고 MCP 키는 절대 담지 않는다."""
    body = _dept_body(
        "x",
        name="엑스학과",
        minInstances={"staff": 1, "student": 0},
    )
    _write_depts(tmp_path, monkeypatch, {"x": body})

    staff_env = build_env("x", "staff")
    student_env = build_env("x", "student")
    metadata = json.loads(
        base64.urlsafe_b64decode(staff_env["DEPLOYMENT_METADATA_B64"]).decode("utf-8")
    )

    assert metadata == {
        "audience": "staff",
        "buckets": {"hwpOriginal": "b-x-hwp", "source": "b-x-src"},
        "code": "x",
        "corpora": {"staff": "c-x-staff", "student": "c-x-student"},
        "corpusMode": "split",
        "drive": {
            "driveIds": ["D_X"],
            "studentFolderIds": ["F_X_A"],
            "syncFolderIds": ["F_X_A", "F_X_B"],
        },
        "managedBy": "gcp-rag",
        "minInstances": {"staff": 1, "student": 0},
        "name": "엑스학과",
        "schemaVersion": 1,
    }
    encoded_text = base64.urlsafe_b64decode(
        staff_env["DEPLOYMENT_METADATA_B64"]
    ).decode("utf-8")
    assert staff_env["MCP_API_KEY"] not in encoded_text
    assert student_env["MCP_API_KEY"] not in encoded_text
    assert "MCP_API_KEY" not in encoded_text
    student_metadata = json.loads(
        base64.urlsafe_b64decode(student_env["DEPLOYMENT_METADATA_B64"]).decode("utf-8")
    )
    assert student_metadata["audience"] == "student"


def test_deploy_checks_only_read_keys_dept_config_exports(tmp_path, monkeypatch):
    """PS 검사가 참조하는 `MCP_API_KEY*` 를 dept_config 가 전부 내보내야 한다.

    이름 표를 눈으로 맞추는 대신 소스끼리 대조한다 — 위 버그가 정확히 '한쪽만
    적었다' 였고, 그건 배포를 돌려 보기 전에는 드러나지 않았다.
    """
    ps = (ROOT / "scripts" / "_load_env.ps1").read_text(encoding="utf-8")
    needed = set(re.findall(r"\$env:(MCP_API_KEY_[A-Z]+)", ps))
    assert needed, "_load_env.ps1 에서 키 참조를 못 찾았다"

    _write_depts(tmp_path, monkeypatch, {"x": _dept_body("x")})
    exported = set(build_env("x", "staff"))
    missing = sorted(needed - exported)
    assert not missing, f"PS 검사가 보는데 dept_config 가 안 내보낸다: {missing}"
