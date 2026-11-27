from __future__ import annotations

import copy
import json
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from urllib.parse import quote

import yaml
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from scripts import gcp_cli, index_issues
from scripts.console import cloud_registry, core, drive_lookup

router = APIRouter()


_SYNC_TARGET_CACHE_LOCK = threading.Lock()


_SYNC_TARGET_CACHE: dict[tuple[str, str], dict[str, Any]] = {}


_SYNC_TARGET_CACHE_TTL_SECONDS = 30


SYNC_WORKFLOW_NAME = "rag-daily-sync"


SYNC_RUN_HISTORY_LIMIT = 30


SYNC_TOKEN_COLLECTION = "sync_tokens"


class WorkflowNotFoundError(RuntimeError):
    pass


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
    status, body, _ = gcp_cli.http_json(url, token, timeout=10)
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
    common = core._common()
    project = str(common.get("GCP_PROJECT_ID") or "")
    region = str(common["GCP_REGION"])
    if not project:
        return {}
    cache_key = (project, region)
    now = time.monotonic()
    with _SYNC_TARGET_CACHE_LOCK:
        cached = _SYNC_TARGET_CACHE.get(cache_key)
        if cached and now - float(cached.get("created", 0)) < _SYNC_TARGET_CACHE_TTL_SECONDS:
            return copy.deepcopy(cached["departments"])

    ok, service = gcp_cli.gcloud_json(
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
    raw = cloud_registry._cloud_run_env(service).get("DEPARTMENTS_JSON", "")
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    if not isinstance(parsed, dict):
        return {}

    departments = {
        str(code).strip().lower(): copy.deepcopy(value)
        for code, value in parsed.items()
        if core.DEPT_CODE_RE.fullmatch(str(code).strip().lower()) and isinstance(value, dict)
    }
    with _SYNC_TARGET_CACHE_LOCK:
        _SYNC_TARGET_CACHE[cache_key] = {
            "created": now,
            "departments": copy.deepcopy(departments),
        }
    return departments


def _sync_department_targets() -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    departments: dict[str, dict[str, Any]] = {}
    configs = cloud_registry._cloud_sync_department_configs()
    for code, config in configs.items():
        drive = config.get("drive") or {}
        target = {
            "code": code,
            "name": str(config.get("name") or code),
            "driveIds": core._normalise_ids(drive.get("driveIds")),
            "syncFolderIds": core._normalise_ids(drive.get("syncFolderIds")),
            "studentFolderIds": core._normalise_ids(drive.get("studentFolderIds")),
            "corpora": config.get("corpora") or {},
        }
        departments[code] = target

    for code, config in _deployed_sync_department_map().items():
        if code not in configs:
            continue
        existing = departments.get(code) or {}
        departments[code] = {
            "code": code,
            "name": str(existing.get("name") or config.get("name") or code),
            "driveIds": core._normalise_ids(config.get("driveIds")),
            "syncFolderIds": core._normalise_ids(config.get("syncFolderIds")),
            "studentFolderIds": core._normalise_ids(config.get("studentFolderIds")),
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
    drive_ids = core._normalise_ids(arguments.get("driveIds"))
    code = str(labels.get("department") or arguments.get("departmentCode") or "")
    if not code:
        owners = {drive_owners[item] for item in drive_ids if item in drive_owners}
        if len(owners) == 1:
            code = owners.pop()
    mode = str(labels.get("mode") or "")
    if mode not in {"backfill", "delta", "reprocess"}:
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
        ok, body = gcp_cli.gcloud_json(
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
    ok, rows = gcp_cli.gcloud_json(
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
        detail_ok, detail = gcp_cli.gcloud_json(
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
    database = str(common["FIRESTORE_DATABASE"])
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
        "createdTime": str(payload.get("createdTime") or ""),
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
        status, body, _ = gcp_cli.http_post_json(
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
        ok, rows = gcp_cli.gcloud_json(
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
        status, body, _ = gcp_cli.http_json(url, token, timeout=10)
        if status != 200 or not isinstance(body, dict):
            return file_id, {}
        fields = body.get("fields") or {}
        decoded = {key: _firestore_value(value) for key, value in fields.items()}
        return file_id, {
            "fileId": file_id,
            "driveId": str(decoded.get("driveId") or ""),
            "name": str(decoded.get("name") or file_id),
            "path": str(decoded.get("path") or ""),
            "status": str(decoded.get("status") or ""),
        }

    unique_ids = list(dict.fromkeys(file_ids))[:50]
    if not unique_ids:
        return {}
    with ThreadPoolExecutor(max_workers=min(8, len(unique_ids))) as executor:
        return dict(executor.map(load, unique_ids))


def _start_manual_sync(code: str, mode: str, *, file_id: str = "", request_id: str = "") -> dict[str, Any]:
    departments, _drive_owners = _sync_department_targets()
    target = departments.get(code)
    if not target:
        raise FileNotFoundError(code)
    if mode not in {"delta", "backfill", "reprocess"}:
        raise ValueError("동기화 방식은 delta 또는 backfill이어야 합니다.")
    drive_ids = target["driveIds"]
    if not drive_ids:
        raise ValueError("선택한 학과에 공유드라이브 ID가 없습니다.")

    common = core._common()
    project = str(common.get("GCP_PROJECT_ID") or "")
    region = str(common["GCP_REGION"])
    selected = None
    if mode == "reprocess":
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", file_id) or not re.fullmatch(r"[a-f0-9]{32}", request_id):
            raise ValueError("문서 또는 재처리 요청 ID가 올바르지 않습니다.")
        names = core._firestore_collection_names()
        snap = core._firestore_client(project).collection(names["docState"]).document(file_id).get(timeout=20)
        doc = snap.to_dict() or {}
        if not snap.exists or doc.get("driveId") not in drive_ids:
            raise FileNotFoundError(file_id)
        if not index_issues.issue_item(file_id, doc):
            raise ValueError("이미 처리됐거나 수집 대상에서 제외된 문서입니다. 목록을 새로고침해 주세요.")
        selected = {"fileId": file_id, "driveId": doc["driveId"]}
        ok, deployed = gcp_cli.gcloud_json([
            "workflows", "describe", SYNC_WORKFLOW_NAME,
            f"--location={region}", f"--project={project}",
        ], timeout=20)
        if not ok or "reprocess_document:" not in str(deployed.get("sourceContents") or ""):
            raise ValueError("선택 문서 재처리를 지원하는 Sync·Workflow 배포가 필요합니다. 새 Sync 이미지와 workflows/daily_sync.yaml을 배포한 뒤 다시 시도해 주세요.")
    rows = _list_sync_execution_rows(project, region, limit=20)
    requested = set(drive_ids)
    for row in rows:
        if selected and (row.get("labels") or {}).get("run_id") == request_id:
            args = _execution_arguments(row)
            if args.get("reprocessFile") == selected and args.get("departmentCode") == code:
                return _sync_execution_record(row, departments, _drive_owners)
            raise ValueError("이미 사용된 요청 ID입니다.")
        if str(row.get("state") or "") != "ACTIVE":
            continue
        labels = row.get("labels") if isinstance(row.get("labels"), dict) else {}
        if str(labels.get("department") or "") == code:
            raise FileExistsError("선택한 학과의 Drive에서 이미 동기화가 실행 중입니다.")
        active_ids = set(core._normalise_ids(_execution_arguments(row).get("driveIds")))
        if requested & active_ids:
            raise FileExistsError("선택한 학과의 Drive에서 이미 동기화가 실행 중입니다.")
        if not labels.get("department") and not active_ids:
            raise FileExistsError(
                "대상을 확인할 수 없는 동기화가 이미 실행 중입니다. 완료 후 다시 실행해 주세요."
            )

    token = gcp_cli.access_token()
    drive_check = drive_lookup._drive_service_account_status(
        {"drive": {"driveIds": drive_ids}}, project, token
    )
    if drive_check.get("status") == "FAIL":
        detail = str(drive_check.get("detail") or "Drive 서비스 계정 연결 실패")
        action = str(drive_check.get("action") or "")
        raise RuntimeError(f"{detail} · {action}" if action else detail)
    sync_url, parser_url = _cloud_run_sync_urls(project, region)
    run_id = request_id if selected else uuid.uuid4().hex
    arguments = {
        "syncUrl": sync_url,
        "parserUrl": parser_url,
        "driveIds": drive_ids,
        "backfill": mode == "backfill",
        "runId": run_id,
        "departmentCode": code,
    }
    if selected:
        arguments["reprocessFile"] = selected
    execution_url = (
        "https://workflowexecutions.googleapis.com/v1/projects/"
        f"{quote(project, safe='')}/locations/{quote(region, safe='')}/workflows/"
        f"{quote(SYNC_WORKFLOW_NAME, safe='')}/executions"
    )
    status, body, _ = gcp_cli.http_post_json(
        execution_url,
        {
            "argument": json.dumps(arguments, ensure_ascii=False, separators=(",", ":")),
            "labels": {"department": code, "mode": mode, "run_id": run_id},
        },
        token,
        timeout=20,
    )
    if status not in {200, 201} or not isinstance(body, dict):
        raise RuntimeError(f"Workflow 실행 접수 여부를 확인하지 못했습니다 (HTTP {status or 'timeout'}). 실행 이력을 확인한 뒤 재시도해 주세요.")
    created = dict(body)
    created.setdefault("argument", json.dumps(arguments, ensure_ascii=False))
    created.setdefault("labels", {"department": code, "mode": mode, "run_id": run_id})
    return _sync_execution_record(created, departments, {item: code for item in drive_ids})


@router.get("/api/v1/sync-runs")
def sync_runs(limit: int = 20) -> JSONResponse:
    try:
        common = core._common()
        project = str(common.get("GCP_PROJECT_ID") or "")
        region = str(common["GCP_REGION"])
        rows = _list_sync_execution_rows(
            project, region, limit=max(1, min(limit, SYNC_RUN_HISTORY_LIMIT))
        )
        token = gcp_cli.access_token()
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


def _sync_run_file_review(project, database, token, run):
    if run.get("state") != "FAILED":
        return None
    base = (f"https://firestore.googleapis.com/v1/projects/{quote(project, safe='')}/"
            f"databases/{quote(database, safe='')}/documents/")
    status, body, _ = gcp_cli.http_json(
        base + "sync_run_reviews/" + quote(run["executionId"], safe=""), token, timeout=10)
    if status == 404:
        from shared.index_results import classify_files, summary

        match = re.search(r"jobId=([a-f0-9]{32})", run.get("error") or "")
        if not match:
            return None
        job_id = match.group(1)
        def read_document(path):
            code, data, _ = gcp_cli.http_json(base + path, token, timeout=10)
            if code != 200:
                return {}
            return {key: _firestore_value(value) for key, value in (data.get("fields") or {}).items()}
        job = read_document("sync_jobs/" + job_id)
        if job.get("driveId") not in run.get("driveIds", []):
            return None
        parts = {pid: read_document(f"sync_jobs/{job_id}/parts/{quote(pid, safe='')}")
                 for pid in job.get("expectedParts", [])}
        if not any(part.get("fileResults") for part in parts.values()):
            return None
        documents = _firestore_document_summaries(project, database, token, job.get("fileIds") or [])
        documents = {fid: doc for fid, doc in documents.items() if doc.get("driveId") == job.get("driveId")}
        files = classify_files(job, parts, documents, {}, version_matches={})
        return {"reviewStatus": "REVIEWED", "files": files, "counts": summary(files),
                "scope": "FAILED_INDEX_JOB", "source": "FILE_CHECKPOINTS", "jobId": job_id}
    if status != 200:
        return {"reviewStatus": "UNAVAILABLE", "reason": "문서별 재검토 결과를 불러오지 못했습니다."}
    review = {key: _firestore_value(value) for key, value in (body.get("fields") or {}).items()}
    if (review.get("executionId") != run["executionId"]
            or review.get("driveId") not in run.get("driveIds", [])
            or review.get("departmentCode") != run.get("departmentCode")):
        return None
    return {key: value for key, value in review.items() if key != "evidence"}


@router.get("/api/v1/sync-runs/{execution_id}")
def sync_run_detail(execution_id: str) -> JSONResponse:
    if not re.fullmatch(
        r"[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}", execution_id
    ):
        raise HTTPException(status_code=404, detail="sync execution not found")
    try:
        common = core._common()
        project = str(common.get("GCP_PROJECT_ID") or "")
        region = str(common["GCP_REGION"])
        database = str(common["FIRESTORE_DATABASE"])
        ok, row = gcp_cli.gcloud_json(
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
        token = gcp_cli.access_token()
        run_id = _execution_run_id(row)
        progress = (
            _firestore_sync_progress(project, database, token, run_id)
            if run_id
            else {}
        )
        run = _sync_execution_record(row, departments, drive_owners, progress)
        file_review = _sync_run_file_review(project, database, token, run)
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
            "fileReview": file_review,
        }
    )


@router.post("/api/v1/sync-runs")
async def start_sync_run(request: Request) -> JSONResponse:
    core._require_local_session(request)
    payload = await request.json()
    if not isinstance(payload, dict):
        payload = {}
    code = str(payload.get("departmentCode") or "").strip().lower()
    mode = str(payload.get("mode") or "delta").strip().lower()
    if mode not in {"delta", "backfill"}:
        raise HTTPException(422, "이 경로는 변경분·전체 동기화만 지원합니다.")
    if not core.DEPT_CODE_RE.fullmatch(code):
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
