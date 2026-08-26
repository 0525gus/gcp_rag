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
from urllib.parse import urlparse

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
_AUTH_PROCESS: subprocess.Popen[Any] | None = None


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


def _has_secret_input(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    for key, child in value.items():
        if key in SECRET_FIELDS or "token" in key.lower() or "secret" in key.lower():
            return True
        if isinstance(child, dict) and _has_secret_input(child):
            return True
    return False


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
    target = DEPT_DIR / f"{code}.yaml"
    if check_existing and code and target.exists():
        _field_error(errors, "code", "이미 같은 학과 YAML이 있습니다.")

    corpora = payload.get("corpora") if isinstance(payload.get("corpora"), dict) else {}
    staff_corpus = str(corpora.get("staff") or "").strip()
    student_corpus = str(corpora.get("student") or "").strip()
    project = str(common.get("GCP_PROJECT_ID") or "").strip()
    region = str(common.get("GCP_REGION") or "asia-northeast3").strip()
    for audience, value in (("staff", staff_corpus), ("student", student_corpus)):
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

    drive = payload.get("drive") if isinstance(payload.get("drive"), dict) else {}
    drive_ids = _normalise_ids(drive.get("driveIds"))
    sync_ids = _normalise_ids(drive.get("syncFolderIds"))
    student_ids = _normalise_ids(drive.get("studentFolderIds"))
    if not drive_ids:
        _field_error(errors, "drive.driveIds", "공유드라이브 ID가 하나 이상 필요합니다.")
    if not sync_ids:
        _field_error(errors, "drive.syncFolderIds", "동기화 폴더가 하나 이상 필요합니다.")
    if not student_ids:
        _field_error(errors, "drive.studentFolderIds", "학생 폴더가 하나 이상 필요합니다.")
    outside = [item for item in student_ids if item not in sync_ids]
    if outside:
        _field_error(
            errors,
            "drive.studentFolderIds",
            "동기화 폴더에 포함되지 않은 ID입니다: " + ", ".join(outside),
        )

    for other in dept_config.list_departments():
        if other == code:
            continue
        try:
            other_cfg = _read_yaml(DEPT_DIR / f"{other}.yaml")
        except (OSError, ValueError, yaml.YAMLError):
            continue
        other_ids = _normalise_ids((other_cfg.get("drive") or {}).get("driveIds"))
        duplicate = sorted(set(drive_ids) & set(other_ids))
        if duplicate:
            _field_error(
                errors,
                "drive.driveIds",
                f"{other} 학과와 중복된 공유드라이브 ID입니다: {', '.join(duplicate)}",
            )

    mins = payload.get("minInstances") if isinstance(payload.get("minInstances"), dict) else {}
    min_instances: dict[str, int] = {}
    for audience in dept_config.AUDIENCES:
        raw = mins.get(audience, 0)
        try:
            parsed = int(raw)
            if parsed < 0:
                raise ValueError
            min_instances[audience] = parsed
        except (TypeError, ValueError):
            _field_error(errors, f"minInstances.{audience}", "0 이상의 정수여야 합니다.")

    candidate = {
        "name": name,
        "corpora": {"staff": staff_corpus, "student": student_corpus},
        "buckets": {"hwpOriginal": hwp_bucket, "source": source_bucket},
        "drive": {
            "driveIds": drive_ids,
            "syncFolderIds": sync_ids,
            "studentFolderIds": student_ids,
        },
        "minInstances": min_instances,
    }
    return candidate, {"valid": not errors, "fieldErrors": errors, "warnings": warnings}


def _render_yaml(
    candidate: dict[str, Any], *, preview: bool, keys: dict[str, str] | None = None
) -> str:
    body = dict(candidate)
    body["keys"] = keys or {
        "staff": "<자동 생성>" if preview else secrets.token_urlsafe(32),
        "student": "<자동 생성>" if preview else secrets.token_urlsafe(32),
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


def create_department(code: str, candidate: dict[str, Any]) -> Path:
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
            dept_config.build_env(code, "staff")
            dept_config.build_env(code, "student")
            dept_config.build_departments_map()
        except SystemExit as exc:
            target.unlink(missing_ok=True)
            raise RuntimeError(str(exc)) from exc
        except (OSError, TypeError, ValueError, yaml.YAMLError):
            target.unlink(missing_ok=True)
            raise
        return target
    finally:
        temp.unlink(missing_ok=True)


def update_department(code: str, candidate: dict[str, Any]) -> Path:
    target = (DEPT_DIR / f"{code}.yaml").resolve()
    if target.parent != DEPT_DIR.resolve() or not target.exists():
        raise FileNotFoundError(target)
    original = target.read_bytes()
    existing = _read_yaml(target)
    keys = existing.get("keys") if isinstance(existing.get("keys"), dict) else {}
    preserved_keys = {
        audience: str(keys.get(audience) or "") for audience in dept_config.AUDIENCES
    }
    if any(not value for value in preserved_keys.values()):
        raise RuntimeError("기존 MCP 키를 읽지 못해 수정을 중단했습니다.")

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
            dept_config.build_env(code, "staff")
            dept_config.build_env(code, "student")
            dept_config.build_departments_map()
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
        "configRevision": _config_revision(path),
    }


def list_department_records() -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    if not DEPT_DIR.exists():
        return records
    for path in sorted(DEPT_DIR.glob("*.yaml")):
        code = path.stem
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
                "lastResult": None if stale else latest,
            }
        )
    return records


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

    try:
        dept_config.build_env(code, "staff")
        dept_config.build_env(code, "student")
        checks.append(_check("LOCAL", "derived-env", "OK", "staff · student 설정 생성 가능"))
    except SystemExit as exc:
        checks.append(_check("LOCAL", "derived-env", "FAIL", str(exc)))

    drive = data.get("drive") or {}
    sync_ids = set(_normalise_ids(drive.get("syncFolderIds")))
    student_ids = set(_normalise_ids(drive.get("studentFolderIds")))
    if not student_ids - sync_ids and student_ids:
        checks.append(_check("LOCAL", "folder-scope", "OK", "student ⊆ sync"))
    else:
        missing = sorted(student_ids - sync_ids)
        checks.append(
            _check("LOCAL", "folder-scope", "FAIL", "부분집합 위반: " + ", ".join(missing))
        )

    keys = data.get("keys") or {}
    weak = [aud for aud in dept_config.AUDIENCES if len(str(keys.get(aud) or "")) < 24]
    if weak:
        checks.append(_check("LOCAL", "mcp-keys", "WARN", "24자 미만: " + ", ".join(weak)))
    else:
        checks.append(_check("LOCAL", "mcp-keys", "OK", "서로 다른 키 · 길이 기준 충족"))
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
        return exc.code, {}, round((time.perf_counter() - started) * 1000)
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
        return exc.code, {}, round((time.perf_counter() - started) * 1000)
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


def _merge_live_resource_validation(candidate: dict[str, Any], result: dict[str, Any]) -> None:
    options = _department_resource_options(_common())
    corpus_names = {item["name"] for item in options["corpora"]}
    bucket_names = {item["name"] for item in options["buckets"]}
    error = str(options.get("error") or "")
    for audience in dept_config.AUDIENCES:
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
    project_ok, project_info = gcloud_json(["projects", "describe", project])
    project_number = str(project_info.get("projectNumber") or "") if project_ok else ""
    if not project_number:
        return _check(
            "RESOURCE", "drive-service-account", "WARN", "프로젝트 번호를 확인하지 못했습니다."
        )

    service_account = f"{project_number}-compute@developer.gserviceaccount.com"
    token_url = (
        "https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/"
        f"{service_account}:generateAccessToken"
    )
    token_status, token_body, token_latency = _http_post_json(
        token_url,
        {
            "scope": ["https://www.googleapis.com/auth/drive.readonly"],
            "lifetime": "300s",
        },
        caller_token,
    )
    impersonated_token = str(token_body.get("accessToken") or "")
    if token_status != 200 or not impersonated_token:
        return _check(
            "RESOURCE",
            "drive-service-account",
            "WARN",
            f"SA 자동 확인 불가 · 가장 토큰 HTTP {token_status or 'timeout'}",
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
    for audience in dept_config.AUDIENCES:
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
    services = ["rag-parser", "rag-sync", f"rag-mcp-{code}-staff", f"rag-mcp-{code}-student"]
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
        checks.append(_check("DEPLOY", label, status, detail))
        url = str(status_data.get("url") or "")
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
        checks.extend(_deploy_and_runtime_status(code, common, cache))
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
    return JSONResponse(
        {
            "status": result["status"],
            "detail": result["detail"],
            "action": result.get("action", ""),
            "latencyMs": result.get("latencyMs", 0),
            "driveIds": drive_ids,
        }
    )


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
    code = str(payload["code"]).strip().lower()
    try:
        target = create_department(code, candidate)
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
        department_public_config(code)
    except (FileNotFoundError, OSError, TypeError, UnicodeError, yaml.YAMLError):
        return JSONResponse(
            {"error": {"code": "NOT_FOUND", "message": "학과 설정을 찾을 수 없습니다."}},
            status_code=404,
        )
    candidate, result = validate_candidate(payload, check_existing=False)
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
    try:
        target = update_department(code, candidate)
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
