"""학과 YAML 생성·배포 상태 확인용 로컬 웹 콘솔.

실행:
    python scripts/dept_gui.py

보안 경계:
- 127.0.0.1 에만 bind
- MCP 키는 생성·저장만 하고 API/로그로 반환하지 않음
- status 는 읽기 전용 gcloud 명령만 실행
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
import webbrowser
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode, urlparse

import yaml
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import dept_config

CONFIG_DIR = ROOT / "config"
DEPT_DIR = CONFIG_DIR / "departments"
WEB_DIR = ROOT / "gui" / "public" / "console"

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
COMMON_DEFAULTS: dict[str, Any] = {
    "DOC_STATE_COLLECTION": "doc_state",
    "QG_MODE": "log",
    "INGEST_CONCURRENCY": 8,
    "RAG_DELETE_CONCURRENCY": 1,
    "RAG_DELETE_PACING_SECONDS": 1.1,
    "PARSER_TIMEOUT": 540,
    "PARSER_CONCURRENCY": 4,
    "PARSER_MAX_INSTANCES": 10,
    "SYNC_CONCURRENCY": 4,
    "TOP_K_DEFAULT": 5,
    "SEARCH_FETCH_MULTIPLIER": 3,
    "SEARCH_FETCH_MAX": 60,
    "MCP_CONCURRENCY": 40,
    "ALLOW_UNAUTH": True,
}
SETUP_REGIONS = [
    {"id": "asia-northeast3", "label": "서울"},
    {"id": "asia-northeast1", "label": "도쿄"},
    {"id": "asia-southeast1", "label": "싱가포르"},
    {"id": "us-central1", "label": "아이오와"},
    {"id": "us-east4", "label": "북버지니아"},
    {"id": "europe-west4", "label": "네덜란드"},
]

_SESSION_NONCE = secrets.token_urlsafe(24)
_RUNS: dict[str, dict[str, Any]] = {}
_RUN_LOCK = threading.Lock()
_LATEST: dict[str, dict[str, Any]] = {}
_RUN_TTL_SECONDS = 15 * 60
_RESOURCE_PLANS: dict[str, dict[str, Any]] = {}
_PROVISION_RUNS: dict[str, dict[str, Any]] = {}
_PROVISION_LOCK = threading.Lock()
_PROVISION_TTL_SECONDS = 30 * 60
_COMMON_RESOURCE_PLANS: dict[str, dict[str, Any]] = {}
_COMMON_PROVISION_RUNS: dict[str, dict[str, Any]] = {}
_COMMON_PROVISION_LOCK = threading.Lock()
_MCP_DEPLOY_RUNS: dict[str, dict[str, Any]] = {}
_MCP_DEPLOY_LOCK = threading.Lock()
_MCP_DEPLOY_TTL_SECONDS = 60 * 60
_AUTH_PROCESS: subprocess.Popen[Any] | None = None
_SYNC_AUTH_LOCK = threading.Lock()
_SYNC_AUTH_TOKEN = ""
_SYNC_AUTH_TOKEN_EXPIRES = 0.0

PROVISION_RESOURCE_DEFINITIONS: dict[str, dict[str, str]] = {
    "bucketHwp": {"kind": "bucket", "label": "HWP 원본 버킷"},
    "bucketSource": {"kind": "bucket", "label": "Source 버킷"},
    "corpusStaff": {"kind": "corpus", "label": "교직원 코퍼스"},
    "corpusStudent": {"kind": "corpus", "label": "학생 코퍼스"},
}

# 공통(프로젝트 1회) 리소스. 학과별이 아니라 전 학과가 공유한다.
# 켤 API 를 같이 적는다 — 새 프로젝트는 전부 꺼져 있어 생성이 그냥 실패한다.
COMMON_PROVISION_RESOURCE_DEFINITIONS: dict[str, dict[str, Any]] = {
    "artifactRepo": {
        "kind": "artifactRepo",
        "label": "Artifact Registry (Docker)",
        "service": "artifactregistry.googleapis.com",
        "defaultName": "rag-mcp",
    },
    "firestoreDatabase": {
        "kind": "firestoreDatabase",
        "label": "Firestore (Native)",
        "service": "firestore.googleapis.com",
        "defaultName": "rag-sync-state",
    },
}
# Firestore 는 위치를 나중에 못 바꾼다. 만들기 전에 화면에서 반드시 알린다.
COMMON_PROVISION_WARNINGS: dict[str, str] = {
    "firestoreDatabase": "생성 후 위치를 변경할 수 없습니다. 리전을 확인해 주세요.",
}
ARTIFACT_REPO_RE = re.compile(r"[a-z][a-z0-9._-]{1,62}")
FIRESTORE_DB_RE = re.compile(r"[a-z][a-z0-9-]{1,61}[a-z0-9]")
RAG_EMBEDDING_MODEL = "text-multilingual-embedding-002"
CORPUS_CHAT_MODEL = "gemini-2.5-flash-lite"
CORPUS_CHAT_LOCATION = "global"
DRIVE_FOLDER_LOOKUP_LIMIT = 200
DRIVE_FOLDER_MIME_TYPE = "application/vnd.google-apps.folder"
SYNC_WORKFLOW_NAME = "rag-daily-sync"
SYNC_RUN_HISTORY_LIMIT = 30
SYNC_TOKEN_COLLECTION = "sync_tokens"


def _read_yaml(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise TypeError("YAML 최상위는 mapping이어야 합니다.")
    return data


def _common() -> dict[str, Any]:
    return _read_yaml(CONFIG_DIR / "common.yaml")


def validate_common_candidate(payload: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    errors: dict[str, list[str]] = {}
    project = str(payload.get("projectId") or "").strip()
    region = str(payload.get("region") or "asia-northeast3").strip()
    artifact_repo = str(payload.get("artifactRepo") or "rag-mcp").strip()
    firestore = str(payload.get("firestoreDatabase") or "rag-sync-state").strip()

    if not PROJECT_RE.fullmatch(project):
        _field_error(errors, "projectId", "유효한 GCP 프로젝트 ID를 입력해 주세요.")
    if not REGION_RE.fullmatch(region):
        _field_error(errors, "region", "예: asia-northeast3 형식이어야 합니다.")
    if not re.fullmatch(r"[a-z][a-z0-9._-]{1,62}", artifact_repo):
        _field_error(errors, "artifactRepo", "유효한 Artifact Registry 저장소 이름이 필요합니다.")
    if firestore != "(default)" and not re.fullmatch(r"[a-z][a-z0-9-]{1,61}[a-z0-9]", firestore):
        _field_error(errors, "firestoreDatabase", "유효한 Firestore 데이터베이스 ID가 필요합니다.")

    candidate = {
        "GCP_PROJECT_ID": project,
        "GCP_REGION": region,
        "ARTIFACT_REPO": artifact_repo,
        "FIRESTORE_DATABASE": firestore,
        **COMMON_DEFAULTS,
    }
    return candidate, {"valid": not errors, "fieldErrors": errors}


def create_common_config(candidate: dict[str, Any]) -> Path:
    target = (CONFIG_DIR / "common.yaml").resolve()
    if target.parent != CONFIG_DIR.resolve():
        raise ValueError("설정 디렉터리 밖의 경로는 사용할 수 없습니다.")
    if target.exists():
        raise FileExistsError(target)
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    temp = CONFIG_DIR / f".common.{uuid.uuid4().hex}.tmp"
    try:
        rendered = yaml.safe_dump(candidate, allow_unicode=True, sort_keys=False, width=1000)
        temp.write_text(rendered, encoding="utf-8", newline="\n")
        parsed = _read_yaml(temp)
        if parsed.get("GCP_PROJECT_ID") != candidate["GCP_PROJECT_ID"]:
            raise RuntimeError("작성한 common.yaml의 재검증에 실패했습니다.")
        os.replace(temp, target)
        return target
    finally:
        temp.unlink(missing_ok=True)


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


def _config_revision(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _field_error(errors: dict[str, list[str]], field: str, message: str) -> None:
    errors.setdefault(field, []).append(message)


def _department_drive_conflicts(code: str, drive_ids: list[str]) -> list[dict[str, Any]]:
    """다른 학과 YAML과 겹치는 공유드라이브 ID. 자기 자신은 제외."""
    wanted = set(drive_ids)
    if not wanted:
        return []
    conflicts: list[dict[str, Any]] = []
    for other in dept_config.list_departments():
        if other == code:
            continue
        try:
            other_cfg = _read_yaml(DEPT_DIR / f"{other}.yaml")
        except (OSError, TypeError, UnicodeError, ValueError, yaml.YAMLError):
            continue
        other_ids = _normalise_ids((other_cfg.get("drive") or {}).get("driveIds"))
        duplicate = sorted(wanted & set(other_ids))
        if not duplicate:
            continue
        conflicts.append(
            {
                "code": other,
                "name": str(other_cfg.get("name") or other),
                "driveIds": duplicate,
            }
        )
    return conflicts


def _drive_conflict_response(conflicts: list[dict[str, Any]]) -> JSONResponse:
    owners = ", ".join(f"{item['name']}({item['code']})" for item in conflicts)
    return JSONResponse(
        {
            "error": {
                "code": "DRIVE_ID_CONFLICT",
                "message": f"다른 학과와 공유드라이브 ID가 겹칩니다: {owners}",
                "driveConflicts": conflicts,
            }
        },
        status_code=409,
    )


def _unacked_drive_conflicts(
    payload: dict[str, Any], result: dict[str, Any]
) -> JSONResponse | None:
    conflicts = result.get("driveConflicts") or []
    if not conflicts or payload.get("allowDuplicateDriveIds"):
        return None
    return _drive_conflict_response(conflicts)


def _has_secret_input(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    for key, child in value.items():
        if key in SECRET_FIELDS or "token" in key.lower() or "secret" in key.lower():
            return True
        if isinstance(child, dict) and _has_secret_input(child):
            return True
    return False


def department_code_availability(code: str, *, current_code: str = "") -> dict[str, Any]:
    """학과 코드 형식과 기존 YAML 충돌을 한 번에 확인한다."""
    normalised = str(code or "").strip().lower()
    current = str(current_code or "").strip().lower()
    if not DEPT_CODE_RE.fullmatch(normalised):
        return {
            "code": normalised,
            "available": False,
            "reason": "영문 소문자로 시작하는 2~20자 코드여야 합니다.",
        }
    if current and current != normalised:
        return {
            "code": normalised,
            "available": False,
            "reason": "수정 중에는 학과 코드를 변경할 수 없습니다.",
        }
    target = DEPT_DIR / f"{normalised}.yaml"
    if target.exists() and current != normalised:
        return {
            "code": normalised,
            "available": False,
            "reason": f"{normalised} 코드는 이미 다른 학과에서 사용 중입니다.",
        }
    return {"code": normalised, "available": True, "reason": "사용 가능한 학과 코드입니다."}


def validate_candidate(payload: dict[str, Any], *, check_existing: bool = True) -> tuple[dict, dict]:
    """GUI 입력 정규화와 필드별 검증. secret은 입력으로 받지 않는다."""
    errors: dict[str, list[str]] = {}
    warnings: list[str] = []
    common = _common()

    code = str(payload.get("code") or "").strip().lower()
    name = str(payload.get("name") or "").strip()
    if not DEPT_CODE_RE.fullmatch(code):
        _field_error(errors, "code", "영문 소문자로 시작하는 2~20자 코드여야 합니다.")
    if not name:
        _field_error(errors, "name", "학과명을 입력해 주세요.")
    if check_existing and code:
        availability = department_code_availability(code)
        if not availability["available"] and DEPT_CODE_RE.fullmatch(code):
            _field_error(errors, "code", availability["reason"])

    corpora = payload.get("corpora") if isinstance(payload.get("corpora"), dict) else {}
    requested_mode = str(payload.get("corpusMode") or "").strip().lower()
    staff_corpus = str(corpora.get("staff") or "").strip()
    student_corpus = str(corpora.get("student") or "").strip()
    drive = payload.get("drive") if isinstance(payload.get("drive"), dict) else {}
    student_ids = _normalise_ids(drive.get("studentFolderIds"))
    split_enabled = requested_mode == "split" if requested_mode else bool(student_corpus or student_ids)
    if requested_mode not in {"", "single", "split"}:
        _field_error(errors, "corpusMode", "단일 코퍼스 또는 학생 분리를 선택해 주세요.")
    if not split_enabled:
        student_corpus = ""
        student_ids = []
    project = str(common.get("GCP_PROJECT_ID") or "").strip()
    region = str(common.get("GCP_REGION") or "asia-northeast3").strip()
    corpus_values = [("staff", staff_corpus)]
    if split_enabled:
        corpus_values.append(("student", student_corpus))
    for audience, value in corpus_values:
        match = CORPUS_RE.fullmatch(value)
        field = f"corpora.{audience}"
        if not match:
            _field_error(errors, field, "projects/.../locations/.../ragCorpora/... 형식이어야 합니다.")
        elif match.group(1) != project or match.group(2) != region:
            _field_error(errors, field, f"공통 설정의 {project}/{region}과 일치해야 합니다.")
    if staff_corpus and staff_corpus == student_corpus:
        _field_error(errors, "corpora.student", "교직원 코퍼스와 달라야 합니다.")

    buckets = payload.get("buckets") if isinstance(payload.get("buckets"), dict) else {}
    hwp_bucket = str(buckets.get("hwpOriginal") or "").removeprefix("gs://").strip()
    source_bucket = str(buckets.get("source") or "").removeprefix("gs://").strip()
    for field, value in (("buckets.hwpOriginal", hwp_bucket), ("buckets.source", source_bucket)):
        if not BUCKET_RE.fullmatch(value):
            _field_error(errors, field, "유효한 GCS 버킷 이름을 입력해 주세요.")
    if hwp_bucket and hwp_bucket == source_bucket:
        _field_error(errors, "buckets.source", "HWP 원본 버킷과 달라야 합니다.")

    drive_ids = _normalise_ids(drive.get("driveIds"))
    sync_ids = _normalise_ids(drive.get("syncFolderIds"))
    if not drive_ids:
        _field_error(errors, "drive.driveIds", "공유드라이브 ID가 하나 이상 필요합니다.")
    if not sync_ids:
        _field_error(errors, "drive.syncFolderIds", "동기화 폴더가 하나 이상 필요합니다.")
    if split_enabled and not student_ids:
        _field_error(errors, "drive.studentFolderIds", "학생 폴더가 하나 이상 필요합니다.")
    outside = [item for item in student_ids if item not in sync_ids]
    if outside:
        _field_error(
            errors,
            "drive.studentFolderIds",
            "동기화 폴더에 포함되지 않은 ID입니다: " + ", ".join(outside),
        )

    drive_conflicts = _department_drive_conflicts(code, drive_ids)
    for item in drive_conflicts:
        warnings.append(
            f"{item['code']} 학과와 중복된 공유드라이브 ID입니다: {', '.join(item['driveIds'])}"
        )

    mins = payload.get("minInstances") if isinstance(payload.get("minInstances"), dict) else {}
    min_instances: dict[str, int] = {}
    active_audiences = dept_config.AUDIENCES if split_enabled else ("staff",)
    for audience in active_audiences:
        raw = mins.get(audience, 0)
        try:
            parsed = int(raw)
            if parsed < 0:
                raise ValueError
            min_instances[audience] = parsed
        except (TypeError, ValueError):
            _field_error(errors, f"minInstances.{audience}", "0 이상의 정수여야 합니다.")

    candidate: dict[str, Any] = {
        "name": name,
        "corpora": {"staff": staff_corpus},
        "buckets": {"hwpOriginal": hwp_bucket, "source": source_bucket},
        "drive": {
            "driveIds": drive_ids,
            "syncFolderIds": sync_ids,
        },
        "minInstances": min_instances,
    }
    if split_enabled:
        candidate["corpora"]["student"] = student_corpus
        candidate["drive"]["studentFolderIds"] = student_ids
    return candidate, {
        "valid": not errors,
        "fieldErrors": errors,
        "warnings": warnings,
        "driveConflicts": drive_conflicts,
    }


def _render_yaml(
    candidate: dict[str, Any], *, preview: bool, keys: dict[str, str] | None = None
) -> str:
    body = dict(candidate)
    split_enabled = bool((body.get("corpora") or {}).get("student"))
    active_audiences = dept_config.AUDIENCES if split_enabled else ("staff",)
    available_keys = keys or {
        audience: "<자동 생성>" if preview else secrets.token_urlsafe(32)
        for audience in active_audiences
    }
    body["keys"] = {
        audience: available_keys[audience]
        for audience in active_audiences
        if audience in available_keys
    }
    ordered = {
        "name": body["name"],
        "corpora": body["corpora"],
        "keys": body["keys"],
        "buckets": body["buckets"],
        "drive": body["drive"],
        "minInstances": body["minInstances"],
    }
    return yaml.safe_dump(ordered, allow_unicode=True, sort_keys=False, width=1000)


def _verify_written_department(code: str, *, allow_duplicate_drives: bool = False) -> None:
    for audience in dept_config.configured_audiences(code):
        dept_config.build_env(code, audience)
    if allow_duplicate_drives:
        return
    dept_config.build_departments_map()


def create_department(
    code: str, candidate: dict[str, Any], *, allow_duplicate_drives: bool = False
) -> Path:
    target = (DEPT_DIR / f"{code}.yaml").resolve()
    dept_root = DEPT_DIR.resolve()
    if target.parent != dept_root:
        raise ValueError("설정 디렉터리 밖의 경로는 사용할 수 없습니다.")
    if target.exists():
        raise FileExistsError(target)
    if "config/departments/*.yaml" not in (ROOT / ".gitignore").read_text(encoding="utf-8"):
        raise RuntimeError("학과 YAML이 .gitignore에 포함되지 않아 생성을 중단했습니다.")

    DEPT_DIR.mkdir(parents=True, exist_ok=True)
    temp = DEPT_DIR / f".{code}.{uuid.uuid4().hex}.tmp"
    try:
        temp.write_text(_render_yaml(candidate, preview=False), encoding="utf-8", newline="\n")
        parsed = _read_yaml(temp)
        if parsed.get("name") != candidate["name"]:
            raise RuntimeError("작성한 YAML의 재검증에 실패했습니다.")
        os.replace(temp, target)
        try:
            _verify_written_department(code, allow_duplicate_drives=allow_duplicate_drives)
        except SystemExit as exc:
            target.unlink(missing_ok=True)
            raise RuntimeError(str(exc)) from exc
        except (OSError, TypeError, ValueError, yaml.YAMLError):
            target.unlink(missing_ok=True)
            raise
        return target
    finally:
        temp.unlink(missing_ok=True)


def update_department(
    code: str, candidate: dict[str, Any], *, allow_duplicate_drives: bool = False
) -> Path:
    target = (DEPT_DIR / f"{code}.yaml").resolve()
    if target.parent != DEPT_DIR.resolve() or not target.exists():
        raise FileNotFoundError(target)
    original = target.read_bytes()
    existing = _read_yaml(target)
    keys = existing.get("keys") if isinstance(existing.get("keys"), dict) else {}
    split_enabled = bool((candidate.get("corpora") or {}).get("student"))
    staff_key = str(keys.get("staff") or "")
    if not staff_key:
        raise RuntimeError("기존 MCP 키를 읽지 못해 수정을 중단했습니다.")
    preserved_keys = {"staff": staff_key}
    if split_enabled:
        preserved_keys["student"] = str(keys.get("student") or "") or secrets.token_urlsafe(32)

    temp = DEPT_DIR / f".{code}.{uuid.uuid4().hex}.tmp"
    rollback = DEPT_DIR / f".{code}.{uuid.uuid4().hex}.rollback"
    try:
        temp.write_text(
            _render_yaml(candidate, preview=False, keys=preserved_keys),
            encoding="utf-8",
            newline="\n",
        )
        parsed = _read_yaml(temp)
        if parsed.get("keys") != preserved_keys or parsed.get("name") != candidate["name"]:
            raise RuntimeError("수정한 YAML의 재검증에 실패했습니다.")
        os.replace(temp, target)
        try:
            _verify_written_department(code, allow_duplicate_drives=allow_duplicate_drives)
        except (SystemExit, OSError, TypeError, ValueError, yaml.YAMLError) as exc:
            rollback.write_bytes(original)
            os.replace(rollback, target)
            raise RuntimeError(str(exc)) from exc
        _LATEST.pop(code, None)
        return target
    finally:
        temp.unlink(missing_ok=True)
        rollback.unlink(missing_ok=True)


def department_public_config(code: str) -> dict[str, Any]:
    if not DEPT_CODE_RE.fullmatch(code):
        raise FileNotFoundError(code)
    path = (DEPT_DIR / f"{code}.yaml").resolve()
    if path.parent != DEPT_DIR.resolve() or not path.exists():
        raise FileNotFoundError(path)
    data = _read_yaml(path)
    return {
        "code": code,
        "name": str(data.get("name") or code),
        "corpora": data.get("corpora") or {},
        "buckets": data.get("buckets") or {},
        "drive": data.get("drive") or {},
        "minInstances": data.get("minInstances") or {},
        "corpusMode": "split" if (data.get("corpora") or {}).get("student") else "single",
        "configRevision": _config_revision(path),
    }


def list_department_records() -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    if not DEPT_DIR.exists():
        return records
    for path in sorted(DEPT_DIR.glob("*.yaml")):
        code = path.stem
        data: dict[str, Any] = {}
        try:
            data = _read_yaml(path)
            name = str(data.get("name") or code)
            parse_error = None
        except (OSError, TypeError, UnicodeError, yaml.YAMLError) as exc:
            name = code
            parse_error = str(exc)[:200]
        revision = _config_revision(path)
        latest = _LATEST.get(code)
        stale = bool(latest and latest.get("configRevision") != revision)
        records.append(
            {
                "code": code,
                "name": name,
                "path": f"config/departments/{path.name}",
                "configRevision": revision,
                "lastStatus": "FAIL" if parse_error else (
                    "STALE" if stale else (latest or {}).get("overall", "UNKNOWN")
                ),
                "parseError": parse_error,
                "corpusMode": (
                    "split" if not parse_error and (data.get("corpora") or {}).get("student") else "single"
                ),
                "lastResult": None if stale else latest,
            }
        )
    return records


def department_mcp_servers(code: str) -> dict[str, Any]:
    """학과별 교직원·학생 MCP Cloud Run 서비스의 실제 URL을 조회한다."""
    normalised = str(code or "").strip().lower()
    if not DEPT_CODE_RE.fullmatch(normalised) or not (DEPT_DIR / f"{normalised}.yaml").exists():
        raise FileNotFoundError(normalised)
    config = _read_yaml(DEPT_DIR / f"{normalised}.yaml")
    split_enabled = bool((config.get("corpora") or {}).get("student"))
    common = _common()
    project = str(common.get("GCP_PROJECT_ID") or "")
    region = str(common.get("GCP_REGION") or "asia-northeast3")
    ok, rows = _gcloud_json(
        [
            "run",
            "services",
            "list",
            "--platform=managed",
            f"--region={region}",
            f"--project={project}",
        ],
        timeout=20,
    )
    if not ok or not isinstance(rows, list):
        raise RuntimeError(str(rows)[:300] or "Cloud Run 서비스 목록을 조회하지 못했습니다.")

    inventory: dict[str, dict[str, Any]] = {}
    for item in rows:
        if not isinstance(item, dict):
            continue
        service_name = str((item.get("metadata") or {}).get("name") or item.get("name") or "")
        if service_name:
            inventory[service_name] = item

    servers: list[dict[str, Any]] = []
    audiences = (("staff", "기본"), ("student", "학생")) if split_enabled else (("staff", "기본"),)
    for audience, label in audiences:
        service_name = f"rag-mcp-{normalised}-{audience}"
        item = inventory.get(service_name) or {}
        status_data = item.get("status") or {}
        conditions = status_data.get("conditions") or item.get("conditions") or []
        ready_condition = next(
            (condition for condition in conditions if condition.get("type") == "Ready"), {}
        )
        ready = str(ready_condition.get("status") or "").lower() == "true"
        url = str(status_data.get("url") or item.get("url") or "")
        if url and not url.startswith("https://"):
            url = ""
        servers.append(
            {
                "audience": audience,
                "label": label,
                "serviceName": service_name,
                "url": url,
                "healthUrl": url.rstrip("/") + "/health" if url else "",
                "status": "READY" if ready and url else ("NOT_READY" if item else "NOT_DEPLOYED"),
            }
        )
    return {"code": normalised, "projectId": project, "region": region, "servers": servers}


def department_mcp_key(code: str, audience: str) -> str:
    """명시적인 로컬 복사 요청에만 MCP 키 하나를 읽어 반환한다."""
    normalised = str(code or "").strip().lower()
    if not DEPT_CODE_RE.fullmatch(normalised):
        raise FileNotFoundError(normalised)
    if audience not in dept_config.AUDIENCES:
        raise ValueError("교직원 또는 학생 MCP 키를 선택해 주세요.")
    path = (DEPT_DIR / f"{normalised}.yaml").resolve()
    if path.parent != DEPT_DIR.resolve() or not path.exists():
        raise FileNotFoundError(path)
    data = _read_yaml(path)
    key = str((data.get("keys") or {}).get(audience) or "").strip()
    if not key or key in dept_config.PLACEHOLDER_KEYS:
        raise ValueError("복사할 MCP 키가 설정되어 있지 않습니다.")
    return key


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


def _local_status(code: str) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    path = DEPT_DIR / f"{code}.yaml"
    started = time.perf_counter()
    try:
        data = _read_yaml(path)
        checks.append(_check("LOCAL", "yaml", "OK", f"config/departments/{path.name}"))
    except (OSError, TypeError, UnicodeError, yaml.YAMLError) as exc:
        return [_check("LOCAL", "yaml", "FAIL", str(exc))]

    split_enabled = bool((data.get("corpora") or {}).get("student"))
    try:
        for audience in dept_config.configured_audiences(code):
            dept_config.build_env(code, audience)
        mode_label = "기본 · 학생 설정 생성 가능" if split_enabled else "단일 코퍼스 설정 생성 가능"
        checks.append(_check("LOCAL", "derived-env", "OK", mode_label))
    except SystemExit as exc:
        checks.append(_check("LOCAL", "derived-env", "FAIL", str(exc)))

    drive = data.get("drive") or {}
    sync_ids = set(_normalise_ids(drive.get("syncFolderIds")))
    student_ids = set(_normalise_ids(drive.get("studentFolderIds")))
    if not split_enabled:
        checks.append(_check("LOCAL", "folder-scope", "SKIP", "단일 코퍼스 운영"))
    elif not student_ids - sync_ids and student_ids:
        checks.append(_check("LOCAL", "folder-scope", "OK", "student ⊆ sync"))
    else:
        missing = sorted(student_ids - sync_ids)
        checks.append(
            _check("LOCAL", "folder-scope", "FAIL", "부분집합 위반: " + ", ".join(missing))
        )

    keys = data.get("keys") or {}
    audiences = dept_config.AUDIENCES if split_enabled else ("staff",)
    weak = [aud for aud in audiences if len(str(keys.get(aud) or "")) < 24]
    if weak:
        checks.append(_check("LOCAL", "mcp-keys", "WARN", "24자 미만: " + ", ".join(weak)))
    else:
        detail = "서로 다른 키 · 길이 기준 충족" if split_enabled else "기본 키 길이 기준 충족"
        checks.append(_check("LOCAL", "mcp-keys", "OK", detail))
    checks[0]["latencyMs"] = round((time.perf_counter() - started) * 1000)
    return checks


def _run_command(args: list[str], timeout: int = 12) -> tuple[bool, str]:
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    try:
        result = subprocess.run(
            args,
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
            shell=False,
            creationflags=flags,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    output = (result.stdout or result.stderr or "").strip()
    return result.returncode == 0, output


def _gcloud_executable() -> str | None:
    """Windows에서는 CreateProcess가 PATHEXT shim을 해석하지 못하므로 .cmd를 고정."""
    if os.name == "nt":
        return shutil.which("gcloud.cmd") or shutil.which("gcloud.CMD")
    return shutil.which("gcloud")


def _sync_access_token() -> str:
    """진행률 폴링이 매번 gcloud 프로세스를 띄우지 않도록 짧게 캐시한다."""
    global _SYNC_AUTH_TOKEN, _SYNC_AUTH_TOKEN_EXPIRES
    with _SYNC_AUTH_LOCK:
        if _SYNC_AUTH_TOKEN and time.time() < _SYNC_AUTH_TOKEN_EXPIRES:
            return _SYNC_AUTH_TOKEN
        gcloud = _gcloud_executable()
        if not gcloud:
            raise RuntimeError("gcloud를 찾을 수 없습니다.")
        ok, token = _run_command([gcloud, "auth", "print-access-token", "--quiet"])
        if not ok or not token:
            raise RuntimeError("gcloud 로그인이 필요합니다.")
        _SYNC_AUTH_TOKEN = token
        _SYNC_AUTH_TOKEN_EXPIRES = time.time() + 5 * 60
        return token


def _gcloud_json(args: list[str], timeout: int = 12) -> tuple[bool, Any]:
    executable = _gcloud_executable()
    if not executable:
        return False, "gcloud를 찾을 수 없습니다."
    ok, output = _run_command([executable, *args, "--format=json", "--quiet"], timeout)
    if not ok:
        return False, output
    try:
        return True, json.loads(output or "{}")
    except json.JSONDecodeError:
        return False, "gcloud 응답이 JSON이 아닙니다."


def _default_compute_service_account(project: str) -> str:
    if not project:
        return ""
    ok, project_info = _gcloud_json(["projects", "describe", project])
    project_number = str(project_info.get("projectNumber") or "") if ok else ""
    return f"{project_number}-compute@developer.gserviceaccount.com" if project_number else ""


class _StatusRunCache:
    """한 상태 실행 안에서 동일한 외부 명령을 한 번만 수행한다."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._gcloud_calls: dict[tuple[tuple[str, ...], int], Future[Any]] = {}
        self._commands: dict[tuple[tuple[str, ...], int], Future[Any]] = {}

    def _once(
        self,
        calls: dict[tuple[tuple[str, ...], int], Future[Any]],
        key: tuple[tuple[str, ...], int],
        producer: Any,
    ) -> Any:
        with self._lock:
            future = calls.get(key)
            owner = future is None
            if future is None:
                future = Future()
                calls[key] = future
        if owner:
            try:
                future.set_result(producer())
            except Exception as exc:  # noqa: BLE001 - 동일 호출 대기자에게 오류 전달
                future.set_exception(exc)
        return future.result()

    def gcloud_json(self, args: list[str], timeout: int = 12) -> tuple[bool, Any]:
        key = (tuple(args), timeout)
        return self._once(
            self._gcloud_calls,
            key,
            lambda: _gcloud_json(args, timeout),
        )

    def run_command(self, args: list[str], timeout: int = 12) -> tuple[bool, str]:
        key = (tuple(args), timeout)
        return self._once(
            self._commands,
            key,
            lambda: _run_command(args, timeout),
        )


def _gcloud_bootstrap_state(*, include_projects: bool = True) -> dict[str, Any]:
    gcloud = _gcloud_executable()
    state: dict[str, Any] = {
        "installed": bool(gcloud),
        "authenticated": False,
        "account": "",
        "currentProject": "",
        "projects": [],
        "regions": SETUP_REGIONS,
    }
    if not gcloud:
        return state

    auth_ok, accounts = _gcloud_json(["auth", "list", "--filter=status:ACTIVE"])
    if not auth_ok or not isinstance(accounts, list) or not accounts:
        return state
    account = str(accounts[0].get("account") or "")
    token_ok, _ = _run_command([gcloud, "auth", "print-access-token", "--quiet"])
    state["authenticated"] = bool(account and token_ok)
    if not state["authenticated"]:
        return state
    if "@" in account:
        local, domain = account.split("@", 1)
        state["account"] = (local[:2] + "***@" + domain) if local else "***@" + domain

    current_ok, current = _run_command(
        [gcloud, "config", "get-value", "project", "--quiet"]
    )
    if current_ok:
        state["currentProject"] = current.strip()

    if include_projects and state["authenticated"]:
        projects_ok, projects = _gcloud_json(
            ["projects", "list", "--filter=lifecycleState:ACTIVE"], timeout=20
        )
        if projects_ok and isinstance(projects, list):
            state["projects"] = sorted(
                [
                    {
                        "id": str(item.get("projectId") or ""),
                        "name": str(item.get("name") or item.get("projectId") or ""),
                    }
                    for item in projects
                    if item.get("projectId")
                ],
                key=lambda item: (item["name"].lower(), item["id"]),
            )
    return state


def _gcloud_project_resources(project: str, region: str) -> dict[str, Any]:
    artifacts_ok, artifacts_raw = _gcloud_json(
        [
            "artifacts",
            "repositories",
            "list",
            f"--project={project}",
            f"--location={region}",
        ],
        timeout=20,
    )
    artifacts: list[dict[str, str]] = []
    if artifacts_ok and isinstance(artifacts_raw, list):
        for item in artifacts_raw:
            name = str(item.get("name") or "")
            repo_id = name.rsplit("/repositories/", 1)[-1] if "/repositories/" in name else name
            repo_format = str(item.get("format") or "")
            if repo_id and repo_format in {"", "DOCKER"}:
                artifacts.append({"id": repo_id, "format": repo_format or "DOCKER"})

    firestore_ok, firestore_raw = _gcloud_json(
        ["firestore", "databases", "list", f"--project={project}"], timeout=20
    )
    databases: list[dict[str, str]] = []
    if firestore_ok and isinstance(firestore_raw, list):
        for item in firestore_raw:
            name = str(item.get("name") or "")
            database_id = name.rsplit("/databases/", 1)[-1] if "/databases/" in name else name
            database_type = str(item.get("type") or "")
            if database_id and database_type in {"", "FIRESTORE_NATIVE"}:
                databases.append(
                    {
                        "id": database_id,
                        "location": str(item.get("locationId") or ""),
                        "type": database_type or "FIRESTORE_NATIVE",
                    }
                )
    return {
        "artifactRepositories": sorted(artifacts, key=lambda item: item["id"]),
        "firestoreDatabases": sorted(databases, key=lambda item: item["id"]),
        "artifactError": "" if artifacts_ok else str(artifacts_raw)[:200],
        "firestoreError": "" if firestore_ok else str(firestore_raw)[:200],
    }


def _start_gcloud_login() -> bool:
    global _AUTH_PROCESS
    if _AUTH_PROCESS is not None and _AUTH_PROCESS.poll() is None:
        if os.name == "nt":
            subprocess.run(
                ["taskkill.exe", "/PID", str(_AUTH_PROCESS.pid), "/T", "/F"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        else:
            _AUTH_PROCESS.terminate()
            try:
                _AUTH_PROCESS.wait(timeout=3)
            except subprocess.TimeoutExpired:
                _AUTH_PROCESS.kill()
        _AUTH_PROCESS = None
    gcloud = _gcloud_executable()
    if not gcloud:
        raise FileNotFoundError("gcloud를 찾을 수 없습니다.")
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    _AUTH_PROCESS = subprocess.Popen(
        [gcloud, "auth", "login", "--quiet"],
        cwd=ROOT,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        shell=False,
        creationflags=flags,
        start_new_session=os.name != "nt",
    )
    return True


def _http_json(url: str, token: str = "", timeout: int = 10) -> tuple[int, Any, int]:
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read(1024 * 1024).decode("utf-8", errors="replace")
            elapsed = round((time.perf_counter() - started) * 1000)
            try:
                return response.status, json.loads(raw or "{}"), elapsed
            except json.JSONDecodeError:
                return response.status, {}, elapsed
    except urllib.error.HTTPError as exc:
        raw = exc.read(1024 * 1024).decode("utf-8", errors="replace")
        try:
            body = json.loads(raw or "{}")
        except json.JSONDecodeError:
            body = {}
        return exc.code, body, round((time.perf_counter() - started) * 1000)
    except (OSError, TimeoutError):
        return 0, {}, round((time.perf_counter() - started) * 1000)


def _http_post_json(
    url: str, payload: dict[str, Any], token: str = "", timeout: int = 10
) -> tuple[int, Any, int]:
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read(1024 * 1024).decode("utf-8", errors="replace")
            elapsed = round((time.perf_counter() - started) * 1000)
            try:
                return response.status, json.loads(raw or "{}"), elapsed
            except json.JSONDecodeError:
                return response.status, {}, elapsed
    except urllib.error.HTTPError as exc:
        raw = exc.read(1024 * 1024).decode("utf-8", errors="replace")
        try:
            body = json.loads(raw or "{}")
        except json.JSONDecodeError:
            body = {}
        return exc.code, body, round((time.perf_counter() - started) * 1000)
    except (OSError, TimeoutError):
        return 0, {}, round((time.perf_counter() - started) * 1000)


def _department_resource_options(common: dict[str, Any]) -> dict[str, Any]:
    project = str(common.get("GCP_PROJECT_ID") or "")
    region = str(common.get("GCP_REGION") or "asia-northeast3")
    gcloud = _gcloud_executable()
    if not gcloud:
        return {"corpora": [], "buckets": [], "error": "gcloud를 찾을 수 없습니다."}
    token_ok, token = _run_command([gcloud, "auth", "print-access-token", "--quiet"])
    if not token_ok:
        return {"corpora": [], "buckets": [], "error": "gcloud 로그인이 필요합니다."}

    corpus_url = (
        f"https://{region}-aiplatform.googleapis.com/v1/projects/{project}/locations/"
        f"{region}/ragCorpora?pageSize=100"
    )
    corpus_status, corpus_body, _ = _http_json(corpus_url, token, timeout=20)
    corpora = []
    if corpus_status == 200:
        corpora = sorted(
            [
                {
                    "name": str(item.get("name") or ""),
                    "displayName": str(item.get("displayName") or "이름 없음"),
                    "description": str(item.get("description") or ""),
                }
                for item in corpus_body.get("ragCorpora") or []
                if item.get("name")
            ],
            key=lambda item: (item["displayName"].lower(), item["name"]),
        )

    buckets_ok, buckets_raw = _gcloud_json(
        ["storage", "buckets", "list", f"--project={project}"], timeout=20
    )
    buckets = []
    if buckets_ok and isinstance(buckets_raw, list):
        for item in buckets_raw:
            name = str(item.get("name") or "")
            location = str(item.get("location") or "").lower()
            uniform = bool(item.get("uniform_bucket_level_access"))
            pap = str(item.get("public_access_prevention") or "").lower()
            if name and location == region.lower() and uniform and pap == "enforced":
                buckets.append({"name": name, "location": location})
        buckets.sort(key=lambda item: item["name"])

    errors: list[str] = []
    if corpus_status != 200:
        errors.append(f"RAG 코퍼스 조회 HTTP {corpus_status or 'timeout'}")
    if not buckets_ok:
        errors.append("GCS 버킷 조회 실패")
    return {"corpora": corpora, "buckets": buckets, "error": " · ".join(errors)}


def _department_bucket_usage() -> dict[str, list[str]]:
    usage: dict[str, set[str]] = {}
    if not DEPT_DIR.exists():
        return {}
    for path in sorted(DEPT_DIR.glob("*.yaml")):
        try:
            config = _read_yaml(path)
        except (OSError, yaml.YAMLError):
            continue
        for bucket in (config.get("buckets") or {}).values():
            name = str(bucket or "").removeprefix("gs://").strip()
            if name:
                usage.setdefault(name, set()).add(path.stem)
    return {name: sorted(codes) for name, codes in usage.items()}


def _cleanup_provision_state() -> None:
    cutoff = time.time() - _PROVISION_TTL_SECONDS
    for plan_id in [
        key for key, item in _RESOURCE_PLANS.items() if item.get("createdEpoch", 0) < cutoff
    ]:
        _RESOURCE_PLANS.pop(plan_id, None)
    for run_id in [
        key
        for key, item in _PROVISION_RUNS.items()
        if item.get("finishedEpoch", item.get("createdEpoch", time.time())) < cutoff
    ]:
        _PROVISION_RUNS.pop(run_id, None)


def _provision_editing_code(payload: dict[str, Any], code: str) -> str:
    editing_code = str(payload.get("editingCode") or "").strip().lower()
    if not editing_code:
        return ""
    if editing_code != code or not (DEPT_DIR / f"{editing_code}.yaml").exists():
        raise ValueError("수정 중인 학과 설정을 다시 열어 주세요.")
    return editing_code


def create_resource_plan(payload: dict[str, Any]) -> dict[str, Any]:
    """외부 리소스를 변경하지 않고 정확한 생성 이름과 옵션을 만든다."""
    code = str(payload.get("code") or "").strip().lower()
    name = str(payload.get("name") or "").strip()
    editing_code = _provision_editing_code(payload, code)
    availability = department_code_availability(code, current_code=editing_code)
    if not availability["available"]:
        raise FileExistsError(availability["reason"])
    if not name:
        raise ValueError("학과명을 먼저 입력해 주세요.")

    requested = payload.get("resources")
    if not isinstance(requested, list):
        requested = list(PROVISION_RESOURCE_DEFINITIONS)
    resource_keys = list(dict.fromkeys(str(item) for item in requested))
    if not resource_keys or any(item not in PROVISION_RESOURCE_DEFINITIONS for item in resource_keys):
        raise ValueError("생성할 리소스를 올바르게 선택해 주세요.")
    corpus_mode = str(payload.get("corpusMode") or "split").strip().lower()
    if corpus_mode not in {"single", "split"}:
        raise ValueError("단일 코퍼스 또는 학생 분리를 선택해 주세요.")
    if corpus_mode == "single" and "corpusStudent" in resource_keys:
        raise ValueError("단일 코퍼스 모드에서는 학생 코퍼스를 생성하지 않습니다.")

    common = _common()
    project = str(common.get("GCP_PROJECT_ID") or "").strip()
    region = str(common.get("GCP_REGION") or "asia-northeast3").strip()
    if not PROJECT_RE.fullmatch(project) or not REGION_RE.fullmatch(region):
        raise ValueError("공통 프로젝트·리전 설정을 먼저 확인해 주세요.")

    suffix = secrets.token_hex(4)
    bucket_names = {
        "bucketHwp": f"rag-{code}-hwp-{suffix}",
        "bucketSource": f"rag-{code}-source-{suffix}",
    }
    corpus_display_names = {
        "corpusStaff": f"{code}-rag-corpus" if corpus_mode == "single" else f"{code}-rag-corpus-staff",
        "corpusStudent": f"{code}-rag-corpus-student",
    }
    resources: list[dict[str, Any]] = []
    for key in resource_keys:
        definition = PROVISION_RESOURCE_DEFINITIONS[key]
        label = "기본 코퍼스" if corpus_mode == "single" and key == "corpusStaff" else definition["label"]
        resources.append(
            {
                "key": key,
                "kind": definition["kind"],
                "label": label,
                "displayName": bucket_names.get(key) or corpus_display_names.get(key) or "",
                "value": bucket_names.get(key, ""),
            }
        )

    plan_id = uuid.uuid4().hex
    plan = {
        "planId": plan_id,
        "code": code,
        "name": name,
        "editingCode": editing_code,
        "corpusMode": corpus_mode,
        "projectId": project,
        "region": region,
        "resources": resources,
        "bucketProtection": {
            "uniformBucketLevelAccess": True,
            "publicAccessPrevention": "enforced",
            "softDeleteDays": 7,
        },
        "corpusConfig": {
            "embeddingModel": RAG_EMBEDDING_MODEL,
            "vectorDatabase": "RAG Managed DB",
        },
        "createdEpoch": time.time(),
        "started": False,
    }
    with _PROVISION_LOCK:
        _cleanup_provision_state()
        _RESOURCE_PLANS[plan_id] = plan
    return copy.deepcopy(plan)


def _apply_resource_plan_overrides(
    plan: dict[str, Any], overrides: dict[str, Any] | None
) -> dict[str, Any]:
    """사용자가 계획 화면에서 수정한 이름을 생성 직전에 다시 검증한다."""
    if overrides is None:
        return plan
    if not isinstance(overrides, dict):
        raise TypeError("수정한 리소스 이름이 올바르지 않습니다.")
    resources = {item["key"]: item for item in plan["resources"]}
    unknown = sorted(set(overrides) - set(resources))
    if unknown:
        raise ValueError("생성 계획에 없는 리소스는 수정할 수 없습니다.")
    for key, raw_value in overrides.items():
        value = str(raw_value or "").strip()
        resource = resources[key]
        if resource["kind"] == "bucket":
            value = value.removeprefix("gs://").strip()
            if not BUCKET_RE.fullmatch(value):
                raise ValueError(f"{resource['label']} 이름이 GCS 버킷 규칙에 맞지 않습니다.")
            resource["value"] = value
            resource["displayName"] = value
        else:
            if not value or len(value) > 128:
                raise ValueError(f"{resource['label']} 이름은 1~128자여야 합니다.")
            resource["displayName"] = value

    bucket_values = [item["value"] for item in resources.values() if item["kind"] == "bucket"]
    if len(bucket_values) != len(set(bucket_values)):
        raise ValueError("HWP 원본 버킷과 Source 버킷 이름은 서로 달라야 합니다.")
    corpus_values = [
        item["displayName"] for item in resources.values() if item["kind"] == "corpus"
    ]
    if len(corpus_values) != len(set(corpus_values)):
        raise ValueError("교직원 코퍼스와 학생 코퍼스 이름은 서로 달라야 합니다.")
    return plan


def _provision_access_token() -> str:
    gcloud = _gcloud_executable()
    if not gcloud:
        raise RuntimeError("gcloud를 찾을 수 없습니다.")
    ok, token = _run_command([gcloud, "auth", "print-access-token", "--quiet"], timeout=20)
    if not ok or not token.strip():
        raise RuntimeError("gcloud 로그인이 필요합니다.")
    return token.strip()


def _create_bucket_resource(name: str, project: str, region: str) -> str:
    gcloud = _gcloud_executable()
    if not gcloud:
        raise RuntimeError("gcloud를 찾을 수 없습니다.")
    ok, output = _run_command(
        [
            gcloud,
            "storage",
            "buckets",
            "create",
            f"gs://{name}",
            f"--project={project}",
            f"--location={region}",
            "--default-storage-class=STANDARD",
            "--uniform-bucket-level-access",
            "--public-access-prevention",
            "--soft-delete-duration=7d",
            "--quiet",
        ],
        timeout=120,
    )
    if not ok:
        raise RuntimeError((output or "GCS 버킷 생성에 실패했습니다.")[-400:])
    described, metadata = _gcloud_json(
        ["storage", "buckets", "describe", f"gs://{name}"], timeout=30
    )
    if not described or not isinstance(metadata, dict):
        raise RuntimeError("버킷은 생성됐지만 보호 설정을 다시 확인하지 못했습니다.")
    location = str(metadata.get("location") or "").lower()
    uniform = bool(metadata.get("uniform_bucket_level_access"))
    pap = str(metadata.get("public_access_prevention") or "").lower()
    if location != region.lower() or not uniform or pap != "enforced":
        raise RuntimeError("버킷은 생성됐지만 필수 리전·접근 보호 설정이 일치하지 않습니다.")
    return name


def _create_corpus_resource(display_name: str, project: str, region: str, token: str) -> str:
    url = (
        f"https://{region}-aiplatform.googleapis.com/v1/projects/{project}/locations/"
        f"{region}/ragCorpora"
    )
    status, body, _ = _http_post_json(
        url,
        {
            "displayName": display_name,
            "description": "학과 관리 콘솔에서 생성한 RAG 코퍼스",
            "vectorDbConfig": {
                "ragEmbeddingModelConfig": {
                    "vertexPredictionEndpoint": {
                        "endpoint": (
                            f"projects/{project}/locations/{region}/publishers/google/models/"
                            f"{RAG_EMBEDDING_MODEL}"
                        )
                    }
                },
                "ragManagedDb": {},
            },
        },
        token,
        timeout=30,
    )
    if status not in {200, 201, 202} or not isinstance(body, dict):
        message = str((body.get("error") or {}).get("message") or "") if isinstance(body, dict) else ""
        detail = f": {message[:300]}" if message else ""
        raise RuntimeError(f"RAG 코퍼스 생성 요청 실패 (HTTP {status or 'timeout'}){detail}")

    if body.get("done"):
        response_name = str((body.get("response") or {}).get("name") or "")
        if response_name:
            return response_name
    operation_name = str(body.get("name") or "")
    if operation_name.startswith("projects/") and "/ragCorpora/" in operation_name:
        return operation_name
    if not operation_name:
        raise RuntimeError("RAG 코퍼스 생성 작업 ID를 받지 못했습니다.")

    operation_url = f"https://{region}-aiplatform.googleapis.com/v1/{operation_name}"
    deadline = time.time() + 600
    while time.time() < deadline:
        poll_status, operation, _ = _http_json(operation_url, token, timeout=20)
        if poll_status == 200 and isinstance(operation, dict):
            if operation.get("done"):
                if operation.get("error"):
                    message = str((operation.get("error") or {}).get("message") or "")
                    raise RuntimeError(message[:400] or "RAG 코퍼스 생성 작업이 실패했습니다.")
                response_name = str((operation.get("response") or {}).get("name") or "")
                if response_name:
                    return response_name
                break
        elif poll_status not in {0, 429, 500, 502, 503, 504}:
            raise RuntimeError(f"RAG 코퍼스 생성 상태 조회 실패 (HTTP {poll_status})")
        time.sleep(2)

    options = _department_resource_options({"GCP_PROJECT_ID": project, "GCP_REGION": region})
    matches = [
        item for item in options.get("corpora", []) if item.get("displayName") == display_name
    ]
    if len(matches) == 1:
        return str(matches[0]["name"])
    raise RuntimeError("코퍼스 생성 작업이 오래 걸리고 있습니다. 리소스를 새로 만들기 전에 목록을 확인해 주세요.")


def _gcloud_enabled_services(project: str) -> tuple[bool, set[str]]:
    """프로젝트에 켜져 있는 API 목록. 조회 실패와 '하나도 없음' 을 구분한다."""
    ok, raw = _gcloud_json(
        ["services", "list", "--enabled", f"--project={project}"], timeout=30
    )
    if not ok or not isinstance(raw, list):
        return False, set()
    names: set[str] = set()
    for item in raw:
        if isinstance(item, dict):
            name = str(item.get("config", {}).get("name") or item.get("name") or "")
            names.add(name.rsplit("/services/", 1)[-1] if "/services/" in name else name)
    return True, {name for name in names if name}


def create_common_resource_plan(payload: dict[str, Any]) -> dict[str, Any]:
    """외부 리소스를 **바꾸지 않고** 무엇을 켜고 무엇을 만들지만 계산한다.

    API 활성화가 계획에 드러나야 사용자가 모르고 켜는 일이 없다. 실제 변경은
    확인 뒤 start_common_provision_run 에서만 일어난다.
    """
    project = str(payload.get("projectId") or "").strip()
    region = str(payload.get("region") or "").strip()
    if not PROJECT_RE.fullmatch(project):
        raise ValueError("GCP 프로젝트를 먼저 선택해 주세요.")
    if not REGION_RE.fullmatch(region):
        raise ValueError("리전을 먼저 선택해 주세요.")

    names = {
        "artifactRepo": str(payload.get("artifactRepo") or "").strip()
        or COMMON_PROVISION_RESOURCE_DEFINITIONS["artifactRepo"]["defaultName"],
        "firestoreDatabase": str(payload.get("firestoreDatabase") or "").strip()
        or COMMON_PROVISION_RESOURCE_DEFINITIONS["firestoreDatabase"]["defaultName"],
    }
    if not ARTIFACT_REPO_RE.fullmatch(names["artifactRepo"]):
        raise ValueError("유효한 Artifact Registry 저장소 이름이 필요합니다.")
    if not FIRESTORE_DB_RE.fullmatch(names["firestoreDatabase"]):
        raise ValueError("유효한 Firestore 데이터베이스 ID가 필요합니다.")
    # (default) 는 Datastore 모드로 굳으면 되돌릴 수 없다 — preflight 가 금지하는 값이다.
    if names["firestoreDatabase"] == "(default)":
        raise ValueError("(default) 데이터베이스는 사용하지 않습니다. 다른 ID를 입력해 주세요.")

    existing = _gcloud_project_resources(project, region)
    existing_ids = {
        "artifactRepo": {item["id"] for item in existing["artifactRepositories"]},
        "firestoreDatabase": {item["id"] for item in existing["firestoreDatabases"]},
    }
    services_ok, enabled = _gcloud_enabled_services(project)

    resources: list[dict[str, Any]] = []
    services: list[dict[str, Any]] = []
    for key, definition in COMMON_PROVISION_RESOURCE_DEFINITIONS.items():
        already = names[key] in existing_ids[key]
        resources.append(
            {
                "key": key,
                "kind": definition["kind"],
                "label": definition["label"],
                "displayName": names[key],
                "value": names[key] if already else "",
                "exists": already,
                "warning": COMMON_PROVISION_WARNINGS.get(key, ""),
            }
        )
        service = str(definition["service"])
        if not any(item["name"] == service for item in services):
            services.append(
                {
                    "name": service,
                    # 조회 실패 시엔 '켜져 있다' 고 단정하지 않는다 — 화면에 확인 필요로 뜬다.
                    "enabled": bool(services_ok and service in enabled),
                    "known": services_ok,
                }
            )

    plan_id = uuid.uuid4().hex
    plan = {
        "planId": plan_id,
        "projectId": project,
        "region": region,
        "resources": resources,
        "services": services,
        "createdEpoch": time.time(),
        "started": False,
    }
    with _COMMON_PROVISION_LOCK:
        _cleanup_common_provision_state()
        _COMMON_RESOURCE_PLANS[plan_id] = plan
    return copy.deepcopy(plan)


def _cleanup_common_provision_state() -> None:
    cutoff = time.time() - _PROVISION_TTL_SECONDS
    for plan_id in [
        key for key, item in _COMMON_RESOURCE_PLANS.items() if item.get("createdEpoch", 0) < cutoff
    ]:
        _COMMON_RESOURCE_PLANS.pop(plan_id, None)
    for run_id in [
        key
        for key, item in _COMMON_PROVISION_RUNS.items()
        if item.get("finishedEpoch", item.get("createdEpoch", time.time())) < cutoff
    ]:
        _COMMON_PROVISION_RUNS.pop(run_id, None)


def _enable_gcloud_service(service: str, project: str) -> None:
    gcloud = _gcloud_executable()
    if not gcloud:
        raise RuntimeError("gcloud를 찾을 수 없습니다.")
    ok, output = _run_command(
        [gcloud, "services", "enable", service, f"--project={project}", "--quiet"],
        timeout=180,
    )
    if not ok:
        raise RuntimeError((output or f"{service} 활성화에 실패했습니다.")[-400:])


def _create_artifact_repo_resource(name: str, project: str, region: str) -> str:
    gcloud = _gcloud_executable()
    if not gcloud:
        raise RuntimeError("gcloud를 찾을 수 없습니다.")
    ok, output = _run_command(
        [
            gcloud,
            "artifacts",
            "repositories",
            "create",
            name,
            "--repository-format=docker",
            f"--location={region}",
            f"--project={project}",
            "--quiet",
        ],
        timeout=180,
    )
    if not ok:
        raise RuntimeError((output or "Artifact Registry 저장소 생성에 실패했습니다.")[-400:])
    described, metadata = _gcloud_json(
        [
            "artifacts",
            "repositories",
            "describe",
            name,
            f"--location={region}",
            f"--project={project}",
        ],
        timeout=30,
    )
    if not described or not isinstance(metadata, dict):
        raise RuntimeError("저장소는 생성됐지만 형식을 다시 확인하지 못했습니다.")
    if str(metadata.get("format") or "").upper() != "DOCKER":
        raise RuntimeError("저장소가 Docker 형식으로 생성되지 않았습니다.")
    return name


def _create_firestore_database_resource(name: str, project: str, region: str) -> str:
    """Native 모드로만 만든다. Datastore 모드로 굳으면 되돌릴 수 없다."""
    gcloud = _gcloud_executable()
    if not gcloud:
        raise RuntimeError("gcloud를 찾을 수 없습니다.")
    ok, output = _run_command(
        [
            gcloud,
            "firestore",
            "databases",
            "create",
            f"--database={name}",
            f"--location={region}",
            "--type=firestore-native",
            f"--project={project}",
            "--quiet",
        ],
        timeout=300,
    )
    if not ok:
        raise RuntimeError((output or "Firestore 데이터베이스 생성에 실패했습니다.")[-400:])
    described, metadata = _gcloud_json(
        [
            "firestore",
            "databases",
            "describe",
            f"--database={name}",
            f"--project={project}",
        ],
        timeout=30,
    )
    if not described or not isinstance(metadata, dict):
        raise RuntimeError("데이터베이스는 생성됐지만 모드를 다시 확인하지 못했습니다.")
    if str(metadata.get("type") or "") != "FIRESTORE_NATIVE":
        raise RuntimeError("데이터베이스가 Native 모드로 생성되지 않았습니다.")
    return name


def retrieve_department_corpus(
    code: str, audience: str, query: str, top_k: int = 5, *, generate: bool = False
) -> dict[str, Any]:
    """로컬 콘솔에서 선택한 학과 코퍼스의 원문 컨텍스트를 조회한다."""
    normalised = str(code or "").strip().lower()
    if not DEPT_CODE_RE.fullmatch(normalised):
        raise FileNotFoundError(normalised)
    if audience not in dept_config.AUDIENCES:
        raise ValueError("교직원 또는 학생 코퍼스를 선택해 주세요.")
    text = str(query or "").strip()
    if not text or len(text) > 1000:
        raise ValueError("질문은 1~1000자로 입력해 주세요.")
    limit = max(1, min(int(top_k), 10))

    config = department_public_config(normalised)
    common = _common()
    project = str(common.get("GCP_PROJECT_ID") or "")
    region = str(common.get("GCP_REGION") or "asia-northeast3")
    corpus = str((config.get("corpora") or {}).get(audience) or "")
    match = CORPUS_RE.fullmatch(corpus)
    if not match or match.group(1) != project or match.group(2) != region:
        raise ValueError("학과 코퍼스가 현재 프로젝트·리전과 일치하지 않습니다.")

    token = _provision_access_token()
    url = (
        f"https://{region}-aiplatform.googleapis.com/v1/projects/{project}/locations/"
        f"{region}:retrieveContexts"
    )
    started = time.perf_counter()
    status, body, _ = _http_post_json(
        url,
        {
            "vertexRagStore": {"ragResources": [{"ragCorpus": corpus}]},
            "query": {"text": text, "ragRetrievalConfig": {"topK": limit}},
        },
        token,
        timeout=45,
    )
    if status != 200 or not isinstance(body, dict):
        message = str((body.get("error") or {}).get("message") or "") if isinstance(body, dict) else ""
        detail = f": {message[:300]}" if message else ""
        raise RuntimeError(f"코퍼스 조회 실패 (HTTP {status or 'timeout'}){detail}")

    raw_contexts = (body.get("contexts") or {}).get("contexts") or []
    contexts: list[dict[str, Any]] = []
    for index, item in enumerate(raw_contexts[:limit], start=1):
        if not isinstance(item, dict):
            continue
        chunk = item.get("chunk") if isinstance(item.get("chunk"), dict) else {}
        context_text = str(item.get("text") or chunk.get("text") or "")[:12000]
        contexts.append(
            {
                "rank": index,
                "text": context_text,
                "sourceDisplayName": str(item.get("sourceDisplayName") or ""),
                "sourceUri": str(item.get("sourceUri") or ""),
                "score": item.get("score"),
            }
        )
    return {
        "code": normalised,
        "departmentName": config["name"],
        "audience": audience,
        "corpus": corpus,
        "query": text,
        "contexts": contexts,
        "latencyMs": round((time.perf_counter() - started) * 1000),
        **(_corpus_answer_fields(text, contexts, project, token) if generate else {}),
    }


def _gemini_text(body: dict[str, Any]) -> str:
    blocked = str(((body.get("promptFeedback") or {}).get("blockReason")) or "")
    if blocked:
        raise RuntimeError(f"답변 생성이 차단되었습니다: {blocked}")
    candidates = body.get("candidates") if isinstance(body.get("candidates"), list) else []
    if not candidates or not isinstance(candidates[0], dict):
        return ""
    parts = ((candidates[0].get("content") or {}).get("parts") or [])
    return "".join(
        str(part.get("text") or "") for part in parts if isinstance(part, dict)
    ).strip()


def _vertex_generate_url(project: str) -> str:
    """생성은 코퍼스 리전과 분리한다. Seoul에는 Gemini publisher model이 없다."""
    location = CORPUS_CHAT_LOCATION
    host = (
        "aiplatform.googleapis.com"
        if location == "global"
        else f"{location}-aiplatform.googleapis.com"
    )
    return (
        f"https://{host}/v1/projects/{project}/locations/{location}"
        f"/publishers/google/models/{CORPUS_CHAT_MODEL}:generateContent"
    )


def _generate_corpus_answer(
    query: str,
    contexts: list[dict[str, Any]],
    project: str,
    token: str,
) -> tuple[str, int]:
    """검색된 원문만 근거로 Gemini 답을 만든다. gcloud access token을 재사용한다."""
    blocks: list[str] = []
    used = 0
    for item in contexts:
        title = str(item.get("sourceDisplayName") or f"결과 {item.get('rank')}")
        block = f"[{item.get('rank')}] {title}\n{item.get('text') or ''}"
        if used + len(block) > 24000:
            break
        blocks.append(block)
        used += len(block)
    prompt = (
        "다음은 Vertex RAG에서 검색한 문서 조각입니다. 이 내용만 근거로 질문에 한국어로 답하세요.\n"
        "근거에 없으면 확인되지 않는다고 답하고, 지어내지 마세요.\n"
        "찾은 자료에 대해 제목 : {제목} 이렇게 표기하세요\n"
        "가능하면 [번호]로 출처를 표시하세요.\n\n"
        f"질문: {query}\n\n검색 결과:\n"
        + ("\n\n".join(blocks) if blocks else "(검색 결과 없음)")
    )
    url = _vertex_generate_url(project)
    status, body, latency = _http_post_json(
        url,
        {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.2, "maxOutputTokens": 2048},
        },
        token,
        timeout=60,
    )
    if status != 200 or not isinstance(body, dict):
        message = str((body.get("error") or {}).get("message") or "") if isinstance(body, dict) else ""
        detail = f": {message[:300]}" if message else ""
        raise RuntimeError(f"답변 생성 실패 (HTTP {status or 'timeout'}){detail}")
    answer = _gemini_text(body)
    if not answer:
        raise RuntimeError("모델이 빈 답변을 반환했습니다.")
    return answer, latency


def _corpus_answer_fields(
    query: str,
    contexts: list[dict[str, Any]],
    project: str,
    token: str,
) -> dict[str, Any]:
    fields = {"answer": "", "answerModel": CORPUS_CHAT_MODEL, "answerError": ""}
    if not contexts:
        return fields
    try:
        answer, _latency = _generate_corpus_answer(query, contexts, project, token)
        fields["answer"] = answer
    except RuntimeError as exc:
        fields["answerError"] = str(exc)[:400]
    return fields


def _set_provision_step(run_id: str, key: str, **changes: Any) -> None:
    with _PROVISION_LOCK:
        run = _PROVISION_RUNS.get(run_id)
        if not run:
            return
        for item in run["resources"]:
            if item["key"] == key:
                item.update(changes)
                return


def _execute_provision_run(run_id: str) -> None:
    with _PROVISION_LOCK:
        run = copy.deepcopy(_PROVISION_RUNS[run_id])
    token = ""
    try:
        token = _provision_access_token()
    except RuntimeError as exc:
        with _PROVISION_LOCK:
            current = _PROVISION_RUNS[run_id]
            for item in current["resources"]:
                item.update(status="FAILED", detail=str(exc))
            current.update(status="FAILED", finishedEpoch=time.time())
        return

    for resource in run["resources"]:
        key = resource["key"]
        _set_provision_step(run_id, key, status="RUNNING", detail="생성 요청 중")
        try:
            if resource["kind"] == "bucket":
                value = _create_bucket_resource(
                    resource["value"], run["projectId"], run["region"]
                )
            else:
                value = _create_corpus_resource(
                    resource["displayName"], run["projectId"], run["region"], token
                )
            _set_provision_step(
                run_id,
                key,
                status="COMPLETE",
                value=value,
                detail="생성 및 연결 확인 완료",
            )
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            _set_provision_step(run_id, key, status="FAILED", detail=str(exc)[:400])

    with _PROVISION_LOCK:
        current = _PROVISION_RUNS[run_id]
        statuses = [item["status"] for item in current["resources"]]
        if all(status == "COMPLETE" for status in statuses):
            current["status"] = "COMPLETED"
        elif any(status == "COMPLETE" for status in statuses):
            current["status"] = "PARTIAL"
        else:
            current["status"] = "FAILED"
        current["finishedEpoch"] = time.time()


def start_provision_run(
    plan_id: str, overrides: dict[str, Any] | None = None
) -> dict[str, Any]:
    with _PROVISION_LOCK:
        _cleanup_provision_state()
        plan = _RESOURCE_PLANS.get(plan_id)
        if not plan:
            raise FileNotFoundError(plan_id)
        if plan["started"]:
            raise FileExistsError("이미 실행한 생성 계획입니다.")
        plan_for_run = _apply_resource_plan_overrides(copy.deepcopy(plan), overrides)
        availability = department_code_availability(
            plan["code"], current_code=plan.get("editingCode", "")
        )
        if not availability["available"]:
            raise FileExistsError(availability["reason"])
        for active in _PROVISION_RUNS.values():
            if active["code"] == plan["code"] and active["status"] == "RUNNING":
                raise FileExistsError("이 학과의 리소스 생성이 이미 진행 중입니다.")

        run_id = uuid.uuid4().hex
        resources = copy.deepcopy(plan_for_run["resources"])
        for item in resources:
            item.update(status="PENDING", detail="대기 중")
        run = {
            "runId": run_id,
            "planId": plan_id,
            "code": plan_for_run["code"],
            "projectId": plan_for_run["projectId"],
            "region": plan_for_run["region"],
            "bucketProtection": copy.deepcopy(plan_for_run["bucketProtection"]),
            "corpusConfig": copy.deepcopy(plan_for_run["corpusConfig"]),
            "status": "RUNNING",
            "resources": resources,
            "createdEpoch": time.time(),
        }
        plan["started"] = True
        _PROVISION_RUNS[run_id] = run

    threading.Thread(
        target=_execute_provision_run,
        args=(run_id,),
        name=f"resource-provision-{run_id[:8]}",
        daemon=True,
    ).start()
    return copy.deepcopy(run)


def _set_common_provision_step(run_id: str, key: str, **changes: Any) -> None:
    with _COMMON_PROVISION_LOCK:
        run = _COMMON_PROVISION_RUNS.get(run_id)
        if not run:
            return
        for item in run["resources"]:
            if item["key"] == key:
                item.update(changes)
                return


def _set_common_provision_service(run_id: str, name: str, **changes: Any) -> None:
    with _COMMON_PROVISION_LOCK:
        run = _COMMON_PROVISION_RUNS.get(run_id)
        if not run:
            return
        for item in run["services"]:
            if item["name"] == name:
                item.update(changes)
                return


def _execute_common_provision_run(run_id: str) -> None:
    """API 를 먼저 켜고 리소스를 만든다. API 가 꺼진 채로는 생성이 그냥 실패한다."""
    with _COMMON_PROVISION_LOCK:
        run = copy.deepcopy(_COMMON_PROVISION_RUNS[run_id])
    project = run["projectId"]
    region = run["region"]

    failed_services: set[str] = set()
    for service in run["services"]:
        name = service["name"]
        if service.get("enabled") and service.get("known"):
            _set_common_provision_service(run_id, name, status="SKIPPED", detail="이미 활성화됨")
            continue
        _set_common_provision_service(run_id, name, status="RUNNING", detail="활성화 중")
        try:
            _enable_gcloud_service(name, project)
        except (OSError, RuntimeError) as exc:
            failed_services.add(name)
            _set_common_provision_service(run_id, name, status="FAILED", detail=str(exc)[:400])
        else:
            _set_common_provision_service(run_id, name, status="COMPLETE", detail="활성화 완료")

    for resource in run["resources"]:
        key = resource["key"]
        if resource.get("exists"):
            _set_common_provision_step(
                run_id, key, status="SKIPPED", detail="이미 존재해 건너뜀"
            )
            continue
        service = str(COMMON_PROVISION_RESOURCE_DEFINITIONS[key]["service"])
        if service in failed_services:
            _set_common_provision_step(
                run_id, key, status="FAILED", detail=f"{service} 활성화 실패로 생성하지 않았습니다."
            )
            continue
        _set_common_provision_step(run_id, key, status="RUNNING", detail="생성 요청 중")
        try:
            if resource["kind"] == "artifactRepo":
                value = _create_artifact_repo_resource(resource["displayName"], project, region)
            else:
                value = _create_firestore_database_resource(
                    resource["displayName"], project, region
                )
            _set_common_provision_step(
                run_id, key, status="COMPLETE", value=value, detail="생성 및 확인 완료"
            )
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            _set_common_provision_step(run_id, key, status="FAILED", detail=str(exc)[:400])

    with _COMMON_PROVISION_LOCK:
        current = _COMMON_PROVISION_RUNS[run_id]
        statuses = [item["status"] for item in current["resources"]]
        settled = [item for item in statuses if item in {"COMPLETE", "SKIPPED"}]
        if len(settled) == len(statuses):
            current["status"] = "COMPLETED"
        elif settled:
            current["status"] = "PARTIAL"
        else:
            current["status"] = "FAILED"
        current["finishedEpoch"] = time.time()


def start_common_provision_run(plan_id: str) -> dict[str, Any]:
    with _COMMON_PROVISION_LOCK:
        _cleanup_common_provision_state()
        plan = _COMMON_RESOURCE_PLANS.get(plan_id)
        if not plan:
            raise FileNotFoundError(plan_id)
        if plan["started"]:
            raise FileExistsError("이미 실행한 생성 계획입니다.")
        for active in _COMMON_PROVISION_RUNS.values():
            if active["status"] == "RUNNING":
                raise FileExistsError("공통 리소스 생성이 이미 진행 중입니다.")

        run_id = uuid.uuid4().hex
        resources = copy.deepcopy(plan["resources"])
        for item in resources:
            item.update(status="PENDING", detail="대기 중")
        services = copy.deepcopy(plan["services"])
        for item in services:
            item.update(status="PENDING", detail="대기 중")
        run = {
            "runId": run_id,
            "planId": plan_id,
            "projectId": plan["projectId"],
            "region": plan["region"],
            "status": "RUNNING",
            "resources": resources,
            "services": services,
            "createdEpoch": time.time(),
        }
        plan["started"] = True
        _COMMON_PROVISION_RUNS[run_id] = run

    threading.Thread(
        target=_execute_common_provision_run,
        args=(run_id,),
        name=f"common-provision-{run_id[:8]}",
        daemon=True,
    ).start()
    return copy.deepcopy(run)


def _cleanup_mcp_deployments() -> None:
    cutoff = time.time() - _MCP_DEPLOY_TTL_SECONDS
    expired = [
        run_id
        for run_id, run in _MCP_DEPLOY_RUNS.items()
        if run.get("finishedEpoch", run.get("createdEpoch", time.time())) < cutoff
    ]
    for run_id in expired:
        _MCP_DEPLOY_RUNS.pop(run_id, None)


def _set_mcp_deploy_step(run_id: str, key: str, **changes: Any) -> None:
    with _MCP_DEPLOY_LOCK:
        run = _MCP_DEPLOY_RUNS.get(run_id)
        if not run:
            return
        for step in run["steps"]:
            if step["key"] == key:
                step.update(changes)
                return


def _append_mcp_deploy_log(run_id: str, line: str, secrets_to_redact: list[str]) -> None:
    clean = str(line or "").strip()
    if not clean:
        return
    for secret in secrets_to_redact:
        if secret:
            clean = clean.replace(secret, "***")
    clean = re.sub(r"(?i)(authorization:\s*bearer\s+)[^\s]+", r"\1***", clean)
    with _MCP_DEPLOY_LOCK:
        run = _MCP_DEPLOY_RUNS.get(run_id)
        if not run:
            return
        run["logs"] = [*run.get("logs", []), clean[:500]][-160:]
        for step in run["steps"]:
            if step["key"] == "deploy" and step["status"] == "RUNNING":
                step["detail"] = clean[:300]
                break


def _run_mcp_deploy_script(
    code: str,
    *,
    skip_build: bool,
    on_line: Any,
) -> int:
    executable = shutil.which("pwsh.exe") or shutil.which("pwsh")
    if not executable:
        raise RuntimeError("PowerShell 7(pwsh)을 찾을 수 없습니다.")
    args = [
        executable,
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(ROOT / "scripts" / "deploy_mcp.ps1"),
        "-Dept",
        code,
    ]
    if skip_build:
        args.append("-SkipBuild")
    env = os.environ.copy()
    env.update(
        {
            "PYTHONUTF8": "1",
            "PYTHONIOENCODING": "utf-8",
            "CLOUDSDK_CORE_DISABLE_PROMPTS": "1",
        }
    )
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    process = subprocess.Popen(
        args,
        cwd=ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        shell=False,
        creationflags=flags,
    )
    if process.stdout:
        for line in process.stdout:
            on_line(line)
    return process.wait()


def _execute_mcp_deployment(run_id: str) -> None:
    with _MCP_DEPLOY_LOCK:
        run = copy.deepcopy(_MCP_DEPLOY_RUNS[run_id])
    code = run["code"]
    try:
        config_path = DEPT_DIR / f"{code}.yaml"
        config = _read_yaml(config_path)
        audiences = dept_config.configured_audiences(code)
        secrets_to_redact = [
            str((config.get("keys") or {}).get(audience) or "") for audience in audiences
        ]
        _set_mcp_deploy_step(
            run_id,
            "config",
            status="COMPLETE",
            detail=f"{len(audiences)}개 MCP 설정 검증 완료",
        )

        common = _common()
        project = str(common.get("GCP_PROJECT_ID") or "")
        region = str(common.get("GCP_REGION") or "asia-northeast3")
        repository = str(common.get("ARTIFACT_REPO") or "rag-mcp")
        image = f"{region}-docker.pkg.dev/{project}/{repository}/mcp:latest"
        _set_mcp_deploy_step(run_id, "image", status="RUNNING", detail="Artifact Registry 확인 중")
        image_ok, _image_info = _gcloud_json(
            ["artifacts", "docker", "images", "describe", image], timeout=30
        )
        _set_mcp_deploy_step(
            run_id,
            "image",
            status="COMPLETE",
            detail="기존 MCP 이미지 사용" if image_ok else "이미지 없음 · 이번 배포에서 빌드",
        )

        _set_mcp_deploy_step(run_id, "deploy", status="RUNNING", detail="Cloud Run 배포 시작")
        exit_code = _run_mcp_deploy_script(
            code,
            skip_build=image_ok,
            on_line=lambda line: _append_mcp_deploy_log(run_id, line, secrets_to_redact),
        )
        if exit_code != 0:
            raise RuntimeError(f"MCP 배포 스크립트가 종료 코드 {exit_code}로 실패했습니다.")
        _set_mcp_deploy_step(run_id, "deploy", status="COMPLETE", detail="Cloud Run 배포 명령 완료")

        _set_mcp_deploy_step(run_id, "ready", status="RUNNING", detail="Cloud Run Ready 확인 중")
        inventory = department_mcp_servers(code)
        servers = inventory.get("servers") or []
        expected = {f"rag-mcp-{code}-{audience}" for audience in audiences}
        ready = {
            str(item.get("serviceName") or "")
            for item in servers
            if item.get("status") == "READY"
        }
        missing = sorted(expected - ready)
        if missing:
            raise RuntimeError("Ready 상태가 아닌 서비스: " + ", ".join(missing))
        _set_mcp_deploy_step(
            run_id,
            "ready",
            status="COMPLETE",
            detail=f"{len(expected)}개 서비스 Ready",
        )

        _set_mcp_deploy_step(run_id, "health", status="RUNNING", detail="서비스 health 확인 중")
        health_failures: list[str] = []
        for server in servers:
            status_code, _body, _latency = _http_json(str(server.get("healthUrl") or ""), timeout=30)
            if status_code != 200:
                health_failures.append(
                    f"{server.get('serviceName')}: HTTP {status_code or 'timeout'}"
                )
        if health_failures:
            raise RuntimeError(" · ".join(health_failures))
        _set_mcp_deploy_step(
            run_id,
            "health",
            status="COMPLETE",
            detail=f"{len(servers)}개 서비스 정상 응답",
        )
        with _MCP_DEPLOY_LOCK:
            current = _MCP_DEPLOY_RUNS[run_id]
            current.update(
                status="COMPLETED",
                servers=servers,
                finishedEpoch=time.time(),
            )
    except (OSError, RuntimeError, SystemExit, TypeError, ValueError, yaml.YAMLError) as exc:
        message = str(exc)[:500]
        with _MCP_DEPLOY_LOCK:
            current = _MCP_DEPLOY_RUNS.get(run_id)
            if not current:
                return
            running_step = next(
                (step for step in current["steps"] if step["status"] == "RUNNING"),
                None,
            )
            if running_step:
                running_step.update(status="FAILED", detail=message)
            current.update(status="FAILED", error=message, finishedEpoch=time.time())


def start_mcp_deployment(code: str) -> dict[str, Any]:
    normalised = str(code or "").strip().lower()
    if not DEPT_CODE_RE.fullmatch(normalised) or not (DEPT_DIR / f"{normalised}.yaml").exists():
        raise FileNotFoundError(normalised)
    config = department_public_config(normalised)
    audiences = dept_config.configured_audiences(normalised)
    services = [f"rag-mcp-{normalised}-{audience}" for audience in audiences]
    with _MCP_DEPLOY_LOCK:
        _cleanup_mcp_deployments()
        active = next(
            (
                copy.deepcopy(run)
                for run in _MCP_DEPLOY_RUNS.values()
                if run["code"] == normalised and run["status"] == "RUNNING"
            ),
            None,
        )
        if active:
            raise FileExistsError(active["runId"])
        run_id = uuid.uuid4().hex
        run = {
            "runId": run_id,
            "code": normalised,
            "name": config["name"],
            "corpusMode": config["corpusMode"],
            "status": "RUNNING",
            "serviceNames": services,
            "services": [],
            "steps": [
                {"key": "config", "label": "설정 확인", "status": "RUNNING", "detail": "YAML 및 MCP 키 검증 중"},
                {"key": "image", "label": "MCP 이미지", "status": "PENDING", "detail": "대기 중"},
                {"key": "deploy", "label": "Cloud Run 배포", "status": "PENDING", "detail": "대기 중"},
                {"key": "ready", "label": "Ready 확인", "status": "PENDING", "detail": "대기 중"},
                {"key": "health", "label": "Health 확인", "status": "PENDING", "detail": "대기 중"},
            ],
            "logs": [],
            "createdEpoch": time.time(),
        }
        _MCP_DEPLOY_RUNS[run_id] = run
    threading.Thread(
        target=_execute_mcp_deployment,
        args=(run_id,),
        name=f"mcp-deploy-{normalised}-{run_id[:8]}",
        daemon=True,
    ).start()
    return copy.deepcopy(run)


def _merge_live_resource_validation(candidate: dict[str, Any], result: dict[str, Any]) -> None:
    options = _department_resource_options(_common())
    corpus_names = {item["name"] for item in options["corpora"]}
    bucket_names = {item["name"] for item in options["buckets"]}
    error = str(options.get("error") or "")
    split_enabled = bool((candidate.get("corpora") or {}).get("student"))
    audiences = dept_config.AUDIENCES if split_enabled else ("staff",)
    for audience in audiences:
        field = f"corpora.{audience}"
        value = str((candidate.get("corpora") or {}).get(audience) or "")
        if value and value not in corpus_names:
            _field_error(
                result["fieldErrors"],
                field,
                error or "현재 프로젝트·리전에 존재하는 RAG 코퍼스를 선택해 주세요.",
            )
    for key, field in (("hwpOriginal", "buckets.hwpOriginal"), ("source", "buckets.source")):
        value = str((candidate.get("buckets") or {}).get(key) or "")
        if value and value not in bucket_names:
            _field_error(
                result["fieldErrors"],
                field,
                error or "현재 프로젝트·리전의 보호된 GCS 버킷을 선택해 주세요.",
            )
    result["valid"] = not result["fieldErrors"]


def _service_account_access_token(
    project: str,
    caller_token: str,
    scopes: list[str],
    *,
    gcloud_json: Any = _gcloud_json,
) -> tuple[str, str, int, str]:
    """Compute SA 가장 토큰. 반환값은 (token, service_account, latency_ms, error)."""
    project_ok, project_info = gcloud_json(["projects", "describe", project])
    project_number = str(project_info.get("projectNumber") or "") if project_ok else ""
    if not project_number:
        return "", "", 0, "프로젝트 번호를 확인하지 못했습니다."

    service_account = f"{project_number}-compute@developer.gserviceaccount.com"
    token_url = (
        "https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/"
        f"{service_account}:generateAccessToken"
    )
    status, body, latency = _http_post_json(
        token_url,
        {"scope": scopes, "lifetime": "900s"},
        caller_token,
    )
    token = str(body.get("accessToken") or "") if isinstance(body, dict) else ""
    if status != 200 or not token:
        return token, service_account, latency, f"서비스 계정 가장 토큰 HTTP {status or 'timeout'}"
    return token, service_account, latency, ""


def _drive_folder_info(folder_id: str, token: str) -> dict[str, Any]:
    """Compute SA가 보는 Drive 폴더의 표시 이름과 소속을 반환한다."""
    params = urlencode(
        {
            "supportsAllDrives": "true",
            "fields": "id,name,driveId,mimeType,parents,trashed",
        }
    )
    url = f"https://www.googleapis.com/drive/v3/files/{quote(folder_id, safe='')}?{params}"
    status, body, latency = _http_json(url, token, timeout=15)
    result: dict[str, Any] = {
        "folderId": folder_id,
        "status": "FAIL",
        "name": "",
        "driveId": "",
        "parentIds": [],
        "latencyMs": latency,
    }
    if status != 200 or not isinstance(body, dict):
        result["reason"] = f"Drive HTTP {status or 'timeout'}"
        return result

    result["name"] = str(body.get("name") or "")
    result["driveId"] = str(body.get("driveId") or "")
    result["parentIds"] = _normalise_ids(body.get("parents"))
    if body.get("trashed"):
        result["reason"] = "휴지통에 있는 폴더입니다."
    elif str(body.get("mimeType") or "") != DRIVE_FOLDER_MIME_TYPE:
        result["reason"] = "폴더가 아닌 Drive 항목입니다."
    else:
        result["status"] = "OK"
        result["reason"] = ""
    return result


def _lookup_drive_folders(folder_ids: list[str], token: str) -> dict[str, Any]:
    """입력 순서를 유지하면서 Drive 폴더 ID를 실제 이름으로 해석한다."""
    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=min(12, max(1, len(folder_ids)))) as executor:
        folders = list(executor.map(lambda item: _drive_folder_info(item, token), folder_ids))
    resolved = sum(item["status"] == "OK" for item in folders)
    return {
        "status": "COMPLETE" if resolved == len(folders) else "PARTIAL",
        "folders": folders,
        "stats": {
            "requested": len(folder_ids),
            "resolved": resolved,
            "failed": len(folder_ids) - resolved,
        },
        "latencyMs": round((time.perf_counter() - started) * 1000),
    }


def _drive_service_account_status(
    cfg: dict[str, Any],
    project: str,
    caller_token: str,
    cache: _StatusRunCache | None = None,
) -> dict[str, Any]:
    drive_ids = _normalise_ids((cfg.get("drive") or {}).get("driveIds"))
    if not drive_ids:
        return _check("RESOURCE", "drive-service-account", "FAIL", "공유드라이브 ID가 없습니다.")

    gcloud_json = cache.gcloud_json if cache else _gcloud_json
    impersonated_token, service_account, token_latency, token_error = _service_account_access_token(
        project,
        caller_token,
        ["https://www.googleapis.com/auth/drive.readonly"],
        gcloud_json=gcloud_json,
    )
    if not service_account:
        return _check(
            "RESOURCE", "drive-service-account", "WARN", "프로젝트 번호를 확인하지 못했습니다."
        )
    if not impersonated_token:
        return _check(
            "RESOURCE",
            "drive-service-account",
            "WARN",
            f"SA 자동 확인 불가 · {token_error}",
            action=(
                "현재 계정에 roles/iam.serviceAccountTokenCreator 필요: "
                f"{service_account}"
            ),
            latencyMs=token_latency,
        )

    failures: list[str] = []
    names: list[str] = []
    total_latency = token_latency
    for drive_id in drive_ids:
        drive_url = f"https://www.googleapis.com/drive/v3/drives/{drive_id}?fields=id,name"
        status_code, body, latency = _http_json(drive_url, impersonated_token)
        total_latency += latency
        if status_code != 200:
            failures.append(f"{drive_id} (접근 HTTP {status_code or 'timeout'})")
            continue

        name = str(body.get("name") or drive_id)
        delta_url = (
            "https://www.googleapis.com/drive/v3/changes/startPageToken"
            f"?driveId={drive_id}&supportsAllDrives=true"
        )
        delta_status, _, delta_latency = _http_json(delta_url, impersonated_token)
        total_latency += delta_latency
        if delta_status != 200:
            failures.append(f"{drive_id} (변경 토큰 HTTP {delta_status or 'timeout'})")
            continue
        names.append(name)

    if failures:
        return _check(
            "RESOURCE",
            "drive-service-account",
            "FAIL",
            "SA 연결 실패: " + ", ".join(failures),
            action=f"Drive 멤버 관리에서 {service_account}를 뷰어 이상으로 추가",
            latencyMs=total_latency,
        )
    summary = ", ".join(names[:3])
    if len(names) > 3:
        summary += f" 외 {len(names) - 3}개"
    return _check(
        "RESOURCE",
        "drive-service-account",
        "OK",
        f"{len(drive_ids)}개 Drive SA 실접근 확인 · {summary}",
        latencyMs=total_latency,
    )


def _resource_status(
    code: str,
    cfg: dict[str, Any],
    common: dict[str, Any],
    cache: _StatusRunCache | None = None,
) -> list[dict]:
    checks: list[dict[str, Any]] = []
    project = str(common.get("GCP_PROJECT_ID") or "")
    region = str(common.get("GCP_REGION") or "asia-northeast3")
    gcloud = _gcloud_executable()
    if not gcloud:
        return [_check("RESOURCE", "gcloud", "FAIL", "gcloud를 찾을 수 없습니다.")]

    gcloud_json = cache.gcloud_json if cache else _gcloud_json
    run_command = cache.run_command if cache else _run_command

    ok, active = gcloud_json(["auth", "list", "--filter=status:ACTIVE"])
    if not ok or not active:
        return [_check("RESOURCE", "gcloud-auth", "FAIL", "활성 gcloud 계정이 없습니다.")]
    checks.append(_check("RESOURCE", "gcloud-auth", "OK", "활성 계정 확인"))

    bucket_inventory: dict[str, dict[str, Any]] | None = None
    if cache:
        list_ok, bucket_rows = gcloud_json(
            ["storage", "buckets", "list", f"--project={project}"]
        )
        if list_ok and isinstance(bucket_rows, list):
            bucket_inventory = {
                str(item.get("name") or ""): item
                for item in bucket_rows
                if isinstance(item, dict)
            }

    for key, label in (("hwpOriginal", "bucket-hwp"), ("source", "bucket-source")):
        bucket = str((cfg.get("buckets") or {}).get(key) or "")
        if bucket_inventory is not None:
            info = bucket_inventory.get(bucket) or {}
            ok = bool(info)
        else:
            ok, info = gcloud_json(["storage", "buckets", "describe", f"gs://{bucket}"])
        if ok:
            location = str(info.get("location") or "").lower()
            status = "OK" if not location or location == region.lower() else "WARN"
            checks.append(_check("RESOURCE", label, status, location or "존재"))
        else:
            checks.append(_check("RESOURCE", label, "FAIL", f"버킷 조회 실패: {bucket}"))

    database = str(common.get("FIRESTORE_DATABASE") or "rag-sync-state")
    ok, db = gcloud_json(
        ["firestore", "databases", "describe", f"--database={database}", f"--project={project}"]
    )
    db_type = str(db.get("type") or "") if ok else ""
    checks.append(
        _check(
            "RESOURCE",
            "firestore",
            "OK" if db_type == "FIRESTORE_NATIVE" else "FAIL",
            db_type or "조회 실패",
        )
    )

    token_ok, token = run_command([gcloud, "auth", "print-access-token", "--quiet"])
    split_enabled = bool((cfg.get("corpora") or {}).get("student"))
    audiences = dept_config.AUDIENCES if split_enabled else ("staff",)
    for audience in audiences:
        corpus = str((cfg.get("corpora") or {}).get(audience) or "")
        name = f"rag-corpus-{audience}"
        if not token_ok:
            checks.append(_check("RESOURCE", name, "WARN", "access token 발급 실패"))
            continue
        url = f"https://{region}-aiplatform.googleapis.com/v1/{corpus}"
        status_code, body, latency = _http_json(url, token)
        state = str(((body.get("corpusStatus") or {}).get("state")) or "UNKNOWN")
        if status_code == 200 and state in {"ACTIVE", "UNKNOWN"}:
            status = "OK" if state == "ACTIVE" else "WARN"
            checks.append(_check("RESOURCE", name, status, state, latencyMs=latency))
        else:
            checks.append(
                _check("RESOURCE", name, "FAIL", f"HTTP {status_code or 'timeout'} · {state}")
            )

    if token_ok:
        checks.append(_drive_service_account_status(cfg, project, token, cache))
    else:
        checks.append(
            _check(
                "RESOURCE",
                "drive-service-account",
                "WARN",
                "gcloud access token이 없어 SA Drive 접근을 확인하지 못했습니다.",
                action="gcloud auth login",
            )
        )
    return checks


def _deploy_and_runtime_status(
    code: str,
    common: dict[str, Any],
    cfg: dict[str, Any] | None = None,
    cache: _StatusRunCache | None = None,
) -> list[dict]:
    checks: list[dict[str, Any]] = []
    project = str(common.get("GCP_PROJECT_ID") or "")
    region = str(common.get("GCP_REGION") or "asia-northeast3")
    gcloud = _gcloud_executable()
    if not gcloud:
        return [_check("DEPLOY", "gcloud", "FAIL", "gcloud를 찾을 수 없습니다.")]
    gcloud_json = cache.gcloud_json if cache else _gcloud_json
    run_command = cache.run_command if cache else _run_command
    split_enabled = bool(((cfg or {}).get("corpora") or {}).get("student"))
    services = ["rag-parser", "rag-sync", f"rag-mcp-{code}-staff"]
    if split_enabled:
        services.append(f"rag-mcp-{code}-student")
    discovered: dict[str, str] = {}
    service_inventory: dict[str, dict[str, Any]] | None = None
    if cache:
        list_ok, service_rows = gcloud_json(
            [
                "run",
                "services",
                "list",
                "--platform=managed",
                f"--region={region}",
                f"--project={project}",
            ]
        )
        if list_ok and isinstance(service_rows, list):
            service_inventory = {
                str((item.get("metadata") or {}).get("name") or ""): item
                for item in service_rows
                if isinstance(item, dict)
            }
    for service in services:
        if service_inventory is not None:
            data = service_inventory.get(service) or {}
            ok = bool(data)
        else:
            ok, data = gcloud_json(
                [
                    "run",
                    "services",
                    "describe",
                    service,
                    f"--region={region}",
                    f"--project={project}",
                ]
            )
        label = service.removeprefix("rag-")
        if not ok:
            missing_detail = str(data or "").lower()
            confirmed_missing = service_inventory is not None or any(
                marker in missing_detail
                for marker in ("not found", "not_found", "does not exist")
            )
            if confirmed_missing and service.startswith(f"rag-mcp-{code}-"):
                checks.append(
                    _check(
                        "DEPLOY",
                        label,
                        "WARN",
                        "Cloud Run MCP 서비스가 아직 배포되지 않았습니다.",
                        action="MCP 배포",
                        actionType="MCP_DEPLOY",
                        departmentCode=code,
                    )
                )
            else:
                checks.append(_check("DEPLOY", label, "FAIL", "Cloud Run 서비스 없음 또는 조회 실패"))
            continue
        status_data = data.get("status") or {}
        ready_condition = next(
            (item for item in status_data.get("conditions") or [] if item.get("type") == "Ready"),
            {},
        )
        ready = str(ready_condition.get("status") or "").lower() == "true"
        latest_ready = str(status_data.get("latestReadyRevisionName") or "")
        latest_created = str(status_data.get("latestCreatedRevisionName") or "")
        revision_ok = bool(latest_ready and latest_ready == latest_created)
        status = "OK" if ready and revision_ok else "FAIL"
        detail = latest_ready or str(ready_condition.get("message") or "Ready 아님")
        url = str(status_data.get("url") or "")
        checks.append(
            _check(
                "DEPLOY",
                label,
                status,
                detail,
                serviceName=service,
                url=url,
            )
        )
        if ready and url:
            discovered[service] = url

    for service in services:
        label = service.removeprefix("rag-") + "-health"
        url = discovered.get(service)
        if not url:
            checks.append(_check("RUNTIME", label, "SKIP", "배포 상태가 준비되지 않음"))
            continue
        token = ""
        if service in {"rag-parser", "rag-sync"}:
            # 사용자 gcloud 계정은 --audiences를 지원하지 않는다. Cloud Run 개발자
            # 호출은 gcloud의 기본 사용자 ID 토큰으로 인증할 수 있다.
            ok, token = run_command([gcloud, "auth", "print-identity-token", "--quiet"])
            if not ok:
                checks.append(_check("RUNTIME", label, "WARN", "ID token 발급 실패"))
                continue
        health_url = url.rstrip("/") + "/health"
        status_code, body, latency = _http_json(health_url, token)
        retried = False
        if status_code == 0:
            # min-instances=0 서비스는 첫 요청 10초 안에 기동하지 못할 수 있다.
            # 첫 요청이 인스턴스를 깨운 뒤 한 번 더 확인해 콜드 스타트를 장애와 구분한다.
            retried = True
            status_code, body, retry_latency = _http_json(health_url, token, timeout=20)
            latency += retry_latency
        health = str(body.get("status") or "")
        status = "OK" if status_code == 200 and health == "ok" else (
            "WARN" if status_code == 200 and health == "degraded" else "FAIL"
        )
        checks.append(
            _check(
                "RUNTIME",
                label,
                status,
                (
                    f"HTTP {status_code or 'timeout'} · {health or '응답 없음'}"
                    + (" · cold-start retry" if retried else "")
                ),
                latencyMs=latency,
            )
        )
    return checks


def _sync_status(
    common: dict[str, Any], cache: _StatusRunCache | None = None
) -> list[dict]:
    project = str(common.get("GCP_PROJECT_ID") or "")
    region = str(common.get("GCP_REGION") or "asia-northeast3")
    gcloud_json = cache.gcloud_json if cache else _gcloud_json
    ok, rows = gcloud_json(
        [
            "workflows",
            "executions",
            "list",
            "rag-daily-sync",
            f"--location={region}",
            f"--project={project}",
            "--limit=1",
        ]
    )
    if not ok or not rows:
        return [_check("SYNC", "latest-workflow", "FAIL", "최근 실행 없음 또는 조회 실패")]
    row = rows[0]
    state = str(row.get("state") or "UNKNOWN")
    started_raw = str(row.get("startTime") or "")
    finished_raw = str(row.get("endTime") or "")
    status = "FAIL"
    detail = state
    if state == "ACTIVE":
        status = "WARN"
        detail = f"ACTIVE · {started_raw}"
    elif state == "SUCCEEDED":
        status = "OK"
        try:
            finished = datetime.fromisoformat(finished_raw)
            hours = (datetime.now(UTC) - finished).total_seconds() / 3600
            if hours > 26:
                status = "WARN"
            detail = f"SUCCEEDED · {hours:.1f}시간 전"
        except ValueError:
            detail = "SUCCEEDED"
    return [_check("SYNC", "latest-workflow", status, detail)]


def _json_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str) or not value.strip():
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _firestore_value(value: Any) -> Any:
    """Firestore REST typed value를 GUI 응답용 일반 값으로 바꾼다."""
    if not isinstance(value, dict):
        return None
    for key in ("stringValue", "timestampValue", "referenceValue", "bytesValue"):
        if key in value:
            return value[key]
    if "integerValue" in value:
        try:
            return int(value["integerValue"])
        except (TypeError, ValueError):
            return 0
    if "doubleValue" in value:
        try:
            return float(value["doubleValue"])
        except (TypeError, ValueError):
            return 0.0
    if "booleanValue" in value:
        return bool(value["booleanValue"])
    if "nullValue" in value:
        return None
    if "arrayValue" in value:
        values = (value.get("arrayValue") or {}).get("values") or []
        return [_firestore_value(item) for item in values]
    if "mapValue" in value:
        fields = (value.get("mapValue") or {}).get("fields") or {}
        return {key: _firestore_value(item) for key, item in fields.items()}
    return None


def _firestore_sync_progress(
    project: str,
    database: str,
    token: str,
    run_id: str,
) -> dict[str, Any]:
    if not re.fullmatch(r"[0-9a-f]{32}", run_id):
        return {}
    collection = SYNC_TOKEN_COLLECTION
    url = (
        "https://firestore.googleapis.com/v1/projects/"
        f"{quote(project, safe='')}/databases/{quote(database, safe='')}/documents/"
        f"{quote(collection, safe='')}/{quote(f'__run__{run_id}', safe='')}"
    )
    status, body, _ = _http_json(url, token, timeout=10)
    if status != 200 or not isinstance(body, dict):
        return {}
    fields = body.get("fields") or {}
    return {key: _firestore_value(value) for key, value in fields.items()}


def _execution_id(name: str) -> str:
    return str(name or "").rstrip("/").rsplit("/", 1)[-1]


def _execution_arguments(row: dict[str, Any]) -> dict[str, Any]:
    return _json_mapping(row.get("argument"))


def _execution_run_id(row: dict[str, Any]) -> str:
    labels = row.get("labels") if isinstance(row.get("labels"), dict) else {}
    return str(labels.get("run_id") or _execution_arguments(row).get("runId") or "")


def _sync_department_targets() -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    departments: dict[str, dict[str, Any]] = {}
    drive_owners: dict[str, str] = {}
    for code in dept_config.list_departments():
        try:
            config = _read_yaml(DEPT_DIR / f"{code}.yaml")
        except (OSError, TypeError, UnicodeError, yaml.YAMLError):
            continue
        drive = config.get("drive") or {}
        target = {
            "code": code,
            "name": str(config.get("name") or code),
            "driveIds": _normalise_ids(drive.get("driveIds")),
            "syncFolderIds": _normalise_ids(drive.get("syncFolderIds")),
            "studentFolderIds": _normalise_ids(drive.get("studentFolderIds")),
            "corpora": config.get("corpora") or {},
        }
        departments[code] = target
        for drive_id in target["driveIds"]:
            drive_owners[drive_id] = code
    return departments, drive_owners


def _sync_execution_record(
    row: dict[str, Any],
    departments: dict[str, dict[str, Any]],
    drive_owners: dict[str, str],
    progress: dict[str, Any] | None = None,
) -> dict[str, Any]:
    arguments = _execution_arguments(row)
    labels = row.get("labels") if isinstance(row.get("labels"), dict) else {}
    drive_ids = _normalise_ids(arguments.get("driveIds"))
    code = str(labels.get("department") or arguments.get("departmentCode") or "")
    if not code:
        owners = {drive_owners[item] for item in drive_ids if item in drive_owners}
        if len(owners) == 1:
            code = owners.pop()
    mode = str(labels.get("mode") or "")
    if mode not in {"backfill", "delta"}:
        mode = "backfill" if bool(arguments.get("backfill")) else "delta"
    run_id = str(labels.get("run_id") or arguments.get("runId") or "")
    result = _json_mapping(row.get("result"))
    error = row.get("error") if isinstance(row.get("error"), dict) else {}
    state = str(row.get("state") or "UNKNOWN")
    progress_data = progress or {}
    effective_mode = str(progress_data.get("mode") or mode)
    return {
        "runId": run_id,
        "executionId": _execution_id(str(row.get("name") or "")),
        "state": state,
        "mode": mode,
        "effectiveMode": effective_mode,
        "departmentCode": code,
        "departmentName": str((departments.get(code) or {}).get("name") or code),
        "driveIds": drive_ids,
        "startTime": str(row.get("startTime") or ""),
        "endTime": str(row.get("endTime") or ""),
        "progress": progress_data,
        "totals": result.get("totals") if isinstance(result.get("totals"), dict) else {},
        "ok": result.get("ok") if "ok" in result else None,
        "error": str(error.get("context") or error.get("message") or ""),
        "manual": bool(run_id),
    }


def _cloud_run_sync_urls(project: str, region: str) -> tuple[str, str]:
    urls: dict[str, str] = {}
    for service in ("rag-sync", "rag-parser"):
        ok, body = _gcloud_json(
            [
                "run",
                "services",
                "describe",
                service,
                f"--region={region}",
                f"--project={project}",
            ],
            timeout=20,
        )
        url = str((body.get("status") or {}).get("url") or "") if ok else ""
        if not url:
            raise RuntimeError(f"{service} 배포 URL을 확인하지 못했습니다.")
        urls[service] = url
    return urls["rag-sync"], urls["rag-parser"]


def _list_sync_execution_rows(
    project: str, region: str, *, limit: int = SYNC_RUN_HISTORY_LIMIT
) -> list[dict[str, Any]]:
    ok, rows = _gcloud_json(
        [
            "workflows",
            "executions",
            "list",
            SYNC_WORKFLOW_NAME,
            f"--location={region}",
            f"--project={project}",
            f"--limit={limit}",
            "--sort-by=~startTime",
        ],
        timeout=20,
    )
    if not ok or not isinstance(rows, list):
        raise RuntimeError(str(rows)[:300] or "동기화 실행 이력을 조회하지 못했습니다.")
    return [row for row in rows if isinstance(row, dict)]


def _sync_execution_records(
    common: dict[str, Any], rows: list[dict[str, Any]], token: str = ""
) -> list[dict[str, Any]]:
    project = str(common.get("GCP_PROJECT_ID") or "")
    database = str(common.get("FIRESTORE_DATABASE") or "rag-sync-state")
    departments, drive_owners = _sync_department_targets()
    run_ids = {_execution_run_id(row) for row in rows}
    run_ids = {item for item in run_ids if re.fullmatch(r"[0-9a-f]{32}", item)}
    progress_by_run: dict[str, dict[str, Any]] = {}
    if token and run_ids:
        def load_progress(run_id: str) -> tuple[str, dict[str, Any]]:
            return run_id, _firestore_sync_progress(project, database, token, run_id)

        with ThreadPoolExecutor(max_workers=min(8, len(run_ids))) as executor:
            progress_by_run = dict(executor.map(load_progress, sorted(run_ids)))
    return [
        _sync_execution_record(
            row,
            departments,
            drive_owners,
            progress_by_run.get(_execution_run_id(row)),
        )
        for row in rows
    ]


def _start_manual_sync(code: str, mode: str) -> dict[str, Any]:
    departments, _drive_owners = _sync_department_targets()
    target = departments.get(code)
    if not target:
        raise FileNotFoundError(code)
    if mode not in {"delta", "backfill"}:
        raise ValueError("동기화 방식은 delta 또는 backfill이어야 합니다.")
    drive_ids = target["driveIds"]
    if not drive_ids:
        raise ValueError("선택한 학과에 공유드라이브 ID가 없습니다.")

    common = _common()
    project = str(common.get("GCP_PROJECT_ID") or "")
    region = str(common.get("GCP_REGION") or "asia-northeast3")
    rows = _list_sync_execution_rows(project, region, limit=20)
    requested = set(drive_ids)
    for row in rows:
        if str(row.get("state") or "") != "ACTIVE":
            continue
        labels = row.get("labels") if isinstance(row.get("labels"), dict) else {}
        if str(labels.get("department") or "") == code:
            raise FileExistsError("선택한 학과의 Drive에서 이미 동기화가 실행 중입니다.")
        active_ids = set(_normalise_ids(_execution_arguments(row).get("driveIds")))
        if requested & active_ids:
            raise FileExistsError("선택한 학과의 Drive에서 이미 동기화가 실행 중입니다.")

    token = _sync_access_token()
    drive_check = _drive_service_account_status(
        {"drive": {"driveIds": drive_ids}}, project, token
    )
    if drive_check.get("status") == "FAIL":
        detail = str(drive_check.get("detail") or "Drive 서비스 계정 연결 실패")
        action = str(drive_check.get("action") or "")
        raise RuntimeError(f"{detail} · {action}" if action else detail)
    sync_url, parser_url = _cloud_run_sync_urls(project, region)
    run_id = uuid.uuid4().hex
    arguments = {
        "syncUrl": sync_url,
        "parserUrl": parser_url,
        "driveIds": drive_ids,
        "backfill": mode == "backfill",
        "runId": run_id,
        "departmentCode": code,
    }
    execution_url = (
        "https://workflowexecutions.googleapis.com/v1/projects/"
        f"{quote(project, safe='')}/locations/{quote(region, safe='')}/workflows/"
        f"{quote(SYNC_WORKFLOW_NAME, safe='')}/executions"
    )
    status, body, _ = _http_post_json(
        execution_url,
        {
            "argument": json.dumps(arguments, ensure_ascii=False, separators=(",", ":")),
            "labels": {"department": code, "mode": mode, "run_id": run_id},
        },
        token,
        timeout=20,
    )
    if status not in {200, 201} or not isinstance(body, dict):
        raise RuntimeError(f"Workflow 실행 요청 실패 (HTTP {status or 'timeout'})")
    created = dict(body)
    created.setdefault("argument", json.dumps(arguments, ensure_ascii=False))
    created.setdefault("labels", {"department": code, "mode": mode, "run_id": run_id})
    return _sync_execution_record(created, departments, {item: code for item in drive_ids})


def _warm_status_cache(cache: _StatusRunCache, common: dict[str, Any]) -> None:
    """서로 독립적인 공통 조회를 먼저 병렬 실행해 CLI 기동 대기를 겹친다."""
    project = str(common.get("GCP_PROJECT_ID") or "")
    region = str(common.get("GCP_REGION") or "asia-northeast3")
    database = str(common.get("FIRESTORE_DATABASE") or "rag-sync-state")
    gcloud = _gcloud_executable()
    if not gcloud or not project:
        return

    jobs: list[tuple[Any, list[str]]] = [
        (cache.gcloud_json, ["auth", "list", "--filter=status:ACTIVE"]),
        (cache.gcloud_json, ["storage", "buckets", "list", f"--project={project}"]),
        (
            cache.gcloud_json,
            [
                "firestore",
                "databases",
                "describe",
                f"--database={database}",
                f"--project={project}",
            ],
        ),
        (cache.gcloud_json, ["projects", "describe", project]),
        (
            cache.gcloud_json,
            [
                "run",
                "services",
                "list",
                "--platform=managed",
                f"--region={region}",
                f"--project={project}",
            ],
        ),
        (
            cache.gcloud_json,
            [
                "workflows",
                "executions",
                "list",
                "rag-daily-sync",
                f"--location={region}",
                f"--project={project}",
                "--limit=1",
            ],
        ),
        (cache.run_command, [gcloud, "auth", "print-access-token", "--quiet"]),
        (cache.run_command, [gcloud, "auth", "print-identity-token", "--quiet"]),
    ]
    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = [executor.submit(operation, args) for operation, args in jobs]
        for future in futures:
            future.result()


def _run_department_status(
    code: str, offline: bool, cache: _StatusRunCache | None = None
) -> dict[str, Any]:
    path = DEPT_DIR / f"{code}.yaml"
    checks = _local_status(code)
    if not path.exists() or any(item["status"] == "FAIL" for item in checks):
        for layer in ("RESOURCE", "DEPLOY", "RUNTIME", "SYNC"):
            checks.append(_check(layer, "prerequisite", "SKIP", "LOCAL 검사 실패"))
    elif offline:
        for layer in ("RESOURCE", "DEPLOY", "RUNTIME", "SYNC"):
            checks.append(_check(layer, "offline", "SKIP", "오프라인 검사"))
    else:
        cfg = _read_yaml(path)
        common = _common()
        checks.extend(_resource_status(code, cfg, common, cache))
        checks.extend(_deploy_and_runtime_status(code, common, cfg, cache))
        checks.extend(_sync_status(common, cache))
    result = {
        "code": code,
        "overall": _overall(checks),
        "checkedAt": datetime.now().astimezone().isoformat(timespec="seconds"),
        "configRevision": _config_revision(path) if path.exists() else None,
        "checks": checks,
    }
    _LATEST[code] = result
    return result


def _cleanup_runs() -> None:
    cutoff = time.time() - _RUN_TTL_SECONDS
    expired = [key for key, run in _RUNS.items() if run.get("finishedEpoch", time.time()) < cutoff]
    for key in expired:
        _RUNS.pop(key, None)


def _execute_run(run_id: str, codes: list[str], offline: bool) -> None:
    results_by_code: dict[str, dict[str, Any]] = {}
    cache = _StatusRunCache()

    def run_one(code: str) -> dict[str, Any] | None:
        with _RUN_LOCK:
            if _RUNS[run_id].get("cancelRequested"):
                return None
            _RUNS[run_id]["currentDepartment"] = code
        return _run_department_status(code, offline, cache)

    try:
        if not offline:
            _warm_status_cache(cache, _common())
        worker_count = min(4, len(codes))
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            futures = {executor.submit(run_one, code): code for code in codes}
            for future in as_completed(futures):
                code = futures[future]
                result = future.result()
                if result is not None:
                    results_by_code[code] = result
                with _RUN_LOCK:
                    ordered = [results_by_code[item] for item in codes if item in results_by_code]
                    _RUNS[run_id]["departments"] = ordered
        with _RUN_LOCK:
            run = _RUNS[run_id]
            if run.get("cancelRequested"):
                run["status"] = "CANCELLED"
            else:
                run["status"] = "COMPLETED"
            run["departments"] = [results_by_code[item] for item in codes if item in results_by_code]
            run["currentDepartment"] = None
            run["finishedEpoch"] = time.time()
            run["finishedAt"] = datetime.now().astimezone().isoformat(timespec="seconds")
    except Exception as exc:  # noqa: BLE001 - background job boundary
        with _RUN_LOCK:
            run = _RUNS[run_id]
            run["status"] = "FAILED"
            run["error"] = str(exc)[:500]
            run["finishedEpoch"] = time.time()


def _require_local_session(request: Request) -> None:
    origin = request.headers.get("origin")
    if origin and (urlparse(origin).hostname not in {"127.0.0.1", "localhost", "testserver"}):
        raise HTTPException(status_code=403, detail="invalid origin")
    if request.headers.get("x-local-session") != _SESSION_NONCE:
        raise HTTPException(status_code=403, detail="invalid local session")


app = FastAPI(title="GCP RAG 학과 관리", docs_url=None, redoc_url=None)


@app.middleware("http")
async def security_headers(request: Request, call_next):  # type: ignore[no-untyped-def]
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; style-src 'self'; "
        "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'"
    )
    return response


@app.get("/api/v1/session")
def session() -> dict[str, str]:
    return {"nonce": _SESSION_NONCE}


@app.get("/api/v1/departments")
def departments() -> dict[str, Any]:
    return {"departments": list_department_records()}


@app.get("/api/v1/departments/code-availability")
def code_availability(code: str, current: str = "") -> JSONResponse:
    return JSONResponse(department_code_availability(code, current_code=current))


@app.post("/api/v1/departments/resource-plans")
async def resource_plan(request: Request) -> JSONResponse:
    _require_local_session(request)
    payload = await request.json()
    if not isinstance(payload, dict) or _has_secret_input(payload):
        return JSONResponse(
            {"error": {"code": "INVALID_RESOURCE_PLAN", "message": "생성 계획 요청이 올바르지 않습니다."}},
            status_code=400,
        )
    try:
        plan = create_resource_plan(payload)
    except FileExistsError as exc:
        return JSONResponse(
            {
                "error": {
                    "code": "DEPARTMENT_CODE_EXISTS",
                    "message": str(exc),
                    "fieldErrors": {"code": [str(exc)]},
                }
            },
            status_code=409,
        )
    except (ValueError, TypeError) as exc:
        return JSONResponse(
            {"error": {"code": "INVALID_RESOURCE_PLAN", "message": str(exc)}},
            status_code=422,
        )
    except (FileNotFoundError, OSError, UnicodeError, yaml.YAMLError):
        return JSONResponse(
            {"error": {"code": "COMMON_REQUIRED", "message": "공통 설정이 먼저 필요합니다."}},
            status_code=428,
        )
    return JSONResponse(plan, status_code=201)


@app.post("/api/v1/departments/resource-provisioning")
async def provision_resources(request: Request) -> JSONResponse:
    _require_local_session(request)
    payload = await request.json()
    plan_id = str(payload.get("planId") or "") if isinstance(payload, dict) else ""
    overrides = payload.get("overrides") if isinstance(payload, dict) else None
    if not re.fullmatch(r"[0-9a-f]{32}", plan_id):
        return JSONResponse(
            {"error": {"code": "INVALID_RESOURCE_PLAN", "message": "생성 계획을 다시 열어 주세요."}},
            status_code=422,
        )
    try:
        run = start_provision_run(plan_id, overrides)
    except (TypeError, ValueError) as exc:
        return JSONResponse(
            {"error": {"code": "INVALID_RESOURCE_NAME", "message": str(exc)}},
            status_code=422,
        )
    except FileNotFoundError:
        return JSONResponse(
            {"error": {"code": "RESOURCE_PLAN_EXPIRED", "message": "생성 계획이 만료되었습니다. 다시 확인해 주세요."}},
            status_code=404,
        )
    except FileExistsError as exc:
        return JSONResponse(
            {"error": {"code": "RESOURCE_PROVISION_CONFLICT", "message": str(exc)}},
            status_code=409,
        )
    return JSONResponse(run, status_code=202)


@app.get("/api/v1/departments/resource-provisioning/{run_id}")
def provision_run(run_id: str) -> JSONResponse:
    with _PROVISION_LOCK:
        _cleanup_provision_state()
        run = _PROVISION_RUNS.get(run_id)
        if not run:
            return JSONResponse(
                {"error": {"code": "PROVISION_RUN_NOT_FOUND", "message": "리소스 생성 실행을 찾을 수 없습니다."}},
                status_code=404,
            )
        return JSONResponse(copy.deepcopy(run))


@app.get("/api/v1/mcp-deployments")
def mcp_deployments(code: str = "", status: str = "") -> JSONResponse:
    with _MCP_DEPLOY_LOCK:
        _cleanup_mcp_deployments()
        rows = [
            copy.deepcopy(run)
            for run in _MCP_DEPLOY_RUNS.values()
            if (not code or run["code"] == code)
            and (not status or run["status"] == status.upper())
        ]
    rows.sort(key=lambda item: item.get("createdEpoch", 0), reverse=True)
    return JSONResponse({"runs": rows})


@app.get("/api/v1/mcp-deployments/{run_id}")
def mcp_deployment(run_id: str) -> JSONResponse:
    with _MCP_DEPLOY_LOCK:
        _cleanup_mcp_deployments()
        run = _MCP_DEPLOY_RUNS.get(run_id)
        if not run:
            return JSONResponse(
                {"error": {"code": "MCP_DEPLOYMENT_NOT_FOUND", "message": "MCP 배포 작업을 찾을 수 없습니다."}},
                status_code=404,
            )
        return JSONResponse(copy.deepcopy(run))


@app.post("/api/v1/departments/{code}/mcp-deployments")
def create_mcp_deployment(code: str, request: Request) -> JSONResponse:
    _require_local_session(request)
    try:
        run = start_mcp_deployment(code)
    except FileNotFoundError:
        return JSONResponse(
            {"error": {"code": "NOT_FOUND", "message": "학과 설정을 찾을 수 없습니다."}},
            status_code=404,
        )
    except FileExistsError as exc:
        return JSONResponse(
            {
                "error": {
                    "code": "MCP_DEPLOYMENT_RUNNING",
                    "message": "이 학과의 MCP 배포가 이미 진행 중입니다.",
                    "runId": str(exc),
                }
            },
            status_code=409,
        )
    except (OSError, RuntimeError, SystemExit, TypeError, ValueError, yaml.YAMLError) as exc:
        return JSONResponse(
            {"error": {"code": "MCP_DEPLOYMENT_INVALID", "message": str(exc)[:400]}},
            status_code=422,
        )
    return JSONResponse(run, status_code=202)


@app.get("/api/v1/environment")
def environment() -> dict[str, Any]:
    common_path = CONFIG_DIR / "common.yaml"
    common_exists = common_path.exists()
    common_valid = False
    common_error = ""
    common: dict[str, Any] = {}
    if common_exists:
        try:
            common = _common()
            common_valid = bool(common.get("GCP_PROJECT_ID"))
            if not common_valid:
                common_error = "GCP_PROJECT_ID가 없습니다."
        except (OSError, TypeError, UnicodeError, yaml.YAMLError) as exc:
            common_error = str(exc)[:200]
    bootstrap = _gcloud_bootstrap_state(include_projects=not common_exists)
    configured_project = str(common.get("GCP_PROJECT_ID") or "")
    service_account = (
        _default_compute_service_account(configured_project)
        if bootstrap["authenticated"] and configured_project
        else ""
    )
    return {
        "repository": str(ROOT),
        "pythonVersion": sys.version.split()[0],
        "gcloudInstalled": bootstrap["installed"],
        "gcloudAuthenticated": bootstrap["authenticated"],
        "gcloudAccount": bootstrap["account"],
        "configuredProject": configured_project,
        "serviceAccount": service_account,
        "gcloudProject": bootstrap["currentProject"],
        "gcloudProjects": bootstrap["projects"],
        "availableRegions": bootstrap["regions"],
        "region": str(common.get("GCP_REGION") or "asia-northeast3"),
        "departmentCount": len(dept_config.list_departments()),
        "commonExists": common_exists,
        "commonValid": common_valid,
        "commonError": common_error,
    }


@app.get("/api/v1/common-config/resources")
def common_config_resources(project: str, region: str) -> JSONResponse:
    if not PROJECT_RE.fullmatch(project) or not REGION_RE.fullmatch(region):
        return JSONResponse(
            {"error": {"code": "INVALID_SCOPE", "message": "프로젝트와 리전을 확인해 주세요."}},
            status_code=400,
        )
    bootstrap = _gcloud_bootstrap_state()
    if not bootstrap["installed"] or not bootstrap["authenticated"]:
        return JSONResponse(
            {"error": {"code": "GCLOUD_AUTH_REQUIRED", "message": "gcloud 로그인이 필요합니다."}},
            status_code=412,
        )
    accessible_projects = {item["id"] for item in bootstrap["projects"]}
    if project not in accessible_projects:
        return JSONResponse(
            {"error": {"code": "PROJECT_FORBIDDEN", "message": "접근할 수 없는 프로젝트입니다."}},
            status_code=403,
        )
    return JSONResponse(_gcloud_project_resources(project, region))


@app.post("/api/v1/common-config/resource-plans")
async def common_config_resource_plan(request: Request) -> JSONResponse:
    """무엇을 켜고 무엇을 만들지만 돌려준다. 여기서는 아무것도 바꾸지 않는다."""
    _require_local_session(request)
    payload = await request.json()
    if not isinstance(payload, dict):
        return JSONResponse(
            {"error": {"code": "INVALID_PAYLOAD", "message": "요청 본문을 확인해 주세요."}},
            status_code=422,
        )
    bootstrap = _gcloud_bootstrap_state()
    if not bootstrap["installed"] or not bootstrap["authenticated"]:
        return JSONResponse(
            {"error": {"code": "GCLOUD_AUTH_REQUIRED", "message": "gcloud 로그인이 필요합니다."}},
            status_code=412,
        )
    accessible_projects = {item["id"] for item in bootstrap["projects"]}
    if str(payload.get("projectId") or "") not in accessible_projects:
        return JSONResponse(
            {"error": {"code": "PROJECT_FORBIDDEN", "message": "접근할 수 없는 프로젝트입니다."}},
            status_code=403,
        )
    try:
        plan = create_common_resource_plan(payload)
    except (TypeError, ValueError) as exc:
        return JSONResponse(
            {"error": {"code": "INVALID_RESOURCE_NAME", "message": str(exc)}},
            status_code=422,
        )
    return JSONResponse(plan, status_code=200)


@app.post("/api/v1/common-config/resource-provisioning")
async def common_config_provision(request: Request) -> JSONResponse:
    _require_local_session(request)
    payload = await request.json()
    plan_id = str(payload.get("planId") or "") if isinstance(payload, dict) else ""
    if not re.fullmatch(r"[0-9a-f]{32}", plan_id):
        return JSONResponse(
            {"error": {"code": "INVALID_RESOURCE_PLAN", "message": "생성 계획을 다시 열어 주세요."}},
            status_code=422,
        )
    try:
        run = start_common_provision_run(plan_id)
    except FileNotFoundError:
        return JSONResponse(
            {
                "error": {
                    "code": "RESOURCE_PLAN_EXPIRED",
                    "message": "생성 계획이 만료되었습니다. 다시 확인해 주세요.",
                }
            },
            status_code=404,
        )
    except FileExistsError as exc:
        return JSONResponse(
            {"error": {"code": "RESOURCE_PROVISION_CONFLICT", "message": str(exc)}},
            status_code=409,
        )
    return JSONResponse(run, status_code=202)


@app.get("/api/v1/common-config/resource-provisioning/{run_id}")
def common_config_provision_run(run_id: str) -> JSONResponse:
    with _COMMON_PROVISION_LOCK:
        _cleanup_common_provision_state()
        run = _COMMON_PROVISION_RUNS.get(run_id)
        if not run:
            return JSONResponse(
                {
                    "error": {
                        "code": "PROVISION_RUN_NOT_FOUND",
                        "message": "리소스 생성 실행을 찾을 수 없습니다.",
                    }
                },
                status_code=404,
            )
        return JSONResponse(copy.deepcopy(run))


@app.post("/api/v1/gcloud-auth/login")
def start_gcloud_login(request: Request) -> JSONResponse:
    _require_local_session(request)
    bootstrap = _gcloud_bootstrap_state(include_projects=False)
    if not bootstrap["installed"]:
        return JSONResponse(
            {"error": {"code": "GCLOUD_NOT_FOUND", "message": "gcloud를 찾을 수 없습니다."}},
            status_code=404,
        )
    if bootstrap["authenticated"]:
        return JSONResponse({"started": False, "authenticated": True})
    try:
        started = _start_gcloud_login()
    except (OSError, ValueError) as exc:
        return JSONResponse(
            {"error": {"code": "LOGIN_START_FAILED", "message": str(exc)[:300]}},
            status_code=500,
        )
    return JSONResponse(
        {"started": started, "authenticated": False, "message": "브라우저 로그인을 기다리는 중"},
        status_code=202,
    )


@app.post("/api/v1/common-config")
async def create_common(request: Request) -> JSONResponse:
    _require_local_session(request)
    payload = await request.json()
    if not isinstance(payload, dict) or _has_secret_input(payload):
        return JSONResponse(
            {"error": {"code": "INVALID_COMMON_CONFIG", "message": "요청이 올바르지 않습니다."}},
            status_code=400,
        )
    bootstrap = _gcloud_bootstrap_state()
    if not bootstrap["installed"] or not bootstrap["authenticated"]:
        return JSONResponse(
            {
                "error": {
                    "code": "GCLOUD_AUTH_REQUIRED",
                    "message": "gcloud auth login 후 로그인 상태를 다시 확인해 주세요.",
                }
            },
            status_code=412,
        )
    candidate, result = validate_common_candidate(payload)
    accessible_projects = {item["id"] for item in bootstrap["projects"]}
    if candidate["GCP_PROJECT_ID"] not in accessible_projects:
        _field_error(
            result["fieldErrors"],
            "projectId",
            "현재 gcloud 계정으로 접근 가능한 프로젝트를 선택해 주세요.",
        )
        result["valid"] = False
    if result["valid"]:
        resources = _gcloud_project_resources(
            candidate["GCP_PROJECT_ID"], candidate["GCP_REGION"]
        )
        artifact_ids = {item["id"] for item in resources["artifactRepositories"]}
        database_ids = {item["id"] for item in resources["firestoreDatabases"]}
        if candidate["ARTIFACT_REPO"] not in artifact_ids:
            _field_error(
                result["fieldErrors"],
                "artifactRepo",
                "선택한 프로젝트·리전에 존재하는 Docker 저장소를 선택해 주세요.",
            )
            result["valid"] = False
        if candidate["FIRESTORE_DATABASE"] not in database_ids:
            _field_error(
                result["fieldErrors"],
                "firestoreDatabase",
                "선택한 프로젝트에 존재하는 Native Firestore DB를 선택해 주세요.",
            )
            result["valid"] = False
    if not result["valid"]:
        return JSONResponse(
            {
                "error": {
                    "code": "VALIDATION_FAILED",
                    "message": "공통 설정 입력값을 확인해 주세요.",
                    "fieldErrors": result["fieldErrors"],
                }
            },
            status_code=422,
        )
    try:
        target = create_common_config(candidate)
    except FileExistsError:
        return JSONResponse(
            {
                "error": {
                    "code": "FILE_EXISTS",
                    "message": "common.yaml이 이미 있어 덮어쓰지 않았습니다.",
                }
            },
            status_code=409,
        )
    except (OSError, RuntimeError, TypeError, ValueError, yaml.YAMLError) as exc:
        return JSONResponse(
            {"error": {"code": "WRITE_FAILED", "message": str(exc)[:300]}}, status_code=500
        )
    return JSONResponse(
        {"created": True, "path": f"config/{target.name}", "projectId": candidate["GCP_PROJECT_ID"]},
        status_code=201,
    )


@app.get("/api/v1/departments/resource-options")
def department_resource_options() -> JSONResponse:
    try:
        common = _common()
    except (FileNotFoundError, OSError, TypeError, UnicodeError, yaml.YAMLError):
        return JSONResponse(
            {"error": {"code": "COMMON_REQUIRED", "message": "공통 설정이 먼저 필요합니다."}},
            status_code=428,
        )
    options = _department_resource_options(common)
    usage = _department_bucket_usage()
    for bucket in options["buckets"]:
        bucket["usedBy"] = usage.get(str(bucket.get("name") or ""), [])
    if options.get("error"):
        return JSONResponse(
            {"error": {"code": "RESOURCE_LOOKUP_FAILED", "message": options["error"]}},
            status_code=503,
        )
    return JSONResponse(options)


@app.get("/api/v1/departments/{code}/mcp-servers")
def get_department_mcp_servers(code: str) -> JSONResponse:
    try:
        return JSONResponse(department_mcp_servers(code))
    except FileNotFoundError:
        return JSONResponse(
            {"error": {"code": "NOT_FOUND", "message": "학과 설정을 찾을 수 없습니다."}},
            status_code=404,
        )
    except (OSError, RuntimeError, TypeError, UnicodeError, yaml.YAMLError) as exc:
        return JSONResponse(
            {"error": {"code": "MCP_LOOKUP_FAILED", "message": str(exc)[:300]}},
            status_code=503,
        )


@app.post("/api/v1/departments/{code}/mcp-keys/{audience}")
def copy_department_mcp_key(code: str, audience: str, request: Request) -> JSONResponse:
    _require_local_session(request)
    try:
        key = department_mcp_key(code, audience)
    except FileNotFoundError:
        return JSONResponse(
            {"error": {"code": "NOT_FOUND", "message": "학과 설정을 찾을 수 없습니다."}},
            status_code=404,
        )
    except (TypeError, ValueError, yaml.YAMLError) as exc:
        return JSONResponse(
            {"error": {"code": "MCP_KEY_UNAVAILABLE", "message": str(exc)}},
            status_code=422,
        )
    except (OSError, UnicodeError) as exc:
        return JSONResponse(
            {"error": {"code": "MCP_KEY_READ_FAILED", "message": str(exc)[:300]}},
            status_code=500,
        )
    return JSONResponse({"audience": audience, "key": key})


@app.post("/api/v1/corpus-query")
async def corpus_query(request: Request) -> JSONResponse:
    _require_local_session(request)
    payload = await request.json()
    if not isinstance(payload, dict):
        payload = {}
    try:
        result = retrieve_department_corpus(
            str(payload.get("code") or ""),
            str(payload.get("audience") or ""),
            str(payload.get("query") or ""),
            int(payload.get("topK") or 5),
            generate=bool(payload.get("generate")),
        )
    except FileNotFoundError:
        return JSONResponse(
            {"error": {"code": "NOT_FOUND", "message": "학과 설정을 찾을 수 없습니다."}},
            status_code=404,
        )
    except (TypeError, ValueError) as exc:
        return JSONResponse(
            {"error": {"code": "INVALID_CORPUS_QUERY", "message": str(exc)}},
            status_code=422,
        )
    except (OSError, RuntimeError, UnicodeError, yaml.YAMLError) as exc:
        return JSONResponse(
            {"error": {"code": "CORPUS_QUERY_FAILED", "message": str(exc)[:400]}},
            status_code=503,
        )
    return JSONResponse(result)


@app.post("/api/v1/departments/drive-preflight")
async def drive_preflight(request: Request) -> JSONResponse:
    _require_local_session(request)
    payload = await request.json()
    drive_ids = _normalise_ids(payload.get("driveIds") if isinstance(payload, dict) else None)
    if not drive_ids:
        return JSONResponse(
            {
                "error": {
                    "code": "DRIVE_ID_REQUIRED",
                    "message": "확인할 공유드라이브 ID를 입력해 주세요.",
                }
            },
            status_code=422,
        )
    try:
        common = _common()
    except (FileNotFoundError, OSError, TypeError, UnicodeError, yaml.YAMLError):
        return JSONResponse(
            {"error": {"code": "COMMON_REQUIRED", "message": "공통 설정이 먼저 필요합니다."}},
            status_code=428,
        )
    gcloud = _gcloud_executable()
    if not gcloud:
        return JSONResponse(
            {"error": {"code": "GCLOUD_REQUIRED", "message": "gcloud를 찾을 수 없습니다."}},
            status_code=503,
        )
    token_ok, token = _run_command([gcloud, "auth", "print-access-token", "--quiet"])
    if not token_ok:
        return JSONResponse(
            {"error": {"code": "GCLOUD_AUTH_REQUIRED", "message": "gcloud 로그인이 필요합니다."}},
            status_code=401,
        )
    project = str(common.get("GCP_PROJECT_ID") or "")
    result = _drive_service_account_status(
        {"drive": {"driveIds": drive_ids}}, project, token
    )
    code = str(payload.get("code") or "").strip().lower() if isinstance(payload, dict) else ""
    return JSONResponse(
        {
            "status": result["status"],
            "detail": result["detail"],
            "action": result.get("action", ""),
            "latencyMs": result.get("latencyMs", 0),
            "driveIds": drive_ids,
            "driveConflicts": _department_drive_conflicts(code, drive_ids),
        }
    )


@app.post("/api/v1/departments/folder-lookup")
async def drive_folder_lookup(request: Request) -> JSONResponse:
    """동기화 폴더 ID를 Compute SA가 보는 실제 Drive 폴더명으로 해석한다."""
    _require_local_session(request)
    payload = await request.json()
    folder_ids = _normalise_ids(
        payload.get("folderIds") if isinstance(payload, dict) else None
    )
    if not folder_ids:
        return JSONResponse(
            {
                "error": {
                    "code": "FOLDER_ID_REQUIRED",
                    "message": "확인할 동기화 폴더 ID를 입력해 주세요.",
                }
            },
            status_code=422,
        )
    if len(folder_ids) > DRIVE_FOLDER_LOOKUP_LIMIT or any(
        not DRIVE_FILE_ID_RE.fullmatch(folder_id) for folder_id in folder_ids
    ):
        return JSONResponse(
            {
                "error": {
                    "code": "INVALID_FOLDER_IDS",
                    "message": (
                        f"Drive 폴더 ID 형식을 확인해 주세요. "
                        f"한 번에 최대 {DRIVE_FOLDER_LOOKUP_LIMIT}개까지 확인할 수 있습니다."
                    ),
                }
            },
            status_code=422,
        )
    try:
        common = _common()
    except (FileNotFoundError, OSError, TypeError, UnicodeError, yaml.YAMLError):
        return JSONResponse(
            {"error": {"code": "COMMON_REQUIRED", "message": "공통 설정이 먼저 필요합니다."}},
            status_code=428,
        )
    gcloud = _gcloud_executable()
    if not gcloud:
        return JSONResponse(
            {"error": {"code": "GCLOUD_REQUIRED", "message": "gcloud를 찾을 수 없습니다."}},
            status_code=503,
        )
    token_ok, caller_token = _run_command([gcloud, "auth", "print-access-token", "--quiet"])
    if not token_ok:
        return JSONResponse(
            {"error": {"code": "GCLOUD_AUTH_REQUIRED", "message": "gcloud 로그인이 필요합니다."}},
            status_code=401,
        )
    token, service_account, _latency, error = _service_account_access_token(
        str(common.get("GCP_PROJECT_ID") or ""),
        caller_token,
        ["https://www.googleapis.com/auth/drive.readonly"],
    )
    if not token:
        suffix = f" ({service_account})" if service_account else ""
        return JSONResponse(
            {
                "error": {
                    "code": "DRIVE_FOLDER_LOOKUP_FAILED",
                    "message": f"{error}{suffix}",
                }
            },
            status_code=503,
        )
    result = _lookup_drive_folders(folder_ids, token)
    result["serviceAccount"] = service_account
    return JSONResponse(result)


@app.post("/api/v1/departments/preview")
async def preview(request: Request) -> JSONResponse:
    _require_local_session(request)
    payload = await request.json()
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="invalid request")
    if _has_secret_input(payload):
        return JSONResponse(
            {"error": {"code": "UNSUPPORTED_SECRET_INPUT", "message": "키는 서버에서 생성합니다."}},
            status_code=400,
        )
    candidate, result = validate_candidate(payload)
    if result["valid"]:
        _merge_live_resource_validation(candidate, result)
    result["yamlPreview"] = _render_yaml(candidate, preview=True)
    return JSONResponse(result)


@app.post("/api/v1/departments")
async def create(request: Request) -> JSONResponse:
    _require_local_session(request)
    payload = await request.json()
    if not isinstance(payload, dict) or _has_secret_input(payload):
        return JSONResponse(
            {"error": {"code": "UNSUPPORTED_SECRET_INPUT", "message": "키는 서버에서 생성합니다."}},
            status_code=400,
        )
    candidate, result = validate_candidate(payload, check_existing=False)
    if result["valid"]:
        _merge_live_resource_validation(candidate, result)
    if not result["valid"]:
        return JSONResponse(
            {
                "error": {
                    "code": "VALIDATION_FAILED",
                    "message": "입력값을 확인해 주세요.",
                    "fieldErrors": result["fieldErrors"],
                }
            },
            status_code=422,
        )
    conflict = _unacked_drive_conflicts(payload, result)
    if conflict:
        return conflict
    code = str(payload["code"]).strip().lower()
    try:
        target = create_department(
            code, candidate, allow_duplicate_drives=bool(payload.get("allowDuplicateDriveIds"))
        )
    except FileExistsError:
        return JSONResponse(
            {"error": {"code": "FILE_EXISTS", "message": "기존 YAML은 변경하지 않았습니다."}},
            status_code=409,
        )
    except (OSError, RuntimeError, TypeError, ValueError, yaml.YAMLError) as exc:
        return JSONResponse(
            {"error": {"code": "WRITE_FAILED", "message": str(exc)[:300]}}, status_code=500
        )
    return JSONResponse(
        {
            "code": code,
            "path": f"config/departments/{target.name}",
            "created": True,
            "nextActions": ["status", "deploy"],
        },
        status_code=201,
    )


@app.get("/api/v1/departments/{code}/config")
def get_department_config(code: str) -> JSONResponse:
    try:
        return JSONResponse(department_public_config(code))
    except (FileNotFoundError, OSError, TypeError, UnicodeError, yaml.YAMLError):
        return JSONResponse(
            {"error": {"code": "NOT_FOUND", "message": "학과 설정을 찾을 수 없습니다."}},
            status_code=404,
        )


@app.post("/api/v1/departments/{code}/preview")
async def preview_update(code: str, request: Request) -> JSONResponse:
    _require_local_session(request)
    payload = await request.json()
    if (
        not isinstance(payload, dict)
        or _has_secret_input(payload)
        or str(payload.get("code") or "").strip().lower() != code
    ):
        return JSONResponse(
            {"error": {"code": "INVALID_UPDATE", "message": "수정 요청이 올바르지 않습니다."}},
            status_code=400,
        )
    try:
        current = department_public_config(code)
    except (FileNotFoundError, OSError, TypeError, UnicodeError, yaml.YAMLError):
        return JSONResponse(
            {"error": {"code": "NOT_FOUND", "message": "학과 설정을 찾을 수 없습니다."}},
            status_code=404,
        )
    candidate, result = validate_candidate(payload, check_existing=False)
    candidate_mode = "split" if candidate.get("corpora", {}).get("student") else "single"
    if candidate_mode != current["corpusMode"]:
        _field_error(
            result["fieldErrors"],
            "corpusMode",
            "운영 중 코퍼스 구성 변경은 재색인·기존 서비스 정리가 필요해 별도 마이그레이션으로 진행해야 합니다.",
        )
        result["valid"] = False
    if result["valid"]:
        _merge_live_resource_validation(candidate, result)
    result["yamlPreview"] = _render_yaml(
        candidate,
        preview=True,
        keys={"staff": "<기존 키 유지>", "student": "<기존 키 유지>"},
    )
    return JSONResponse(result)


@app.put("/api/v1/departments/{code}")
async def update(code: str, request: Request) -> JSONResponse:
    _require_local_session(request)
    payload = await request.json()
    if (
        not isinstance(payload, dict)
        or _has_secret_input(payload)
        or str(payload.get("code") or "").strip().lower() != code
    ):
        return JSONResponse(
            {"error": {"code": "INVALID_UPDATE", "message": "수정 요청이 올바르지 않습니다."}},
            status_code=400,
        )
    try:
        current = department_public_config(code)
    except (FileNotFoundError, OSError, TypeError, UnicodeError, yaml.YAMLError):
        return JSONResponse(
            {"error": {"code": "NOT_FOUND", "message": "학과 설정을 찾을 수 없습니다."}},
            status_code=404,
        )
    if payload.get("configRevision") != current["configRevision"]:
        return JSONResponse(
            {
                "error": {
                    "code": "REVISION_CONFLICT",
                    "message": "파일이 다른 곳에서 변경되었습니다. 다시 열어 주세요.",
                }
            },
            status_code=409,
        )
    candidate, result = validate_candidate(payload, check_existing=False)
    candidate_mode = "split" if candidate.get("corpora", {}).get("student") else "single"
    if candidate_mode != current["corpusMode"]:
        _field_error(
            result["fieldErrors"],
            "corpusMode",
            "운영 중 코퍼스 구성 변경은 별도 마이그레이션이 필요합니다.",
        )
        result["valid"] = False
    if result["valid"]:
        _merge_live_resource_validation(candidate, result)
    if not result["valid"]:
        return JSONResponse(
            {
                "error": {
                    "code": "VALIDATION_FAILED",
                    "message": "입력값을 확인해 주세요.",
                    "fieldErrors": result["fieldErrors"],
                }
            },
            status_code=422,
        )
    conflict = _unacked_drive_conflicts(payload, result)
    if conflict:
        return conflict
    try:
        target = update_department(
            code, candidate, allow_duplicate_drives=bool(payload.get("allowDuplicateDriveIds"))
        )
    except (OSError, RuntimeError, TypeError, ValueError, yaml.YAMLError) as exc:
        return JSONResponse(
            {"error": {"code": "WRITE_FAILED", "message": str(exc)[:300]}}, status_code=500
        )
    return JSONResponse(
        {
            "code": code,
            "path": f"config/departments/{target.name}",
            "updated": True,
            "configRevision": _config_revision(target),
        }
    )


@app.get("/api/v1/sync-runs")
def sync_runs(limit: int = 20) -> JSONResponse:
    try:
        common = _common()
        project = str(common.get("GCP_PROJECT_ID") or "")
        region = str(common.get("GCP_REGION") or "asia-northeast3")
        rows = _list_sync_execution_rows(
            project, region, limit=max(1, min(limit, SYNC_RUN_HISTORY_LIMIT))
        )
        token = _sync_access_token()
        records = _sync_execution_records(common, rows, token)
        departments, _drive_owners = _sync_department_targets()
    except (FileNotFoundError, OSError, RuntimeError, TypeError, UnicodeError, yaml.YAMLError) as exc:
        return JSONResponse(
            {"error": {"code": "SYNC_RUN_LOOKUP_FAILED", "message": str(exc)[:400]}},
            status_code=503,
        )
    return JSONResponse(
        {
            "runs": records,
            "departments": list(departments.values()),
            "workflow": SYNC_WORKFLOW_NAME,
        }
    )


@app.post("/api/v1/sync-runs")
async def start_sync_run(request: Request) -> JSONResponse:
    _require_local_session(request)
    payload = await request.json()
    if not isinstance(payload, dict):
        payload = {}
    code = str(payload.get("departmentCode") or "").strip().lower()
    mode = str(payload.get("mode") or "delta").strip().lower()
    if not DEPT_CODE_RE.fullmatch(code):
        return JSONResponse(
            {"error": {"code": "DEPARTMENT_REQUIRED", "message": "동기화할 학과를 선택해 주세요."}},
            status_code=422,
        )
    try:
        run = _start_manual_sync(code, mode)
    except FileNotFoundError:
        return JSONResponse(
            {"error": {"code": "DEPARTMENT_NOT_FOUND", "message": "학과 설정을 찾을 수 없습니다."}},
            status_code=404,
        )
    except FileExistsError as exc:
        return JSONResponse(
            {"error": {"code": "SYNC_ALREADY_RUNNING", "message": str(exc)}},
            status_code=409,
        )
    except ValueError as exc:
        return JSONResponse(
            {"error": {"code": "INVALID_SYNC_REQUEST", "message": str(exc)}},
            status_code=422,
        )
    except (OSError, RuntimeError, TypeError, UnicodeError, yaml.YAMLError) as exc:
        return JSONResponse(
            {"error": {"code": "SYNC_START_FAILED", "message": str(exc)[:400]}},
            status_code=503,
        )
    return JSONResponse(run, status_code=202)


@app.post("/api/v1/status-runs")
async def start_status(request: Request) -> JSONResponse:
    _require_local_session(request)
    payload = await request.json()
    requested = payload.get("departments") if isinstance(payload, dict) else []
    codes = sorted(str(item) for item in requested) if requested else dept_config.list_departments()
    known = set(dept_config.list_departments())
    if not codes or any(code not in known for code in codes):
        raise HTTPException(status_code=404, detail="department not found")
    offline = bool(payload.get("offline", False))
    with _RUN_LOCK:
        _cleanup_runs()
        for run in _RUNS.values():
            if run["status"] == "RUNNING" and run["scope"] == codes and run["offline"] == offline:
                return JSONResponse({"runId": run["runId"], "status": "RUNNING"})
        active = sum(run["status"] == "RUNNING" for run in _RUNS.values())
        if active >= 2:
            return JSONResponse(
                {"error": {"code": "STATUS_CAPACITY", "message": "상태 검사 2개가 실행 중입니다."}},
                status_code=429,
            )
        run_id = uuid.uuid4().hex
        run = {
            "runId": run_id,
            "status": "RUNNING",
            "scope": codes,
            "offline": offline,
            "startedAt": datetime.now().astimezone().isoformat(timespec="seconds"),
            "startedEpoch": time.time(),
            "departments": [],
            "currentDepartment": None,
            "cancelRequested": False,
        }
        _RUNS[run_id] = run
    threading.Thread(target=_execute_run, args=(run_id, codes, offline), daemon=True).start()
    return JSONResponse({"runId": run_id, "status": "RUNNING"}, status_code=202)


@app.get("/api/v1/status-runs")
def list_runs(status: str | None = None) -> dict[str, Any]:
    with _RUN_LOCK:
        _cleanup_runs()
        rows = [dict(run) for run in _RUNS.values() if not status or run["status"] == status]
    for row in rows:
        row.pop("startedEpoch", None)
        row.pop("finishedEpoch", None)
        row.pop("cancelRequested", None)
    return {"runs": rows}


@app.get("/api/v1/status-runs/{run_id}")
def get_run(run_id: str) -> dict[str, Any]:
    with _RUN_LOCK:
        run = _RUNS.get(run_id)
        if not run:
            raise HTTPException(status_code=404, detail="status run not found")
        result = dict(run)
    result.pop("startedEpoch", None)
    result.pop("finishedEpoch", None)
    result.pop("cancelRequested", None)
    return result


@app.delete("/api/v1/status-runs/{run_id}")
def cancel_run(run_id: str, request: Request) -> dict[str, str]:
    _require_local_session(request)
    with _RUN_LOCK:
        run = _RUNS.get(run_id)
        if not run:
            raise HTTPException(status_code=404, detail="status run not found")
        run["cancelRequested"] = True
    return {"status": "CANCELLING"}


@app.get("/")
def index() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")


@app.get("/og.png")
def social_preview() -> FileResponse:
    return FileResponse(ROOT / "gui" / "public" / "og.png")


if WEB_DIR.exists():
    app.mount("/console", StaticFiles(directory=WEB_DIR), name="console")


def main() -> None:
    parser = argparse.ArgumentParser(description="GCP RAG 학과 관리 GUI")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    if not WEB_DIR.is_dir():
        raise SystemExit(f"GUI asset directory not found: {WEB_DIR}")
    if not args.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(f"http://127.0.0.1:{args.port}")).start()
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
