from __future__ import annotations

import inspect
import re
from dataclasses import fields
from pathlib import Path

import pytest
import yaml

from scripts import common_config
from shared import config as shared_config
from shared.config import Settings

ROOT = Path(__file__).resolve().parents[1]
REQUIRED_ENV = {
    "GCP_PROJECT_ID": "p",
    "GCS_HWP_ORIGINAL_BUCKET": "hwp",
    "GCS_SOURCE_BUCKET": "src",
    "RAG_CORPUS_NAME": "corpus",
}


def _env_names_read_by_settings() -> set[str]:
    return set(re.findall(r'"([A-Z][A-Z0-9_]+)"', inspect.getsource(Settings.from_env)))


def test_env_names_follow_field_names():
    from_env_fields = {
        f.name.upper() for f in fields(Settings) if f.name not in shared_config._FIELDS_NOT_FROM_ENV
    }
    expected = from_env_fields | {"DEPARTMENTS_JSON", "DEPARTMENTS_REQUIRED"}
    assert _env_names_read_by_settings() == expected, (
        "env 이름이 필드 이름의 대문자가 아니면 env_defaults 가 그 기본값을 배포에 싣지 못한다"
    )


def test_from_env_defaults_are_the_field_defaults(monkeypatch):
    for name in _env_names_read_by_settings():
        monkeypatch.delenv(name, raising=False)
    for name, value in REQUIRED_ENV.items():
        monkeypatch.setenv(name, value)

    from_env = Settings.from_env()
    declared = Settings(
        gcp_project_id="p",
        gcs_hwp_original_bucket="hwp",
        gcs_source_bucket="src",
        rag_corpus_name="corpus",
    )

    assert from_env == declared


def test_operating_defaults_match_what_the_workflow_needs():
    defaults = common_config.defaults()
    assert defaults["CLOUD_TASKS_ENABLED"] is True, (
        "Workflow 는 /sync/index-gcs-async 만 부른다. 꺼져 있으면 409 로 색인이 전부 실패한다"
    )
    assert defaults["RAG_MAPPING_WRITE_ENABLED"] is True
    assert defaults["RAG_MAPPING_READ_ENABLED"] is True
    assert defaults["TOP_K_DEFAULT"] == 7
    assert defaults["SEARCH_FETCH_MULTIPLIER"] == 9


def test_resolve_fills_defaults_and_keeps_explicit_values():
    resolved = common_config.resolve({
        "GCP_PROJECT_ID": "p",
        "GCP_REGION": "us-central1",
        "SEARCH_REWRITE_ENABLED": True,
        "CLOUD_TASKS_ENABLED": False,
        "RAG_METADATA_BUCKET": "",
        "QG_MODE": None,
    })

    assert resolved["GCP_PROJECT_ID"] == "p"
    assert resolved["SEARCH_REWRITE_ENABLED"] is True
    assert resolved["CLOUD_TASKS_ENABLED"] is False
    assert resolved["QG_MODE"] == "log"
    assert resolved["RAG_METADATA_BUCKET"] == ""
    assert resolved["TASK_QUEUE_LOCATION"] == "us-central1"
    assert common_config.resolve({"GCP_REGION": "us-central1", "TASK_QUEUE_LOCATION": "us-east1"})[
        "TASK_QUEUE_LOCATION"
    ] == "us-east1"


def test_resolve_is_idempotent():
    once = common_config.resolve({"GCP_PROJECT_ID": "p", "GCP_REGION": "europe-west4"})
    assert common_config.resolve(once) == once


def _repo_common() -> dict:
    return yaml.safe_load((ROOT / "config" / "common.yaml").read_text(encoding="utf-8"))


def test_repo_common_yaml_has_only_known_keys():
    unknown = sorted(set(_repo_common()) - common_config.known_keys())
    assert not unknown, f"아무도 읽지 않는 키(오타면 배포값이 그대로다): {unknown}"


def test_repo_common_yaml_does_not_copy_defaults():
    common = _repo_common()
    defaults = common_config.defaults(common.get("GCP_REGION"))
    copies = sorted(
        key for key, value in common.items()
        if key not in common_config.BOOTSTRAP_KEYS and key in defaults and value == defaults[key]
    )
    assert not copies, f"기본값을 복사해 두면 코드 기본값이 바뀌어도 옛 값에 남는다. 지울 키: {copies}"


DEFAULTED_PS_READ = re.compile(r'Get-EnvOr\s+([A-Z_][A-Z0-9_]*)\s+("[^"]+"|\$\w+)')


@pytest.mark.parametrize("script", sorted((ROOT / "scripts").glob("*.ps1")), ids=lambda p: p.name)
def test_powershell_does_not_carry_its_own_defaults(script: Path):
    defaulted = set(common_config.defaults())
    text = script.read_text(encoding="utf-8-sig")
    offenders = [m.group(0) for m in DEFAULTED_PS_READ.finditer(text) if m.group(1) in defaulted]
    assert not offenders, f"기본값은 dept_config.py 가 env 로 채운다. $env:X 로 바로 읽을 것: {offenders}"
