from __future__ import annotations

import copy
import hashlib
import json
import re
import secrets
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml
from fastapi import HTTPException, Request

from scripts import common_config, gcp_cli

ROOT = Path(__file__).resolve().parents[2]


CONFIG_DIR = ROOT / "config"


SYNC_SERVICE = "rag-sync"


DEPT_CODE_RE = re.compile(r"^[a-z][a-z0-9-]{1,19}$")


CORPUS_RE = re.compile(
    r"^projects/([^/]+)/locations/([^/]+)/ragCorpora/([^/]+)$"
)


BUCKET_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,61}[a-z0-9]$")


DRIVE_FILE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{10,}$")


PROJECT_RE = re.compile(r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$")


REGION_RE = re.compile(r"^[a-z]+-[a-z]+[0-9]$")


SECRET_FIELDS = {"keys", "MCP_API_KEY", "MCP_API_KEY_STAFF", "MCP_API_KEY_STUDENT"}


STATUS_ORDER = {"FAIL": 4, "WARN": 3, "CHECKING": 2, "UNKNOWN": 1, "OK": 0, "SKIP": 0}


SETUP_REGIONS = [
    {"id": "asia-northeast3", "label": "서울"},
    {"id": "asia-northeast1", "label": "도쿄"},
    {"id": "asia-southeast1", "label": "싱가포르"},
    {"id": "us-central1", "label": "아이오와"},
    {"id": "us-east4", "label": "북버지니아"},
    {"id": "europe-west4", "label": "네덜란드"},
]


_SESSION_NONCE = secrets.token_urlsafe(24)


_LATEST: dict[str, dict[str, Any]] = {}


def _read_yaml(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise TypeError("YAML 최상위는 mapping이어야 합니다.")
    return data


def _common() -> dict[str, Any]:
    return common_config.load_common(CONFIG_DIR)


def _normalise_ids(value: Any) -> list[str]:
    raw: list[str] = []
    if isinstance(value, str):
        raw = re.split(r"[,\r\n]+", value)
    elif isinstance(value, list):
        for item in value:
            raw.extend(re.split(r"[,\r\n]+", str(item)))
    out: list[str] = []
    seen: set[str] = set()
    for item in raw:
        cleaned = item.strip()
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            out.append(cleaned)
    return out


def _mapping_revision(value: dict[str, Any]) -> str:
    raw = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _field_error(errors: dict[str, list[str]], field: str, message: str) -> None:
    errors.setdefault(field, []).append(message)


def _public_cloud_yaml(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _public_cloud_yaml(child)
            for key, child in value.items()
            if key not in SECRET_FIELDS
            and "token" not in key.lower()
            and "secret" not in key.lower()
        }
    if isinstance(value, list):
        return [_public_cloud_yaml(item) for item in value]
    return copy.deepcopy(value)


def _check(layer: str, name: str, status: str, detail: str, **extra: Any) -> dict[str, Any]:
    item = {"layer": layer, "name": name, "status": status, "detail": detail[:500]}
    item.update(extra)
    return item


def _overall(checks: list[dict[str, Any]]) -> str:
    if not checks:
        return "UNKNOWN"
    status = max((item["status"] for item in checks), key=lambda value: STATUS_ORDER[value])
    if status == "SKIP":
        return "WARN"
    return status


def _missing_resource_output(output: str) -> bool:
    lowered = str(output or "").lower()
    return any(
        marker in lowered
        for marker in ("not_found", "not found", "does not exist", "no longer exists")
    )


def _firestore_database_name() -> str:
    return str(_common()["FIRESTORE_DATABASE"])


def _firestore_collection_names() -> dict[str, str]:
    common = _common()
    return {
        "docState": str(common["DOC_STATE_COLLECTION"]),
        "syncTokens": str(common["SYNC_TOKEN_COLLECTION"]),
        "dlq": str(common["DLQ_COLLECTION"]),
        "splitQueue": str(common["SPLIT_QUEUE_COLLECTION"]),
    }


def _firestore_client(project: str):
    """활성 gcloud 계정으로 운영 상태를 관리할 Firestore 클라이언트를 만든다.

    ADC는 gcloud 활성 계정과 별도로 저장된다. 로컬 콘솔이 서로 다른 두 계정을
    섞으면 리소스 조회는 되지만 Firestore 삭제만 403으로 실패할 수 있으므로,
    다른 관리 API와 동일하게 활성 gcloud 계정의 토큰을 사용한다.
    """
    try:
        from google.cloud import firestore
        from google.oauth2.credentials import Credentials
    except ImportError as exc:  # pragma: no cover - 설치 환경에 따라 갈린다
        raise RuntimeError(
            "google-cloud-firestore 가 설치되어 있지 않습니다. "
            "`pip install google-cloud-firestore` 후 다시 시도하세요."
        ) from exc
    return firestore.Client(
        project=project,
        database=_firestore_database_name(),
        credentials=Credentials(token=gcp_cli.access_token()),
    )


def _require_local_session(request: Request) -> None:
    origin = request.headers.get("origin")
    if origin and (urlparse(origin).hostname not in {"127.0.0.1", "localhost", "testserver"}):
        raise HTTPException(status_code=403, detail="invalid origin")
    if request.headers.get("x-local-session") != _SESSION_NONCE:
        raise HTTPException(status_code=403, detail="invalid local session")
