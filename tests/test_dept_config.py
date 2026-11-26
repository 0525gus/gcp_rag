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
from scripts.dept_config import _fmt, build_env

def test_bool_is_lowercase():
    assert _fmt(True) == "true"
    assert _fmt(False) == "false"


def test_list_is_comma_joined():
    assert _fmt(["a", "b"]) == "a,b"
    assert _fmt(["a", "  ", "b"]) == "a,b"


def _write_dept(tmp_path: Path, monkeypatch, body: dict) -> None:
    split_enabled = bool((body.get("corpora") or {}).get("student"))
    default_keys = {"staff": "S" * 30}
    if split_enabled:
        default_keys["student"] = "T" * 30
        body.setdefault("drive", {}).setdefault("studentFolderIds", ["F_STUDENT"])
    body.setdefault("keys", default_keys)
    (tmp_path / "common.yaml").write_text("GCP_PROJECT_ID: p\n", encoding="utf-8")
    monkeypatch.setattr(dept_config, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(dept_config, "registry_configs", lambda: {"x": body})


def test_identical_corpora_rejected(tmp_path, monkeypatch):
    _write_dept(tmp_path, monkeypatch, {"corpora": {"staff": "c/1", "student": "c/1"}})
    with pytest.raises(SystemExit, match="같다"):
        build_env("x", "student")


def test_missing_corpus_rejected(tmp_path, monkeypatch):
    _write_dept(tmp_path, monkeypatch, {"corpora": {"student": "c/2"}})
    with pytest.raises(SystemExit, match="corpora"):
        build_env("x", "staff")


def test_half_configured_buckets_rejected(tmp_path, monkeypatch):
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
    _write_dept(tmp_path, monkeypatch, {"corpora": {"staff": "c/1", "student": "c/2"}})
    (tmp_path / "common.yaml").write_text(
        yaml.safe_dump({"GCP_PROJECT_ID": "p", "GCS_SOURCE_BUCKET": "shared-src"}),
        encoding="utf-8",
    )
    assert build_env("x", "staff")["GCS_SOURCE_BUCKET"] == "shared-src"


def test_placeholder_key_is_rejected(tmp_path, monkeypatch):
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


from shared.config import Settings, _departments_from_json


def _write_depts(tmp_path: Path, monkeypatch, depts: dict[str, dict]) -> None:
    for i, (code, body) in enumerate(depts.items()):
        body.setdefault("keys", {"staff": f"S{i}" * 20, "student": f"T{i}" * 20})
    (tmp_path / "common.yaml").write_text("GCP_PROJECT_ID: p\n", encoding="utf-8")
    monkeypatch.setattr(dept_config, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(dept_config, "registry_configs", lambda: depts)


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
    _write_depts(tmp_path, monkeypatch, {"cs": _dept_body("cs"), "ee": _dept_body("ee")})
    s = Settings(
        gcp_project_id="p",
        rag_corpus_name="corpus-base",
        departments=_departments_from_json(dept_config.departments_json()),
    )
    assert s.for_drive("D_CS").rag_corpus_name == "c-cs-staff"
    assert s.for_drive("D_EE").rag_corpus_name == "c-ee-staff"


def test_map_carries_no_secret(tmp_path, monkeypatch):
    _write_depts(tmp_path, monkeypatch, {"cs": _dept_body("cs")})
    raw = dept_config.departments_json()
    key = build_env("cs", "staff")["MCP_API_KEY"]
    assert key and key not in raw
    assert build_env("cs", "student")["MCP_API_KEY"] not in raw
    for name in ("MCP_API_KEY", "key", "Key"):
        assert name not in raw


def test_map_is_one_line_without_spaces(tmp_path, monkeypatch):
    _write_depts(tmp_path, monkeypatch, {"cs": _dept_body("cs", name="컴퓨터 공학과")})
    raw = dept_config.departments_json()
    assert not any(c.isspace() for c in raw), raw
    assert raw.isascii(), raw


def test_map_rejects_department_without_drive_ids(tmp_path, monkeypatch):
    body = _dept_body("cs")
    body["drive"].pop("driveIds")
    _write_depts(tmp_path, monkeypatch, {"cs": body})
    with pytest.raises(SystemExit, match="driveIds"):
        dept_config.departments_json()


def test_map_rejects_department_without_sync_folders(tmp_path, monkeypatch):
    body = _dept_body("cs")
    body["drive"].pop("syncFolderIds")
    _write_depts(tmp_path, monkeypatch, {"cs": body})
    with pytest.raises(SystemExit, match="syncFolderIds"):
        dept_config.departments_json()


def test_map_rejects_drive_shared_by_two_departments(tmp_path, monkeypatch):
    ee = _dept_body("ee")
    ee["drive"]["driveIds"] = ["D_CS"]
    _write_depts(tmp_path, monkeypatch, {"cs": _dept_body("cs"), "ee": ee})
    with pytest.raises(SystemExit, match="driveId 중복"):
        dept_config.departments_json()


def test_map_field_names_are_the_ones_sync_reads():
    src = (ROOT / "shared" / "config.py").read_text(encoding="utf-8")
    for field, _env_key, _is_list in dept_config._MAP_FIELDS:
        assert f'd.get("{field}")' in src, f"sync 가 안 읽는 필드: {field}"


def test_every_department_shape_emits_the_same_keys(tmp_path, monkeypatch):
    full = _dept_body("x", name="이름")
    bare = {
        "corpora": {"staff": "c-y-staff"},
        "drive": {"driveIds": ["D_Y"], "syncFolderIds": ["F_Y"]},
        "keys": {"staff": "S" * 30},
    }
    _write_depts(tmp_path, monkeypatch, {"x": full, "y": bare})

    shapes = {
        "split/staff": set(build_env("x", "staff")),
        "split/student": set(build_env("x", "student")),
        "single/staff": set(build_env("y", "staff")),
    }

    reference = shapes["split/staff"]
    for name, keys in shapes.items():
        assert keys == reference, (
            f"{name}: 키 집합이 학과마다 다르면 Set-DeptConfig 가 앞 학과 값을 남긴다. "
            f"빠짐 {sorted(reference - keys)}, 추가 {sorted(keys - reference)}"
        )


def test_load_config_env_leaves_empty_values_unset(tmp_path, monkeypatch):
    _write_depts(tmp_path, monkeypatch, {"y": {
        "corpora": {"staff": "c-y-staff"},
        "drive": {"driveIds": ["D_Y"], "syncFolderIds": ["F_Y"]},
        "keys": {"staff": "S" * 30},
    }})
    monkeypatch.setattr(dept_config.os, "environ", {})

    dept_config.load_config_env("y")

    assert "RAG_CORPUS_NAME_STUDENT" not in dept_config.os.environ, (
        "빈 값을 넣으면 Settings 의 필수값 누락이 빈 문자열로 가려진다"
    )
    assert dept_config.os.environ["RAG_CORPUS_NAME"] == "c-y-staff"


def test_both_audience_keys_are_exported(tmp_path, monkeypatch):
    _write_depts(tmp_path, monkeypatch, {"x": _dept_body("x")})
    for audience in ("staff", "student"):
        env = build_env("x", audience)
        assert env["MCP_API_KEY_STAFF"], f"{audience} 배포에 STAFF 키가 없다"
        assert env["MCP_API_KEY_STUDENT"], f"{audience} 배포에 STUDENT 키가 없다"
        assert env["MCP_API_KEY_STAFF"] != env["MCP_API_KEY_STUDENT"]
        assert env["MCP_API_KEY"] == env[f"MCP_API_KEY_{audience.upper()}"]


def test_deployment_metadata_contains_the_complete_department_yaml(tmp_path, monkeypatch):
    body = _dept_body(
        "x",
        name="엑스학과",
        minInstances={"staff": 1, "student": 0},
    )
    body["futureOption"] = {"enabled": True, "labels": ["a", "b"]}
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
        "schemaVersion": 2,
        "yaml": body,
    }
    encoded_text = base64.urlsafe_b64decode(
        staff_env["DEPLOYMENT_METADATA_B64"]
    ).decode("utf-8")
    assert staff_env["MCP_API_KEY"] in encoded_text
    assert student_env["MCP_API_KEY"] in encoded_text
    student_metadata = json.loads(
        base64.urlsafe_b64decode(student_env["DEPLOYMENT_METADATA_B64"]).decode("utf-8")
    )
    assert student_metadata["audience"] == "student"


def test_deploy_checks_only_read_keys_dept_config_exports(tmp_path, monkeypatch):
    ps = (ROOT / "scripts" / "_load_env.ps1").read_text(encoding="utf-8")
    needed = set(re.findall(r"\$env:(MCP_API_KEY_[A-Z]+)", ps))
    assert needed, "_load_env.ps1 에서 키 참조를 못 찾았다"

    _write_depts(tmp_path, monkeypatch, {"x": _dept_body("x")})
    exported = set(build_env("x", "staff"))
    missing = sorted(needed - exported)
    assert not missing, f"PS 검사가 보는데 dept_config 가 안 내보낸다: {missing}"
