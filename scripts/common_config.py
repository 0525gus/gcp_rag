from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from shared.config import env_defaults

FILE_NAME = "common.yaml"

BOOTSTRAP_KEYS = ("GCP_PROJECT_ID", "GCP_REGION", "ARTIFACT_REPO", "FIRESTORE_DATABASE")

DEPLOY_DEFAULTS: dict[str, Any] = {
    "ARTIFACT_REPO": "rag-mcp",
    "PARSER_TIMEOUT": 540,
    "PARSER_CONCURRENCY": 4,
    "PARSER_MAX_INSTANCES": 10,
    "SYNC_CONCURRENCY": 4,
    "MCP_CONCURRENCY": 40,
    # 2026-09-11 FactChat 동시 40 대화 검증에서 MCP 오류 0건으로 정한 값.
    "MCP_CPU": 2,
    # FactChat 커넥터는 정적 헤더만 보내므로 Cloud Run IAM 을 열어야 한다.
    # 경계는 MCP_API_KEY 뿐이다 — 키가 새면 코퍼스 전량이 열린다.
    "ALLOW_UNAUTH": True,
}

SHARED_BUCKET_KEYS = ("GCS_HWP_ORIGINAL_BUCKET", "GCS_SOURCE_BUCKET")


def defaults(region: str | None = None) -> dict[str, Any]:
    return {**env_defaults(region), **DEPLOY_DEFAULTS}


def known_keys() -> frozenset[str]:
    overrides = {"GCP_PROJECT_ID", "TASK_SERVICE_ACCOUNT", "SYNC_TASK_BASE_URL"}
    return frozenset(defaults()) | overrides | set(SHARED_BUCKET_KEYS)


def read_common(config_dir: Path) -> dict[str, Any]:
    data = yaml.safe_load((config_dir / FILE_NAME).read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise TypeError(f"{FILE_NAME} 최상위는 mapping이어야 합니다.")
    return data


def resolve(raw: dict[str, Any]) -> dict[str, Any]:
    explicit = {key: value for key, value in raw.items() if value is not None and value != ""}
    region = explicit.get("GCP_REGION")
    return {**defaults(str(region) if region else None), **explicit}


def load_common(config_dir: Path) -> dict[str, Any]:
    return resolve(read_common(config_dir))


def flag(value: Any) -> bool:
    return str(value).strip().lower() in ("1", "true", "yes")
