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
import base64
import copy
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
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
DEPLOYMENT_METADATA_ANNOTATION = "gcp-rag.dev/department-metadata"
# 학과 라우팅 맵(DEPARTMENTS_JSON)을 들고 있는 공용 동기화 런타임.
SYNC_SERVICE = "rag-sync"
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
_SA_REPAIR_PLANS: dict[str, dict[str, Any]] = {}
_SA_REPAIR_RUNS: dict[str, dict[str, Any]] = {}
_SA_REPAIR_LOCK = threading.Lock()
_MCP_DEPLOY_RUNS: dict[str, dict[str, Any]] = {}
_MCP_DEPLOY_CONFIGS: dict[str, dict[str, Any]] = {}
_MCP_DEPLOY_LOCK = threading.Lock()
_MCP_DEPLOY_TTL_SECONDS = 60 * 60
_COMMON_RUNTIME_DEPLOY_RUNS: dict[str, dict[str, Any]] = {}
_COMMON_RUNTIME_DEPLOY_LOCK = threading.Lock()
_COMMON_RUNTIME_DEPLOY_TTL_SECONDS = 60 * 60

_TEARDOWN_RUNS: dict[str, dict[str, Any]] = {}
_TEARDOWN_LOCK = threading.Lock()
_TEARDOWN_TTL_SECONDS = 60 * 60
_AUTH_PROCESS: subprocess.Popen[Any] | None = None
_SYNC_AUTH_LOCK = threading.Lock()
_SYNC_AUTH_TOKEN = ""
_SYNC_AUTH_TOKEN_EXPIRES = 0.0
_SYNC_TARGET_CACHE_LOCK = threading.Lock()
_SYNC_TARGET_CACHE: dict[tuple[str, str], dict[str, Any]] = {}
_SYNC_TARGET_CACHE_TTL_SECONDS = 30
_CLOUD_DEPARTMENT_CONFIG_CACHE_LOCK = threading.Lock()
_CLOUD_DEPARTMENT_CONFIG_CACHE: dict[tuple[str, str, str], dict[str, Any]] = {}
_CLOUD_DEPARTMENT_CONFIG_CACHE_TTL_SECONDS = 60

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
# 콘솔과 배포/동기화가 실제로 호출하는 프로젝트 API 전체. 공통 설정을 저장하기
# 전에 이 순서대로 상태를 확인하고, 꺼진 항목만 활성화한다.
COMMON_REQUIRED_SERVICES: tuple[dict[str, str], ...] = (
    {"name": "compute.googleapis.com", "label": "Compute Engine API"},
    {"name": "iamcredentials.googleapis.com", "label": "IAM Service Account Credentials API"},
    {"name": "run.googleapis.com", "label": "Cloud Run Admin API"},
    {"name": "artifactregistry.googleapis.com", "label": "Artifact Registry API"},
    {"name": "cloudbuild.googleapis.com", "label": "Cloud Build API"},
    {"name": "aiplatform.googleapis.com", "label": "Vertex AI API"},
    {"name": "documentai.googleapis.com", "label": "Document AI API"},
    {"name": "storage.googleapis.com", "label": "Cloud Storage API"},
    {"name": "firestore.googleapis.com", "label": "Cloud Firestore API"},
    {"name": "workflows.googleapis.com", "label": "Workflows API"},
    {"name": "workflowexecutions.googleapis.com", "label": "Workflow Executions API"},
    {"name": "cloudscheduler.googleapis.com", "label": "Cloud Scheduler API"},
    {"name": "appengine.googleapis.com", "label": "App Engine Admin API"},
    {"name": "drive.googleapis.com", "label": "Google Drive API"},
    {"name": "secretmanager.googleapis.com", "label": "Secret Manager API"},
)
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


class WorkflowNotFoundError(RuntimeError):
    """동기화 Workflow가 아직 배포되지 않은 정상적인 빈 상태."""



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


def _mapping_revision(value: dict[str, Any]) -> str:
    raw = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


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


def department_public_config_any(code: str) -> dict[str, Any]:
    path = DEPT_DIR / f"{code}.yaml"
    if path.exists():
        result = department_public_config(code)
        result["source"] = "local"
        return result
    return cloud_department_public_config(code)


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


def _decode_cloud_department_metadata(value: str) -> dict[str, Any] | None:
    try:
        raw = base64.urlsafe_b64decode(str(value or "").encode("ascii"))
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or data.get("managedBy") != "gcp-rag":
        return None
    code = str(data.get("code") or "").strip().lower()
    audience = str(data.get("audience") or "").strip().lower()
    if not DEPT_CODE_RE.fullmatch(code) or audience not in dept_config.AUDIENCES:
        return None
    return data


def _cloud_department_yaml(metadata: dict[str, Any]) -> dict[str, Any]:
    """v2의 YAML 전체 사본을 반환하고 기존 v1 주석도 계속 지원한다."""
    uploaded = metadata.get("yaml")
    if isinstance(uploaded, dict):
        return copy.deepcopy(uploaded)
    return {
        "name": str(metadata.get("name") or metadata.get("code") or ""),
        "corpora": copy.deepcopy(metadata.get("corpora") or {}),
        "buckets": copy.deepcopy(metadata.get("buckets") or {}),
        "drive": copy.deepcopy(metadata.get("drive") or {}),
        "minInstances": copy.deepcopy(metadata.get("minInstances") or {}),
    }


def _public_cloud_yaml(value: Any) -> Any:
    """브라우저용 Cloud 설정에서 키·토큰 계열 필드를 제거한다."""
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


def _load_cloud_department_config(
    code: str, *, require_full: bool
) -> tuple[dict[str, Any], str, bool]:
    """교직원 MCP 주석에서 설정을 읽는다. v1은 상태 검사에만 허용한다."""
    normalised = str(code or "").strip().lower()
    if not DEPT_CODE_RE.fullmatch(normalised):
        raise FileNotFoundError(normalised)
    common = _common()
    project = str(common.get("GCP_PROJECT_ID") or "")
    region = str(common.get("GCP_REGION") or "asia-northeast3")
    cache_key = (project, region, normalised)
    now = time.monotonic()
    with _CLOUD_DEPARTMENT_CONFIG_CACHE_LOCK:
        cached = _CLOUD_DEPARTMENT_CONFIG_CACHE.get(cache_key)
        if (
            cached
            and now - float(cached.get("created", 0))
            < _CLOUD_DEPARTMENT_CONFIG_CACHE_TTL_SECONDS
            and (not require_full or bool(cached.get("complete")))
        ):
            config = copy.deepcopy(cached["config"])
            return config, _mapping_revision(config), bool(cached.get("complete"))
    ok, service = _gcloud_json(
        [
            "run",
            "services",
            "describe",
            f"rag-mcp-{normalised}-staff",
            f"--region={region}",
            f"--project={project}",
        ],
        timeout=30,
    )
    if not ok or not isinstance(service, dict):
        raise FileNotFoundError(normalised)
    metadata = _decode_cloud_department_metadata(_cloud_run_management_annotation(service))
    if not metadata or metadata.get("code") != normalised:
        raise FileNotFoundError(normalised)
    uploaded = metadata.get("yaml")
    complete = isinstance(uploaded, dict)
    if require_full and not complete:
        raise TypeError("기존 v1 배포에는 전체 YAML이 없습니다. 원래 환경에서 한 번 재배포해 주세요.")
    config = copy.deepcopy(uploaded) if complete else _cloud_department_yaml(metadata)
    with _CLOUD_DEPARTMENT_CONFIG_CACHE_LOCK:
        _CLOUD_DEPARTMENT_CONFIG_CACHE[cache_key] = {
            "created": now,
            "config": copy.deepcopy(config),
            "complete": complete,
        }
    return config, _mapping_revision(config), complete


def cloud_department_config(code: str) -> tuple[dict[str, Any], str]:
    """편집·키 조회용으로 v2의 학과 YAML 전체와 revision을 읽는다."""
    config, revision, _complete = _load_cloud_department_config(code, require_full=True)
    return config, revision


def cloud_department_status_config(code: str) -> tuple[dict[str, Any], str, bool]:
    """상태 검사용 설정. 전체 YAML이 없는 v1 메타데이터도 복원한다."""
    return _load_cloud_department_config(code, require_full=False)


def cloud_department_public_config(code: str) -> dict[str, Any]:
    config, revision = cloud_department_config(code)
    return {
        "code": code,
        "name": str(config.get("name") or code),
        "corpora": copy.deepcopy(config.get("corpora") or {}),
        "buckets": copy.deepcopy(config.get("buckets") or {}),
        "drive": copy.deepcopy(config.get("drive") or {}),
        "minInstances": copy.deepcopy(config.get("minInstances") or {}),
        "corpusMode": "split" if (config.get("corpora") or {}).get("student") else "single",
        "configRevision": revision,
        "source": "cloud",
    }


def _cloud_run_env(service: dict[str, Any]) -> dict[str, str]:
    spec = service.get("spec") if isinstance(service.get("spec"), dict) else {}
    template = spec.get("template") if isinstance(spec.get("template"), dict) else {}
    template_spec = template.get("spec") if isinstance(template.get("spec"), dict) else {}
    containers = template_spec.get("containers") if isinstance(template_spec.get("containers"), list) else []
    env_rows = containers[0].get("env") if containers and isinstance(containers[0], dict) else []
    return {
        str(item.get("name") or ""): str(item.get("value") or "")
        for item in env_rows or []
        if isinstance(item, dict) and item.get("name") and item.get("name") != "MCP_API_KEY"
    }


def _cloud_run_management_annotation(service: dict[str, Any]) -> str:
    metadata = service.get("metadata") if isinstance(service.get("metadata"), dict) else {}
    spec = service.get("spec") if isinstance(service.get("spec"), dict) else {}
    template = spec.get("template") if isinstance(spec.get("template"), dict) else {}
    template_metadata = (
        template.get("metadata") if isinstance(template.get("metadata"), dict) else {}
    )
    for annotation_map in (
        metadata.get("annotations"),
        template_metadata.get("annotations"),
    ):
        if isinstance(annotation_map, dict) and annotation_map.get(
            DEPLOYMENT_METADATA_ANNOTATION
        ):
            return str(annotation_map[DEPLOYMENT_METADATA_ANNOTATION])
    return ""


def cloud_mcp_department_records() -> list[dict[str, Any]]:
    """Cloud Run 관리 메타데이터로 로컬 YAML 없는 학과도 안전하게 재구성한다."""
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
        timeout=30,
    )
    if not ok or not isinstance(rows, list):
        raise RuntimeError(str(rows)[:300] or "Cloud Run 서비스 목록을 조회하지 못했습니다.")

    names: list[str] = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        name = str((item.get("metadata") or {}).get("name") or item.get("name") or "")
        if re.fullmatch(r"rag-mcp-[a-z][a-z0-9-]{1,19}-(?:staff|student)", name):
            names.append(name)

    described: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=min(8, len(names) or 1)) as pool:
        futures = {
            pool.submit(
                _gcloud_json,
                [
                    "run",
                    "services",
                    "describe",
                    name,
                    f"--region={region}",
                    f"--project={project}",
                ],
                30,
            ): name
            for name in names
        }
        for future in as_completed(futures):
            service_ok, service = future.result()
            if service_ok and isinstance(service, dict):
                described.append(service)

    grouped: dict[str, dict[str, Any]] = {}
    now = datetime.now(UTC).isoformat()
    for service in described:
        service_name = str((service.get("metadata") or {}).get("name") or "")
        match = re.fullmatch(r"rag-mcp-([a-z][a-z0-9-]{1,19})-(staff|student)", service_name)
        if not match:
            continue
        code, audience = match.groups()
        metadata = _decode_cloud_department_metadata(
            _cloud_run_management_annotation(service)
        )
        annotation_valid = bool(
            metadata and metadata.get("code") == code and metadata.get("audience") == audience
        )
        env = _cloud_run_env(service)
        if not annotation_valid:
            metadata = {
                "schemaVersion": 0,
                "managedBy": "legacy-cloud-run",
                "code": code,
                "name": code,
                "audience": audience,
                "corpusMode": "single",
                "corpora": {
                    "staff": env.get("RAG_CORPUS_NAME", "") if audience == "staff" else "",
                    "student": env.get("RAG_CORPUS_NAME", "") if audience == "student" else "",
                },
                "buckets": {
                    "hwpOriginal": env.get("GCS_HWP_ORIGINAL_BUCKET", ""),
                    "source": env.get("GCS_SOURCE_BUCKET", ""),
                },
                "drive": {"driveIds": [], "syncFolderIds": [], "studentFolderIds": []},
                "minInstances": {},
            }
        cloud_yaml = _cloud_department_yaml(metadata)
        if annotation_valid:
            complete = isinstance(metadata.get("yaml"), dict)
            with _CLOUD_DEPARTMENT_CONFIG_CACHE_LOCK:
                _CLOUD_DEPARTMENT_CONFIG_CACHE[(project, region, code)] = {
                    "created": time.monotonic(),
                    "config": copy.deepcopy(cloud_yaml),
                    "complete": complete,
                }
        status_data = service.get("status") if isinstance(service.get("status"), dict) else {}
        conditions = status_data.get("conditions") if isinstance(status_data.get("conditions"), list) else []
        ready = any(
            isinstance(item, dict)
            and item.get("type") == "Ready"
            and str(item.get("status") or "").lower() == "true"
            for item in conditions
        )
        url = str(status_data.get("url") or "")
        record = grouped.setdefault(
            code,
            {
                "code": code,
                "name": str(cloud_yaml.get("name") or metadata.get("name") or code),
                "path": "Cloud Run management metadata",
                "configRevision": _mapping_revision(cloud_yaml) if annotation_valid else "",
                "lastStatus": "WARN",
                "parseError": None,
                "corpusMode": str(metadata.get("corpusMode") or "single"),
                "cloudOnly": True,
                "cloudEditable": annotation_valid and isinstance(metadata.get("yaml"), dict),
                "metadataComplete": annotation_valid,
                "metadata": _public_cloud_yaml(cloud_yaml),
                "cloudServices": [],
            },
        )
        if annotation_valid and not record["metadataComplete"]:
            record.update(
                name=str(cloud_yaml.get("name") or metadata.get("name") or code),
                corpusMode=str(metadata.get("corpusMode") or "single"),
                cloudEditable=isinstance(metadata.get("yaml"), dict),
                metadataComplete=True,
                metadata=_public_cloud_yaml(cloud_yaml),
                configRevision=_mapping_revision(cloud_yaml),
            )
        record["cloudServices"].append(
            {
                "audience": audience,
                "label": "교직원" if audience == "staff" else "학생",
                "serviceName": service_name,
                "status": "READY" if ready else "NOT_READY",
                "url": url,
                "mcpUrl": url.rstrip("/") + "/mcp" if url else "",
                "healthUrl": url.rstrip("/") + "/health" if url else "",
                "latestReadyRevision": str(status_data.get("latestReadyRevisionName") or ""),
            }
        )

    records: list[dict[str, Any]] = []
    for record in grouped.values():
        services = record["cloudServices"]
        all_ready = bool(services) and all(item["status"] == "READY" for item in services)
        buckets_meta = record["metadata"].get("buckets") or {}
        corpora_meta = record["metadata"].get("corpora") or {}
        # 실제 상태 확인(_resource_status)과 이름을 맞춰서 GCS 버킷 · 코퍼스를
        # 뭉뚱그린 하나의 배지가 아니라 리소스별로 나눠 보여준다.
        resource_checks = [
            _check(
                "RESOURCE",
                "bucket-hwp",
                "OK" if buckets_meta.get("hwpOriginal") else "WARN",
                buckets_meta.get("hwpOriginal") or "버킷 메타데이터 없음",
            ),
            _check(
                "RESOURCE",
                "bucket-source",
                "OK" if buckets_meta.get("source") else "WARN",
                buckets_meta.get("source") or "버킷 메타데이터 없음",
            ),
            _check(
                "RESOURCE",
                "rag-corpus-staff",
                "OK" if corpora_meta.get("staff") else "WARN",
                corpora_meta.get("staff") or "코퍼스 메타데이터 없음",
            ),
        ]
        if corpora_meta.get("student"):
            resource_checks.append(
                _check("RESOURCE", "rag-corpus-student", "OK", corpora_meta.get("student")),
            )
        resource_ok = all(item["status"] == "OK" for item in resource_checks)
        # DEPLOY·RUNTIME도 실제 상태 확인처럼 staff/student를 각자 나눠서 보여준다.
        # cloudServices는 이미 audience별로 나뉘어 있으니 그대로 매핑만 하면 된다.
        deploy_checks = [
            _check(
                "DEPLOY",
                f"mcp-{record['code']}-{svc['audience']}",
                "OK" if svc["status"] == "READY" else "WARN",
                svc["latestReadyRevision"] or "Ready 확인 필요",
            )
            for svc in services
        ] or [_check("DEPLOY", f"mcp-{record['code']}", "WARN", "Cloud Run MCP 서비스를 찾지 못했습니다.")]
        runtime_checks = [
            _check(
                "RUNTIME",
                f"mcp-{record['code']}-{svc['audience']}-health",
                "OK" if svc["status"] == "READY" else "WARN",
                region,
            )
            for svc in services
        ] or [_check("RUNTIME", f"mcp-{record['code']}-health", "WARN", "Cloud Run MCP 서비스를 찾지 못했습니다.")]
        checks = [
            _check(
                "LOCAL",
                "cloud-metadata",
                "OK" if record["metadataComplete"] else "WARN",
                "Cloud Run 관리 메타데이터 확인"
                if record["metadataComplete"]
                else "구형 배포 · 학과명과 Drive 범위 메타데이터 없음",
                hidden=bool(record["metadataComplete"]),
            ),
            *resource_checks,
            *deploy_checks,
            *runtime_checks,
        ]
        record["lastStatus"] = (
            "OK"
            if record["metadataComplete"] and resource_ok and all_ready
            else "WARN"
        )
        cloud_result = {
            "code": record["code"],
            "overall": record["lastStatus"],
            "checkedAt": now,
            "checks": checks,
        }
        latest = _LATEST.get(record["code"])
        if latest and latest.get("configRevision") == record["configRevision"]:
            record["lastStatus"] = str(latest.get("overall") or record["lastStatus"])
            record["lastResult"] = latest
        else:
            record["lastResult"] = cloud_result
        records.append(record)
    return sorted(records, key=lambda item: item["code"])


def department_mcp_servers(code: str) -> dict[str, Any]:
    """학과별 교직원·학생 MCP Cloud Run 서비스의 실제 URL을 조회한다."""
    normalised = str(code or "").strip().lower()
    if not DEPT_CODE_RE.fullmatch(normalised):
        raise FileNotFoundError(normalised)
    path = DEPT_DIR / f"{normalised}.yaml"
    config = _read_yaml(path) if path.exists() else cloud_department_config(normalised)[0]
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
                "mcpUrl": url.rstrip("/") + "/mcp" if url else "",
                "healthUrl": url.rstrip("/") + "/health" if url else "",
                "status": "READY" if ready and url else ("NOT_READY" if item else "NOT_DEPLOYED"),
            }
        )
    return {"code": normalised, "projectId": project, "region": region, "servers": servers}


def department_mcp_key(code: str, audience: str) -> str:
    """명시적인 복사 요청에만 로컬 또는 Cloud 설정의 MCP 키 하나를 반환한다."""
    normalised = str(code or "").strip().lower()
    if not DEPT_CODE_RE.fullmatch(normalised):
        raise FileNotFoundError(normalised)
    if audience not in dept_config.AUDIENCES:
        raise ValueError("교직원 또는 학생 MCP 키를 선택해 주세요.")
    path = (DEPT_DIR / f"{normalised}.yaml").resolve()
    if path.parent != DEPT_DIR.resolve():
        raise FileNotFoundError(path)
    data = _read_yaml(path) if path.exists() else cloud_department_config(normalised)[0]
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


def _cloud_config_status(
    code: str, data: dict[str, Any], *, complete: bool
) -> list[dict[str, Any]]:
    """Cloud Run v2 주석을 로컬 YAML과 같은 설정 계층으로 검사한다."""
    started = time.perf_counter()
    checks = [
        _check(
            "LOCAL",
            "cloud-metadata",
            "OK",
            f"rag-mcp-{code}-staff · Cloud Run 관리 메타데이터 {'v2' if complete else 'v1'}",
            hidden=True,
        )
    ]
    split_enabled = bool((data.get("corpora") or {}).get("student"))
    if complete:
        try:
            audiences = _cloud_config_audiences(data)
            mode_label = "Cloud 설정 · 교직원/학생" if len(audiences) == 2 else "Cloud 설정 · 단일 코퍼스"
            checks.append(_check("LOCAL", "derived-env", "OK", mode_label))
        except ValueError as exc:
            checks.append(_check("LOCAL", "derived-env", "FAIL", str(exc)))
    else:
        checks.append(
            _check(
                "LOCAL",
                "derived-env",
                "SKIP",
                "v1 메타데이터 복원 · 전체 YAML 확인은 v2 재배포 후 가능",
                hidden=True,
            )
        )

    drive = data.get("drive") or {}
    sync_ids = set(_normalise_ids(drive.get("syncFolderIds")))
    student_ids = set(_normalise_ids(drive.get("studentFolderIds")))
    if not split_enabled:
        checks.append(_check("LOCAL", "folder-scope", "SKIP", "단일 코퍼스 운영"))
    elif student_ids and not student_ids - sync_ids:
        checks.append(_check("LOCAL", "folder-scope", "OK", "student ⊆ sync"))
    else:
        missing = sorted(student_ids - sync_ids)
        checks.append(
            _check(
                "LOCAL",
                "folder-scope",
                "FAIL",
                "학생 폴더 없음" if not student_ids else "부분집합 위반: " + ", ".join(missing),
            )
        )

    if complete:
        keys = data.get("keys") or {}
        audiences = dept_config.AUDIENCES if split_enabled else ("staff",)
        weak = [audience for audience in audiences if len(str(keys.get(audience) or "")) < 24]
        checks.append(
            _check(
                "LOCAL",
                "mcp-keys",
                "WARN" if weak else "OK",
                "24자 미만: " + ", ".join(weak) if weak else "키 길이 기준 충족",
            )
        )
    else:
        checks.append(
            _check(
                "LOCAL",
                "mcp-keys",
                "SKIP",
                "v1 주석에는 키 정보가 없음",
                hidden=True,
            )
        )
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

    # 넷은 서로를 필요로 하지 않는다. gcloud 한 번이 1초 이상이라 순차로 돌면
    # 그대로 부팅 지연이 된다. projects list 가 제일 느리므로 인증 확인을
    # 기다리지 않고 같이 띄운다 — 미인증이면 결과를 그냥 버린다.
    with ThreadPoolExecutor(max_workers=4) as pool:
        auth_future = pool.submit(_gcloud_json, ["auth", "list", "--filter=status:ACTIVE"])
        token_future = pool.submit(
            _run_command, [gcloud, "auth", "print-access-token", "--quiet"]
        )
        current_future = pool.submit(
            _run_command, [gcloud, "config", "get-value", "project", "--quiet"]
        )
        auth_ok, accounts = auth_future.result()
        token_ok, _ = token_future.result()
        current_ok, current = current_future.result()

    if not auth_ok or not isinstance(accounts, list) or not accounts:
        return state
    account = str(accounts[0].get("account") or "")
    state["authenticated"] = bool(account and token_ok)
    if not state["authenticated"]:
        return state
    if "@" in account:
        local, domain = account.split("@", 1)
        state["account"] = (local[:2] + "***@" + domain) if local else "***@" + domain
    if current_ok:
        state["currentProject"] = current.strip()

    # 프로젝트 **전량**은 싣지 않는다. 접근 가능한 프로젝트가 수천 개인 계정에서
    # 이 한 번이 6초를 먹었고, 그 목록을 그대로 드롭다운에 부으면 고를 수도 없다.
    # 화면은 현재 프로젝트로 시작하고 나머지는 /api/v1/projects/search 로 찾는다.
    if include_projects and state["authenticated"] and state["currentProject"]:
        state["projects"] = [
            {"id": state["currentProject"], "name": state["currentProject"]}
        ]
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


def _http_delete_json(
    url: str, token: str = "", timeout: int = 10
) -> tuple[int, Any, int]:
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers, method="DELETE")
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
    # 학과 코드를 맨 앞에 둔다 — 코퍼스(`{code}-rag-corpus-*`)와 같은 순서라야
    # 콘솔 목록에서 학과별로 붙어 보이고, 접두어 검색도 코드 하나로 끝난다.
    bucket_names = {
        "bucketHwp": f"{code}-rag-hwp-{suffix}",
        "bucketSource": f"{code}-rag-source-{suffix}",
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

    # 리소스와 API는 동시에 읽는다. 서비스 계정 조회까지 같은 순간에 여러 gcloud
    # 프로세스로 실행하면 Windows에서 프로젝트 번호 조회가 간헐적으로 실패하므로
    # 정책/연결 확인은 둘이 끝난 뒤 안정적으로 실행한다.
    with ThreadPoolExecutor(max_workers=2) as pool:
        resources_future = pool.submit(_gcloud_project_resources, project, region)
        services_future = pool.submit(_gcloud_enabled_services, project)
        existing = resources_future.result()
        services_ok, enabled = services_future.result()
    service_account_status = drive_service_account_status(project)
    existing_ids = {
        "artifactRepo": {item["id"] for item in existing["artifactRepositories"]},
        "firestoreDatabase": {item["id"] for item in existing["firestoreDatabases"]},
    }

    resources: list[dict[str, Any]] = []
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

    services = [
        {
            "name": definition["name"],
            "label": definition["label"],
            # 조회 실패 시엔 '켜져 있다' 고 단정하지 않는다 — 화면에 확인 필요로 뜬다.
            "enabled": bool(services_ok and definition["name"] in enabled),
            "known": services_ok,
        }
        for definition in COMMON_REQUIRED_SERVICES
    ]

    plan_id = uuid.uuid4().hex
    plan = {
        "planId": plan_id,
        "projectId": project,
        "region": region,
        "resources": resources,
        "services": services,
        "serviceAccount": {
            "key": "driveImpersonation",
            "label": "Drive 서비스 계정 사용 권한",
            "role": "roles/iam.serviceAccountTokenCreator",
            "serviceAccount": str(service_account_status.get("serviceAccount") or ""),
            "account": str(service_account_status.get("account") or ""),
            "ready": service_account_status.get("status") == "OK",
            "issues": list(service_account_status.get("issues") or []),
            "detail": str(service_account_status.get("detail") or "상태를 확인하지 못했습니다."),
        },
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


def _set_common_provision_service_account(run_id: str, **changes: Any) -> None:
    with _COMMON_PROVISION_LOCK:
        run = _COMMON_PROVISION_RUNS.get(run_id)
        if run:
            run["serviceAccount"].update(changes)


def _prepare_common_service_account(
    run_id: str, project: str, failed_services: set[str]
) -> None:
    """Compute SA 가장 권한을 확인하고 필요할 때 현재 계정에 부여한다."""
    dependencies = {COMPUTE_SERVICE, IAM_CREDENTIALS_SERVICE}
    failed_dependencies = sorted(dependencies & failed_services)
    if failed_dependencies:
        _set_common_provision_service_account(
            run_id,
            status="FAILED",
            detail=f"{', '.join(failed_dependencies)} 활성화 실패로 확인하지 못했습니다.",
        )
        return

    _set_common_provision_service_account(
        run_id, status="RUNNING", detail="서비스 계정 연결 상태를 확인하는 중"
    )
    status = drive_service_account_status(project)
    service_account = str(status.get("serviceAccount") or "")
    account = str(status.get("account") or "")
    _set_common_provision_service_account(
        run_id, serviceAccount=service_account, account=account
    )
    if status.get("status") == "OK":
        _set_common_provision_service_account(
            run_id, status="SKIPPED", detail="서비스 계정 연결 확인됨 · 이미 정상"
        )
        return
    if not service_account or not account:
        _set_common_provision_service_account(
            run_id,
            status="FAILED",
            detail=str(status.get("detail") or "서비스 계정을 확인하지 못했습니다.")[:400],
        )
        return

    member_type = "serviceAccount" if account.endswith(".gserviceaccount.com") else "user"
    _set_common_provision_service_account(
        run_id,
        status="RUNNING",
        detail=f"{account}에 Token Creator 권한을 부여하는 중",
    )
    try:
        _grant_service_account_role(
            service_account,
            f"{member_type}:{account}",
            SA_TOKEN_CREATOR_ROLE,
            project,
        )
    except (OSError, RuntimeError) as exc:
        _set_common_provision_service_account(
            run_id, status="FAILED", detail=str(exc)[:400]
        )
        return

    # IAM 정책 반영은 실제로 10초 이상 걸릴 수 있다. 명령 성공 직후의 403을
    # 최종 실패로 오판하지 않도록 약 30초 동안 실제 토큰 발급을 재확인한다.
    verified = status
    verification_attempts = 11
    for attempt in range(1, verification_attempts + 1):
        _set_common_provision_service_account(
            run_id,
            status="RUNNING",
            detail=f"권한 적용 후 서비스 계정 연결 재확인 중 ({attempt}/{verification_attempts})",
        )
        verified = drive_service_account_status(project)
        if verified.get("status") == "OK":
            break
        if attempt < verification_attempts:
            time.sleep(3)
    if verified.get("status") == "OK":
        _set_common_provision_service_account(
            run_id, status="COMPLETE", detail="Token Creator 권한 부여 및 서비스 계정 연결 확인 완료"
        )
    else:
        _set_common_provision_service_account(
            run_id,
            status="FAILED",
            detail=str(verified.get("detail") or "서비스 계정 연결 재확인에 실패했습니다.")[:400],
        )


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

    _prepare_common_service_account(run_id, project, failed_services)

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
        statuses = [item["status"] for item in current["services"] + current["resources"]]
        statuses.append(current["serviceAccount"]["status"])
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
            if item.get("exists"):
                item.update(status="SKIPPED", detail="이미 존재해 건너뜀")
            else:
                item.update(status="PENDING", detail="생성 대기 중")
        services = copy.deepcopy(plan["services"])
        for item in services:
            item.update(status="PENDING", detail="대기 중")
        service_account = copy.deepcopy(plan["serviceAccount"])
        if service_account.get("ready"):
            service_account.update(status="SKIPPED", detail="서비스 계정 연결 확인됨 · 이미 설정됨")
        else:
            service_account.update(status="PENDING", detail="필수 API 준비 후 권한 확인 및 설정")
        run = {
            "runId": run_id,
            "planId": plan_id,
            "projectId": plan["projectId"],
            "region": plan["region"],
            "status": "RUNNING",
            "resources": resources,
            "services": services,
            "serviceAccount": service_account,
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


# Drive 확인용 Compute SA 가 막히는 두 가지 원인. 새 프로젝트는 둘 다 해당한다.
#   1) Compute Engine API 가 꺼져 있어 기본 SA 자체가 아직 안 만들어졌다
#   2) SA 는 있는데 현재 로그인 계정에 가장(impersonate) 권한이 없다
# 둘은 조치가 다르므로 반드시 구분해서 알린다 — 뭉뚱그리면 엉뚱한 걸 누른다.
SA_TOKEN_CREATOR_ROLE = "roles/iam.serviceAccountTokenCreator"
COMPUTE_SERVICE = "compute.googleapis.com"
IAM_CREDENTIALS_SERVICE = "iamcredentials.googleapis.com"


def drive_service_account_status(project: str) -> dict[str, Any]:
    """Drive 확인 SA 가 실제로 쓸 수 있는 상태인지 본다. 아무것도 바꾸지 않는다."""
    result: dict[str, Any] = {
        "projectId": project,
        "serviceAccount": "",
        "account": "",
        "status": "FAIL",
        "detail": "",
        "issues": [],
    }
    if not PROJECT_RE.fullmatch(project or ""):
        result["detail"] = "공통 설정의 프로젝트를 먼저 확인해 주세요."
        return result

    bootstrap = _gcloud_bootstrap_state(include_projects=False)
    if not bootstrap["installed"] or not bootstrap["authenticated"]:
        result["detail"] = "gcloud 로그인이 필요합니다."
        result["issues"].append("gcloudAuth")
        return result
    # bootstrap 응답의 account는 화면 표시용으로 마스킹되어 있다. IAM member에는
    # 원문 계정이 필요하므로 활성 계정을 다시 읽고, 조회 실패 때만 기존 값을 쓴다.
    account_ok, active_accounts = _gcloud_json(
        ["auth", "list", "--filter=status:ACTIVE"], timeout=20
    )
    if account_ok and isinstance(active_accounts, list) and active_accounts:
        result["account"] = str(active_accounts[0].get("account") or "")
    else:
        result["account"] = bootstrap["account"]

    service_account = _default_compute_service_account(project)
    if not service_account:
        result["detail"] = "프로젝트 번호를 확인하지 못했습니다."
        result["issues"].append("projectNumber")
        return result
    result["serviceAccount"] = service_account

    services_ok, enabled = _gcloud_enabled_services(project)
    if services_ok and COMPUTE_SERVICE not in enabled:
        # 기본 Compute SA 는 Compute Engine API 를 켤 때 만들어진다.
        result["issues"].append("computeApi")
    if services_ok and IAM_CREDENTIALS_SERVICE not in enabled:
        result["issues"].append("iamCredentialsApi")

    exists_ok, _ = _gcloud_json(
        ["iam", "service-accounts", "describe", service_account, f"--project={project}"],
        timeout=20,
    )
    if not exists_ok:
        if "computeApi" not in result["issues"]:
            result["issues"].append("serviceAccountMissing")
        result["status"] = "FAIL"
        result["detail"] = "기본 Compute 서비스 계정이 아직 없습니다."
        return result

    # 실제로 가장 토큰을 받아본다 — 권한 표를 읽는 것보다 이게 정확하다.
    try:
        caller_token = _sync_access_token()
    except RuntimeError as exc:
        result["detail"] = str(exc)[:200]
        result["issues"].append("gcloudAuth")
        return result

    _token, _sa, latency, token_error = _service_account_access_token(
        project,
        caller_token,
        ["https://www.googleapis.com/auth/drive.readonly"],
    )
    result["latencyMs"] = latency
    if token_error:
        result["status"] = "FAIL"
        result["detail"] = token_error
        if "tokenCreator" not in result["issues"]:
            result["issues"].append("tokenCreator")
        return result

    result["status"] = "OK"
    result["detail"] = "서비스 계정 연결 확인됨"
    return result


def create_sa_repair_plan(project: str) -> dict[str, Any]:
    """무엇을 켜고 어떤 권한을 줄지만 계산한다. 외부는 바꾸지 않는다."""
    status = drive_service_account_status(project)
    if status["status"] == "OK":
        raise FileExistsError("서비스 계정이 이미 정상입니다.")
    if "gcloudAuth" in status["issues"] or "projectNumber" in status["issues"]:
        raise ValueError(status["detail"] or "gcloud 로그인을 먼저 확인해 주세요.")

    account = status["account"]
    service_account = status["serviceAccount"]
    steps: list[dict[str, Any]] = []
    if "computeApi" in status["issues"] or "serviceAccountMissing" in status["issues"]:
        steps.append(
            {
                "key": "enableCompute",
                "kind": "service",
                "label": "Compute Engine API 활성화",
                "target": COMPUTE_SERVICE,
                "detail": "기본 Compute 서비스 계정은 이 API 를 켤 때 만들어집니다.",
            }
        )
    if "iamCredentialsApi" in status["issues"]:
        steps.append(
            {
                "key": "enableIamCredentials",
                "kind": "service",
                "label": "IAM Credentials API 활성화",
                "target": IAM_CREDENTIALS_SERVICE,
                "detail": "서비스 계정 임시 접근 토큰 발급에 필요합니다.",
            }
        )
    # SA 가 아직 없으면 가장을 시험해 볼 수 없어 권한 필요 여부를 알 수 없다.
    # 바인딩은 멱등이므로 이미 있으면 무해하다 — 두 번 왕복시키는 것보다 낫다.
    # 어차피 계획 화면에 그대로 뜨고 사용자가 확인한 뒤에만 적용된다.
    if service_account:
        member_type = (
            "serviceAccount" if account.endswith(".gserviceaccount.com") else "user"
        )
        steps.append(
            {
                "key": "grantTokenCreator",
                "kind": "iamBinding",
                "label": "토큰 생성 권한 부여",
                "target": service_account,
                "role": SA_TOKEN_CREATOR_ROLE,
                "member": f"{member_type}:{account}",
                "detail": f"{account} 계정이 이 서비스 계정으로 연결할 수 있게 합니다.",
            }
        )
    if not steps:
        raise FileExistsError("자동으로 조치할 항목이 없습니다.")

    plan_id = uuid.uuid4().hex
    plan = {
        "planId": plan_id,
        "projectId": project,
        "account": account,
        "serviceAccount": service_account,
        "steps": steps,
        "createdEpoch": time.time(),
        "started": False,
    }
    with _SA_REPAIR_LOCK:
        _cleanup_sa_repair_state()
        _SA_REPAIR_PLANS[plan_id] = plan
    return copy.deepcopy(plan)


def _cleanup_sa_repair_state() -> None:
    cutoff = time.time() - _PROVISION_TTL_SECONDS
    for plan_id in [
        key for key, item in _SA_REPAIR_PLANS.items() if item.get("createdEpoch", 0) < cutoff
    ]:
        _SA_REPAIR_PLANS.pop(plan_id, None)
    for run_id in [
        key
        for key, item in _SA_REPAIR_RUNS.items()
        if item.get("finishedEpoch", item.get("createdEpoch", time.time())) < cutoff
    ]:
        _SA_REPAIR_RUNS.pop(run_id, None)


def _grant_service_account_role(
    service_account: str, member: str, role: str, project: str
) -> None:
    gcloud = _gcloud_executable()
    if not gcloud:
        raise RuntimeError("gcloud를 찾을 수 없습니다.")
    ok, output = _run_command(
        [
            gcloud,
            "iam",
            "service-accounts",
            "add-iam-policy-binding",
            service_account,
            f"--member={member}",
            f"--role={role}",
            f"--project={project}",
            "--quiet",
        ],
        timeout=120,
    )
    if not ok:
        raise RuntimeError((output or "권한 부여에 실패했습니다.")[-400:])


def _set_sa_repair_step(run_id: str, key: str, **changes: Any) -> None:
    with _SA_REPAIR_LOCK:
        run = _SA_REPAIR_RUNS.get(run_id)
        if not run:
            return
        for item in run["steps"]:
            if item["key"] == key:
                item.update(changes)
                return


def _execute_sa_repair_run(run_id: str) -> None:
    with _SA_REPAIR_LOCK:
        run = copy.deepcopy(_SA_REPAIR_RUNS[run_id])
    project = run["projectId"]

    for step in run["steps"]:
        key = step["key"]
        _set_sa_repair_step(run_id, key, status="RUNNING", detail="진행 중")
        try:
            if step["kind"] == "service":
                _enable_gcloud_service(step["target"], project)
            else:
                _grant_service_account_role(
                    step["target"], step["member"], step["role"], project
                )
        except (OSError, RuntimeError) as exc:
            _set_sa_repair_step(run_id, key, status="FAILED", detail=str(exc)[:400])
        else:
            _set_sa_repair_step(run_id, key, status="COMPLETE", detail="완료")

    # 조치가 실제로 통했는지 같은 방법으로 되읽는다. IAM 반영 지연 때문에
    # 명령 성공 직후에는 잠시 403일 수 있으므로 약 30초 동안 재확인한다.
    verified: dict[str, Any] = {}
    with _SA_REPAIR_LOCK:
        grant_succeeded = any(
            item.get("key") == "grantTokenCreator" and item.get("status") == "COMPLETE"
            for item in _SA_REPAIR_RUNS[run_id]["steps"]
        )
    verification_attempts = 11 if grant_succeeded else 1
    for attempt in range(1, verification_attempts + 1):
        verified = drive_service_account_status(project)
        if verified["status"] == "OK":
            break
        if attempt < verification_attempts:
            time.sleep(3)
    with _SA_REPAIR_LOCK:
        current = _SA_REPAIR_RUNS[run_id]
        current["verification"] = verified
        statuses = [item.get("status") for item in current["steps"]]
        if verified["status"] == "OK":
            current["status"] = "COMPLETED"
        elif any(item == "COMPLETE" for item in statuses):
            current["status"] = "PARTIAL"
        else:
            current["status"] = "FAILED"
        current["finishedEpoch"] = time.time()


def start_sa_repair_run(plan_id: str) -> dict[str, Any]:
    with _SA_REPAIR_LOCK:
        _cleanup_sa_repair_state()
        plan = _SA_REPAIR_PLANS.get(plan_id)
        if not plan:
            raise FileNotFoundError(plan_id)
        if plan["started"]:
            raise FileExistsError("이미 실행한 조치 계획입니다.")
        for active in _SA_REPAIR_RUNS.values():
            if active["status"] == "RUNNING":
                raise FileExistsError("서비스 계정 조치가 이미 진행 중입니다.")

        run_id = uuid.uuid4().hex
        steps = copy.deepcopy(plan["steps"])
        for item in steps:
            item.update(status="PENDING", detail="대기 중")
        run = {
            "runId": run_id,
            "planId": plan_id,
            "projectId": plan["projectId"],
            "account": plan["account"],
            "serviceAccount": plan["serviceAccount"],
            "status": "RUNNING",
            "steps": steps,
            "verification": {},
            "createdEpoch": time.time(),
        }
        plan["started"] = True
        _SA_REPAIR_RUNS[run_id] = run

    threading.Thread(
        target=_execute_sa_repair_run,
        args=(run_id,),
        name=f"sa-repair-{run_id[:8]}",
        daemon=True,
    ).start()
    return copy.deepcopy(run)


# Resource Manager REST. gcloud CLI 는 기동만 1.4초라 대화형 검색에 못 쓴다
# (실측: CLI 6.3s / REST 0.35s, 접근 가능 프로젝트 2,676개 기준).
_CRM_PROJECTS = "https://cloudresourcemanager.googleapis.com/v1/projects"
PROJECT_SEARCH_LIMIT = 20


def _crm_projects(filter_expr: str, limit: int, token: str) -> list[dict[str, str]]:
    params = {"pageSize": max(1, min(limit, 50))}
    if filter_expr:
        params["filter"] = filter_expr
    status, body, _ = _http_json(f"{_CRM_PROJECTS}?{urlencode(params)}", token, timeout=20)
    if status != 200 or not isinstance(body, dict):
        return []
    found: list[dict[str, str]] = []
    for item in body.get("projects") or []:
        project_id = str(item.get("projectId") or "")
        if project_id and str(item.get("lifecycleState") or "ACTIVE") == "ACTIVE":
            found.append({"id": project_id, "name": str(item.get("name") or project_id)})
    return found


def search_projects(term: str, limit: int = PROJECT_SEARCH_LIMIT) -> dict[str, Any]:
    """프로젝트를 부분 일치로 찾는다. 전량(수천 건)을 받지 않는다.

    ID 와 표시 이름 중 어디에 맞을지 모르므로 둘 다 묻는다 — REST 필터가 OR 를
    지원하지 않아 두 번 물어 합친다. 동시에 던지면 왕복은 한 번 값이다.
    """
    term = str(term or "").strip().lower()[:64]
    try:
        token = _sync_access_token()
    except RuntimeError as exc:
        return {"projects": [], "term": term, "error": str(exc)[:200]}

    if not term:
        # 검색어가 없으면 무작위 상위 20개는 의미가 없다(sys-* 가 앞을 채운다).
        # 화면은 현재·최근 프로젝트를 대신 보여준다.
        return {"projects": [], "term": "", "error": ""}

    escaped = term.replace("*", "")
    with ThreadPoolExecutor(max_workers=2) as pool:
        by_id = pool.submit(_crm_projects, f"id:*{escaped}*", limit, token)
        by_name = pool.submit(_crm_projects, f"name:*{escaped}*", limit, token)
        merged = by_id.result() + by_name.result()

    seen: set[str] = set()
    projects: list[dict[str, str]] = []
    for item in merged:
        if item["id"] in seen:
            continue
        seen.add(item["id"])
        projects.append(item)
    # 접두어 일치를 위로 — 사람은 보통 앞부터 친다.
    projects.sort(key=lambda item: (not item["id"].startswith(escaped), item["id"]))
    return {"projects": projects[:limit], "term": term, "error": ""}


def _project_accessible(project_id: str) -> bool:
    """이 프로젝트 하나만 확인한다. 전량 목록을 받아 멤버십을 보던 것을 대체한다.

    전량 조회는 프로젝트가 많은 계정에서 수 초가 걸리는 데다, 목록에 없다는
    이유로 실제로는 접근 가능한 프로젝트를 막는 오탐도 났다.
    """
    if not PROJECT_RE.fullmatch(project_id or ""):
        return False
    try:
        token = _sync_access_token()
    except RuntimeError:
        return False
    status, body, _ = _http_json(f"{_CRM_PROJECTS}/{quote(project_id, safe='')}", token, timeout=20)
    if status != 200 or not isinstance(body, dict):
        return False
    return str(body.get("lifecycleState") or "ACTIVE") == "ACTIVE"


def _expected_runtime_env() -> tuple[str, dict[str, str], str]:
    """드리프트 기대값의 원본. 로컬 YAML 이 있으면 그것을, 없으면 Cloud v2 주석을 쓴다.

    (기준 학과, 그 학과 env, 전 학과 DEPARTMENTS_JSON) 을 준다. 로컬 YAML 만
    보던 시절에는 YAML 없는 운영 환경에서 이 검사가 통째로 UNKNOWN 으로 빠졌다
    — rag-sync 의 학과 맵이 낡아도 경고 한 줄 없었다.
    """
    codes = dept_config.list_departments()
    if codes:
        return (
            codes[0],
            dept_config.build_env(codes[0], "staff"),
            dept_config.departments_json(),
        )
    configs = _cloud_department_configs()
    first = sorted(configs)[0]
    return (
        first,
        dept_config.build_env_from_config(first, "staff", configs[first]),
        dept_config.departments_json_from_configs(configs),
    )


def runtime_env_drift(cache: _StatusRunCache | None = None) -> dict[str, Any]:
    """배포된 Cloud Run env 가 지금 config 와 같은지 본다.

    `--set-env-vars` 는 배포 때만 갱신된다. 학과를 추가·삭제하거나 버킷·코퍼스를
    바꾸면 서비스는 살아 있는데 값만 낡는다. 그 상태의 rag-sync 는 없어진 버킷에
    업로드를 시도해 전량 404 → DLQ 로 보낸다(실측 1445건). 오류 로그를 뒤지기
    전에는 안 보이므로 여기서 먼저 잡는다.

    DEPARTMENTS_JSON 하나에 전 학과의 버킷·코퍼스·폴더가 들어 있어 라우팅 드리프트는
    그 값 하나로 판정된다. parser 는 그 값을 안 받으므로 기본 학과 값으로 대조한다.
    """
    common = _common()
    project = str(common.get("GCP_PROJECT_ID") or "")
    region = str(common.get("GCP_REGION") or "asia-northeast3")
    if not project:
        return {"status": "UNKNOWN", "reason": "공통 설정이 없습니다.", "services": []}

    try:
        base_code, base_env, expected_departments = _expected_runtime_env()
    except (OSError, RuntimeError, SystemExit, TypeError, ValueError, yaml.YAMLError) as exc:
        return {"status": "UNKNOWN", "reason": str(exc)[:200], "services": []}

    expected_by_service = {
        "rag-parser": {
            "GCS_HWP_ORIGINAL_BUCKET": base_env.get("GCS_HWP_ORIGINAL_BUCKET", ""),
            "GCS_SOURCE_BUCKET": base_env.get("GCS_SOURCE_BUCKET", ""),
            "RAG_CORPUS_NAME": base_env.get("RAG_CORPUS_NAME", ""),
        },
        "rag-sync": {
            "GCS_HWP_ORIGINAL_BUCKET": base_env.get("GCS_HWP_ORIGINAL_BUCKET", ""),
            "GCS_SOURCE_BUCKET": base_env.get("GCS_SOURCE_BUCKET", ""),
            "RAG_CORPUS_NAME": base_env.get("RAG_CORPUS_NAME", ""),
            "DEPARTMENTS_JSON": expected_departments,
        },
    }

    gcloud_json = cache.gcloud_json if cache else _gcloud_json
    services: list[dict[str, Any]] = []
    drifted = False
    for service, expected in expected_by_service.items():
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
        if not ok or not isinstance(data, dict):
            # 서비스가 없으면 드리프트가 아니라 미배포다 — 그건 DEPLOY 층이 잡는다.
            services.append({"serviceName": service, "status": "UNKNOWN", "staleKeys": []})
            continue
        deployed = _cloud_run_env(data)
        stale = [
            {
                "key": key,
                "deployed": str(deployed.get(key) or "")[:200],
                "expected": str(value or "")[:200],
            }
            for key, value in expected.items()
            if value and str(deployed.get(key) or "") != str(value)
        ]
        if stale:
            drifted = True
        services.append(
            {
                "serviceName": service,
                "status": "DRIFT" if stale else "OK",
                "staleKeys": stale,
            }
        )
    return {
        "status": "DRIFT" if drifted else "OK",
        "projectId": project,
        "region": region,
        "baseDepartment": base_code,
        "services": services,
    }


def _cleanup_common_runtime_deployments() -> None:
    cutoff = time.time() - _COMMON_RUNTIME_DEPLOY_TTL_SECONDS
    expired = [
        run_id
        for run_id, run in _COMMON_RUNTIME_DEPLOY_RUNS.items()
        if run.get("finishedEpoch", run.get("createdEpoch", time.time())) < cutoff
    ]
    for run_id in expired:
        _COMMON_RUNTIME_DEPLOY_RUNS.pop(run_id, None)


def _set_common_runtime_deploy_step(run_id: str, key: str, **changes: Any) -> None:
    with _COMMON_RUNTIME_DEPLOY_LOCK:
        run = _COMMON_RUNTIME_DEPLOY_RUNS.get(run_id)
        if not run:
            return
        for step in run["steps"]:
            if step["key"] == key:
                step.update(changes)
                return


def _advance_common_runtime_deploy_from_log(run_id: str, line: str) -> None:
    markers = (
        ("== Refresh Cloud Run env ==", "config", "cloudRun", "Parser / Sync env 갱신 중"),
        ("== Build & push images ==", "config", "images", "Parser / Sync 이미지 조회 중 · 없으면 빌드"),
        ("== Deploy Cloud Run ==", "images", "cloudRun", "Parser / Sync 조회 중 · 없으면 배포"),
        ("== Deploy Workflow ==", "cloudRun", "workflow", "Workflow 배포 시작"),
        ("== Ensure Scheduler SA / App Engine ==", "workflow", "scheduler", "Scheduler 준비 시작"),
    )
    for marker, completed_key, running_key, detail in markers:
        if marker in line:
            _set_common_runtime_deploy_step(
                run_id, completed_key, status="COMPLETE", detail="배포 명령 완료"
            )
            _set_common_runtime_deploy_step(
                run_id, running_key, status="RUNNING", detail=detail
            )
            return
    if "== Cloud Scheduler" in line:
        _set_common_runtime_deploy_step(
            run_id, "scheduler", status="RUNNING", detail="Scheduler 작업 등록 중"
        )


def _append_common_runtime_deploy_log(run_id: str, line: str) -> None:
    clean = str(line or "").strip()
    if not clean:
        return
    clean = re.sub(r"(?i)(authorization:\s*bearer\s+)[^\s]+", r"\1***", clean)
    clean = re.sub(r"(?i)(access[_ -]?token[=:]\s*)[^\s]+", r"\1***", clean)
    with _COMMON_RUNTIME_DEPLOY_LOCK:
        run = _COMMON_RUNTIME_DEPLOY_RUNS.get(run_id)
        if not run:
            return
        run["logs"] = [*run.get("logs", []), clean[:500]][-220:]
        for step in run["steps"]:
            if step["status"] == "RUNNING":
                step["detail"] = clean[:300]
                break
    _advance_common_runtime_deploy_from_log(run_id, clean)


def _run_common_runtime_deploy_script(*, on_line: Any, env_only: bool = False) -> int:
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
        str(ROOT / "scripts" / "deploy.ps1"),
    ]
    if env_only:
        # 설정만 바뀐 경우다. 이미지·Workflow·Scheduler 를 건드리지 않고 env 만
        # 새로 씌운다(리비전 하나, 수십 초).
        args.append("-EnvOnly")
    else:
        # 런타임이 없어서 올리는 흐름이다. 이미 올라간 parser/sync 이미지와
        # 이미 떠 있는 Cloud Run 서비스는 건너뛴다.
        args.extend(["-SkipMcp", "-ReuseExisting"])
    env = os.environ.copy()
    env.update(
        {
            "PYTHONUTF8": "1",
            "PYTHONIOENCODING": "utf-8",
            "CLOUDSDK_CORE_DISABLE_PROMPTS": "1",
            "PREFLIGHT_NO_FIX": "1",
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


def _verify_common_runtime_deployment() -> list[dict[str, str]]:
    common = _common()
    project = str(common.get("GCP_PROJECT_ID") or "")
    region = str(common.get("GCP_REGION") or "asia-northeast3")
    services: list[dict[str, str]] = []
    failures: list[str] = []
    for service in ("rag-parser", "rag-sync"):
        ok, data = _gcloud_json(
            [
                "run",
                "services",
                "describe",
                service,
                f"--region={region}",
                f"--project={project}",
            ],
            timeout=30,
        )
        status_data = data.get("status") if ok and isinstance(data, dict) else {}
        status_data = status_data if isinstance(status_data, dict) else {}
        ready_condition = next(
            (
                item
                for item in status_data.get("conditions") or []
                if item.get("type") == "Ready"
            ),
            {},
        )
        ready = str(ready_condition.get("status") or "").lower() == "true"
        url = str(status_data.get("url") or "")
        if not ok or not ready or not url:
            detail = str(data if not ok else ready_condition.get("message") or "Ready 아님")
            failures.append(f"{service}: {detail[:180]}")
            continue
        services.append(
            {
                "serviceName": service,
                "url": url,
                "healthUrl": url.rstrip("/") + "/health",
            }
        )

    for kind, args in (
        (
            "Workflow",
            ["workflows", "describe", "rag-daily-sync", f"--location={region}", f"--project={project}"],
        ),
        (
            "Scheduler",
            ["scheduler", "jobs", "describe", "rag-daily-sync", f"--location={region}", f"--project={project}"],
        ),
    ):
        ok, detail = _gcloud_json(args, timeout=30)
        if not ok:
            failures.append(f"{kind}: {str(detail)[:180]}")
    if failures:
        raise RuntimeError(" · ".join(failures))
    return services


def _execute_common_runtime_deployment(run_id: str) -> None:
    with _COMMON_RUNTIME_DEPLOY_LOCK:
        env_only = bool((_COMMON_RUNTIME_DEPLOY_RUNS.get(run_id) or {}).get("envOnly"))
    try:
        if env_only and not dept_config.list_departments():
            # deploy.ps1 -EnvOnly 는 학과 맵을 로컬 YAML 에서 만든다. YAML 을 두지
            # 않는 운영 환경에서는 그 자리에서 죽어, 정작 낡기 쉬운 학과 라우팅
            # 맵을 갱신할 방법이 없었다. 이 환경에서는 Cloud v2 주석에서 맵을
            # 재구성해 그 키 하나만 갱신한다 — parser/sync 의 나머지 env 는
            # deploy.ps1 의 $syncEnv 한 곳에만 두고 여기서 복제하지 않는다.
            codes = update_sync_department_map(
                on_line=lambda line: _append_common_runtime_deploy_log(run_id, line)
            )
            for key in ("config", "cloudRun"):
                _set_common_runtime_deploy_step(
                    run_id, key, status="COMPLETE", detail=f"동기화 대상 학과: {codes}"
                )
        else:
            exit_code = _run_common_runtime_deploy_script(
                on_line=lambda line: _append_common_runtime_deploy_log(run_id, line),
                env_only=env_only,
            )
            if exit_code != 0:
                raise RuntimeError(f"공통 런타임 배포 스크립트가 종료 코드 {exit_code}로 실패했습니다.")
            for key in ("config", "images", "cloudRun", "workflow", "scheduler"):
                _set_common_runtime_deploy_step(
                    run_id, key, status="COMPLETE", detail="배포 명령 완료"
                )
        _set_common_runtime_deploy_step(
            run_id, "ready", status="RUNNING", detail="배포 리소스 Ready 확인 중"
        )
        services = _verify_common_runtime_deployment()
        _set_common_runtime_deploy_step(
            run_id,
            "ready",
            status="COMPLETE",
            detail="Parser / Sync / Workflow / Scheduler 확인 완료",
        )
        with _COMMON_RUNTIME_DEPLOY_LOCK:
            current = _COMMON_RUNTIME_DEPLOY_RUNS[run_id]
            current.update(status="COMPLETED", services=services, finishedEpoch=time.time())
    except (OSError, RuntimeError, SystemExit, TypeError, ValueError, yaml.YAMLError) as exc:
        message = str(exc)[:500]
        with _COMMON_RUNTIME_DEPLOY_LOCK:
            current = _COMMON_RUNTIME_DEPLOY_RUNS.get(run_id)
            if not current:
                return
            running_step = next(
                (step for step in current["steps"] if step["status"] == "RUNNING"),
                None,
            )
            if running_step:
                running_step.update(status="FAILED", detail=message)
            current.update(status="FAILED", error=message, finishedEpoch=time.time())


def start_common_runtime_deployment(
    follow_up_department_code: str = "", env_only: bool = False
) -> dict[str, Any]:
    follow_up = str(follow_up_department_code or "").strip().lower()
    if follow_up and (
        not DEPT_CODE_RE.fullmatch(follow_up)
        or not (DEPT_DIR / f"{follow_up}.yaml").exists()
    ):
        raise FileNotFoundError(follow_up)
    common = _common()
    project = str(common.get("GCP_PROJECT_ID") or "")
    region = str(common.get("GCP_REGION") or "asia-northeast3")
    if not project:
        raise ValueError("공통 환경의 GCP 프로젝트가 설정되어 있지 않습니다.")
    with _COMMON_RUNTIME_DEPLOY_LOCK:
        _cleanup_common_runtime_deployments()
        active = next(
            (
                copy.deepcopy(run)
                for run in _COMMON_RUNTIME_DEPLOY_RUNS.values()
                if run["status"] == "RUNNING"
            ),
            None,
        )
        if active:
            if follow_up:
                _COMMON_RUNTIME_DEPLOY_RUNS[active["runId"]]["followUpDepartmentCode"] = follow_up
            raise FileExistsError(active["runId"])
        run_id = uuid.uuid4().hex
        run = {
            "runId": run_id,
            "status": "RUNNING",
            "project": project,
            "region": region,
            "followUpDepartmentCode": follow_up,
            "serviceNames": ["rag-parser", "rag-sync", "rag-daily-sync"],
            "services": [],
            "envOnly": bool(env_only),
            # env 갱신은 이미지·Workflow·Scheduler 를 건드리지 않는다. 없는 단계를
            # 회색으로 세워 두면 "안 돌았다" 로 읽히므로 아예 빼고 보여준다.
            "steps": [
                {"key": "config", "label": "설정 확인", "status": "RUNNING", "detail": "학과 라우팅 및 공통 설정 확인 중"},
                *(
                    []
                    if env_only
                    else [
                        {"key": "images", "label": "공통 이미지", "status": "PENDING", "detail": "Parser / Sync 이미지 조회 대기"},
                    ]
                ),
                {
                    "key": "cloudRun",
                    "label": "Parser / Sync",
                    "status": "PENDING",
                    "detail": "Cloud Run env 갱신 대기" if env_only else "Cloud Run 조회 대기",
                },
                *(
                    []
                    if env_only
                    else [
                        {"key": "workflow", "label": "Workflow", "status": "PENDING", "detail": "rag-daily-sync 배포 대기"},
                        {"key": "scheduler", "label": "Scheduler", "status": "PENDING", "detail": "정기 동기화 작업 등록 대기"},
                    ]
                ),
                {"key": "ready", "label": "Ready 확인", "status": "PENDING", "detail": "배포 리소스 확인 대기"},
            ],
            "logs": [],
            "createdEpoch": time.time(),
        }
        _COMMON_RUNTIME_DEPLOY_RUNS[run_id] = run
    threading.Thread(
        target=_execute_common_runtime_deployment,
        args=(run_id,),
        name=f"common-runtime-deploy-{run_id[:8]}",
        daemon=True,
    ).start()
    return copy.deepcopy(run)


# ---- 리소스 철거 (학과 / 공통 런타임) --------------------------------------
# 만드는 쪽과 달리 되돌릴 수 없다. 그래서 세 겹으로 막는다.
#   1. 계획(plan)을 먼저 만들어 무엇이 지워지는지 그대로 보여준다.
#   2. 학과 코드(공통은 프로젝트 ID)를 손으로 입력받는다.
#   3. **다른 학과가 참조하는 코퍼스·버킷은 건너뛴다.** 이름만 보고 지우면 남은
#      학과의 검색이 조용히 빈 결과를 낸다 — 오류도 안 난다.


def _cleanup_teardown_runs() -> None:
    cutoff = time.time() - _TEARDOWN_TTL_SECONDS
    for run_id in [
        key
        for key, run in _TEARDOWN_RUNS.items()
        if run.get("finishedEpoch", run.get("createdEpoch", time.time())) < cutoff
    ]:
        _TEARDOWN_RUNS.pop(run_id, None)


def _set_teardown_target(run_id: str, key: str, **changes: Any) -> None:
    with _TEARDOWN_LOCK:
        run = _TEARDOWN_RUNS.get(run_id)
        if not run:
            return
        for target in run["targets"]:
            if target["key"] == key:
                target.update(changes)
                return


def _department_corpus_usage() -> dict[str, list[str]]:
    """코퍼스 → 그 코퍼스를 가리키는 학과 코드. 버킷과 같은 이유로 필요하다."""
    usage: dict[str, set[str]] = {}
    if not DEPT_DIR.exists():
        return {}
    for path in sorted(DEPT_DIR.glob("*.yaml")):
        try:
            config = _read_yaml(path)
        except (OSError, yaml.YAMLError):
            continue
        for corpus in (config.get("corpora") or {}).values():
            name = str(corpus or "").strip()
            if name:
                usage.setdefault(name, set()).add(path.stem)
    return {name: sorted(codes) for name, codes in usage.items()}


def _teardown_target(
    key: str, kind: str, label: str, name: str, shared: list[str]
) -> dict[str, Any]:
    return {
        "key": key,
        "kind": kind,
        "label": label,
        "name": name,
        "sharedWith": shared,
        "skipped": bool(shared),
        "status": "PENDING",
        "detail": (
            f"다른 학과가 사용 중이라 남깁니다: {', '.join(shared)}" if shared else "삭제 대기"
        ),
    }


def department_teardown_plan(code: str) -> dict[str, Any]:
    """학과 하나를 지울 때 무엇이 사라지는지 한 벌로 만든다."""
    normalised = str(code or "").strip().lower()
    path = DEPT_DIR / f"{normalised}.yaml"
    if not DEPT_CODE_RE.fullmatch(normalised) or not path.exists():
        raise FileNotFoundError(normalised)
    config = _read_yaml(path)
    common = _common()
    project = str(common.get("GCP_PROJECT_ID") or "")
    region = str(common.get("GCP_REGION") or "asia-northeast3")
    corpora = config.get("corpora") or {}
    buckets = config.get("buckets") or {}
    corpus_usage = _department_corpus_usage()
    bucket_usage = _department_bucket_usage()

    targets: list[dict[str, Any]] = []
    audiences = ["staff", "student"] if str(corpora.get("student") or "").strip() else ["staff"]
    for audience in audiences:
        targets.append(
            _teardown_target(
                f"mcp-{audience}",
                "cloudRun",
                f"MCP Cloud Run ({audience})",
                f"rag-mcp-{normalised}-{audience}",
                [],
            )
        )
    for audience in ("staff", "student"):
        name = str(corpora.get(audience) or "").strip()
        if not name:
            continue
        shared = [item for item in corpus_usage.get(name, []) if item != normalised]
        targets.append(
            _teardown_target(
                f"corpus-{audience}", "corpus", f"RAG 코퍼스 ({audience})", name, shared
            )
        )
    for slot, label in (("hwpOriginal", "원본 HWP 버킷"), ("source", "Source 버킷")):
        name = str(buckets.get(slot) or "").removeprefix("gs://").strip()
        if not name:
            continue
        shared = [item for item in bucket_usage.get(name, []) if item != normalised]
        targets.append(_teardown_target(f"bucket-{slot}", "bucket", label, name, shared))
    # 설정 파일은 **맨 뒤**다. 앞이 하나라도 실패하면 남겨서 재시도할 수 있어야 한다.
    targets.append(_teardown_target("config", "config", "학과 설정 파일", path.name, []))

    return {
        "kind": "department",
        "code": normalised,
        "name": str(config.get("name") or normalised),
        "projectId": project,
        "region": region,
        "confirmWord": normalised,
        "targets": targets,
    }


def common_runtime_teardown_plan() -> dict[str, Any]:
    """공통 런타임(parser/sync/Workflow/Scheduler)을 지울 때의 계획.

    학과 리소스는 건드리지 않는다. 다만 **남아 있는 학과가 있으면 그 학과의
    동기화가 통째로 멈춘다** — 목록을 계획에 실어 화면에서 보이게 한다.
    """
    common = _common()
    project = str(common.get("GCP_PROJECT_ID") or "")
    region = str(common.get("GCP_REGION") or "asia-northeast3")
    if not project:
        raise ValueError("공통 환경의 GCP 프로젝트가 설정되어 있지 않습니다.")
    remaining = sorted(path.stem for path in DEPT_DIR.glob("*.yaml")) if DEPT_DIR.exists() else []
    targets = [
        _teardown_target("scheduler", "scheduler", "Cloud Scheduler 작업", "rag-daily-sync", []),
        _teardown_target("workflow", "workflow", "Workflow", "rag-daily-sync", []),
        _teardown_target("sync", "cloudRun", "Cloud Run", "rag-sync", []),
        _teardown_target("parser", "cloudRun", "Cloud Run", "rag-parser", []),
    ]
    return {
        "kind": "commonRuntime",
        "code": "",
        "name": "공통 런타임",
        "projectId": project,
        "region": region,
        "confirmWord": project,
        "remainingDepartments": remaining,
        "targets": targets,
    }


def _missing_resource_output(output: str) -> bool:
    """없어서 실패한 것은 성공으로 본다 — 철거는 몇 번을 돌려도 같아야 한다."""
    lowered = str(output or "").lower()
    return any(
        marker in lowered
        for marker in ("not_found", "not found", "does not exist", "no longer exists")
    )


def _delete_cloud_run_service(name: str, project: str, region: str) -> str:
    gcloud = _gcloud_executable()
    if not gcloud:
        raise RuntimeError("gcloud를 찾을 수 없습니다.")
    ok, output = _run_command(
        [
            gcloud,
            "run",
            "services",
            "delete",
            name,
            f"--region={region}",
            f"--project={project}",
            "--quiet",
        ],
        timeout=300,
    )
    if ok:
        return "삭제 완료"
    if _missing_resource_output(output):
        return "이미 없습니다"
    raise RuntimeError((output or "Cloud Run 서비스 삭제에 실패했습니다.")[-400:])


def _delete_workflow_resource(name: str, project: str, region: str) -> str:
    gcloud = _gcloud_executable()
    if not gcloud:
        raise RuntimeError("gcloud를 찾을 수 없습니다.")
    ok, output = _run_command(
        [
            gcloud,
            "workflows",
            "delete",
            name,
            f"--location={region}",
            f"--project={project}",
            "--quiet",
        ],
        timeout=180,
    )
    if ok:
        return "삭제 완료"
    if _missing_resource_output(output):
        return "이미 없습니다"
    raise RuntimeError((output or "Workflow 삭제에 실패했습니다.")[-400:])


def _delete_scheduler_job(name: str, project: str, region: str) -> str:
    gcloud = _gcloud_executable()
    if not gcloud:
        raise RuntimeError("gcloud를 찾을 수 없습니다.")
    ok, output = _run_command(
        [
            gcloud,
            "scheduler",
            "jobs",
            "delete",
            name,
            f"--location={region}",
            f"--project={project}",
            "--quiet",
        ],
        timeout=180,
    )
    if ok:
        return "삭제 완료"
    if _missing_resource_output(output):
        return "이미 없습니다"
    raise RuntimeError((output or "Scheduler 작업 삭제에 실패했습니다.")[-400:])


def _delete_rag_corpus(name: str, region: str, token: str) -> str:
    """코퍼스는 force=true 로 지운다 — 안 그러면 파일이 남았다며 거부한다."""
    url = f"https://{region}-aiplatform.googleapis.com/v1/{name}?force=true"
    status, body, _ = _http_delete_json(url, token, timeout=30)
    if status == 404:
        return "이미 없습니다"
    if status not in {200, 202}:
        message = (
            str((body.get("error") or {}).get("message") or "") if isinstance(body, dict) else ""
        )
        detail = f": {message[:300]}" if message else ""
        raise RuntimeError(f"코퍼스 삭제 실패 (HTTP {status or 'timeout'}){detail}")
    operation = str(body.get("name") or "") if isinstance(body, dict) else ""
    if not operation or (isinstance(body, dict) and body.get("done")):
        return "삭제 완료"
    # 파일이 많으면 오래 걸린다. 끝을 못 봐도 삭제 자체는 서버에서 계속 진행된다.
    operation_url = f"https://{region}-aiplatform.googleapis.com/v1/{operation}"
    deadline = time.time() + 300
    while time.time() < deadline:
        poll_status, payload, _ = _http_json(operation_url, token, timeout=20)
        if poll_status == 200 and isinstance(payload, dict) and payload.get("done"):
            if payload.get("error"):
                raise RuntimeError(
                    str((payload.get("error") or {}).get("message") or "")[:400]
                    or "코퍼스 삭제 작업이 실패했습니다."
                )
            return "삭제 완료"
        time.sleep(3)
    return "삭제 진행 중 (백그라운드)"


def _delete_bucket_resource(name: str) -> str:
    """객체를 먼저 비우고 버킷을 지운다. 비어 있으면 첫 단계는 그냥 넘어간다."""
    gcloud = _gcloud_executable()
    if not gcloud:
        raise RuntimeError("gcloud를 찾을 수 없습니다.")
    ok, output = _run_command(
        [gcloud, "storage", "rm", "--recursive", f"gs://{name}/**", "--quiet"],
        timeout=1800,
    )
    lowered = str(output or "").lower()
    if not ok and not _missing_resource_output(output) and "matched no objects" not in lowered:
        raise RuntimeError((output or "버킷 객체 삭제에 실패했습니다.")[-400:])
    ok, output = _run_command(
        [gcloud, "storage", "buckets", "delete", f"gs://{name}", "--quiet"], timeout=300
    )
    if ok:
        return "삭제 완료"
    if _missing_resource_output(output):
        return "이미 없습니다"
    raise RuntimeError((output or "버킷 삭제에 실패했습니다.")[-400:])


def _delete_department_config(code: str) -> str:
    path = DEPT_DIR / f"{code}.yaml"
    if not path.exists():
        return "이미 없습니다"
    path.unlink()
    return "삭제 완료"


def _execute_teardown_run(run_id: str) -> None:
    with _TEARDOWN_LOCK:
        run = copy.deepcopy(_TEARDOWN_RUNS[run_id])
    project = run["projectId"]
    region = run["region"]
    token = ""
    failed = 0
    for target in run["targets"]:
        key = target["key"]
        kind = target["kind"]
        name = target["name"]
        if target.get("skipped"):
            _set_teardown_target(run_id, key, status="SKIPPED")
            continue
        # 설정 파일은 GCP 쪽이 다 지워진 뒤에만 지운다. 남겨야 재시도가 된다.
        if kind == "config" and failed:
            _set_teardown_target(
                run_id, key, status="SKIPPED", detail="GCP 리소스가 남아 설정 파일은 유지합니다"
            )
            continue
        _set_teardown_target(run_id, key, status="RUNNING", detail="삭제 중")
        try:
            if kind == "cloudRun":
                detail = _delete_cloud_run_service(name, project, region)
            elif kind == "workflow":
                detail = _delete_workflow_resource(name, project, region)
            elif kind == "scheduler":
                detail = _delete_scheduler_job(name, project, region)
            elif kind == "corpus":
                token = token or _provision_access_token()
                detail = _delete_rag_corpus(name, region, token)
            elif kind == "bucket":
                detail = _delete_bucket_resource(name)
            else:
                detail = _delete_department_config(run["code"])
            _set_teardown_target(run_id, key, status="COMPLETE", detail=detail)
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            failed += 1
            _set_teardown_target(run_id, key, status="FAILED", detail=str(exc)[:400])

    with _TEARDOWN_LOCK:
        current = _TEARDOWN_RUNS.get(run_id)
        if not current:
            return
        statuses = [item["status"] for item in current["targets"]]
        if any(status == "FAILED" for status in statuses):
            current["status"] = "PARTIAL" if "COMPLETE" in statuses else "FAILED"
        else:
            current["status"] = "COMPLETED"
        current["finishedEpoch"] = time.time()


def start_teardown_run(plan: dict[str, Any], confirm: str) -> dict[str, Any]:
    """계획을 확정해 실행한다. 확인 문구가 어긋나면 아무것도 하지 않는다."""
    expected = str(plan.get("confirmWord") or "")
    if str(confirm or "").strip() != expected:
        raise PermissionError(expected)
    with _TEARDOWN_LOCK:
        _cleanup_teardown_runs()
        active = next(
            (item for item in _TEARDOWN_RUNS.values() if item["status"] == "RUNNING"), None
        )
        if active:
            raise FileExistsError(active["runId"])
        run_id = uuid.uuid4().hex
        run = {
            "runId": run_id,
            "kind": plan["kind"],
            "code": plan.get("code", ""),
            "name": plan.get("name", ""),
            "projectId": plan["projectId"],
            "region": plan["region"],
            "status": "RUNNING",
            "targets": copy.deepcopy(plan["targets"]),
            "createdEpoch": time.time(),
        }
        _TEARDOWN_RUNS[run_id] = run
    threading.Thread(
        target=_execute_teardown_run,
        args=(run_id,),
        name=f"teardown-{run_id[:8]}",
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
        _MCP_DEPLOY_CONFIGS.pop(run_id, None)


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


def _gcloud_env_argument(values: dict[str, Any]) -> str:
    rendered = {key: str(value) for key, value in values.items()}
    delimiter = "~"
    while any(delimiter in value for value in rendered.values()):
        delimiter += "~"
    return f"^{delimiter}^" + delimiter.join(
        f"{key}={value}" for key, value in rendered.items()
    )


def _cloud_department_configs() -> dict[str, dict[str, Any]]:
    """배포된 v2 주석에서 전 학과 설정을 모은다. rag-sync 라우팅 맵의 원본이다.

    교직원 서비스만 조회한다 — 주석 하나에 학생 코퍼스·폴더까지 들어 있어
    학생 서비스를 또 describe 할 이유가 없다.

    v2 주석이 없는 학과가 하나라도 있으면 맵을 만들지 않고 죽는다. 그 학과를
    빼고 맵을 씌우면 rag-sync 가 그 드라이브를 UnknownDriveError 로 건너뛰는데,
    갱신 자체는 성공한 것처럼 보여 누락이 드러나지 않는다.
    """
    common = _common()
    project = str(common.get("GCP_PROJECT_ID") or "")
    region = str(common.get("GCP_REGION") or "asia-northeast3")
    if not project:
        raise RuntimeError("공통 설정에 GCP_PROJECT_ID가 없습니다.")
    ok, rows = _gcloud_json(
        [
            "run",
            "services",
            "list",
            "--platform=managed",
            f"--region={region}",
            f"--project={project}",
        ],
        timeout=30,
    )
    if not ok or not isinstance(rows, list):
        raise RuntimeError(str(rows)[:300] or "Cloud Run 서비스 목록을 조회하지 못했습니다.")

    names: list[str] = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        name = str((item.get("metadata") or {}).get("name") or item.get("name") or "")
        if re.fullmatch(r"rag-mcp-[a-z][a-z0-9-]{1,19}-staff", name):
            names.append(name)
    if not names:
        raise RuntimeError("배포된 MCP 서비스가 없어 학과 라우팅 맵을 만들 수 없습니다.")

    configs: dict[str, dict[str, Any]] = {}
    incomplete: list[str] = []
    with ThreadPoolExecutor(max_workers=min(8, len(names))) as pool:
        futures = {
            pool.submit(
                _gcloud_json,
                [
                    "run",
                    "services",
                    "describe",
                    name,
                    f"--region={region}",
                    f"--project={project}",
                ],
                30,
            ): name
            for name in names
        }
        for future in as_completed(futures):
            name = futures[future]
            code = str(re.fullmatch(r"rag-mcp-(.+)-staff", name).group(1))
            service_ok, service = future.result()
            metadata = (
                _decode_cloud_department_metadata(
                    _cloud_run_management_annotation(service)
                )
                if service_ok and isinstance(service, dict)
                else None
            )
            uploaded = (metadata or {}).get("yaml")
            if not metadata or metadata.get("code") != code or not isinstance(uploaded, dict):
                incomplete.append(code)
                continue
            configs[code] = copy.deepcopy(uploaded)
    if incomplete:
        raise RuntimeError(
            "전체 설정(v2) 주석이 없는 학과가 있어 라우팅 맵을 만들 수 없습니다: "
            + ", ".join(sorted(incomplete))
            + " · 해당 학과를 한 번 재배포해 주세요."
        )
    return configs


def cloud_departments_json() -> str:
    """Cloud v2 주석 기준 rag-sync DEPARTMENTS_JSON 한 줄."""
    return dept_config.departments_json_from_configs(_cloud_department_configs())


def update_sync_department_map(*, on_line: Any = lambda _line: None) -> str:
    """rag-sync 의 DEPARTMENTS_JSON 을 지금 배포된 학과 전체로 맞춘다.

    **--update-env-vars 여야 한다.** --set-env-vars 는 env 를 통째로 치환하므로
    여기서 이 키 하나만 넘기면 버킷·코퍼스·Cloud Tasks 설정이 통째로 사라진다
    (그 목록은 deploy.ps1 의 $syncEnv 한 곳에만 있다).
    """
    gcloud = _gcloud_executable()
    if not gcloud:
        raise RuntimeError("gcloud를 찾을 수 없습니다.")
    common = _common()
    project = str(common.get("GCP_PROJECT_ID") or "")
    region = str(common.get("GCP_REGION") or "asia-northeast3")
    expected = cloud_departments_json()

    ok, service = _gcloud_json(
        [
            "run",
            "services",
            "describe",
            SYNC_SERVICE,
            f"--region={region}",
            f"--project={project}",
        ],
        timeout=30,
    )
    if not ok or not isinstance(service, dict):
        raise RuntimeError(f"{SYNC_SERVICE} 조회 실패: {str(service)[:200]}")
    if _cloud_run_env(service).get("DEPARTMENTS_JSON", "") == expected:
        codes = ", ".join(sorted(json.loads(expected)))
        on_line(f"{SYNC_SERVICE}: 학과 라우팅 맵이 이미 최신입니다 ({codes})")
        return codes

    on_line(f"{SYNC_SERVICE}: 학과 라우팅 맵 갱신 중")
    # 값을 명령줄에 싣지 않고 flags-file 로 넘긴다. 학과 맵에는 콤마가 있어
    # --update-env-vars 는 `^|^` 커스텀 구분자를 요구하는데, Windows 에서
    # gcloud 는 .cmd 배치 shim 이라 그 `^` 를 cmd.exe 가 먼저 먹는다. 그러면
    # gcloud 는 콤마마다 값을 끊어 "Bad syntax for dict arg" 로 죽는다(실측).
    # flags-file 은 YAML 이라 셸 인용 규칙을 전혀 타지 않는다.
    handle = tempfile.NamedTemporaryFile(
        "w", suffix=".yaml", delete=False, encoding="utf-8"
    )
    try:
        yaml.safe_dump(
            [{"--update-env-vars": {"DEPARTMENTS_JSON": expected}}],
            handle,
            allow_unicode=False,
            default_flow_style=False,
        )
        handle.close()
        args = [
            gcloud,
            "run",
            "services",
            "update",
            SYNC_SERVICE,
            f"--region={region}",
            f"--project={project}",
            "--platform=managed",
            f"--flags-file={handle.name}",
            "--quiet",
        ]
        update_ok, output = _run_command(args, timeout=600)
    finally:
        handle.close()
        Path(handle.name).unlink(missing_ok=True)
    if not update_ok:
        raise RuntimeError(f"{SYNC_SERVICE}: {str(output or '갱신 실패')[:400]}")
    # 동기화 탭은 배포된 맵을 30초 캐시로 읽는다. 방금 올린 학과가 곧바로
    # 목록에 뜨도록 비운다.
    with _SYNC_TARGET_CACHE_LOCK:
        _SYNC_TARGET_CACHE.clear()
    codes = ", ".join(sorted(json.loads(expected)))
    on_line(f"{SYNC_SERVICE}: 학과 라우팅 맵 갱신 완료 ({codes})")
    return codes


def _cloud_config_audiences(config: dict[str, Any]) -> tuple[str, ...]:
    corpora = config.get("corpora") if isinstance(config.get("corpora"), dict) else {}
    drive = config.get("drive") if isinstance(config.get("drive"), dict) else {}
    keys = config.get("keys") if isinstance(config.get("keys"), dict) else {}
    staff_key = str(keys.get("staff") or "").strip()
    if not staff_key or staff_key in dept_config.PLACEHOLDER_KEYS:
        raise ValueError("keys.staff가 없어 Cloud 설정을 배포할 수 없습니다.")
    student_parts = (
        bool(str(corpora.get("student") or "").strip()),
        bool(_normalise_ids(drive.get("studentFolderIds"))),
        bool(str(keys.get("student") or "").strip()),
    )
    if any(student_parts) and not all(student_parts):
        raise ValueError("학생 분리는 corpus, 폴더, MCP 키가 모두 필요합니다.")
    if all(student_parts) and str(keys.get("student") or "") == staff_key:
        raise ValueError("교직원과 학생 MCP 키가 같습니다.")
    return dept_config.AUDIENCES if all(student_parts) else ("staff",)


def _update_cloud_department_config(
    code: str, config: dict[str, Any], *, on_line: Any = lambda _line: None
) -> list[str]:
    """로컬 파일 없이 기존 MCP 서비스의 env와 전체-YAML 주석을 갱신한다."""
    gcloud = _gcloud_executable()
    if not gcloud:
        raise RuntimeError("gcloud를 찾을 수 없습니다.")
    common = _common()
    project = str(common.get("GCP_PROJECT_ID") or "")
    region = str(common.get("GCP_REGION") or "asia-northeast3")
    corpora = config.get("corpora") or {}
    buckets = config.get("buckets") or {}
    keys = config.get("keys") or {}
    mins = config.get("minInstances") or {}
    effective_buckets = {
        "hwpOriginal": str(
            buckets.get("hwpOriginal") or common.get("GCS_HWP_ORIGINAL_BUCKET") or "unused"
        ),
        "source": str(buckets.get("source") or common.get("GCS_SOURCE_BUCKET") or "unused"),
    }
    audiences = _cloud_config_audiences(config)
    updated: list[str] = []
    for audience in audiences:
        service = f"rag-mcp-{code}-{audience}"
        env_for_metadata = {
            "GCS_HWP_ORIGINAL_BUCKET": effective_buckets["hwpOriginal"],
            "GCS_SOURCE_BUCKET": effective_buckets["source"],
        }
        annotation = dept_config._deployment_metadata_b64(
            code,
            audience,
            config,
            env_for_metadata,
            staff_corpus=str(corpora.get("staff") or ""),
            student_corpus=str(corpora.get("student") or ""),
        )
        environment = {
            "GCP_PROJECT_ID": project,
            "GCP_REGION": region,
            "RAG_CORPUS_NAME": str(corpora.get(audience) or ""),
            "GCS_HWP_ORIGINAL_BUCKET": effective_buckets["hwpOriginal"],
            "GCS_SOURCE_BUCKET": effective_buckets["source"],
            "FIRESTORE_DATABASE": str(common.get("FIRESTORE_DATABASE") or "rag-sync-state"),
            "DOC_STATE_COLLECTION": str(common.get("DOC_STATE_COLLECTION") or "doc_state"),
            "MCP_API_KEY": str(keys.get(audience) or ""),
            "TOP_K_DEFAULT": str(common.get("TOP_K_DEFAULT") or 5),
            "SEARCH_FETCH_MULTIPLIER": str(common.get("SEARCH_FETCH_MULTIPLIER") or 3),
            "SEARCH_FETCH_MAX": str(common.get("SEARCH_FETCH_MAX") or 60),
        }
        args = [
            gcloud,
            "run",
            "services",
            "update",
            service,
            f"--region={region}",
            f"--project={project}",
            "--platform=managed",
            (
                "--update-labels="
                f"gcp-rag-managed=true,gcp-rag-dept={code},"
                f"gcp-rag-audience={audience},gcp-rag-schema=v2"
            ),
            f"--update-annotations={DEPLOYMENT_METADATA_ANNOTATION}={annotation}",
            f"--set-env-vars={_gcloud_env_argument(environment)}",
            f"--concurrency={int(common.get('MCP_CONCURRENCY') or 40)}",
            f"--min-instances={int(mins.get(audience, 0) or 0)}",
            "--quiet",
        ]
        on_line(f"{service}: Cloud 설정 배포 중")
        ok, output = _run_command(args, timeout=600)
        if not ok:
            clean = str(output or "Cloud Run 업데이트 실패")
            for secret in keys.values():
                if secret:
                    clean = clean.replace(str(secret), "***")
            raise RuntimeError(f"{service}: {clean[:400]}")
        updated.append(service)
        on_line(f"{service}: Cloud 설정 배포 완료")
    return updated


def _execute_mcp_deployment(run_id: str) -> None:
    with _MCP_DEPLOY_LOCK:
        run = copy.deepcopy(_MCP_DEPLOY_RUNS[run_id])
        cloud_config = copy.deepcopy(_MCP_DEPLOY_CONFIGS.get(run_id))
    code = run["code"]
    try:
        if cloud_config is not None:
            config = cloud_config
            audiences = _cloud_config_audiences(config)
        else:
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

        _set_mcp_deploy_step(run_id, "deploy", status="RUNNING", detail="Cloud Run 배포 시작")
        if cloud_config is not None:
            _set_mcp_deploy_step(
                run_id, "image", status="COMPLETE", detail="현재 배포 이미지 유지"
            )
            _update_cloud_department_config(
                code,
                config,
                on_line=lambda line: _append_mcp_deploy_log(
                    run_id, line, secrets_to_redact
                ),
            )
        else:
            common = _common()
            project = str(common.get("GCP_PROJECT_ID") or "")
            region = str(common.get("GCP_REGION") or "asia-northeast3")
            repository = str(common.get("ARTIFACT_REPO") or "rag-mcp")
            image = f"{region}-docker.pkg.dev/{project}/{repository}/mcp:latest"
            _set_mcp_deploy_step(
                run_id, "image", status="RUNNING", detail="Artifact Registry 확인 중"
            )
            image_ok, _image_info = _gcloud_json(
                ["artifacts", "docker", "images", "describe", image], timeout=30
            )
            _set_mcp_deploy_step(
                run_id,
                "image",
                status="COMPLETE",
                detail="기존 MCP 이미지 사용" if image_ok else "이미지 없음 · 이번 배포에서 빌드",
            )
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

        # MCP 만 올리고 끝내면 rag-sync 의 DEPARTMENTS_JSON 이 그대로라 이 학과의
        # 드라이브가 라우팅 맵에 없다. 그 상태에서는 동기화 탭에 학과가 뜨지도
        # 않고, 실행해도 UnknownDriveError 로 건너뛴다(실측 — cs 가 그랬다).
        # 배포의 마지막 단계로 묶어야 학과 추가 때마다 사람이 기억해야 하는
        # 수동 절차가 되지 않는다.
        _set_mcp_deploy_step(
            run_id, "syncMap", status="RUNNING", detail="rag-sync 학과 라우팅 맵 갱신 중"
        )
        codes = update_sync_department_map(
            on_line=lambda line: _append_mcp_deploy_log(run_id, line, secrets_to_redact)
        )
        _set_mcp_deploy_step(
            run_id, "syncMap", status="COMPLETE", detail=f"동기화 대상 학과: {codes}"
        )

        with _MCP_DEPLOY_LOCK:
            current = _MCP_DEPLOY_RUNS[run_id]
            current.update(
                status="COMPLETED",
                servers=servers,
                finishedEpoch=time.time(),
            )
            _MCP_DEPLOY_CONFIGS.pop(run_id, None)
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
            _MCP_DEPLOY_CONFIGS.pop(run_id, None)


def start_mcp_deployment(
    code: str, *, cloud_config: dict[str, Any] | None = None
) -> dict[str, Any]:
    normalised = str(code or "").strip().lower()
    if not DEPT_CODE_RE.fullmatch(normalised):
        raise FileNotFoundError(normalised)
    local = (DEPT_DIR / f"{normalised}.yaml").exists()
    if cloud_config is None and local:
        config = department_public_config(normalised)
        audiences = dept_config.configured_audiences(normalised)
    else:
        full_config = copy.deepcopy(cloud_config) if cloud_config is not None else cloud_department_config(normalised)[0]
        audiences = _cloud_config_audiences(full_config)
        config = {
            "name": str(full_config.get("name") or normalised),
            "corpusMode": "split" if len(audiences) == 2 else "single",
        }
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
                {"key": "syncMap", "label": "학과 라우팅 반영", "status": "PENDING", "detail": "대기 중"},
            ],
            "logs": [],
            "createdEpoch": time.time(),
        }
        _MCP_DEPLOY_RUNS[run_id] = run
        if not local or cloud_config is not None:
            _MCP_DEPLOY_CONFIGS[run_id] = full_config
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
        return token, service_account, latency, f"서비스 계정 임시 접근 토큰 HTTP {status or 'timeout'}"
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


def _drive_folder_children(drive_id: str, parent_id: str, token: str) -> dict[str, Any]:
    """공유드라이브 안에서 한 단계의 폴더만 조회한다(트리 지연 로딩용)."""
    query = (
        f"'{parent_id}' in parents and "
        f"mimeType = '{DRIVE_FOLDER_MIME_TYPE}' and trashed = false"
    )
    params = urlencode(
        {
            "q": query,
            "corpora": "drive",
            "driveId": drive_id,
            "includeItemsFromAllDrives": "true",
            "supportsAllDrives": "true",
            "pageSize": "200",
            "orderBy": "folder,name_natural",
            "fields": "nextPageToken,files(id,name,driveId,parents,mimeType)",
        }
    )
    status, body, latency = _http_json(
        f"https://www.googleapis.com/drive/v3/files?{params}", token, timeout=20
    )
    if status != 200 or not isinstance(body, dict):
        raise RuntimeError(f"Drive 폴더 목록 HTTP {status or 'timeout'}")
    folders = [
        {
            "folderId": str(item.get("id") or ""),
            "name": str(item.get("name") or "이름 없는 폴더"),
            "driveId": str(item.get("driveId") or drive_id),
            "parentIds": _normalise_ids(item.get("parents")),
        }
        for item in body.get("files") or []
        if isinstance(item, dict) and item.get("id")
    ]
    return {
        "driveId": drive_id,
        "parentId": parent_id,
        "folders": folders,
        "truncated": bool(body.get("nextPageToken")),
        "latencyMs": latency,
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
            if confirmed_missing and service in {"rag-parser", "rag-sync"}:
                checks.append(
                    _check(
                        "DEPLOY",
                        label,
                        "WARN",
                        "공통 Cloud Run 서비스가 아직 배포되지 않았습니다.",
                        action="공통 런타임 배포",
                        actionType="COMMON_RUNTIME_DEPLOY",
                        departmentCode=code,
                    )
                )
            elif confirmed_missing and service.startswith(f"rag-mcp-{code}-"):
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
        action_extra = (
            {
                "action": "공통 런타임 다시 배포",
                "actionType": "COMMON_RUNTIME_DEPLOY",
                "departmentCode": code,
            }
            if status != "OK" and service in {"rag-parser", "rag-sync"}
            else {}
        )
        checks.append(
            _check(
                "DEPLOY",
                label,
                status,
                detail,
                serviceName=service,
                url=url,
                **action_extra,
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
    common: dict[str, Any],
    cache: _StatusRunCache | None = None,
    deploy_checks: list[dict[str, Any]] | None = None,
    code: str = "",
) -> list[dict]:
    """정기 동기화 상태. **원인이 앞 단계에 있으면 그쪽으로 넘긴다.**

    예전에는 무엇이 잘못됐든 "최근 실행 없음 또는 조회 실패" 한 줄이었다. 공통
    런타임이 없어서 워크플로가 아예 없는 경우와, 워크플로는 있는데 아직 안 돈
    경우와, 조회가 실패한 경우가 같은 문구로 보였다 — 화면에서 다음에 뭘 눌러야
    하는지 알 수 없었다.
    """
    project = str(common.get("GCP_PROJECT_ID") or "")
    region = str(common.get("GCP_REGION") or "asia-northeast3")
    gcloud_json = cache.gcloud_json if cache else _gcloud_json

    # 배포 단계에서 이미 걸린 것이 있으면 여기서 또 오류를 내지 않는다.
    # 같은 사고를 두 줄로 세면 어느 쪽을 고쳐야 하는지가 흐려진다.
    blockers = [
        str(item.get("name") or "")
        for item in (deploy_checks or [])
        if item.get("layer") == "DEPLOY"
        and str(item.get("name") or "") in {"parser", "sync"}
        and item.get("status") != "OK"
    ]
    if blockers:
        return [
            _check(
                "SYNC",
                "latest-workflow",
                "SKIP",
                f"배포 단계가 끝나지 않아 확인하지 못했습니다: {', '.join(blockers)}",
                action="공통 런타임 배포",
                actionType="COMMON_RUNTIME_DEPLOY",
                blockedBy=blockers,
                departmentCode=code,
            )
        ]

    workflow_ok, workflow = gcloud_json(
        [
            "workflows",
            "describe",
            "rag-daily-sync",
            f"--location={region}",
            f"--project={project}",
        ]
    )
    if not workflow_ok:
        detail = str(workflow or "").lower()
        if any(marker in detail for marker in ("not found", "not_found", "does not exist")):
            return [
                _check(
                    "SYNC",
                    "latest-workflow",
                    "WARN",
                    "정기 동기화 Workflow(rag-daily-sync)가 아직 배포되지 않았습니다.",
                    action="공통 런타임 배포",
                    actionType="COMMON_RUNTIME_DEPLOY",
                    departmentCode=code,
                )
            ]
        return [
            _check(
                "SYNC",
                "latest-workflow",
                "FAIL",
                f"Workflow 조회 실패 · {str(workflow or '')[:200]}",
            )
        ]

    # 정기 실행을 거는 것은 Scheduler 잡이다. 여기가 비어 있으면 워크플로가 멀쩡해도
    # 아무것도 안 돈다 — 배포가 중간에 죽으면 딱 이 상태가 되고, 예전에는 어느
    # 검사에도 안 잡혀서 "최근 실행 없음" 한 줄로만 드러났다(실측).
    checks: list[dict[str, Any]] = []
    scheduler_ready = False
    job_ok, job = gcloud_json(
        [
            "scheduler",
            "jobs",
            "describe",
            "rag-daily-sync",
            f"--location={region}",
            f"--project={project}",
        ]
    )
    if not job_ok:
        job_detail = str(job or "").lower()
        if any(marker in job_detail for marker in ("not found", "not_found", "does not exist")):
            checks.append(
                _check(
                    "SYNC",
                    "scheduler-job",
                    "WARN",
                    "정기 동기화 작업(Cloud Scheduler)이 등록되지 않아 자동 실행되지 않습니다.",
                    action="공통 런타임 배포",
                    actionType="COMMON_RUNTIME_DEPLOY",
                    departmentCode=code,
                )
            )
        else:
            checks.append(
                _check(
                    "SYNC",
                    "scheduler-job",
                    "FAIL",
                    f"Scheduler 작업 조회 실패 · {str(job or '')[:200]}",
                )
            )
    else:
        state = str((job or {}).get("state") or "UNKNOWN")
        schedule = str((job or {}).get("schedule") or "")
        zone = str((job or {}).get("timeZone") or "")
        if state == "ENABLED":
            scheduler_ready = True
            checks.append(
                _check("SYNC", "scheduler-job", "OK", f"ENABLED · {schedule} {zone}".strip())
            )
        else:
            checks.append(
                _check(
                    "SYNC",
                    "scheduler-job",
                    "WARN",
                    f"{state} · 자동 실행이 멈춰 있습니다",
                    action=f"gcloud scheduler jobs resume rag-daily-sync --location={region}",
                )
            )

    ok, rows = gcloud_json(
        [
            "workflows",
            "executions",
            "list",
            "rag-daily-sync",
            f"--location={region}",
            f"--project={project}",
            # 사용자가 중복 실행을 취소한 직후에도 그 CANCELLED 한 건만 보고
            # 전체 동기화가 실패했다고 오판하지 않도록 최근 이력을 함께 본다.
            "--limit=10",
        ]
    )
    if not ok:
        checks.append(
            _check(
                "SYNC",
                "latest-workflow",
                "FAIL",
                f"실행 이력 조회 실패 · {str(rows or '')[:200]}",
            )
        )
        return checks
    if not rows:
        if not scheduler_ready:
            checks.append(
                _check(
                    "SYNC",
                    "latest-workflow",
                    "SKIP",
                    "정기 실행이 걸려 있지 않아 실행 이력이 없습니다: scheduler-job",
                    action="공통 런타임 배포",
                    actionType="COMMON_RUNTIME_DEPLOY",
                    blockedBy=["scheduler-job"],
                    departmentCode=code,
                )
            )
            return checks
        checks.append(
            _check(
                "SYNC",
                "latest-workflow",
                "WARN",
                "정기 실행은 등록됐지만 아직 실행된 적이 없습니다. 첫 동기화를 실행해 확인하세요.",
                action="동기화 실행",
                actionType="MANUAL_SYNC",
                departmentCode=code,
            )
        )
        return checks
    # CANCELLED 는 운영자가 중복 작업을 정리한 정상적인 결과일 수 있다. 최근
    # 유효 실행이 따로 있으면 그것으로 상태를 판정하고, 전부 취소된 경우에만
    # 마지막 취소를 WARN 으로 보여 준다. FAILED 는 실제 장애이므로 건너뛰지 않는다.
    row = next(
        (item for item in rows if str(item.get("state") or "") != "CANCELLED"),
        rows[0],
    )
    state = str(row.get("state") or "UNKNOWN")
    started_raw = str(row.get("startTime") or "")
    finished_raw = str(row.get("endTime") or "")
    status = "FAIL"
    detail = state
    if state == "ACTIVE":
        status = "WARN"
        detail = f"ACTIVE · {started_raw}"
    elif state == "CANCELLED":
        status = "WARN"
        detail = "CANCELLED · 취소된 실행이며 서비스 장애는 아닙니다"
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
    checks.append(_check("SYNC", "latest-workflow", status, detail))
    return checks


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


def _deployed_sync_department_map() -> dict[str, dict[str, Any]]:
    """rag-sync가 실제 사용하는 학과 라우팅 맵을 Cloud Run env에서 읽는다.

    데이터 동기화는 MCP 주석보다 이 값이 중요하다. 주석에 학과가 있어도
    rag-sync의 DEPARTMENTS_JSON에 아직 반영되지 않았다면 실행 즉시
    UnknownDriveError가 나기 때문이다. 실행 이력 화면은 5초마다 갱신되므로 짧은
    프로세스 캐시로 describe 호출만 줄이고, 배포 변경은 최대 30초 안에 반영한다.
    """
    common = _common()
    project = str(common.get("GCP_PROJECT_ID") or "")
    region = str(common.get("GCP_REGION") or "asia-northeast3")
    if not project:
        return {}
    cache_key = (project, region)
    now = time.monotonic()
    with _SYNC_TARGET_CACHE_LOCK:
        cached = _SYNC_TARGET_CACHE.get(cache_key)
        if cached and now - float(cached.get("created", 0)) < _SYNC_TARGET_CACHE_TTL_SECONDS:
            return copy.deepcopy(cached["departments"])

    ok, service = _gcloud_json(
        [
            "run",
            "services",
            "describe",
            "rag-sync",
            f"--region={region}",
            f"--project={project}",
        ],
        timeout=30,
    )
    if not ok or not isinstance(service, dict):
        return {}
    raw = _cloud_run_env(service).get("DEPARTMENTS_JSON", "")
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    if not isinstance(parsed, dict):
        return {}

    departments = {
        str(code).strip().lower(): copy.deepcopy(value)
        for code, value in parsed.items()
        if DEPT_CODE_RE.fullmatch(str(code).strip().lower()) and isinstance(value, dict)
    }
    with _SYNC_TARGET_CACHE_LOCK:
        _SYNC_TARGET_CACHE[cache_key] = {
            "created": now,
            "departments": copy.deepcopy(departments),
        }
    return departments


def _sync_department_targets() -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    departments: dict[str, dict[str, Any]] = {}
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

    # 로컬 YAML이 없는 운영 환경에서는 rag-sync에 배포된 맵이 영속 원본이다.
    # 로컬 항목이 있더라도 배포 맵의 라우팅 필드는 실제 실행값으로 덮어쓴다.
    for code, config in _deployed_sync_department_map().items():
        existing = departments.get(code) or {}
        departments[code] = {
            "code": code,
            "name": str(existing.get("name") or config.get("name") or code),
            "driveIds": _normalise_ids(config.get("driveIds")),
            "syncFolderIds": _normalise_ids(config.get("syncFolderIds")),
            "studentFolderIds": _normalise_ids(config.get("studentFolderIds")),
            "corpora": {
                "staff": str(config.get("staffCorpus") or ""),
                "student": str(config.get("studentCorpus") or ""),
            },
            "source": "cloud-run-env",
        }

    drive_owners: dict[str, str] = {}
    for code, target in departments.items():
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
        "syncFolderIds": list((departments.get(code) or {}).get("syncFolderIds") or []),
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
        message = str(rows)[:400]
        normalized = message.lower()
        if (
            "not_found" in normalized
            and SYNC_WORKFLOW_NAME in normalized
            and ("does not exist" in normalized or "not found" in normalized)
        ):
            raise WorkflowNotFoundError(
                f"{SYNC_WORKFLOW_NAME} 워크플로우가 아직 배포되지 않았습니다."
            )
        raise RuntimeError(message[:300] or "동기화 실행 이력을 조회하지 못했습니다.")
    records = [row for row in rows if isinstance(row, dict)]
    # executions list 는 기본 뷰라 완료 실행의 argument/labels/result 를 주지 않는다.
    # 이 상태로 이력 행을 만들면 수동 실행도 "자동 실행"이 되고 totals 는 전부 0,
    # Workflow 가 정상 return 한 ok=false 도 잃어버려 SUCCEEDED=완료로 오표시된다.
    needs_detail = [
        row
        for row in records
        if (
            not row.get("argument")
            or not row.get("labels")
            or (
                str(row.get("state") or "") == "SUCCEEDED"
                and not row.get("result")
            )
            or (
                str(row.get("state") or "") == "FAILED"
                and not row.get("error")
            )
        )
    ]

    def describe(row: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        execution_id = _execution_id(str(row.get("name") or ""))
        detail_ok, detail = _gcloud_json(
            [
                "workflows",
                "executions",
                "describe",
                execution_id,
                f"--workflow={SYNC_WORKFLOW_NAME}",
                f"--location={region}",
                f"--project={project}",
            ],
            timeout=20,
        )
        return execution_id, detail if detail_ok and isinstance(detail, dict) else {}

    if needs_detail:
        with ThreadPoolExecutor(max_workers=min(6, len(needs_detail))) as executor:
            details = dict(executor.map(describe, needs_detail))
        records = [
            {**row, **details.get(_execution_id(str(row.get("name") or "")), {})}
            for row in records
        ]
    return records


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


def _sync_log_stage(text: str) -> str:
    lowered = text.lower()
    for needle, label in (
        ("fetch-changes", "Drive 변경 조회"),
        ("backfill", "전체 적재"),
        ("index-gcs", "RAG 색인"),
        ("reindex-pending", "미색인 복구"),
        ("retry-failed", "실패 문서 복구"),
        ("ingest", "파일 변환·업로드"),
        ("delete", "삭제"),
        ("reconcile", "정합성 검사"),
        ("commit-token", "변경 토큰 저장"),
    ):
        if needle in lowered:
            return label
    return "Workflow"


def _sync_log_file_ids(text: str) -> list[str]:
    found: list[str] = []
    patterns = (
        r"gs://[^/\s\"']+/([A-Za-z0-9_-]{10,})(?:\.[A-Za-z0-9.]+)",
        r"fileId(?:=|\\?\"\s*:\s*\\?\")([A-Za-z0-9_-]{10,})",
    )
    for pattern in patterns:
        for file_id in re.findall(pattern, text):
            if file_id not in found:
                found.append(file_id)
    return found[:50]


def _sync_log_message(text: str) -> str:
    prefix, marker, encoded = text.partition(" {")
    if not marker:
        return text[:1000]
    try:
        payload = json.loads("{" + encoded)
    except json.JSONDecodeError:
        return text[:1000]
    if not isinstance(payload, dict):
        return text[:1000]
    code = payload.get("code")
    body = payload.get("body")
    details = (body.get("detail") or []) if isinstance(body, dict) else []
    messages = list(
        dict.fromkeys(
            str(item.get("msg") or "").strip()
            for item in details
            if isinstance(item, dict) and item.get("msg")
        )
    )
    if isinstance(details, str) and details.strip():
        messages.append(details.strip())
    if isinstance(body, str) and body.strip():
        messages.append(body.strip())
    suffix = " · ".join(messages[:3])
    if code:
        suffix = f"HTTP {code}" + (f" · {suffix}" if suffix else "")
    return (prefix.strip() + (f" · {suffix}" if suffix else ""))[:1000]


def _sync_file_item(text: str, timestamp: str) -> dict[str, Any] | None:
    match = re.fullmatch(r"sync-file-result status=([A-Z_]+) (\{.*\})", text)
    if not match:
        return None
    try:
        payload = json.loads(match.group(2))
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    file_id = str(payload.get("fileId") or "")
    if not file_id:
        return None
    return {
        "eventType": "file",
        "timestamp": timestamp,
        "status": match.group(1),
        "operation": str(payload.get("operation") or "UPDATED"),
        "fileId": file_id,
        "name": str(payload.get("name") or ""),
        "mimeType": str(payload.get("mimeType") or ""),
        "modifiedTime": str(payload.get("modifiedTime") or ""),
        "route": str(payload.get("route") or ""),
    }


def _sync_workflow_logs(
    project: str,
    execution_id: str,
    token: str = "",
    start_time: str = "",
    end_time: str = "",
) -> list[dict[str, Any]]:
    log_filter = (
        'resource.type="workflows.googleapis.com/Workflow" '
        'AND resource.labels.workflow_id="rag-daily-sync" '
        f'AND labels.execution_id="{execution_id}" '
        'AND (severity>=WARNING OR textPayload:"sync-file-result status=")'
    )
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}T[^\s]+Z", start_time):
        log_filter += f' AND timestamp>="{start_time}"'
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}T[^\s]+Z", end_time):
        log_filter += f' AND timestamp<="{end_time}"'
    if token:
        status, body, _ = _http_post_json(
            "https://logging.googleapis.com/v2/entries:list",
            {
                "resourceNames": [f"projects/{project}"],
                "filter": log_filter,
                "orderBy": "timestamp asc",
                "pageSize": 500,
            },
            token,
            timeout=15,
        )
        if status != 200 or not isinstance(body, dict):
            detail = str(((body or {}).get("error") or {}).get("message") or "")
            raise RuntimeError(
                detail[:300] or f"Workflow 로그 조회 실패 (HTTP {status or 'timeout'})"
            )
        rows = body.get("entries") or []
    else:
        ok, rows = _gcloud_json(
            [
                "logging",
                "read",
                log_filter,
                f"--project={project}",
                "--limit=500",
                "--order=asc",
            ],
            timeout=30,
        )
        if not ok or not isinstance(rows, list):
            raise RuntimeError(str(rows)[:300] or "Workflow 로그를 조회하지 못했습니다.")
    logs: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        raw = str(row.get("textPayload") or "").strip()
        if not raw:
            continue
        file_item = _sync_file_item(raw, str(row.get("timestamp") or ""))
        if file_item:
            logs.append(file_item)
            continue
        logs.append(
            {
                "eventType": "log",
                "timestamp": str(row.get("timestamp") or ""),
                "severity": str(row.get("severity") or "WARNING"),
                "stage": _sync_log_stage(raw),
                "message": _sync_log_message(raw),
                "fileIds": _sync_log_file_ids(raw),
                "raw": raw[:4000],
            }
        )
    return logs


def _firestore_document_summaries(
    project: str,
    database: str,
    token: str,
    file_ids: list[str],
) -> dict[str, dict[str, str]]:
    def load(file_id: str) -> tuple[str, dict[str, str]]:
        url = (
            "https://firestore.googleapis.com/v1/projects/"
            f"{quote(project, safe='')}/databases/{quote(database, safe='')}/documents/"
            f"doc_state/{quote(file_id, safe='')}"
        )
        status, body, _ = _http_json(url, token, timeout=10)
        if status != 200 or not isinstance(body, dict):
            return file_id, {}
        fields = body.get("fields") or {}
        decoded = {key: _firestore_value(value) for key, value in fields.items()}
        return file_id, {
            "fileId": file_id,
            "name": str(decoded.get("name") or file_id),
            "path": str(decoded.get("path") or ""),
            "status": str(decoded.get("status") or ""),
        }

    unique_ids = list(dict.fromkeys(file_ids))[:50]
    if not unique_ids:
        return {}
    with ThreadPoolExecutor(max_workers=min(8, len(unique_ids))) as executor:
        return dict(executor.map(load, unique_ids))


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
        if not labels.get("department") and not active_ids:
            raise FileExistsError(
                "대상을 확인할 수 없는 동기화가 이미 실행 중입니다. 완료 후 다시 실행해 주세요."
            )

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
    cfg: dict[str, Any] | None = None
    config_revision: str | None = None
    if path.exists():
        checks = _local_status(code)
        if not any(item["status"] == "FAIL" for item in checks):
            cfg = _read_yaml(path)
            config_revision = _config_revision(path)
    else:
        try:
            cfg, config_revision, complete = cloud_department_status_config(code)
            checks = _cloud_config_status(code, cfg, complete=complete)
        except (FileNotFoundError, OSError, RuntimeError, TypeError, ValueError, yaml.YAMLError) as exc:
            checks = [_check("LOCAL", "cloud-metadata", "FAIL", str(exc))]

    if cfg is None or any(item["status"] == "FAIL" for item in checks):
        for layer in ("RESOURCE", "DEPLOY", "RUNTIME", "SYNC"):
            checks.append(_check(layer, "prerequisite", "SKIP", "설정 검사 실패"))
    elif offline:
        for layer in ("RESOURCE", "DEPLOY", "RUNTIME", "SYNC"):
            checks.append(_check(layer, "offline", "SKIP", "오프라인 검사"))
    else:
        common = _common()
        checks.extend(_resource_status(code, cfg, common, cache))
        deploy_checks = _deploy_and_runtime_status(code, common, cfg, cache)
        checks.extend(deploy_checks)
        # SYNC 는 배포 결과를 보고 원인을 앞 단계로 넘긴다.
        checks.extend(_sync_status(common, cache, deploy_checks=deploy_checks, code=code))
    result = {
        "code": code,
        "overall": _overall(checks),
        "checkedAt": datetime.now().astimezone().isoformat(timespec="seconds"),
        "configRevision": config_revision,
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
def session() -> dict[str, Any]:
    """부팅 첫 호출. gcloud 를 타지 않아야 한다 — 여기서 막히면 화면이 통째로 늦다.

    `commonExists` 는 파일 존재 확인뿐이므로 즉시 답한다. 콘솔은 이 값만 보고
    공통 설정 화면을 먼저 띄우고, 느린 gcloud 상태는 뒤에서 채운다.
    """
    return {"nonce": _SESSION_NONCE, "commonExists": (CONFIG_DIR / "common.yaml").exists()}


@app.get("/api/v1/departments")
def departments() -> dict[str, Any]:
    return {"departments": list_department_records()}


@app.get("/api/v1/cloud-mcp-services")
def cloud_mcp_services() -> JSONResponse:
    """로컬 YAML과 무관하게 Cloud Run에 배포된 관리 대상 MCP를 조회한다."""
    try:
        return JSONResponse({"departments": cloud_mcp_department_records()})
    except (FileNotFoundError, OSError, RuntimeError, TypeError, yaml.YAMLError) as exc:
        return JSONResponse(
            {"error": {"code": "CLOUD_MCP_LOOKUP_FAILED", "message": str(exc)[:400]}},
            status_code=503,
        )


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


@app.get("/api/v1/common-runtime-deployments")
def common_runtime_deployments(status: str = "") -> JSONResponse:
    with _COMMON_RUNTIME_DEPLOY_LOCK:
        _cleanup_common_runtime_deployments()
        rows = [
            copy.deepcopy(run)
            for run in _COMMON_RUNTIME_DEPLOY_RUNS.values()
            if not status or run["status"] == status.upper()
        ]
    rows.sort(key=lambda item: item.get("createdEpoch", 0), reverse=True)
    return JSONResponse({"runs": rows})


@app.get("/api/v1/common-runtime-deployments/{run_id}")
def common_runtime_deployment(run_id: str) -> JSONResponse:
    with _COMMON_RUNTIME_DEPLOY_LOCK:
        _cleanup_common_runtime_deployments()
        run = _COMMON_RUNTIME_DEPLOY_RUNS.get(run_id)
        if not run:
            return JSONResponse(
                {
                    "error": {
                        "code": "COMMON_RUNTIME_DEPLOYMENT_NOT_FOUND",
                        "message": "공통 런타임 배포 작업을 찾을 수 없습니다.",
                    }
                },
                status_code=404,
            )
        return JSONResponse(copy.deepcopy(run))


@app.get("/api/v1/runtime-env")
def runtime_env() -> JSONResponse:
    """배포된 parser/sync env 가 현재 학과 설정과 같은지."""
    try:
        return JSONResponse(runtime_env_drift())
    except (OSError, RuntimeError, TypeError, ValueError, yaml.YAMLError) as exc:
        return JSONResponse(
            {"status": "UNKNOWN", "reason": str(exc)[:300], "services": []},
        )


@app.post("/api/v1/common-runtime-deployments")
def create_common_runtime_deployment(
    request: Request, followUpDepartmentCode: str = "", envOnly: bool = False
) -> JSONResponse:
    _require_local_session(request)
    try:
        run = start_common_runtime_deployment(followUpDepartmentCode, env_only=envOnly)
    except FileNotFoundError:
        return JSONResponse(
            {"error": {"code": "NOT_FOUND", "message": "후속 MCP 배포 대상 학과를 찾을 수 없습니다."}},
            status_code=404,
        )
    except FileExistsError as exc:
        return JSONResponse(
            {
                "error": {
                    "code": "COMMON_RUNTIME_DEPLOYMENT_RUNNING",
                    "message": "공통 런타임 배포가 이미 진행 중입니다.",
                    "runId": str(exc),
                }
            },
            status_code=409,
        )
    except (OSError, RuntimeError, SystemExit, TypeError, ValueError, yaml.YAMLError) as exc:
        return JSONResponse(
            {"error": {"code": "COMMON_RUNTIME_DEPLOYMENT_INVALID", "message": str(exc)[:400]}},
            status_code=422,
        )
    return JSONResponse(run, status_code=202)


@app.get("/api/v1/departments/{code}/teardown-plan")
def department_teardown_plan_endpoint(code: str) -> JSONResponse:
    try:
        return JSONResponse(department_teardown_plan(code))
    except FileNotFoundError:
        return JSONResponse(
            {"error": {"code": "NOT_FOUND", "message": "학과를 찾을 수 없습니다."}},
            status_code=404,
        )
    except (OSError, RuntimeError, TypeError, ValueError, yaml.YAMLError) as exc:
        return JSONResponse(
            {"error": {"code": "TEARDOWN_PLAN_FAILED", "message": str(exc)[:400]}},
            status_code=422,
        )


@app.get("/api/v1/common-runtime/teardown-plan")
def common_runtime_teardown_plan_endpoint() -> JSONResponse:
    try:
        return JSONResponse(common_runtime_teardown_plan())
    except (OSError, RuntimeError, TypeError, ValueError, yaml.YAMLError) as exc:
        return JSONResponse(
            {"error": {"code": "TEARDOWN_PLAN_FAILED", "message": str(exc)[:400]}},
            status_code=422,
        )


def _teardown_response(plan: dict[str, Any], confirm: str) -> JSONResponse:
    try:
        run = start_teardown_run(plan, confirm)
    except PermissionError as exc:
        return JSONResponse(
            {
                "error": {
                    "code": "TEARDOWN_CONFIRM_MISMATCH",
                    "message": f"확인 문구가 다릅니다. '{exc}' 를 그대로 입력해 주세요.",
                }
            },
            status_code=422,
        )
    except FileExistsError as exc:
        return JSONResponse(
            {
                "error": {
                    "code": "TEARDOWN_RUNNING",
                    "message": "이미 진행 중인 삭제 작업이 있습니다.",
                    "runId": str(exc),
                }
            },
            status_code=409,
        )
    return JSONResponse(run, status_code=202)


@app.post("/api/v1/departments/{code}/teardown")
async def create_department_teardown(code: str, request: Request) -> JSONResponse:
    _require_local_session(request)
    payload = await request.json()
    if not isinstance(payload, dict):
        return JSONResponse(
            {"error": {"code": "INVALID_PAYLOAD", "message": "본문이 올바르지 않습니다."}},
            status_code=400,
        )
    try:
        plan = department_teardown_plan(code)
    except FileNotFoundError:
        return JSONResponse(
            {"error": {"code": "NOT_FOUND", "message": "학과를 찾을 수 없습니다."}},
            status_code=404,
        )
    except (OSError, RuntimeError, TypeError, ValueError, yaml.YAMLError) as exc:
        return JSONResponse(
            {"error": {"code": "TEARDOWN_PLAN_FAILED", "message": str(exc)[:400]}},
            status_code=422,
        )
    return _teardown_response(plan, str(payload.get("confirm") or ""))


@app.post("/api/v1/common-runtime/teardown")
async def create_common_runtime_teardown(request: Request) -> JSONResponse:
    _require_local_session(request)
    payload = await request.json()
    if not isinstance(payload, dict):
        return JSONResponse(
            {"error": {"code": "INVALID_PAYLOAD", "message": "본문이 올바르지 않습니다."}},
            status_code=400,
        )
    try:
        plan = common_runtime_teardown_plan()
    except (OSError, RuntimeError, TypeError, ValueError, yaml.YAMLError) as exc:
        return JSONResponse(
            {"error": {"code": "TEARDOWN_PLAN_FAILED", "message": str(exc)[:400]}},
            status_code=422,
        )
    return _teardown_response(plan, str(payload.get("confirm") or ""))


@app.get("/api/v1/teardowns/{run_id}")
def teardown_run(run_id: str) -> JSONResponse:
    with _TEARDOWN_LOCK:
        _cleanup_teardown_runs()
        run = _TEARDOWN_RUNS.get(run_id)
        if not run:
            return JSONResponse(
                {"error": {"code": "NOT_FOUND", "message": "삭제 작업을 찾을 수 없습니다."}},
                status_code=404,
            )
        return JSONResponse(copy.deepcopy(run))


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
    configured_project = str(common.get("GCP_PROJECT_ID") or "")
    # bootstrap 과 SA 조회는 서로 독립이다. 순차로 돌면 gcloud 왕복이 그대로 더해진다.
    with ThreadPoolExecutor(max_workers=2) as pool:
        bootstrap_future = pool.submit(
            _gcloud_bootstrap_state, include_projects=not common_exists
        )
        sa_future = (
            pool.submit(_default_compute_service_account, configured_project)
            if configured_project
            else None
        )
        bootstrap = bootstrap_future.result()
        service_account = (
            sa_future.result() if sa_future and bootstrap["authenticated"] else ""
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
    if not _project_accessible(project):
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
    if not _project_accessible(str(payload.get("projectId") or "")):
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


@app.get("/api/v1/projects/search")
def project_search(q: str = "", limit: int = PROJECT_SEARCH_LIMIT) -> JSONResponse:
    """프로젝트 부분 일치 검색. 전량을 받지 않으므로 타이핑 중에도 쓸 수 있다."""
    return JSONResponse(search_projects(q, limit))


@app.get("/api/v1/drive-service-account/status")
def drive_sa_status() -> JSONResponse:
    """Drive 확인 SA 를 실제 가장 토큰까지 받아보고 판정한다."""
    try:
        project = str(_common().get("GCP_PROJECT_ID") or "")
    except (OSError, TypeError, UnicodeError, yaml.YAMLError):
        project = ""
    return JSONResponse(drive_service_account_status(project))


@app.post("/api/v1/drive-service-account/repair-plans")
def drive_sa_repair_plan(request: Request) -> JSONResponse:
    """무엇을 켜고 어떤 권한을 줄지만 돌려준다. 여기서는 아무것도 바꾸지 않는다."""
    _require_local_session(request)
    try:
        project = str(_common().get("GCP_PROJECT_ID") or "")
    except (OSError, TypeError, UnicodeError, yaml.YAMLError):
        project = ""
    try:
        plan = create_sa_repair_plan(project)
    except FileExistsError as exc:
        return JSONResponse(
            {"error": {"code": "SA_ALREADY_OK", "message": str(exc)}}, status_code=409
        )
    except (TypeError, ValueError) as exc:
        return JSONResponse(
            {"error": {"code": "SA_REPAIR_UNAVAILABLE", "message": str(exc)}}, status_code=422
        )
    return JSONResponse(plan, status_code=200)


@app.post("/api/v1/drive-service-account/repairs")
async def drive_sa_repair(request: Request) -> JSONResponse:
    _require_local_session(request)
    payload = await request.json()
    plan_id = str(payload.get("planId") or "") if isinstance(payload, dict) else ""
    if not re.fullmatch(r"[0-9a-f]{32}", plan_id):
        return JSONResponse(
            {"error": {"code": "INVALID_REPAIR_PLAN", "message": "조치 계획을 다시 열어 주세요."}},
            status_code=422,
        )
    try:
        run = start_sa_repair_run(plan_id)
    except FileNotFoundError:
        return JSONResponse(
            {
                "error": {
                    "code": "REPAIR_PLAN_EXPIRED",
                    "message": "조치 계획이 만료되었습니다. 상태를 다시 확인해 주세요.",
                }
            },
            status_code=404,
        )
    except FileExistsError as exc:
        return JSONResponse(
            {"error": {"code": "SA_REPAIR_CONFLICT", "message": str(exc)}}, status_code=409
        )
    return JSONResponse(run, status_code=202)


@app.get("/api/v1/drive-service-account/repairs/{run_id}")
def drive_sa_repair_run(run_id: str) -> JSONResponse:
    with _SA_REPAIR_LOCK:
        _cleanup_sa_repair_state()
        run = _SA_REPAIR_RUNS.get(run_id)
        if not run:
            return JSONResponse(
                {
                    "error": {
                        "code": "REPAIR_RUN_NOT_FOUND",
                        "message": "조치 실행을 찾을 수 없습니다.",
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
    if not _project_accessible(candidate["GCP_PROJECT_ID"]):
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


@app.post("/api/v1/departments/drive-folders")
async def drive_folder_children(request: Request) -> JSONResponse:
    """공유드라이브 폴더 탐색기의 한 단계 자식을 Compute SA 권한으로 조회한다."""
    _require_local_session(request)
    payload = await request.json()
    drive_id = str(payload.get("driveId") or "").strip() if isinstance(payload, dict) else ""
    parent_id = str(payload.get("parentId") or drive_id).strip() if isinstance(payload, dict) else ""
    if not DRIVE_FILE_ID_RE.fullmatch(drive_id) or not DRIVE_FILE_ID_RE.fullmatch(parent_id):
        return JSONResponse(
            {"error": {"code": "INVALID_DRIVE_FOLDER", "message": "공유드라이브 또는 폴더 ID 형식을 확인해 주세요."}},
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
            {"error": {"code": "DRIVE_FOLDER_BROWSE_FAILED", "message": f"{error}{suffix}"}},
            status_code=503,
        )
    try:
        result = _drive_folder_children(drive_id, parent_id, token)
    except RuntimeError as exc:
        return JSONResponse(
            {"error": {"code": "DRIVE_FOLDER_BROWSE_FAILED", "message": str(exc)}},
            status_code=503,
        )
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
        return JSONResponse(department_public_config_any(code))
    except (FileNotFoundError, OSError, TypeError, UnicodeError, ValueError, yaml.YAMLError) as exc:
        return JSONResponse(
            {"error": {"code": "NOT_FOUND", "message": str(exc) or "학과 설정을 찾을 수 없습니다."}},
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
        current = department_public_config_any(code)
    except (FileNotFoundError, OSError, TypeError, UnicodeError, ValueError, yaml.YAMLError):
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
        current = department_public_config_any(code)
    except (FileNotFoundError, OSError, TypeError, UnicodeError, ValueError, yaml.YAMLError):
        return JSONResponse(
            {"error": {"code": "NOT_FOUND", "message": "학과 설정을 찾을 수 없습니다."}},
            status_code=404,
        )
    if payload.get("configRevision") != current["configRevision"]:
        return JSONResponse(
            {
                "error": {
                    "code": "REVISION_CONFLICT",
                    "message": "설정이 다른 곳에서 변경되었습니다. 다시 열어 주세요.",
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
    if current.get("source") == "cloud":
        try:
            existing, cloud_revision = cloud_department_config(code)
            if cloud_revision != current["configRevision"]:
                raise RuntimeError("Cloud 설정이 변경되었습니다. 다시 열어 주세요.")
            merged = copy.deepcopy(existing)
            for key in ("name", "corpora", "buckets", "drive", "minInstances"):
                merged[key] = copy.deepcopy(candidate[key])
            run = start_mcp_deployment(code, cloud_config=merged)
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
        except (OSError, RuntimeError, TypeError, ValueError, yaml.YAMLError) as exc:
            return JSONResponse(
                {"error": {"code": "CLOUD_UPDATE_FAILED", "message": str(exc)[:400]}},
                status_code=422,
            )
        return JSONResponse(
            {
                "code": code,
                "path": "Cloud Run management metadata",
                "updated": True,
                "configRevision": _mapping_revision(merged),
                "deployment": run,
            },
            status_code=202,
        )
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
    except WorkflowNotFoundError as exc:
        departments, _drive_owners = _sync_department_targets()
        return JSONResponse(
            {
                "runs": [],
                "departments": list(departments.values()),
                "workflow": SYNC_WORKFLOW_NAME,
                "workflowStatus": "NOT_FOUND",
                "message": str(exc),
            }
        )
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
            "workflowStatus": "READY",
        }
    )


@app.get("/api/v1/sync-runs/{execution_id}")
def sync_run_detail(execution_id: str) -> JSONResponse:
    if not re.fullmatch(
        r"[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}", execution_id
    ):
        raise HTTPException(status_code=404, detail="sync execution not found")
    try:
        common = _common()
        project = str(common.get("GCP_PROJECT_ID") or "")
        region = str(common.get("GCP_REGION") or "asia-northeast3")
        database = str(common.get("FIRESTORE_DATABASE") or "rag-sync-state")
        ok, row = _gcloud_json(
            [
                "workflows",
                "executions",
                "describe",
                execution_id,
                f"--workflow={SYNC_WORKFLOW_NAME}",
                f"--location={region}",
                f"--project={project}",
            ],
            timeout=20,
        )
        if not ok or not isinstance(row, dict):
            raise FileNotFoundError(execution_id)
        departments, drive_owners = _sync_department_targets()
        token = _sync_access_token()
        run_id = _execution_run_id(row)
        progress = (
            _firestore_sync_progress(project, database, token, run_id)
            if run_id
            else {}
        )
        run = _sync_execution_record(row, departments, drive_owners, progress)
        log_lookup_error = ""
        try:
            events = _sync_workflow_logs(
                project,
                execution_id,
                token,
                str(row.get("startTime") or ""),
                str(row.get("endTime") or ""),
            )
        except RuntimeError as exc:
            events = []
            log_lookup_error = str(exc)[:300]
        logs = [item for item in events if item.get("eventType") != "file"]
        items = [item for item in events if item.get("eventType") == "file"]
        file_ids = [file_id for item in logs for file_id in item["fileIds"]]
        file_ids.extend(item["fileId"] for item in items)
        files = _firestore_document_summaries(project, database, token, file_ids)
        for item in logs:
            item["files"] = [
                files.get(file_id) or {"fileId": file_id, "name": file_id, "path": "", "status": ""}
                for file_id in item.pop("fileIds")
            ]
        for item in items:
            file = files.get(item["fileId"]) or {}
            item["name"] = item["name"] or str(file.get("name") or item["fileId"])
            item["path"] = str(file.get("path") or "")
            item["documentStatus"] = str(file.get("status") or "")
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="sync execution not found") from None
    except (OSError, RuntimeError, TypeError, UnicodeError, yaml.YAMLError) as exc:
        return JSONResponse(
            {"error": {"code": "SYNC_LOG_LOOKUP_FAILED", "message": str(exc)[:400]}},
            status_code=503,
        )
    return JSONResponse(
        {
            "run": run,
            "logs": logs,
            "items": items,
            "files": list(files.values()),
            "logLookupError": log_lookup_error,
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
    except WorkflowNotFoundError as exc:
        return JSONResponse(
            {"error": {"code": "SYNC_WORKFLOW_NOT_FOUND", "message": str(exc)}},
            status_code=412,
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
    local_codes = set(dept_config.list_departments())
    if requested:
        codes = sorted({str(item).strip().lower() for item in requested})
    else:
        cloud_codes = {str(item.get("code") or "") for item in cloud_mcp_department_records()}
        codes = sorted(local_codes | cloud_codes)
    if not codes or any(not DEPT_CODE_RE.fullmatch(code) for code in codes):
        raise HTTPException(status_code=404, detail="department not found")
    for code in codes:
        if code in local_codes:
            continue
        try:
            cloud_department_status_config(code)
        except (FileNotFoundError, OSError, RuntimeError, TypeError, ValueError, yaml.YAMLError):
            raise HTTPException(status_code=404, detail="department not found") from None
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
