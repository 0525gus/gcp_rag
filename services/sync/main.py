from __future__ import annotations

import io
from dataclasses import dataclass, replace
import logging
import os
import re
import sys
import time
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
from fastapi import BackgroundTasks, FastAPI, HTTPException
from pydantic import BaseModel, Field

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from shared.config import Settings, UnknownDriveError, get_settings  # noqa: E402
from shared.drive import DriveClient, parse_drive_size
from services.sync import metadata_refresh
from shared.ocr_status import is_empty_ocr_error  # noqa: E402
from shared.firestore_state import DocStateStore  # noqa: E402
from shared.gcs import GcsClient, gs_uri  # noqa: E402
from shared.hashing import sha256_bytes, sha256_text  # noqa: E402
from shared.logging_config import setup_logging  # noqa: E402
from shared.mime_types import (  # noqa: E402
    GOOGLE_EXPORT_MAP,
    IMAGE_OCR_MAX_BYTES,
    IMAGE_OCR_MIME,
    PDF_OCR_MAX_BYTES,
    PDF_OCR_MAX_PAGES,
    PDF_REVIEW_BYTES,
    PDF_TEXT_PAGE_MIN_CHARS,
    SIDECAR_ONLY_MIME,
    RouteKind,
    classify_route,
    is_hwpx,
    rag_size_limit,
)
from shared.pdf_split import PdfSplitError, split_pdf  # noqa: E402
from shared.xlsx_md import XlsxParseError, xlsx_to_markdown  # noqa: E402
from shared.html_text import html_to_text  # noqa: E402
from shared.models import Audience, DocState, DocStatus, ParseRoute  # noqa: E402
from shared.path_context import (  # noqa: E402
    PathContext,
    build_breadcrumb_markdown,
)
from shared.rag_engine import (  # noqa: E402
    MAX_IMPORT_URIS,
    ImportOutcome,
    RagEngineClient,
    RagImportThrottledError,
)
from shared.rag_mapping import RagFileMapping, RagFileMappingStore  # noqa: E402
from shared.search_postprocess import extract_file_id  # noqa: E402
from shared.task_queue import IndexTaskQueue  # noqa: E402
from shared.index_guard import (  # noqa: E402
    MutationLease, MutationOwnershipLost, StaleIndexTask, activate, check_active_mutation,
    check_versions, document_mutation, document_version, mutation_lock_names, routing_version,
)

setup_logging()
logger = logging.getLogger("sync_service")

_TEXT_COPY_MIMES = frozenset(
    {
        "text/plain",
        "text/markdown",
        "text/html",
        "text/csv",
    }
)

# 스프레드시트는 셀을 마크다운 표로 뽑아 본문으로 삼는다.
# RAG Engine 기본 파서가 xlsx 를 못 읽어 원본은 색인 대상이 아니다.
_SPREADSHEET_COPY_MIMES = frozenset(
    {
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.ms-excel.sheet.macroenabled.12",
    }
)


@dataclass(frozen=True)
class PdfTextProfile:
    pages: int
    text_pages: int
    reason: str | None

    @property
    def image_dominant(self) -> bool:
        return self.pages > 0 and self.text_pages * 2 < self.pages


def _pdf_text_profile(data: bytes) -> PdfTextProfile:
    """페이지별로 텍스트가 있는지 센다.

    스캔 이미지뿐인 PDF는 Vertex ``import_files``가 요청 자체는 성공시킨 뒤
    파일 단위 ``INVALID_ARGUMENT``로 거부한다. 그 상태를 일시 오류처럼
    재시도하면 같은 배치의 정상 파일까지 계속 지우고 다시 색인한다. 텍스트
    페이지가 없거나 PDF 자체를 읽지 못하면 사유를 돌려주고, 호출자는 OCR 또는
    경로 사이드카로 분기한다.
    """
    from pypdf import PdfReader  # noqa: PLC0415

    try:
        reader = PdfReader(io.BytesIO(data))
        pages = list(reader.pages)
    except Exception as exc:  # noqa: BLE001
        return PdfTextProfile(0, 0, f"PDF_UNREADABLE:{type(exc).__name__}:{exc}")

    text_pages = extraction_errors = 0
    any_text = False
    for page in pages:
        try:
            text = (page.extract_text() or "").strip()
        except Exception:  # noqa: BLE001
            extraction_errors += 1
            continue
        any_text = any_text or bool(text)
        if len(text) >= PDF_TEXT_PAGE_MIN_CHARS:
            text_pages += 1
    if any_text:
        return PdfTextProfile(len(pages), text_pages, None)
    reason = "PDF_NO_EXTRACTABLE_TEXT"
    if extraction_errors:
        reason += f":extractErrors={extraction_errors}"
    return PdfTextProfile(len(pages), 0, reason)

@asynccontextmanager
async def _lifespan(_app: FastAPI):
    get_settings()
    yield


app = FastAPI(title="Drive Sync Service (GCS-only index)", version="2.0.0", lifespan=_lifespan)


class DriveIdBody(BaseModel):
    drive_id: str = Field(..., alias="driveId")

    model_config = {"populate_by_name": True}


class BootstrapBody(DriveIdBody):
    baseline_only: bool = Field(default=False, alias="baselineOnly")


class BackfillBody(BaseModel):
    drive_id: str = Field(..., alias="driveId")
    force: bool = False

    model_config = {"populate_by_name": True}


class ChangesBody(DriveIdBody):
    max_changes: int | None = Field(default=None, alias="maxChanges", ge=1, le=2000)


class CommitTokenBody(BaseModel):
    drive_id: str = Field(..., alias="driveId")
    page_token: str = Field(..., alias="pageToken")

    model_config = {"populate_by_name": True}


class IngestBody(BaseModel):
    file_id: str = Field(..., alias="fileId")
    drive_id: str = Field(..., alias="driveId")
    name: str = ""
    mime_type: str = Field(default="", alias="mimeType")
    modified_time: str | None = Field(default=None, alias="modifiedTime")
    refresh_content: bool = Field(default=False, alias="refreshContent")
    removed: bool = False
    web_view_link: str | None = Field(default=None, alias="webViewLink")
    size_bytes: int | None = Field(default=None, alias="sizeBytes")
    route: str | None = None
    parser_url: str = Field(default="", alias="parserUrl")

    model_config = {"populate_by_name": True}


class IndexGcsBody(BaseModel):
    gcs_uris: list[str] = Field(..., alias="gcsUris")
    file_ids: list[str] = Field(default_factory=list, alias="fileIds")
    drive_id: str | None = Field(default=None, alias="driveId")

    model_config = {"populate_by_name": True}


class ReprocessFileBody(BaseModel):
    file_id: str = Field(alias="fileId", pattern=r"^[A-Za-z0-9_-]{1,200}$")
    drive_id: str = Field(alias="driveId", min_length=1)
    request_id: str = Field(alias="requestId", pattern=r"^[a-f0-9]{32}$")
    parser_url: str = Field(default="", alias="parserUrl")


class IndexGcsTaskBody(BaseModel):
    job_id: str = Field(..., alias="jobId")
    part_id: str = Field(..., alias="partId")
    drive_id: str = Field(..., alias="driveId")
    audience: str
    gcs_uris: list[str] = Field(..., alias="gcsUris")
    file_ids: list[str] = Field(..., alias="fileIds")
    file_versions: dict[str, str] = Field(default_factory=dict, alias="fileVersions")
    routing_version: str = Field(default="", alias="routingVersion")

    model_config = {"populate_by_name": True}


class DeleteBody(BaseModel):
    file_id: str = Field(..., alias="fileId")
    drive_id: str | None = Field(default=None, alias="driveId")

    model_config = {"populate_by_name": True}


class BackfillRagMappingsBody(DriveIdBody):
    dry_run: bool = Field(default=True, alias="dryRun")


class ReconcileBody(BaseModel):
    drive_id: str = Field(..., alias="driveId")
    listed: int
    gcs_uploaded: int = Field(..., alias="gcsUploaded")
    uris: int = 0
    indexed: int
    index_job_id: str = Field(default="", alias="indexJobId")
    deferred_uris: int = Field(default=0, alias="deferredUris", ge=0)
    failed: int
    skipped: int
    deleted: int
    unchanged: int = 0
    dlq: int = 0
    split_queued: int = Field(default=0, alias="splitQueued")
    parked: int = 0
    excluded: int = 0

    model_config = {"populate_by_name": True}


@app.get("/health")
def health() -> dict[str, str]:
    get_settings()
    return {"status": "ok", "indexPath": "gcs-only"}


def _route_file_meta(
    *,
    drive_id: str,
    file_meta: dict[str, Any],
    folder_ids: list[str],
    drive: DriveClient,
    enable_image_ocr: bool = False,
) -> dict[str, Any] | None:
    mime = file_meta.get("mimeType") or ""
    if mime == "application/vnd.google-apps.folder" or file_meta.get("name") == "metadata.json":
        return None
    file_id = file_meta.get("id") or ""
    name = file_meta.get("name") or ""
    parents = list(file_meta.get("parents") or [])
    kind = classify_route(mime, name, removed=False, enable_image_ocr=enable_image_ocr)
    skip_reason: str | None = None
    if folder_ids and kind != RouteKind.DELETE:
        if not drive.is_in_sync_scope(file_id, folder_ids, parents=parents):
            kind = RouteKind.EXCLUDE
            skip_reason = "out_of_folder_scope"
    entry: dict[str, Any] = {
        "fileId": file_id,
        "driveId": file_meta.get("driveId") or drive_id,
        "name": name,
        "mimeType": mime,
        "modifiedTime": file_meta.get("modifiedTime"),
        "createdTime": file_meta.get("createdTime"),
        "removed": False,
        "webViewLink": file_meta.get("webViewLink"),
        "sizeBytes": parse_drive_size(file_meta.get("size")),
        "route": kind.value,
    }
    if skip_reason:
        entry["skipReason"] = skip_reason
    return entry


def _build_backfill_changes(
    drive_id: str,
    *,
    store: DocStateStore,
    drive: DriveClient,
    settings: Settings,
) -> dict[str, Any]:
    folder_ids = settings.sync_folder_id_list
    token = store.get_start_page_token(drive_id)
    if not token:
        token = drive.get_start_page_token(drive_id)

    routed: list[dict[str, Any]] = []
    skipped_out_of_scope = 0
    for meta in drive.iter_backfill_files(drive_id, folder_ids):
        entry = _route_file_meta(
            drive_id=drive_id,
            file_meta=meta,
            folder_ids=folder_ids,
            drive=drive,
            enable_image_ocr=getattr(settings, "enable_image_ocr", False),
        )
        if entry is None:
            continue
        if entry.get("skipReason") == "out_of_folder_scope":
            skipped_out_of_scope += 1
            continue
        if entry["route"] == RouteKind.SKIP.value:
            continue
        routed.append(entry)

    return {
        "driveId": drive_id,
        "changes": routed,
        "pendingPageToken": token,
        "count": len(routed),
        "syncFolderIds": folder_ids,
        "skippedOutOfScope": skipped_out_of_scope,
        "mode": "backfill",
        "message": "full snapshot for initial / forced backfill",
    }


@app.post("/sync/bootstrap")
def bootstrap(body: BootstrapBody) -> dict[str, str]:
    store = DocStateStore()
    drive = DriveClient()
    existing = store.get_start_page_token(body.drive_id)
    if existing:
        return {"driveId": body.drive_id, "pageToken": existing, "status": "exists"}
    if not body.baseline_only:
        raise HTTPException(
            status_code=409,
            detail=(
                "bootstrap would skip the current Drive snapshot; use /sync/changes "
                "for initial backfill or explicitly set baselineOnly=true"
            ),
        )
    token = drive.get_start_page_token(body.drive_id)
    store.set_start_page_token(body.drive_id, token)
    return {
        "driveId": body.drive_id,
        "pageToken": token,
        "status": "created_baseline_only",
    }


@app.post("/sync/backfill")
def backfill(body: BackfillBody) -> dict[str, Any]:
    store = DocStateStore()
    drive = DriveClient()
    settings = _settings_for_drive(get_settings(), body.drive_id)
    logger.info(
        "backfill list drive=%s folders=%s force=%s",
        body.drive_id,
        settings.sync_folder_id_list,
        body.force,
    )
    return _build_backfill_changes(
        body.drive_id, store=store, drive=drive, settings=settings
    )


class BackfillRunBody(BaseModel):
    drive_id: str = Field(..., alias="driveId")
    parser_url: str = Field(default="", alias="parserUrl")
    index_batch_size: int = Field(default=50, alias="indexBatchSize", ge=1, le=50)
    run_id: str = Field(default="", alias="runId", max_length=32)
    department_code: str = Field(default="", alias="departmentCode", max_length=20)
    drive_index: int = Field(default=1, alias="driveIndex", ge=1)
    drive_count: int = Field(default=1, alias="driveCount", ge=1)

    model_config = {"populate_by_name": True}


# Cloud Run 요청 타임아웃(1800s)보다 넉넉히 잡되, 프로세스가 죽었을 때 다음 실행이
# 하루 안에는 반드시 들어올 수 있어야 한다.
_BACKFILL_LOCK_TTL_SECONDS = 3600


def _publish_backfill_progress(
    store: DocStateStore,
    body: BackfillRunBody,
    fields: dict[str, Any],
) -> None:
    if not body.run_id:
        return
    payload = {
        "departmentCode": body.department_code,
        "mode": "backfill",
        "driveId": body.drive_id,
        "driveIndex": body.drive_index,
        "driveCount": body.drive_count,
        **fields,
    }
    try:
        store.update_sync_run(body.run_id, payload)
    except Exception:  # noqa: BLE001 - 관측 실패가 실제 동기화를 중단하면 안 된다.
        logger.exception("failed to publish backfill progress run=%s", body.run_id)


@app.post("/sync/backfill-run")
def backfill_run(body: BackfillRunBody) -> dict[str, Any]:
    store = DocStateStore()
    lock_name = f"backfill:{body.drive_id}"
    if not store.try_acquire_lock(lock_name, ttl_seconds=_BACKFILL_LOCK_TTL_SECONDS):
        logger.warning("backfill already running drive=%s — rejecting", body.drive_id)
        raise HTTPException(
            status_code=409,
            detail=f"backfill already running for drive {body.drive_id}",
        )
    try:
        return _backfill_run_locked(body, store)
    except Exception as exc:
        _publish_backfill_progress(
            store,
            body,
            {"status": "FAILED", "phase": "FAILED", "error": str(exc)[:500]},
        )
        raise
    finally:
        store.release_lock(lock_name)


def _backfill_run_locked(body: BackfillRunBody, store: DocStateStore) -> dict[str, Any]:
    import threading
    from concurrent.futures import ThreadPoolExecutor, as_completed

    settings = _settings_for_drive(get_settings(), body.drive_id)
    drive = DriveClient()
    # Firestore/Storage 클라이언트는 스레드 안전하므로 워커들이 공유한다.
    gcs = GcsClient(settings)
    parser_url = body.parser_url or os.environ.get("PARSER_URL", "")
    workers = settings.ingest_concurrency

    _publish_backfill_progress(
        store,
        body,
        {
            "status": "RUNNING",
            "phase": "LISTING",
            "processed": 0,
            "syncFolderIds": settings.sync_folder_id_list,
        },
    )
    snapshot = _build_backfill_changes(
        body.drive_id, store=store, drive=drive, settings=settings
    )
    changes = snapshot["changes"]
    pending_page_token = snapshot["pendingPageToken"]

    totals = {
        "listed": len(changes),
        "gcsUploaded": 0,
        "uris": 0,
        "indexed": 0,
        "failed": 0,
        "skipped": 0,
        "deleted": 0,
        "unchanged": 0,
        "dlq": 0,
        "splitQueued": 0,
        "parked": 0,
        "indexFailed": 0,
        "excluded": int(snapshot.get("skippedOutOfScope") or 0),
    }
    pending_uris: list[str] = []
    pending_ids: list[str] = []
    processed_count = 0
    last_progress_at = 0.0
    progress_context: dict[str, Any] = {}
    lock = threading.Lock()
    index_lock = threading.Lock()

    rag = RagEngineClient(settings)

    def publish_progress(phase: str, *, force: bool = False) -> None:
        nonlocal last_progress_at
        now = time.monotonic()
        if not force and processed_count % 10 != 0 and now - last_progress_at < 2:
            return
        _publish_backfill_progress(
            store,
            body,
            {
                "status": "RUNNING",
                "phase": phase,
                "processed": processed_count,
                "totals": dict(totals),
                **dict(progress_context),
            },
        )
        last_progress_at = now

    publish_progress("INGESTING", force=True)

    def _take_batch(min_size: int) -> tuple[list[str], list[str]] | None:
        nonlocal pending_uris, pending_ids
        with lock:
            if len(pending_uris) < min_size or not pending_uris:
                return None
            uris, ids = pending_uris, pending_ids
            pending_uris, pending_ids = [], []
            return uris, ids

    def flush_index(min_size: int = 1) -> int:
        batch = _take_batch(min_size)
        if batch is None:
            return 0
        uris, ids = batch
        try:
            # import 는 직렬화하되(Vertex RPM), 집계 lock 은 쥐지 않는다.
            with lock:
                progress_context.update(
                    currentAction="RAG 코퍼스 색인",
                    currentBatchSize=len(ids),
                    currentStatus="INDEXING",
                )
            publish_progress("INDEXING", force=True)
            with index_lock:
                split_enabled = settings.audience_split_enabled
                part = _import_and_mark(
                    store,
                    uris,
                    ids,
                    rag=rag,
                    mark_indexed=not split_enabled,
                    mapping_settings=settings,
                    corpus_type="FACULTY",
                )
                indexed = part.imported
                student = _sync_student_corpus(uris, ids, settings, store)
                if split_enabled:
                    if not bool(student.get("ok", True)):
                        raise RuntimeError("student corpus import incomplete")
                    if part.ok:
                        _mark_indexed_states(store, ids)
        except Exception:  # noqa: BLE001
            logger.exception("backfill index flush failed")
            return len(dict.fromkeys(ids))
        with lock:
            totals["indexed"] += indexed
            totals["indexFailed"] += max(0, len(uris) - indexed)
        publish_progress("INGESTING", force=True)
        return 0

    # googleapiclient 의 service 객체는 스레드 안전하지 않다(httplib2.Http 공유).
    # 워커마다 하나씩 두면 8개로 끝나고, 스레드 안에서 parents/name 캐시가 살아
    # 남아 같은 폴더를 반복 조회하지 않는다.
    tls = threading.local()

    def _worker_drive() -> DriveClient:
        client = getattr(tls, "drive", None)
        if client is None:
            client = DriveClient()
            tls.drive = client
        return client

    def _ingest_one(ch: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        ingest_body = IngestBody(
            fileId=ch["fileId"],
            driveId=ch.get("driveId") or body.drive_id,
            name=ch.get("name") or "",
            mimeType=ch.get("mimeType") or "",
            modifiedTime=ch.get("modifiedTime"),
            removed=False,
            webViewLink=ch.get("webViewLink"),
            sizeBytes=ch.get("sizeBytes"),
            route=ch.get("route"),
            parserUrl=parser_url,
        )
        return ch, _ingest_with(
            ingest_body,
            store=store,
            settings=settings,
            gcs=gcs,
            drive=_worker_drive(),
        )

    logger.info(
        "backfill-run parallel workers=%s files=%s", workers, len(changes)
    )

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(_ingest_one, ch) for ch in changes]
        for fut in as_completed(futures):
            processed_count += 1
            try:
                ch, result = fut.result()
            except Exception:  # noqa: BLE001
                logger.exception("backfill ingest worker failed")
                with lock:
                    totals["failed"] += 1
                    totals["dlq"] += 1
                publish_progress("INGESTING")
                continue

            status = result.get("status")
            with lock:
                progress_context.update(
                    currentAction="Drive 파일 처리",
                    currentFileId=str(ch.get("fileId") or ""),
                    currentFileName=str(ch.get("name") or ch.get("fileId") or ""),
                    currentFilePath=str(result.get("path") or ch.get("name") or ""),
                    currentStatus=str(status or "UNKNOWN"),
                )
                if status == "GCS_READY":
                    uris = list(result.get("gcsUris") or [])
                    if not uris and result.get("gcsUri"):
                        uris = [result["gcsUri"]]
                    if uris:
                        pending_uris.extend(uris)
                        pending_ids.extend([ch["fileId"]] * len(uris))
                        totals["gcsUploaded"] += 1
                        totals["uris"] += len(uris)
                    else:
                        logger.error(
                            "backfill ingest returned GCS_READY without URI: %s",
                            ch["fileId"],
                        )
                        totals["failed"] += 1
                elif status in {"UNCHANGED", "HASH_UNCHANGED"}:
                    totals["unchanged"] += 1
                elif status in {"SKIPPED", "skipped"}:
                    totals["skipped"] += 1
                elif status == "DLQ":
                    totals["dlq"] += 1
                    totals["parked"] += 1
                elif status == "SPLIT_QUEUED":
                    totals["splitQueued"] += 1
                    totals["parked"] += 1
                else:
                    totals["failed"] += 1

            index_failed = flush_index(body.index_batch_size)
            if index_failed:
                with lock:
                    totals["indexFailed"] += index_failed
            publish_progress("INGESTING")

    index_failed = flush_index()
    if index_failed:
        totals["indexFailed"] += index_failed

    index_complete = totals["indexed"] == totals["uris"]
    ok = totals["failed"] == 0 and totals["indexFailed"] == 0 and index_complete

    if pending_page_token and ok:
        store.set_start_page_token(body.drive_id, pending_page_token)

    logger.info("backfill-run done drive=%s totals=%s", body.drive_id, totals)
    _publish_backfill_progress(
        store,
        body,
        {
            "status": "SUCCEEDED" if ok else "FAILED",
            "phase": "COMPLETE" if ok else "FAILED",
            "processed": processed_count,
            "totals": dict(totals),
        },
    )
    return {
        "driveId": body.drive_id,
        "mode": "backfill-run",
        "pendingPageToken": pending_page_token,
        "workers": workers,
        "totals": totals,
        "ok": ok,
    }


@app.post("/sync/changes")
def list_changes(body: ChangesBody) -> dict[str, Any]:
    store = DocStateStore()
    drive = DriveClient()
    settings = _settings_for_drive(get_settings(), body.drive_id)
    folder_ids = settings.sync_folder_id_list
    token = store.get_start_page_token(body.drive_id)
    if not token:
        logger.info("no page token drive=%s — delegating to backfill-run", body.drive_id)
        return {
            "driveId": body.drive_id,
            "changes": [],
            "pendingPageToken": "",
            "count": 0,
            "syncFolderIds": folder_ids,
            "skippedOutOfScope": 0,
            "hasMore": False,
            "mode": "backfill_required",
            "message": "no page token; run /sync/backfill-run for initial load",
        }

    limit = body.max_changes or settings.sync_max_changes
    changes, new_token, has_more = drive.list_changes(
        body.drive_id, token, max_changes=limit
    )
    routed: list[dict[str, Any]] = []
    skipped_out_of_scope = 0
    metadata_jobs = []
    for ch in changes:
        if ch.name == "metadata.json" or ch.removed:
            jobs = metadata_refresh.schedule_change(ch, new_token, store, settings, drive)
            metadata_jobs.extend(jobs)
            if ch.name == "metadata.json" or jobs:
                continue
        # Drive changes 피드는 파일 생성/삭제 시 상위 폴더의 변경 레코드도 함께
        # 보낼 수 있다. 폴더 자체는 다운로드·색인·삭제할 대상이 아니므로 Workflow
        # 집계로 넘기지 않는다. pageToken 은 응답 전체 기준으로 계속 전진한다.
        if ch.mime_type == "application/vnd.google-apps.folder" or ch.name == "metadata.json":
            continue
        kind = classify_route(
            ch.mime_type, ch.name, removed=ch.removed,
            enable_image_ocr=getattr(settings, "enable_image_ocr", False),
        )
        skip_reason: str | None = None
        if folder_ids and kind != RouteKind.DELETE:
            in_scope = drive.is_in_sync_scope(
                ch.file_id, folder_ids, parents=ch.parents or None
            )
            if not in_scope:
                kind = RouteKind.EXCLUDE
                skip_reason = "out_of_folder_scope"
                skipped_out_of_scope += 1
        entry: dict[str, Any] = {
            "fileId": ch.file_id,
            "driveId": ch.drive_id,
            "name": ch.name,
            "mimeType": ch.mime_type,
            "operation": (
                "DELETED"
                if kind == RouteKind.DELETE
                else "CREATED"
                # Drive uploads may preserve the local file's older mtime.  In that
                # case createdTime is *later* than modifiedTime even though this is
                # a brand-new Drive file (observed with desktop-uploaded TXT files).
                # Exact equality therefore mislabels new uploads as UPDATED.
                if ch.created_time
                and ch.modified_time
                and ch.created_time >= ch.modified_time
                else "UPDATED"
            ),
            "modifiedTime": ch.modified_time,
            "createdTime": ch.created_time,
            "removed": ch.removed,
            "webViewLink": ch.web_view_link,
            "sizeBytes": ch.size_bytes,
            "route": kind.value,
        }
        if skip_reason:
            entry["skipReason"] = skip_reason
        routed.append(entry)

    if metadata_jobs:
        logger.info("metadata refresh queued drive=%s jobs=%s", body.drive_id, metadata_jobs)
    return {
        "driveId": body.drive_id,
        "changes": routed,
        "pendingPageToken": new_token,
        "count": len(routed),
        "syncFolderIds": folder_ids,
        "skippedOutOfScope": skipped_out_of_scope,
        "hasMore": has_more,
        "mode": "delta",
        "metadataJobs": metadata_jobs,
    }


class MetadataRefreshBody(BaseModel):
    job_id: str = Field(alias="jobId", pattern=r"^metadata-[0-9a-f]{32}$")


@app.post("/sync/metadata-refresh-task")
def metadata_refresh_task(body: MetadataRefreshBody) -> dict[str, Any]:
    return metadata_refresh.run_page(body.job_id, base=get_settings(),
        settings_for_drive=_settings_for_drive, source_uris=_source_uris_for_file,
        index=lambda uris, ids, drive_id: index_gcs(IndexGcsBody(
            gcsUris=uris, fileIds=ids, driveId=drive_id)))


@app.post("/sync/commit-token")
def commit_token(body: CommitTokenBody) -> dict[str, str]:
    store = DocStateStore()
    store.set_start_page_token(body.drive_id, body.page_token)
    return {"driveId": body.drive_id, "pageToken": body.page_token, "status": "committed"}


_OUT_OF_SCOPE_CLEANUP_ERROR = "out_of_folder_scope_cleanup_failed"
_OUT_OF_SCOPE_REASON = "out_of_folder_scope"


def _already_evicted(existing: DocState | None) -> bool:
    return bool(
        existing
        and existing.status in {DocStatus.EXCLUDED, DocStatus.SKIPPED}
        and (existing.error or "") == _OUT_OF_SCOPE_REASON
    )


def _delete_gcs_objects_for_file(gcs: GcsClient, bucket: str, file_id: str) -> int:
    if not bucket:
        return 0
    blobs = list(
        gcs._client.list_blobs(
            bucket,
            prefix=file_id,
        )
    )
    deleted = 0
    failures: list[Exception] = []
    for blob in blobs:
        rest = blob.name[len(file_id) :]
        # A Drive id can be a prefix of another id. Only delete this id's
        # ``{file_id}.ext`` objects (including ``.meta.md``).
        if rest and not rest.startswith("."):
            continue
        try:
            blob.delete()
            deleted += 1
        except Exception as exc:
            failures.append(exc)
            logger.exception("GCS cleanup failed: %s", blob.name)
    if failures:
        raise RuntimeError(
            f"failed to delete {len(failures)} GCS object(s) for {file_id}"
        ) from failures[0]
    return deleted


def _cleanup_out_of_scope_file(
    gcs: GcsClient, settings: Settings, file_id: str
) -> tuple[bool, int, int]:
    failures: list[Exception] = []
    rag_deleted = False
    source_deleted = 0
    hwp_original_deleted = 0
    try:
        rag_deleted = RagEngineClient(settings).delete_by_file_id(file_id)
    except Exception as exc:
        failures.append(exc)
        logger.exception("out-of-scope RAG cleanup failed: %s", file_id)
    if settings.audience_split_enabled:
        try:
            student_deleted = RagEngineClient(
                settings, corpus_name=settings.rag_corpus_name_student
            ).delete_by_file_id(file_id)
            rag_deleted = rag_deleted or student_deleted
        except Exception as exc:
            failures.append(exc)
            logger.exception("out-of-scope 학생 코퍼스 cleanup failed: %s", file_id)
    for bucket, kind in (
        (settings.gcs_source_bucket, "source"),
        (settings.gcs_hwp_original_bucket, "hwpOriginal"),
    ):
        try:
            count = _delete_gcs_objects_for_file(gcs, bucket, file_id)
            if kind == "source":
                source_deleted = count
            else:
                hwp_original_deleted = count
        except Exception as exc:
            failures.append(exc)
            logger.exception("out-of-scope %s GCS cleanup failed: %s", kind, file_id)
    if failures:
        raise RuntimeError(
            f"out-of-scope cleanup failed for {file_id} ({len(failures)} target(s))"
        ) from failures[0]
    return rag_deleted, source_deleted, hwp_original_deleted


@app.post("/sync/ingest")
def ingest(body: IngestBody) -> dict[str, Any]:
    # 빈 fileId 는 Firestore 문서 경로를 `doc_state/` 로 만들어 400 을 던진다.
    # 호출측을 고쳐도(shared/drive.py) 이 엔드포인트가 500 으로 죽을 이유는 없다.
    if not body.file_id.strip():
        raise HTTPException(status_code=400, detail="fileId is required")

    settings = _settings_for_drive(get_settings(), body.drive_id)
    return _ingest_with(
        body,
        store=DocStateStore(),
        settings=settings,
        gcs=GcsClient(settings),
        drive=DriveClient(),
    )


def _ingest_with(
    body: IngestBody,
    *,
    store: DocStateStore,
    settings: Settings,
    gcs: GcsClient,
    drive: DriveClient,
) -> dict[str, Any]:
    with document_mutation(store, settings, [body.file_id]):
        return _ingest_locked(body, store=store, settings=settings, gcs=gcs, drive=drive)


def _ingest_locked(
    body: IngestBody, *, store: DocStateStore, settings: Settings,
    gcs: GcsClient, drive: DriveClient,
) -> dict[str, Any]:
    if body.name == "metadata.json" and not body.removed:
        existing = store.get(body.file_id)
        if existing and existing.status not in {DocStatus.DELETED, DocStatus.EXCLUDED}:
            _cleanup_out_of_scope_file(gcs, settings, body.file_id)
        store.upsert(DocState(file_id=body.file_id, drive_id=body.drive_id,
                              name=body.name, mime_type=body.mime_type,
                              modified_time=body.modified_time, status=DocStatus.EXCLUDED,
                              error="metadata_sidecar"))
        return {"fileId": body.file_id, "status": "EXCLUDED", "reason": "metadata_sidecar"}
    folder_ids = settings.sync_folder_id_list
    routed_out_of_scope = body.route == RouteKind.EXCLUDE.value
    if not body.removed and (
        routed_out_of_scope
        or (folder_ids and not drive.is_in_sync_scope(body.file_id, folder_ids))
    ):
        rag_deleted = False
        source_deleted = 0
        hwp_original_deleted = 0
        if _already_evicted(store.get(body.file_id)):
            return {
                "fileId": body.file_id,
                "status": DocStatus.EXCLUDED.value,
                "reason": _OUT_OF_SCOPE_REASON,
                "ragDeleted": False,
                "sourceDeleted": 0,
                "hwpOriginalDeleted": 0,
                "cleanupSkipped": True,
            }
        try:
            rag_deleted, source_deleted, hwp_original_deleted = (
                _cleanup_out_of_scope_file(gcs, settings, body.file_id)
            )
        except Exception as exc:
            reason = f"{_OUT_OF_SCOPE_CLEANUP_ERROR}: {exc}"
            try:
                store.enqueue_dlq(
                    body.file_id,
                    reason,
                    driveId=body.drive_id,
                    name=body.name,
                    mimeType=body.mime_type,
                    modifiedTime=body.modified_time,
                    route=RouteKind.SKIP.value,
                    sourceUri=body.web_view_link,
                )
            except Exception as state_exc:
                logger.exception(
                    "failed to record out-of-scope cleanup failure: %s",
                    body.file_id,
                )
                raise HTTPException(
                    status_code=500,
                    detail=f"{_OUT_OF_SCOPE_CLEANUP_ERROR}: state_record_failed",
                ) from state_exc
            raise HTTPException(status_code=500, detail=reason[:500]) from exc
        store.upsert(
            DocState(
                file_id=body.file_id,
                drive_id=body.drive_id,
                name=body.name,
                mime_type=body.mime_type,
                modified_time=body.modified_time,
                status=DocStatus.EXCLUDED,
                parse_route=ParseRoute.NONE,
                source_uri=body.web_view_link,
                error=_OUT_OF_SCOPE_REASON,
            )
        )
        return {
            "fileId": body.file_id,
            "status": DocStatus.EXCLUDED.value,
            "reason": _OUT_OF_SCOPE_REASON,
            "ragDeleted": rag_deleted,
            "sourceDeleted": source_deleted,
            "hwpOriginalDeleted": hwp_original_deleted,
        }

    if not body.removed and not body.refresh_content and body.mime_type.lower() in IMAGE_OCR_MIME:
        held = store.get(body.file_id)
        if (held and held.status == DocStatus.SKIPPED and held.error == "OCR_EMPTY_TEXT"
                and held.modified_time == body.modified_time):
            return {"fileId":body.file_id,"status":"SKIPPED","reason":"OCR_EMPTY_TEXT",
                    "route":"IMAGE_OCR","automaticRetry":False}

    image_ocr_enabled = getattr(settings, "enable_image_ocr", False)
    image_audience = None
    if not body.removed and (body.mime_type.lower() in IMAGE_OCR_MIME or body.route == RouteKind.IMAGE_OCR.value):
        image_audience = _resolve_audience(drive, settings, body)
        image_ocr_enabled = settings.parser_option_for("enable_image_ocr", image_audience)

    route = RouteKind(body.route) if body.route else classify_route(
        body.mime_type, body.name, removed=body.removed,
        enable_image_ocr=image_ocr_enabled,
    )
    if route != RouteKind.DELETE and not body.removed and (body.mime_type.lower() in IMAGE_OCR_MIME or route == RouteKind.IMAGE_OCR):
        route = classify_route(
            body.mime_type, body.name,
            enable_image_ocr=image_ocr_enabled,
        )

    if route == RouteKind.DELETE or body.removed:
        return {"fileId": body.file_id, "status": "DELETE_PENDING", "route": "DELETE"}

    if route == RouteKind.SKIP:
        if body.mime_type.lower() in IMAGE_OCR_MIME:
            existing = store.get(body.file_id)
            if existing and existing.status == DocStatus.INDEXED:
                return {"fileId": body.file_id, "status": "SKIPPED", "route": "SKIP"}
        store.upsert(
            DocState(
                file_id=body.file_id,
                drive_id=body.drive_id,
                name=body.name,
                mime_type=body.mime_type,
                modified_time=body.modified_time,
                status=DocStatus.SKIPPED,
                parse_route=ParseRoute.NONE,
                source_uri=body.web_view_link,
                audience=image_audience or _resolve_audience(drive, settings, body),
                error="IMAGE_OCR_DISABLED" if body.mime_type.lower() in IMAGE_OCR_MIME else "UNSUPPORTED_FORMAT",
            )
        )
        return {"fileId": body.file_id, "status": DocStatus.SKIPPED.value, "route": route.value}

    if not body.refresh_content and not store.should_reparse(body.file_id, body.modified_time):
        existing = store.get(body.file_id)
        if (
            existing
            and (existing.path or "").strip()
            and existing.status == DocStatus.INDEXED
        ):
            context = _resolve_path_ctx(drive, body)
            if (context.source_title == (existing.source_title or "")
                    and context.website_url == (existing.website_url or "")):
                return {"fileId": body.file_id, "status": "UNCHANGED", "route": route.value}

    try:
        if route == RouteKind.IMAGE_OCR:
            return _ingest_image_ocr(body, store, gcs, drive, settings, audience=image_audience)
        if route == RouteKind.HWP_PARSE:
            return _ingest_hwp(body, store, gcs, drive, settings)
        if route == RouteKind.GOOGLE_EXPORT:
            return _ingest_google_export(body, store, gcs, drive, settings)
        if route == RouteKind.FILE_COPY:
            return _ingest_direct(body, store, gcs, drive, settings)
        store.upsert(
            DocState(
                file_id=body.file_id,
                drive_id=body.drive_id,
                name=body.name,
                mime_type=body.mime_type,
                modified_time=body.modified_time,
                status=DocStatus.SKIPPED,
                parse_route=ParseRoute.NONE,
            )
        )
        return {"fileId": body.file_id, "status": DocStatus.SKIPPED.value}
    except Exception as exc:  # noqa: BLE001
        logger.exception("ingest failed: %s", body.file_id)
        store.enqueue_dlq(
            body.file_id,
            str(exc),
            driveId=body.drive_id,
            name=body.name,
            mimeType=body.mime_type,
            modifiedTime=body.modified_time,
            route=route.value,
            sourceUri=body.web_view_link,
        )
        return {
            "fileId": body.file_id,
            "status": "DLQ",
            "route": route.value,
            "error": str(exc)[:500],
        }


def _effective_limit(settings: Settings, ext: str) -> int:
    """RAG Engine 타입별 한도와 우리 상한 중 작은 쪽.

    RAG Engine 은 초과분을 잘라 주는 게 아니라 import 자체를 거부한다 — 게이트를
    통과시키면 index-gcs 가 배치 전체를 실패로 돌리고, 그 문서는 PARSED 에 머물며
    매일 재시도만 반복한다. 그래서 한도를 넘겨 올려봐야 의미가 없고,
    MAX_GCS_BYTES 는 그보다 더 조이고 싶을 때만 쓰인다.

    타입별 한도표는 shared/mime_types.py 가 단일 소스다(rag_size_limit).
    """
    return min(settings.max_gcs_bytes, rag_size_limit(ext))


def _size_gate(
    store: DocStateStore,
    settings: Settings,
    body: IngestBody,
    size: int | None,
    *,
    splittable: bool,
    ext: str,
    limit: int | None = None,
) -> dict[str, Any] | None:
    if size is None:
        return None
    if limit is None:
        limit = _effective_limit(settings, ext)
    if size <= limit:
        return None
    reason = f"SIZE_EXCEEDED:{size}>{limit}({ext})"
    if splittable:
        store.enqueue_split(
            body.file_id,
            reason,
            size,
            driveId=body.drive_id,
            name=body.name,
            mimeType=body.mime_type,
            modifiedTime=body.modified_time,
        )
        return {
            "fileId": body.file_id,
            "status": "SPLIT_QUEUED",
            "sizeBytes": size,
            "error": reason,
        }
    store.enqueue_dlq(
        body.file_id,
        reason,
        driveId=body.drive_id,
        name=body.name,
        mimeType=body.mime_type,
        modifiedTime=body.modified_time,
        sizeBytes=size,
    )
    return {
        "fileId": body.file_id,
        "status": "DLQ",
        "sizeBytes": size,
        "error": reason,
    }


def _resolve_path_ctx(drive: DriveClient, body: IngestBody) -> PathContext:
    try:
        return drive.resolve_path_context(body.file_id, body.name or body.file_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("path resolve failed %s: %s", body.file_id, exc)
        return PathContext(
            path=body.name or body.file_id,
            bundle="",
            segments=(body.name or body.file_id,),
        )


def _resolve_audience(
    drive: DriveClient, settings: Settings, body: IngestBody
) -> Audience:
    folder_ids = settings.student_folder_id_list
    if not folder_ids:
        return Audience.STAFF
    try:
        in_student = drive.is_in_sync_scope(body.file_id, folder_ids)
    except Exception as exc:  # noqa: BLE001
        logger.warning("audience 판정 실패 %s: %s — STAFF 로 둔다", body.file_id, exc)
        return Audience.STAFF
    return Audience.STUDENT if in_student else Audience.STAFF


def _state_fields(
    body: IngestBody,
    *,
    content_hash: str | None,
    status: DocStatus,
    parse_route: ParseRoute,
    source_uri: str | None,
    path_ctx: PathContext,
    audience: Audience = Audience.STAFF,
    error: str | None = None,
) -> DocState:
    return DocState(
        file_id=body.file_id,
        drive_id=body.drive_id,
        name=body.name,
        mime_type=body.mime_type,
        modified_time=body.modified_time,
        content_hash=content_hash,
        status=status,
        parse_route=parse_route,
        source_uri=source_uri,
        path=path_ctx.path,
        bundle=path_ctx.bundle,
        source_title=path_ctx.source_title or None,
        website_url=path_ctx.website_url or None,
        metadata_file_id=path_ctx.metadata_file_id or None,
        audience=audience,
        error=error,
    )


def _gcs_ready(
    *,
    body: IngestBody,
    route: str,
    parse_route: ParseRoute,
    uris: list[str],
    content_hash: str,
    path_ctx: PathContext,
) -> dict[str, Any]:
    primary = uris[0] if uris else ""
    return {
        "fileId": body.file_id,
        "status": "GCS_READY",
        "route": route,
        "parseRoute": parse_route.value,
        "gcsUri": primary,
        "gcsUris": uris,
        "contentHash": content_hash,
        "path": path_ctx.path,
        "bundle": path_ctx.bundle,
    }


def _cloud_run_auth_headers(audience_url: str) -> dict[str, str]:
    import google.auth.transport.requests
    import google.oauth2.id_token

    base = audience_url.rstrip("/")
    request = google.auth.transport.requests.Request()
    token = google.oauth2.id_token.fetch_id_token(request, base)
    return {"Authorization": f"Bearer {token}"}


def _ingest_hwp(
    body: IngestBody,
    store: DocStateStore,
    gcs: GcsClient,
    drive: DriveClient,
    settings: Settings,
) -> dict[str, Any]:
    if not body.parser_url:
        raise ValueError("parserUrl required for HWP_PARSE")

    src_ext = ".hwpx" if is_hwpx(body.mime_type, body.name) else ".hwp"
    gated = _size_gate(
        store,
        settings,
        body,
        body.size_bytes,
        splittable=True,
        ext=src_ext,
        limit=settings.max_gcs_bytes,
    )
    if gated:
        gated["route"] = "HWP_PARSE"
        return gated

    path_ctx = _resolve_path_ctx(drive, body)
    audience = _resolve_audience(drive, settings, body)
    raw = drive.download_file(body.file_id)
    ext = src_ext
    raw_uri = gcs.upload_hwp_original(raw, body.file_id, ext)

    headers = _cloud_run_auth_headers(body.parser_url)
    with httpx.Client(timeout=600.0) as client:
        resp = client.post(
            body.parser_url.rstrip("/") + "/parse",
            headers=headers,
            json={
                "gcsUri": raw_uri,
                "mimeType": body.mime_type,
                "fileId": body.file_id,
                "sourceBucket": settings.gcs_source_bucket,
                "enableDocaiFallback": settings.parser_option_for("enable_docai_fallback", audience),
            },
        )
        if resp.status_code == 422:
            detail = (
                resp.json()
                if resp.headers.get("content-type", "").startswith("application/json")
                else {"detail": resp.text}
            )
            kind = "PARSE_REJECTED"
            if isinstance(detail, dict):
                inner = detail.get("detail") if isinstance(detail.get("detail"), dict) else detail
                kind = str(inner.get("error") or kind)
            reason = f"{kind}:{detail}"
            store.enqueue_dlq(
                body.file_id,
                reason,
                driveId=body.drive_id,
                name=body.name,
                mimeType=body.mime_type,
                modifiedTime=body.modified_time,
                parseRoute=(
                    ParseRoute.HWPX.value
                    if is_hwpx(body.mime_type, body.name)
                    else ParseRoute.RHWP.value
                ),
                sourceUri=body.web_view_link,
                path=path_ctx.path,
                bundle=path_ctx.bundle,
            )
            try:
                gcs.delete(raw_uri)
            except Exception:  # noqa: BLE001
                logger.warning("raw cleanup after parse rejection failed: %s", raw_uri)
            return {
                "fileId": body.file_id,
                "status": "DLQ",
                "route": "HWP_PARSE",
                "errorKind": kind,
                "error": reason,
            }
        resp.raise_for_status()
        parsed = resp.json()

    md_uri = parsed["gcsMarkdownUri"]
    route = ParseRoute(parsed.get("route", ParseRoute.RHWP.value))

    md_bytes = gcs.download_bytes(md_uri)
    md_text = build_breadcrumb_markdown(
        path=path_ctx.path,
        bundle=path_ctx.bundle,
        title=body.name or body.file_id, source_title=path_ctx.source_title, website_url=path_ctx.website_url,
        body=md_bytes.decode("utf-8", errors="replace"),
    )
    md_bytes = md_text.encode("utf-8")
    gated = _size_gate(store, settings, body, len(md_bytes), splittable=True, ext=".md")
    if gated:
        gated["route"] = "HWP_PARSE"
        return gated

    content_hash = sha256_text(md_text)
    md_uri = gcs.upload_source_md(md_text, body.file_id)

    if store.should_skip_reindex(body.file_id, content_hash):
        store.touch_modified_time(
            body.file_id, body.modified_time, audience=audience
        )
        return {
            "fileId": body.file_id,
            "status": "HASH_UNCHANGED",
            "route": "HWP_PARSE",
            "contentHash": content_hash,
            "path": path_ctx.path,
            "bundle": path_ctx.bundle,
        }

    store.upsert(
        _state_fields(
            body,
            content_hash=content_hash,
            status=DocStatus.PARSED,
            parse_route=route,
            source_uri=body.web_view_link or md_uri,
            path_ctx=path_ctx,
            audience=audience,
        )
    )
    return _gcs_ready(
        body=body,
        route="HWP_PARSE",
        parse_route=route,
        uris=[md_uri],
        content_hash=content_hash,
        path_ctx=path_ctx,
    )


def _hold_empty_ocr(body, store, drive, settings, gcs, audience):
    existing = store.get(body.file_id)
    if existing and existing.status not in {DocStatus.SKIPPED, DocStatus.DELETED, DocStatus.EXCLUDED}:
        _cleanup_out_of_scope_file(gcs, settings, body.file_id)
    context = _resolve_path_ctx(drive, body)
    store.upsert(_state_fields(body, content_hash=None, status=DocStatus.SKIPPED,
        parse_route=ParseRoute.IMAGE_DOCAI, source_uri=body.web_view_link,
        path_ctx=context, audience=audience, error="OCR_EMPTY_TEXT"))
    store.clear_dlq(body.file_id)
    return {"fileId":body.file_id,"status":"SKIPPED","route":"IMAGE_OCR",
            "reason":"OCR_EMPTY_TEXT","automaticRetry":False}


def _ingest_image_ocr(
    body: IngestBody, store: DocStateStore, gcs: GcsClient,
    drive: DriveClient, settings: Settings, *, audience: Audience | None = None,
) -> dict[str, Any]:
    audience = audience if audience is not None else _resolve_audience(drive, settings, body)
    if not settings.parser_option_for("enable_image_ocr", audience):
        raise ValueError("IMAGE_OCR_DISABLED")
    mime = body.mime_type.lower()
    if mime not in IMAGE_OCR_MIME:
        raise ValueError("OCR_UNSUPPORTED_TYPE")
    if not body.parser_url:
        raise ValueError("parserUrl required for IMAGE_OCR")
    limit = min(settings.max_gcs_bytes, IMAGE_OCR_MAX_BYTES)
    gated = _size_gate(store, settings, body, body.size_bytes, splittable=False, ext=IMAGE_OCR_MIME[mime], limit=limit)
    if gated:
        return {**gated, "route": RouteKind.IMAGE_OCR.value}
    raw = drive.download_file(body.file_id, max_bytes=limit)
    gated = _size_gate(store, settings, body, len(raw), splittable=False, ext=IMAGE_OCR_MIME[mime], limit=limit)
    if gated:
        return {**gated, "route": RouteKind.IMAGE_OCR.value}
    raw_uri = gcs.upload_bytes(
        raw, settings.gcs_hwp_original_bucket, body.file_id + IMAGE_OCR_MIME[mime], mime,
    )
    with httpx.Client(timeout=180.0) as client:
        response = client.post(
            body.parser_url.rstrip("/") + "/ocr",
            headers=_cloud_run_auth_headers(body.parser_url),
            json={"gcsUri": raw_uri, "fileId": body.file_id, "mimeType": mime,
                  "enableImageOcr": True},
        )
        if response.status_code != 200:
            error = f"IMAGE_OCR_FAILED:{response.status_code}:{response.text[:500]}"
            if is_empty_ocr_error(error, mime):
                return _hold_empty_ocr(body, store, drive, settings, gcs, audience)
            raise ValueError(error)
        result = response.json()
    text = result.get("text")
    if not isinstance(text, str):
        raise ValueError("OCR_INVALID_RESPONSE: text must be a string")
    if not text.strip():
        return _hold_empty_ocr(body, store, drive, settings, gcs, audience)
    return _index_ocr_text(
        body, store, gcs, settings, text, audience, _resolve_path_ctx(drive, body), "IMAGE_OCR",
    )


def _index_ocr_text(
    body: IngestBody, store: DocStateStore, gcs: GcsClient, settings: Settings,
    text: str, audience: Audience, path_ctx: PathContext, route: str,
) -> dict[str, Any]:
    markdown = build_breadcrumb_markdown(
        path=path_ctx.path, bundle=path_ctx.bundle, title=body.name or body.file_id, source_title=path_ctx.source_title, website_url=path_ctx.website_url, body=text,
    )
    gated = _size_gate(store, settings, body, len(markdown.encode("utf-8")), splittable=False, ext=".md")
    if gated:
        return {**gated, "route": route}
    content_hash = sha256_text(markdown)
    if store.should_skip_reindex(body.file_id, content_hash):
        store.touch_modified_time(body.file_id, body.modified_time, audience=audience)
        return {"fileId": body.file_id, "status": "HASH_UNCHANGED", "route": route}
    md_uri = gcs.upload_source_md(markdown, body.file_id)
    store.upsert(_state_fields(
        body, content_hash=content_hash, status=DocStatus.PARSED,
        parse_route=ParseRoute.IMAGE_DOCAI, source_uri=body.web_view_link or md_uri,
        path_ctx=path_ctx, audience=audience,
    ))
    return _gcs_ready(
        body=body, route=route, parse_route=ParseRoute.IMAGE_DOCAI,
        uris=[md_uri], content_hash=content_hash, path_ctx=path_ctx,
    )


def _hold_for_admin_review(
    body: IngestBody, store: DocStateStore, gcs: GcsClient, drive: DriveClient,
    settings: Settings, size: int | None, limit: int,
) -> dict[str, Any]:
    reason = f"ADMIN_REVIEW_SIZE:{size if size is not None else 'unknown'}>{limit}(.pdf)"
    existing = store.get(body.file_id)
    if existing and existing.status not in {DocStatus.SKIPPED, DocStatus.DELETED, DocStatus.EXCLUDED}:
        _cleanup_out_of_scope_file(gcs, settings, body.file_id)
    store.upsert(_state_fields(
        body, content_hash=None, status=DocStatus.SKIPPED, parse_route=ParseRoute.NONE,
        source_uri=body.web_view_link, path_ctx=_resolve_path_ctx(drive, body),
        audience=_resolve_audience(drive, settings, body), error=reason,
    ))
    store.clear_dlq(body.file_id)
    logger.warning("PDF 관리자 확인 대상 %s (%s): %s", body.file_id, body.name, reason)
    return {"fileId": body.file_id, "status": "SKIPPED", "route": "FILE_COPY",
            "reason": reason, "automaticRetry": False}


def _ingest_pdf_ocr(
    body: IngestBody, store: DocStateStore, gcs: GcsClient, drive: DriveClient,
    settings: Settings, data: bytes, audience: Audience, path_ctx: PathContext,
) -> dict[str, Any] | None:
    raw_uri = gcs.upload_bytes(
        data, settings.gcs_hwp_original_bucket, f"{body.file_id}.pdf", "application/pdf",
    )
    try:
        with httpx.Client(timeout=180.0) as client:
            response = client.post(
                body.parser_url.rstrip("/") + "/ocr",
                headers=_cloud_run_auth_headers(body.parser_url),
                json={"gcsUri": raw_uri, "fileId": body.file_id,
                      "mimeType": "application/pdf", "enableImageOcr": True},
            )
        if response.status_code != 200:
            raise ValueError(f"PDF_OCR_FAILED:{response.status_code}:{response.text[:300]}")
        text = response.json().get("text")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("PDF_OCR_EMPTY_TEXT")
    except Exception as exc:  # noqa: BLE001
        logger.warning("PDF OCR 실패 %s (%s): %s — 기존 PDF 경로로 처리한다", body.file_id, body.name, exc)
        return None
    result = _index_ocr_text(body, store, gcs, settings, text, audience, path_ctx, "FILE_COPY")
    if result.get("status") == "GCS_READY":
        keep = f"{body.file_id}.md"
        try:
            for name in gcs.list_blob_names_for_file(settings.gcs_source_bucket, body.file_id):
                if name != keep:
                    gcs.delete(gs_uri(settings.gcs_source_bucket, name))
        except Exception as exc:  # noqa: BLE001
            logger.warning("PDF OCR 이전 원본 정리 실패 %s: %s", body.file_id, exc)
    return result


def _ingest_google_export(
    body: IngestBody,
    store: DocStateStore,
    gcs: GcsClient,
    drive: DriveClient,
    settings: Settings,
) -> dict[str, Any]:
    path_ctx = _resolve_path_ctx(drive, body)
    audience = _resolve_audience(drive, settings, body)
    export_mime, ext = GOOGLE_EXPORT_MAP[body.mime_type]
    data = drive.export_file(body.file_id, export_mime)
    gated = _size_gate(store, settings, body, len(data), splittable=True, ext=ext)
    if gated:
        gated["route"] = "GOOGLE_EXPORT"
        return gated

    sidecar = build_breadcrumb_markdown(
        path=path_ctx.path,
        bundle=path_ctx.bundle,
        title=body.name or body.file_id, source_title=path_ctx.source_title, website_url=path_ctx.website_url,
    )
    content_hash = sha256_text(f"{sha256_bytes(data)}|{path_ctx.path}|{sidecar}")
    if store.should_skip_reindex(body.file_id, content_hash):
        store.touch_modified_time(body.file_id, body.modified_time)
        return {
            "fileId": body.file_id,
            "status": "HASH_UNCHANGED",
            "route": "GOOGLE_EXPORT",
            "contentHash": content_hash,
            "path": path_ctx.path,
            "bundle": path_ctx.bundle,
        }

    blob = f"{body.file_id}{ext}"
    gcs_uri = gcs.upload_bytes(
        data, settings.gcs_source_bucket, blob, content_type=export_mime
    )
    meta_uri = gcs.upload_source_sidecar_md(sidecar, body.file_id)
    store.upsert(
        _state_fields(
            body,
            content_hash=content_hash,
            status=DocStatus.PARSED,
            parse_route=ParseRoute.GCS_EXPORT,
            source_uri=body.web_view_link or gcs_uri,
            path_ctx=path_ctx,
            audience=audience,
        )
    )
    return _gcs_ready(
        body=body,
        route="GOOGLE_EXPORT",
        parse_route=ParseRoute.GCS_EXPORT,
        uris=[gcs_uri, meta_uri],
        content_hash=content_hash,
        path_ctx=path_ctx,
    )


def _ingest_sidecar_only(
    body: IngestBody,
    store: DocStateStore,
    gcs: GcsClient,
    drive: DriveClient,
    settings: Settings,
) -> dict[str, Any]:
    path_ctx = _resolve_path_ctx(drive, body)
    audience = _resolve_audience(drive, settings, body)
    sidecar = build_breadcrumb_markdown(
        path=path_ctx.path,
        bundle=path_ctx.bundle,
        title=body.name or body.file_id, source_title=path_ctx.source_title, website_url=path_ctx.website_url,
        body="",
    )
    content_hash = sha256_text(f"{body.file_id}|{path_ctx.path}|{sidecar}")
    if store.should_skip_reindex(body.file_id, content_hash):
        store.touch_modified_time(body.file_id, body.modified_time)
        return {
            "fileId": body.file_id,
            "status": "HASH_UNCHANGED",
            "route": "FILE_COPY",
            "contentHash": content_hash,
            "path": path_ctx.path,
            "bundle": path_ctx.bundle,
        }

    uris = [gcs.upload_source_sidecar_md(sidecar, body.file_id)]
    store.upsert(
        _state_fields(
            body,
            content_hash=content_hash,
            status=DocStatus.PARSED,
            parse_route=ParseRoute.GCS_COPY,
            source_uri=body.web_view_link or uris[0],
            path_ctx=path_ctx,
            audience=audience,
            error=f"NO_BODY_EXTRACTOR:{body.mime_type}",
        )
    )
    logger.info(
        "sidecar-only ingest %s (%s) — 본문 없이 경로만 색인",
        body.file_id,
        body.mime_type,
    )
    return _gcs_ready(
        body=body,
        route="FILE_COPY",
        parse_route=ParseRoute.GCS_COPY,
        uris=uris,
        content_hash=content_hash,
        path_ctx=path_ctx,
    )


def _ingest_direct(
    body: IngestBody,
    store: DocStateStore,
    gcs: GcsClient,
    drive: DriveClient,
    settings: Settings,
) -> dict[str, Any]:
    name = body.name or body.file_id
    mime = (body.mime_type or "").lower()
    ext = Path(name).suffix or _ext_for_mime(body.mime_type)

    if mime in SIDECAR_ONLY_MIME:
        return _ingest_sidecar_only(body, store, gcs, drive, settings)

    upload_ext = ".md" if mime in _TEXT_COPY_MIMES else ext

    if mime in _SPREADSHEET_COPY_MIMES or ext.lower() == ".pdf":
        pre_ext, pre_limit = ext, settings.max_gcs_bytes
    else:
        pre_ext, pre_limit = upload_ext, None

    is_pdf = ext.lower() == ".pdf"
    pdf_limit = min(settings.max_gcs_bytes, PDF_REVIEW_BYTES)
    if is_pdf and body.size_bytes is not None and body.size_bytes > pdf_limit:
        return _hold_for_admin_review(body, store, gcs, drive, settings, body.size_bytes, pdf_limit)

    if not is_pdf:
        gated = _size_gate(
            store,
            settings,
            body,
            body.size_bytes,
            splittable=True,
            ext=pre_ext,
            limit=pre_limit,
        )
        if gated:
            gated["route"] = "FILE_COPY"
            return gated

    path_ctx = _resolve_path_ctx(drive, body)
    audience = _resolve_audience(drive, settings, body)
    if is_pdf:
        try:
            data = drive.download_file(body.file_id, max_bytes=pdf_limit)
        except ValueError as exc:
            if not str(exc).startswith("DOWNLOAD_SIZE_EXCEEDED"):
                raise
            return _hold_for_admin_review(body, store, gcs, drive, settings, None, pdf_limit)
    else:
        data = drive.download_file(body.file_id)

    index_original = True
    skip_reason: str | None = None
    if is_pdf:
        profile = _pdf_text_profile(data)
        if (
            profile.image_dominant
            and body.parser_url
            and settings.parser_option_for("enable_image_ocr", audience)
            and len(data) <= PDF_OCR_MAX_BYTES
            and profile.pages <= PDF_OCR_MAX_PAGES
        ):
            ocr_result = _ingest_pdf_ocr(body, store, gcs, drive, settings, data, audience, path_ctx)
            if ocr_result is not None:
                return ocr_result
        index_original, skip_reason = profile.reason is None, profile.reason
        if not index_original:
            logger.warning(
                "PDF 본문 추출 불가 %s (%s): %s — 사이드카만 색인한다",
                body.file_id,
                name,
                skip_reason,
            )

    pdf_parts: list[bytes] | None = None
    if (
        index_original
        and ext.lower() == ".pdf"
        and len(data) > _effective_limit(settings, ext)
    ):
        try:
            pdf_parts = split_pdf(data, _effective_limit(settings, ext))
            logger.info(
                "oversized PDF split %s: %sB -> %s parts",
                body.file_id, len(data), len(pdf_parts),
            )
        except PdfSplitError as exc:
            logger.warning("PDF split failed %s: %s", body.file_id, exc)
            pdf_parts = None

    if pdf_parts is None and index_original:
        raw_limit = (
            settings.max_gcs_bytes if mime in _SPREADSHEET_COPY_MIMES else None
        )
        gated = _size_gate(
            store, settings, body, len(data), splittable=True, ext=ext, limit=raw_limit
        )
        if gated:
            gated["route"] = "FILE_COPY"
            return gated

    body_text: str | None = None
    if mime in _SPREADSHEET_COPY_MIMES:
        try:
            body_text = xlsx_to_markdown(data)
        except XlsxParseError as exc:
            index_original = False
            skip_reason = f"XLSX_UNREADABLE:{exc}"
            logger.warning(
                "xlsx→md 실패 %s (%s): %s — 사이드카만 색인한다",
                body.file_id, name, exc,
            )
    elif mime in _TEXT_COPY_MIMES:
        try:
            body_text = data.decode("utf-8")
        except UnicodeDecodeError:
            body_text = data.decode("utf-8", errors="replace")
        if mime == "text/html":
            body_text = html_to_text(body_text)

    if body_text is not None:
        md_text = build_breadcrumb_markdown(
            path=path_ctx.path,
            bundle=path_ctx.bundle,
            title=name, source_title=path_ctx.source_title, website_url=path_ctx.website_url,
            body=body_text,
        )
        md_bytes = md_text.encode("utf-8")
        gated = _size_gate(
            store, settings, body, len(md_bytes), splittable=True, ext=".md"
        )
        if gated:
            gated["route"] = "FILE_COPY"
            return gated
        content_hash = sha256_text(md_text)
        if store.should_skip_reindex(body.file_id, content_hash):
            store.touch_modified_time(body.file_id, body.modified_time)
            return {
                "fileId": body.file_id,
                "status": "HASH_UNCHANGED",
                "route": "FILE_COPY",
                "contentHash": content_hash,
                "path": path_ctx.path,
                "bundle": path_ctx.bundle,
            }
        gcs_uri = gcs.upload_source_md(md_text, body.file_id)
        uris = [gcs_uri]
        _drop_stale_sidecar(gcs, settings, body.file_id)
    else:
        if not pdf_parts and index_original:
            gated = _size_gate(store, settings, body, len(data), splittable=True, ext=ext)
            if gated:
                gated["route"] = "FILE_COPY"
                return gated
        sidecar = build_breadcrumb_markdown(
            path=path_ctx.path,
            bundle=path_ctx.bundle,
            title=name, source_title=path_ctx.source_title, website_url=path_ctx.website_url,
        )
        content_hash = sha256_text(f"{sha256_bytes(data)}|{path_ctx.path}|{sidecar}")
        if store.should_skip_reindex(body.file_id, content_hash):
            store.touch_modified_time(body.file_id, body.modified_time)
            return {
                "fileId": body.file_id,
                "status": "HASH_UNCHANGED",
                "route": "FILE_COPY",
                "contentHash": content_hash,
                "path": path_ctx.path,
                "bundle": path_ctx.bundle,
            }
        ctype = body.mime_type or "application/octet-stream"
        if not index_original:
            uris = []
        elif pdf_parts and len(pdf_parts) > 1:
            uris = []
            for i, part in enumerate(pdf_parts, start=1):
                uris.append(
                    gcs.upload_bytes(
                        part,
                        settings.gcs_source_bucket,
                        f"{body.file_id}.part{i}{ext}",
                        content_type=ctype,
                    )
                )
        else:
            uris = [
                gcs.upload_bytes(
                    data,
                    settings.gcs_source_bucket,
                    f"{body.file_id}{ext}",
                    content_type=ctype,
                )
            ]
        uris.append(gcs.upload_source_sidecar_md(sidecar, body.file_id))
        if is_pdf:
            try:
                gcs.delete(gs_uri(settings.gcs_source_bucket, f"{body.file_id}.md"))
            except Exception as exc:  # noqa: BLE001
                logger.debug("이전 OCR 본문 정리 건너뜀 %s: %s", body.file_id, exc)

    store.upsert(
        _state_fields(
            body,
            content_hash=content_hash,
            status=DocStatus.PARSED,
            parse_route=ParseRoute.GCS_COPY,
            source_uri=body.web_view_link or uris[0],
            path_ctx=path_ctx,
            audience=audience,
            error=skip_reason,
        )
    )
    return _gcs_ready(
        body=body,
        route="FILE_COPY",
        parse_route=ParseRoute.GCS_COPY,
        uris=uris,
        content_hash=content_hash,
        path_ctx=path_ctx,
    )


def _drop_stale_sidecar(gcs: GcsClient, settings: Settings, file_id: str) -> None:
    uri = gs_uri(settings.gcs_source_bucket, f"{file_id}.meta.md")
    try:
        gcs.delete(uri)
    except Exception as exc:  # noqa: BLE001
        logger.debug("사이드카 정리 건너뜀 %s: %s", uri, exc)


def _ext_for_mime(mime: str | None) -> str:
    mapping = {
        "application/pdf": ".pdf",
        "text/plain": ".txt",
        "text/markdown": ".md",
        "text/html": ".html",
        "text/csv": ".csv",
        "application/rtf": ".rtf",
        "application/msword": ".doc",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    }
    return mapping.get((mime or "").lower(), ".bin")


# Drive fileId 는 영숫자/-/_ 조합이라 점이 들어갈 수 없다.
_FILE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{10,}$")


def _clean_file_ids(raw_ids: list[str]) -> tuple[list[str], list[str]]:
    ok: list[str] = []
    bad: list[str] = []
    for raw in raw_ids:
        fid = extract_file_id(raw)
        (ok if _FILE_ID_RE.match(fid) else bad).append(fid)
    return list(dict.fromkeys(ok)), bad


def _sync_student_corpus(
    gcs_uris: list[str], file_ids: list[str], settings: Settings, store: DocStateStore
) -> dict[str, Any]:
    ids = sorted(set(file_ids) | {extract_file_id(uri) for uri in gcs_uris})
    with document_mutation(store, settings, ids):
        return _sync_student_corpus_locked(gcs_uris, file_ids, settings, store)


def _sync_student_corpus_locked(
    gcs_uris: list[str], file_ids: list[str], settings: Settings, store: DocStateStore
) -> dict[str, Any]:
    if not settings.audience_split_enabled:
        return {"enabled": False}

    student_uris: list[str] = []
    touched: set[str] = {fid for fid in file_ids if fid}
    for uri in gcs_uris:
        fid = extract_file_id(uri)
        if not _FILE_ID_RE.match(fid):
            logger.warning("학생 코퍼스: fileId 판정 불가라 제외 %s", uri)
            continue
        touched.add(fid)
        state = store.get(fid)
        if state and state.audience == Audience.STUDENT:
            student_uris.append(uri)

    rag = RagEngineClient(settings, corpus_name=settings.rag_corpus_name_student)
    try:
        removed = rag.delete_files_by_ids(sorted(touched))
    except Exception:  # noqa: BLE001
        logger.exception("학생 코퍼스 pre-delete 실패 files=%s", len(touched))
        raise

    outcome = rag.import_from_gcs(student_uris) if student_uris else None
    check_active_mutation()
    if outcome and not outcome.ok:
        logger.error(
            "학생 코퍼스 import 부분 실패 uris=%s imported=%s failed=%s skipped=%s",
            len(student_uris), outcome.imported, outcome.failed, outcome.skipped,
        )
    logger.info(
        "학생 코퍼스 동기화: 대상 %s / 검토 %s URI",
        outcome.imported if outcome else 0,
        len(gcs_uris),
    )
    if outcome is None or outcome.ok:
        _write_rag_mappings(
            state_store=store,
            settings=settings,
            corpus_type="STUDENT",
            corpus_name=settings.rag_corpus_name_student,
            gcs_uris=student_uris,
            file_ids=sorted(touched),
            outcome=outcome or ImportOutcome([], 0, 0, 0),
            clear_without_uri=True,
        )
    return {
        "enabled": True,
        "imported": outcome.imported if outcome else 0,
        "removed": removed,
        "ok": outcome.ok if outcome else True,
    }


def _group_docs_by_drive(
    targets: list[DocState], settings: Settings
) -> list[tuple[Settings, list[DocState]]]:
    if not getattr(settings, "departments", ()):
        return [(settings, targets)]

    groups: dict[str, list[DocState]] = {}
    dropped: list[str] = []
    for doc in targets:
        drive_id = doc.drive_id or ""
        if not drive_id or settings.department_for_drive(drive_id) is None:
            dropped.append(doc.file_id)
            continue
        groups.setdefault(drive_id, []).append(doc)
    if dropped:
        logger.warning(
            "학과 판정 불가로 복구 보류 %s건: %s", len(dropped), dropped[:10]
        )
    return [(settings.for_drive(d), docs) for d, docs in groups.items()]


def _merge_outcomes(a: ImportOutcome, b: ImportOutcome) -> ImportOutcome:
    return ImportOutcome(
        uris=a.uris + b.uris,
        imported=a.imported + b.imported,
        failed=a.failed + b.failed,
        skipped=a.skipped + b.skipped,
        results=a.results + b.results,
        result_sinks=a.result_sinks + b.result_sinks,
    )


def _write_rag_mappings(
    *,
    state_store: DocStateStore,
    settings: Settings,
    corpus_type: str,
    corpus_name: str,
    gcs_uris: list[str],
    file_ids: list[str],
    outcome: ImportOutcome,
    clear_without_uri: bool = False,
) -> dict[str, int | bool]:
    if not getattr(settings, "rag_mapping_write_enabled", False):
        return {"enabled": False, "written": 0, "deferred": 0, "failed": 0}

    try:
        mapping_store = RagFileMappingStore(settings)
    except Exception:
        logger.exception("RAG mapping store initialization failed")
        return {"enabled": True, "written": 0, "deferred": len(file_ids), "failed": 1}

    requested_by_file: dict[str, list[str]] = {}
    for uri in gcs_uris:
        fid = extract_file_id(uri)
        if _FILE_ID_RE.match(fid):
            requested_by_file.setdefault(fid, []).append(uri)

    successful_by_uri: dict[str, list[Any]] = {}
    for result in outcome.results:
        if result.succeeded and result.gcs_uri and result.rag_file_name:
            successful_by_uri.setdefault(result.gcs_uri, []).append(result)

    targets = list(dict.fromkeys([*file_ids, *requested_by_file.keys()]))
    written = 0
    deferred = 0
    failed = 0
    for fid in targets:
        expected = list(dict.fromkeys(requested_by_file.get(fid, [])))
        if not expected:
            if not clear_without_uri:
                continue
            rows: list[RagFileMapping] = []
        else:
            missing = [uri for uri in expected if not successful_by_uri.get(uri)]
            if missing:
                deferred += 1
                logger.warning(
                    "RAG mapping deferred corpus=%s fileId=%s missingSinkRows=%s",
                    corpus_type,
                    fid,
                    len(missing),
                )
                continue
            state = state_store.get(fid)
            generation = str(state.content_hash or "") if state else ""
            rows = [
                RagFileMapping(
                    file_id=fid,
                    corpus_type=corpus_type,
                    corpus_name=corpus_name,
                    rag_file_name=result.rag_file_name,
                    gcs_uri=result.gcs_uri,
                    generation=generation,
                    import_result_sink=result.import_result_sink,
                )
                for uri in expected
                for result in successful_by_uri[uri]
            ]
        try:
            mapping_store.replace_for_corpus(fid, corpus_type, rows)
            written += len(rows)
        except Exception:
            failed += 1
            logger.exception(
                "RAG mapping write failed corpus=%s fileId=%s",
                corpus_type,
                fid,
            )
    return {"enabled": True, "written": written, "deferred": deferred, "failed": failed}


def _mapping_from_existing_rag_file(
    rag_file: Any,
    *,
    corpus_type: str,
    corpus_name: str,
) -> RagFileMapping | None:
    resource_name = str(getattr(rag_file, "name", "") or "").strip()
    display_name = str(getattr(rag_file, "display_name", "") or "").strip()
    source_uri = str(getattr(rag_file, "source_uri", "") or "").strip()
    if not resource_name:
        return None

    identity = source_uri if source_uri.startswith("gs://") else display_name
    file_id = extract_file_id(identity)
    if not _FILE_ID_RE.fullmatch(file_id):
        return None
    # 일부 SDK의 list 응답에는 source_uri가 없다. 직접 삭제에는 resource_name만
    # 필요하므로 빈 URI도 유효하며 mapping_id는 resource_name으로 안정화된다.
    gcs_uri = source_uri if source_uri.startswith("gs://") else ""
    if not gcs_uri and display_name.startswith("gs://"):
        gcs_uri = display_name
    return RagFileMapping(
        file_id=file_id,
        corpus_type=corpus_type,
        corpus_name=corpus_name,
        rag_file_name=resource_name,
        gcs_uri=gcs_uri,
        generation="",
        status="BACKFILLED",
    )


@app.post("/sync/backfill-rag-mappings")
def backfill_rag_mappings(body: BackfillRagMappingsBody) -> dict[str, Any]:
    settings = _settings_for_drive(get_settings(), body.drive_id)
    if not settings.rag_mapping_write_enabled:
        raise HTTPException(409, "RAG mapping write is disabled")

    corpora: list[tuple[str, str]] = [("FACULTY", settings.rag_corpus_name)]
    if settings.audience_split_enabled:
        corpora.append(("STUDENT", settings.rag_corpus_name_student))

    seen_corpora: set[tuple[str, str]] = set()
    summaries: list[dict[str, Any]] = []
    store = None if body.dry_run else RagFileMappingStore(settings)
    for corpus_type, corpus_name in corpora:
        key = (corpus_type, corpus_name)
        if not corpus_name or key in seen_corpora:
            continue
        seen_corpora.add(key)

        try:
            rag_files = RagEngineClient(settings, corpus_name=corpus_name).list_files()
        except RagImportThrottledError as exc:
            raise HTTPException(status_code=429, detail=str(exc)) from exc
        mappings = [
            mapping
            for item in rag_files
            if (
                mapping := _mapping_from_existing_rag_file(
                    item,
                    corpus_type=corpus_type,
                    corpus_name=corpus_name,
                )
            )
            is not None
        ]
        written = 0 if store is None else store.upsert_many(mappings)
        summaries.append(
            {
                "corpusType": corpus_type,
                "corpusName": corpus_name,
                "listed": len(rag_files),
                "mappable": len(mappings),
                "skipped": len(rag_files) - len(mappings),
                "written": written,
            }
        )

    return {
        "driveId": body.drive_id,
        "dryRun": body.dry_run,
        "corpora": summaries,
        "totals": {
            "listed": sum(row["listed"] for row in summaries),
            "mappable": sum(row["mappable"] for row in summaries),
            "skipped": sum(row["skipped"] for row in summaries),
            "written": sum(row["written"] for row in summaries),
        },
    }


def _settings_for_drive(base: Settings, drive_id: str | None) -> Settings:
    if not getattr(base, "departments", ()):
        return base
    if not drive_id:
        raise UnknownDriveError("driveId 없이 학과를 정할 수 없다")
    return base.for_drive(drive_id)


def _split_by_drive(
    store: DocStateStore,
    gcs_uris: list[str],
    file_ids: list[str],
    settings: Settings,
) -> list[tuple[Settings, list[str], list[str]]]:
    if not getattr(settings, "departments", ()):
        return [(settings, gcs_uris, file_ids)]

    groups: dict[str, tuple[list[str], list[str]]] = {}
    dropped: list[str] = []

    def _bucket(fid: str) -> tuple[list[str], list[str]] | None:
        state = store.get(fid)
        drive_id = state.drive_id if state else ""
        if not drive_id or settings.department_for_drive(drive_id) is None:
            dropped.append(fid)
            return None
        return groups.setdefault(drive_id, ([], []))

    for uri in gcs_uris:
        fid = extract_file_id(uri)
        g = _bucket(fid)
        if g is not None:
            g[0].append(uri)
    for fid in file_ids:
        g = _bucket(fid)
        if g is not None and fid not in g[1]:
            g[1].append(fid)

    if dropped:
        logger.warning(
            "학과 판정 불가로 색인 보류 %s건: %s", len(dropped), sorted(set(dropped))[:10]
        )
    return [
        (settings.for_drive(drive_id), uris, ids)
        for drive_id, (uris, ids) in groups.items()
    ]


def _import_and_mark(
    store: DocStateStore,
    gcs_uris: list[str],
    file_ids: list[str],
    *,
    rag: Any | None = None,
    mark_indexed: bool = True,
    mapping_settings: Settings | None = None,
    corpus_type: str = "FACULTY",
) -> ImportOutcome:
    settings = mapping_settings or getattr(rag, "settings", None) or get_settings()
    ids = sorted(set(file_ids) | {extract_file_id(uri) for uri in gcs_uris})
    with document_mutation(store, settings, ids):
        return _import_and_mark_locked(
            store, gcs_uris, file_ids, rag=rag, mark_indexed=mark_indexed,
            mapping_settings=mapping_settings, corpus_type=corpus_type,
        )


def _import_and_mark_locked(
    store: DocStateStore, gcs_uris: list[str], file_ids: list[str], *,
    rag: Any | None = None, mark_indexed: bool = True,
    mapping_settings: Settings | None = None, corpus_type: str = "FACULTY",
) -> ImportOutcome:
    client = rag if rag is not None else RagEngineClient()
    client.delete_files_by_ids(list(dict.fromkeys(file_ids)))
    check_active_mutation()
    outcome = client.import_from_gcs(gcs_uris)
    check_active_mutation()
    if not outcome.ok:
        logger.error(
            "RAG import 부분 실패 — INDEXED 로 올리지 않는다 "
            "files=%s uris=%s imported=%s failed=%s skipped=%s",
            len(dict.fromkeys(file_ids)),
            len(gcs_uris),
            outcome.imported,
            outcome.failed,
            outcome.skipped,
        )
        return outcome
    if mapping_settings is not None and getattr(
        mapping_settings, "rag_mapping_write_enabled", False
    ):
        _write_rag_mappings(
            state_store=store,
            settings=mapping_settings,
            corpus_type=corpus_type,
            corpus_name=str(
                getattr(client, "corpus_name", mapping_settings.rag_corpus_name)
            ),
            gcs_uris=gcs_uris,
            file_ids=file_ids,
            outcome=outcome,
        )
    if mark_indexed:
        _mark_indexed_states(store, file_ids)
    return outcome


def _mark_indexed_states(store: DocStateStore, file_ids: list[str]) -> None:
    for fid in dict.fromkeys(file_ids):
        existing = store.get(fid)
        if existing:
            existing.status = DocStatus.INDEXED
            store.upsert(existing)
        else:
            store.mark_indexed(fid)


@app.post("/sync/index-gcs")
def index_gcs(body: IndexGcsBody) -> dict[str, Any]:
    if not body.gcs_uris:
        return {"imported": [], "count": 0, "status": "EMPTY"}

    settings = get_settings()
    store = DocStateStore()

    ids = sorted(set(body.file_ids) | {extract_file_id(uri) for uri in body.gcs_uris})
    with document_mutation(store, settings, ids):
        return _index_gcs_locked(body, settings, store)


def _index_gcs_locked(body: IndexGcsBody, settings: Settings, store: DocStateStore) -> dict[str, Any]:
    file_ids, bad_ids = _clean_file_ids(body.file_ids)
    if bad_ids:
        logger.warning(
            "index-gcs dropped %s malformed fileIds: %s", len(bad_ids), bad_ids[:10]
        )

    outcome = ImportOutcome(uris=[], imported=0, failed=0, skipped=0)
    student: dict[str, Any] = {"enabled": False}
    for dept_settings, uris, ids in _split_by_drive(
        store, body.gcs_uris, file_ids, settings
    ):
        split_enabled = dept_settings.audience_split_enabled
        try:
            part = _import_and_mark(
                store,
                uris,
                ids,
                rag=RagEngineClient(dept_settings),
                mark_indexed=not split_enabled,
                mapping_settings=dept_settings,
                corpus_type="FACULTY",
            )
            student = _sync_student_corpus(uris, ids, dept_settings, store)
        except RagImportThrottledError as exc:
            raise HTTPException(status_code=429, detail=str(exc)) from exc
        outcome = _merge_outcomes(outcome, part)

        if split_enabled:
            if not bool(student.get("ok", True)):
                raise HTTPException(
                    status_code=502,
                    detail="student corpus import incomplete",
                )
            if part.ok:
                _mark_indexed_states(store, ids)

    return {
        "imported": outcome.uris,
        "count": outcome.imported,
        "failed": outcome.failed,
        "skipped": outcome.skipped,
        "ok": outcome.ok,
        "droppedFileIds": len(bad_ids),
        "student": student,
        "status": "INDEXED" if outcome.ok else "PARTIAL",
    }


def _index_job_refs(settings: Settings, job_id: str):
    store = DocStateStore(settings)
    job = store._db.collection(settings.sync_job_collection).document(job_id)  # noqa: SLF001
    return store, job, job.collection("parts")


def _finalize_index_job(settings: Settings, job_id: str) -> dict[str, Any]:
    from google.cloud import firestore

    store, job_ref, parts_ref = _index_job_refs(settings, job_id)

    @firestore.transactional
    def finalize(txn):
        snap = job_ref.get(transaction=txn)
        if not snap.exists:
            raise HTTPException(404, "index job not found")
        job = snap.to_dict() or {}
        if job.get("status") in {"DONE", "FAILED"}:
            return {"status": job["status"], **(job.get("result") or {})}
        if job.get("deadlineAt") and job["deadlineAt"] <= datetime.now(UTC):
            error = "index tasks did not complete before deadline"
            txn.set(job_ref, {"status": "FAILED", "error": error}, merge=True)
            return {"status": "FAILED", "error": error}
        expected = job.get("expectedParts") or []
        parts = [parts_ref.document(pid).get(transaction=txn).to_dict() or {} for pid in expected]
        if not expected or any(part.get("status") != "DONE" for part in parts):
            return {"status": "RUNNING"}
        if job.get("routingVersion") != routing_version(
            _settings_for_drive(settings, job.get("driveId"))
        ):
            error = "department routing changed; enqueue again"
            txn.set(job_ref, {"status": "FAILED", "error": error}, merge=True)
            return {"status": "FAILED", "error": error}
        try:
            check_versions(store, job.get("fileVersions") or {}, txn)
        except StaleIndexTask as exc:
            txn.set(job_ref, {"status": "FAILED", "error": str(exc)}, merge=True)
            return {"status": "FAILED", "error": str(exc)}
        for fid in job["fileVersions"]:
            for name in mutation_lock_names(fid):
                if store._tokens.document(name).get(transaction=txn).exists:
                    return {"status": "RUNNING"}
        faculty = [part for part in parts if part.get("audience") == "FACULTY"]
        result = {
            "count": sum(int(part.get("count") or 0) for part in faculty),
            "failed": sum(int(part.get("failed") or 0) for part in parts),
            "skipped": sum(int(part.get("skipped") or 0) for part in parts),
            "student": next(
                (part.get("result") for part in parts if part.get("audience") == "STUDENT"),
                {"enabled": False},
            ),
        }
        failed_ids = [fid for fid in job["fileVersions"] if any(
            (part.get("fileResults", {}).get(fid) or {}).get("status") == "FAILED"
            for part in parts)]
        completed_ids = [fid for fid in job["fileVersions"] if fid not in failed_ids]
        deferred_uris = [uri for uri in job.get("gcsUris", []) if extract_file_id(uri) in failed_ids]
        result.update(failedFileIds=failed_ids, completedFileIds=completed_ids,
                      failed=len(failed_ids), deferredUris=len(deferred_uris),
                      count=len(job.get("gcsUris", [])) - len(deferred_uris),
                      partial=bool(failed_ids))
        for fid in failed_ids:
            txn.set(store._col.document(fid).collection("index_failures").document(job_id), {
                "fileId": fid, "driveId": job["driveId"], "jobId": job_id,
                "status": "PENDING", "fileVersion": job["fileVersions"][fid],
                "routingVersion": job.get("routingVersion"),
                "gcsUris": [uri for uri in deferred_uris if extract_file_id(uri) == fid],
                "corpora": {part.get("audience", "UNKNOWN"): (part.get("fileResults") or {}).get(fid, {}) for part in parts},
                "createdAt": firestore.SERVER_TIMESTAMP,
            })
        for fid in completed_ids:
            txn.update(store._col.document(fid), {
                "status": "INDEXED", "lastSyncedAt": firestore.SERVER_TIMESTAMP,
            })
        txn.set(
            job_ref,
            {
                "status": "DONE",
                "result": result,
                "completedAt": firestore.SERVER_TIMESTAMP,
                "updatedAt": firestore.SERVER_TIMESTAMP,
            },
            merge=True,
        )
        return {"status": "DONE", **result}

    return finalize(store._db.transaction())


@app.post("/sync/index-gcs-async", status_code=202)
def index_gcs_async(body: IndexGcsBody) -> dict[str, Any]:
    from google.cloud import firestore

    settings = get_settings()
    if not settings.cloud_tasks_enabled:
        raise HTTPException(409, "Cloud Tasks indexing is disabled")
    if not body.gcs_uris:
        return {"status": "DONE", "count": 0, "jobId": ""}
    if not body.drive_id:
        raise HTTPException(422, "driveId is required for async indexing")

    dept_settings = _settings_for_drive(settings, body.drive_id)
    file_ids, bad_ids = _clean_file_ids(body.file_ids)
    if bad_ids:
        raise HTTPException(422, f"malformed fileIds: {bad_ids[:5]}")
    if not file_ids or {extract_file_id(uri) for uri in body.gcs_uris} != set(file_ids):
        raise HTTPException(422, "gcsUris must match the complete fileIds set")
    if any(not uri.startswith(f"gs://{dept_settings.gcs_source_bucket}/") for uri in body.gcs_uris):
        raise HTTPException(422, "gcsUris must belong to this department's source bucket")
    validation_store = DocStateStore(settings)
    versions: dict[str, str] = {}
    for fid in file_ids:
        state = validation_store.get(fid)
        if not state or state.drive_id != body.drive_id:
            raise HTTPException(409, f"file missing or belongs to another drive: {fid}")
        if state.status not in {DocStatus.PARSED, DocStatus.INDEXED}:
            raise HTTPException(409, f"file is not ready for indexing: {fid}")
        versions[fid] = document_version(state)

    job_id = uuid.uuid4().hex
    parts: list[dict[str, Any]] = [
        {
            "partId": "faculty",
            "audience": "FACULTY",
            "queue": settings.task_queue_faculty,
        }
    ]
    if dept_settings.audience_split_enabled:
        parts.append(
            {
                "partId": "student",
                "audience": "STUDENT",
                "queue": settings.task_queue_student,
            }
        )

    job_store, job_ref, parts_ref = _index_job_refs(settings, job_id)
    deadline = datetime.now(UTC) + timedelta(seconds=settings.index_job_timeout_seconds)
    batch = job_store._db.batch()  # noqa: SLF001
    batch.set(
        job_ref,
        {
            "jobId": job_id,
            "kind": "INDEX_GCS",
            "status": "RUNNING",
            "driveId": body.drive_id,
            "fileIds": file_ids,
            "fileVersions": versions,
            "routingVersion": routing_version(dept_settings),
            "gcsUris": body.gcs_uris,
            "requestedUris": len(body.gcs_uris),
            "expectedParts": [part["partId"] for part in parts],
            "deadlineAt": deadline,
            "createdAt": firestore.SERVER_TIMESTAMP,
            "updatedAt": firestore.SERVER_TIMESTAMP,
        },
    )
    for part in parts:
        batch.set(
            parts_ref.document(part["partId"]),
            {
                "partId": part["partId"],
                "audience": part["audience"],
                "status": "QUEUED",
                "updatedAt": firestore.SERVER_TIMESTAMP,
            },
        )
    batch.commit()

    producer = IndexTaskQueue(settings)
    try:
        for part in parts:
            producer.enqueue(
                queue=part["queue"],
                task_id=f"{job_id}-{part['partId']}",
                payload={
                    "jobId": job_id,
                    "partId": part["partId"],
                    "driveId": body.drive_id,
                    "audience": part["audience"],
                    "gcsUris": body.gcs_uris,
                    "fileIds": file_ids,
                    "fileVersions": versions,
                    "routingVersion": routing_version(dept_settings),
                },
            )
    except Exception as exc:
        job_ref.set(
            {
                "status": "FAILED",
                "error": f"task enqueue failed: {exc}"[:2000],
                "updatedAt": firestore.SERVER_TIMESTAMP,
            },
            merge=True,
        )
        raise HTTPException(503, "failed to enqueue index tasks") from exc
    return {
        "jobId": job_id,
        "status": "RUNNING",
        "parts": [part["partId"] for part in parts],
        "requestedUris": len(body.gcs_uris),
    }


def _index_file_groups(
    file_ids: list[str], uris_by_id: dict[str, list[str]]
) -> list[list[str]]:
    groups: list[list[str]] = []
    current: list[str] = []
    size = 0
    for fid in file_ids:
        count = len(uris_by_id.get(fid) or [])
        if count == 0 or count > MAX_IMPORT_URIS:
            groups.append([fid])
            continue
        if current and size + count > MAX_IMPORT_URIS:
            groups.append(current)
            current, size = [], 0
        current.append(fid)
        size += count
    if current:
        groups.append(current)
    return groups


def _index_group(
    audience: str, file_ids: list[str], uris_by_id: dict[str, list[str]],
    store: DocStateStore, dept_settings: Settings, rag: Any,
) -> dict[str, dict[str, Any]]:
    uris = [uri for fid in file_ids for uri in uris_by_id.get(fid) or []]
    single = len(file_ids) == 1
    if audience == "FACULTY":
        outcome = _import_and_mark(
            store, uris, file_ids, rag=rag, mark_indexed=False,
            mapping_settings=dept_settings, corpus_type="FACULTY",
        )
        ok = bool(uris) and outcome.ok
        items = {
            fid: ({"count": outcome.imported, "failed": outcome.failed, "skipped": outcome.skipped}
                  if single else
                  {"count": len(uris_by_id[fid]) if ok else 0, "failed": 0, "skipped": 0})
            for fid in file_ids
        }
    else:
        student = _sync_student_corpus(uris, file_ids, dept_settings, store)
        ok = bool(student.get("ok", True))
        items = {}
        for index, fid in enumerate(file_ids):
            share = single or index == 0
            items[fid] = {
                "count": int(student.get("imported") or 0) if share else 0,
                "failed": int(student.get("failed") or 0) if share else 0,
                "skipped": int(student.get("skipped") or 0) if share else 0,
                "result": student if share else {"enabled": student.get("enabled"), "ok": ok,
                                                 "imported": 0, "removed": 0},
            }
    for fid, item in items.items():
        item["status"] = "DONE" if ok else "FAILED"
        item["error"] = None if ok else f"{audience.lower()} import incomplete fileId={fid}"
    return items


@app.post("/sync/index-gcs-task")
def index_gcs_task(body: IndexGcsTaskBody) -> dict[str, Any]:
    settings = get_settings()
    if not settings.cloud_tasks_enabled:
        raise HTTPException(409, "Cloud Tasks indexing is disabled")
    if body.audience not in {"FACULTY", "STUDENT"}:
        raise HTTPException(422, "audience must be FACULTY or STUDENT")
    dept_settings = _settings_for_drive(settings, body.drive_id)
    store, job_ref, parts_ref = _index_job_refs(settings, body.job_id)
    part_ref = parts_ref.document(body.part_id)
    job = job_ref.get().to_dict() or {}
    part = part_ref.get().to_dict() or {}
    if not job or not part:
        raise HTTPException(404, "index job/part not found")
    if part.get("status") == "DONE":
        return {**_finalize_index_job(settings, body.job_id), "idempotent": True}
    if job.get("status") == "FAILED":
        return {"status": "FAILED", "error": job.get("error")}
    if not body.file_versions or not job.get("fileVersions"):
        job_ref.set({"status": "FAILED", "error": "unversioned task; enqueue again"}, merge=True)
        return {"status": "FAILED"}
    if (
        body.drive_id != job.get("driveId")
        or body.audience != part.get("audience")
        or body.file_ids != job.get("fileIds")
        or body.gcs_uris != job.get("gcsUris")
        or body.file_versions != job.get("fileVersions")
        or body.routing_version != job.get("routingVersion")
    ):
        raise HTTPException(409, "task payload does not match persisted job")
    if body.routing_version != routing_version(dept_settings):
        job_ref.set(
            {"status": "FAILED", "error": "unversioned task or routing changed; enqueue again"},
            merge=True,
        )
        return {"status": "FAILED"}
    lease = MutationLease(
        store, body.file_ids, versions=body.file_versions, job_ref=job_ref, part_ref=part_ref
    )
    claimed = False
    try:
        claimed = lease.claim()
        if not claimed:
            return {**_finalize_index_job(settings, body.job_id), "idempotent": True}
        lease.pin()
        with activate(lease):
            file_results = dict((part_ref.get().to_dict() or {}).get("fileResults") or {})
            uris_by_id = {
                fid: [uri for uri in body.gcs_uris if extract_file_id(uri) == fid]
                for fid in body.file_ids
            }
            pending = [fid for fid in body.file_ids
                       if (file_results.get(fid) or {}).get("status") != "DONE"]
            rag = RagEngineClient(dept_settings) if body.audience == "FACULTY" else None
            for group in _index_file_groups(pending, uris_by_id):
                lease.checkpoint_files({fid: {"status": "RUNNING", "error": None} for fid in group})
                items = _index_group(body.audience, group, uris_by_id, store, dept_settings, rag)
                if len(group) > 1 and any(item["status"] != "DONE" for item in items.values()):
                    logger.warning("색인 묶음 부분 실패 — 파일별로 다시 처리 files=%s", len(group))
                    items = {}
                    for fid in group:
                        items.update(_index_group(
                            body.audience, [fid], uris_by_id, store, dept_settings, rag
                        ))
                lease.checkpoint_files(items)
                file_results.update(items)
            failed_ids = [fid for fid in body.file_ids
                          if (file_results.get(fid) or {}).get("status") != "DONE"]
            result = {
                "count": sum(int(item.get("count") or 0) for item in file_results.values()),
                "failed": sum(int(item.get("failed") or 0) for item in file_results.values()),
                "skipped": sum(int(item.get("skipped") or 0) for item in file_results.values()),
                "fileResults": file_results,
                "failedFileIds": failed_ids,
            }
            if body.audience == "STUDENT":
                result["result"] = {
                    "enabled": True, "ok": True, "imported": result["count"],
                    "removed": sum(int((item.get("result") or {}).get("removed") or 0)
                                   for item in file_results.values()),
                }
            lease.finish({"audience": body.audience, **result})
        return _finalize_index_job(settings, body.job_id)
    except MutationOwnershipLost as exc:
        if claimed:
            lease.finish(error=exc)
        raise HTTPException(409, "task ownership changed; retry later") from exc
    except StaleIndexTask as exc:
        if claimed:
            lease.finish(error=exc)
        job_ref.set({"status": "FAILED", "error": str(exc)[:2000]}, merge=True)
        return {"status": "FAILED", "error": str(exc)}
    except Exception as exc:
        if claimed:
            lease.finish(error=exc)
        raise


@app.get("/sync/index-jobs/{job_id}")
def index_job_status(job_id: str) -> dict[str, Any]:
    settings = get_settings()
    _finalize_index_job(settings, job_id)
    _, job_ref, parts_ref = _index_job_refs(settings, job_id)
    snap = job_ref.get()
    if not snap.exists:
        raise HTTPException(404, "index job not found")
    data = snap.to_dict() or {}
    status = str(data.get("status") or "RUNNING")
    parts = []
    for part_id in data.get("expectedParts") or []:
        part = parts_ref.document(str(part_id)).get()
        parts.append(part.to_dict() or {"partId": part_id, "status": "MISSING"})
    return {
        "jobId": job_id,
        "status": status,
        "requestedUris": int(data.get("requestedUris") or 0),
        "result": data.get("result"),
        "error": data.get("error"),
        "parts": parts,
    }


class ReindexPendingBody(BaseModel):
    limit: int = Field(default=200, ge=1, le=2000)
    # URI 개수 기준이다(문서 수 아님 — 아래 루프의 len(pending_uris) 비교).
    # rag.import_files 는 호출 1회당 URI 25개까지만 받고(MAX_IMPORT_URIS),
    # 호출 지연이 URI 수와 거의 무관하게 ~21초다. 즉 **호출 횟수가 곧 시간**이라
    # 한 번에 최대한 담아야 한다. 실측(1,211건 재색인): 10 이면 ~140회 42분.
    #
    # 상한이 25인데 기본값을 24로 두는 이유: 문서 하나가 URI 를 최대 2개
    # 만든다(본문 + .meta.md, 실측 892건이 1개 / 317건이 2개). 임계값이 25면
    # pending 이 24일 때 2-URI 문서가 와서 26이 되고, 25+1 로 쪼개져 호출이
    # 한 번 더 든다. 그 1개짜리 호출도 21초를 그대로 먹는다.
    # 24면 pending 이 23 이하이므로 2를 더해도 25 — 항상 한 번에 끝난다.
    index_batch_size: int = Field(default=24, alias="indexBatchSize", ge=1, le=25)
    force: bool = False
    background: bool = False
    job_id: str | None = Field(default=None, alias="jobId")

    model_config = {"populate_by_name": True}


def _job_set(job_id: str, **fields: Any) -> None:
    from google.cloud import firestore

    store = DocStateStore()
    store._db.collection(get_settings().sync_job_collection).document(job_id).set(  # noqa: SLF001
        {"jobId": job_id, "updatedAt": firestore.SERVER_TIMESTAMP, **fields},
        merge=True,
    )


@app.get("/sync/jobs/{job_id}")
def job_status(job_id: str) -> dict[str, Any]:
    store = DocStateStore()
    jobs = store._db.collection(get_settings().sync_job_collection)  # noqa: SLF001
    snap = jobs.document(job_id).get()
    if not snap.exists:
        raise HTTPException(status_code=404, detail=f"unknown job: {job_id}")
    return snap.to_dict() or {}


_INDEXABLE_SUFFIXES = (
    ".md",
    ".meta.md",
    ".pdf",
    ".txt",
    ".html",
    ".doc",
    ".docx",
    ".pptx",
    ".rtf",
    ".xls",
    ".xlsx",
    ".csv",
)


def _source_uris_for_file(
    settings: Settings, file_id: str, gcs: GcsClient | None = None
) -> list[str]:
    client = gcs or GcsClient(settings)
    uris: list[str] = []
    for name in client.list_blob_names_for_file(settings.gcs_source_bucket, file_id):
        lower = name.lower()
        if any(lower.endswith(suf) for suf in _INDEXABLE_SUFFIXES):
            uris.append(gs_uri(settings.gcs_source_bucket, name))
    return uris


@app.post("/sync/reindex-pending")
def reindex_pending(
    body: ReindexPendingBody, tasks: BackgroundTasks
) -> dict[str, Any]:
    if body.background:
        job_id = f"reindex-{uuid.uuid4().hex[:12]}"
        _job_set(job_id, status="RUNNING", mode="reindex-pending",
                 limit=body.limit, force=body.force)
        inner = body.model_copy(update={"background": False, "job_id": job_id})
        tasks.add_task(_run_reindex_job, job_id, inner)
        return {"jobId": job_id, "status": "RUNNING",
                "statusUrl": f"/sync/jobs/{job_id}"}
    return _reindex_pending_sync(body)


def _run_reindex_job(job_id: str, body: ReindexPendingBody) -> None:
    try:
        result = _reindex_pending_sync(body)
        _job_set(job_id, status="DONE", result=result, totals=result.get("totals"))
        logger.info("reindex job %s done: %s", job_id, result.get("totals"))
    except Exception as exc:  # noqa: BLE001
        logger.exception("reindex job %s failed", job_id)
        _job_set(job_id, status="FAILED", error=str(exc)[:1000])


def _reindex_group(
    body: ReindexPendingBody,
    settings: Settings,
    store: DocStateStore,
    targets: list[DocState],
    totals: dict[str, int],
) -> None:
    gcs = GcsClient(settings)
    pending_uris: list[str] = []
    pending_ids: list[str] = []

    rag = RagEngineClient(settings)

    def flush() -> None:
        nonlocal pending_uris, pending_ids
        if not pending_uris:
            return
        uris, ids = pending_uris, pending_ids
        uniq_ids = list(dict.fromkeys(ids))
        outcome = _import_and_mark(store, uris, uniq_ids, rag=rag)
        _sync_student_corpus(uris, uniq_ids, settings, store)
        if outcome.ok:
            for fid in uniq_ids:
                try:
                    store.clear_dlq(fid)
                except Exception:  # noqa: BLE001
                    logger.exception("failed to clear recovered DLQ entry: %s", fid)
        totals["indexed"] += outcome.imported
        totals["failed"] += max(0, len(uris) - outcome.imported)
        pending_uris, pending_ids = [], []

    resolved: list[tuple[str, list[str]]] = []
    for doc in targets:
        uris = _source_uris_for_file(settings, doc.file_id, gcs)
        if not uris:
            totals["skippedNoUri"] += 1
            totals["failed"] += 1
            try:
                store.enqueue_dlq(
                    doc.file_id,
                    "reindex_no_source_uri",
                    driveId=doc.drive_id,
                    name=doc.name,
                    mimeType=doc.mime_type,
                    modifiedTime=doc.modified_time,
                    sourceUri=doc.source_uri,
                )
            except Exception:  # noqa: BLE001
                logger.exception(
                    "failed to enqueue no-URI document for ingest retry: %s",
                    doc.file_id,
                )
            continue
        totals["withUris"] += 1
        resolved.append((doc.file_id, uris))

    for file_id, uris in resolved:
        pending_uris.extend(uris)
        pending_ids.extend([file_id] * len(uris))
        if len(pending_uris) >= body.index_batch_size:
            try:
                flush()
            except Exception:  # noqa: BLE001
                logger.exception("reindex-pending flush failed")
                totals["failed"] += len(pending_uris)
                pending_uris, pending_ids = [], []
            if body.job_id:
                _job_set(body.job_id, status="RUNNING", totals=dict(totals))

    try:
        flush()
    except Exception:  # noqa: BLE001
        logger.exception("reindex-pending final flush failed")
        totals["failed"] += len(pending_uris)
        pending_uris, pending_ids = [], []



def _reindex_pending_sync(body: ReindexPendingBody) -> dict[str, Any]:
    base_settings = get_settings()
    store = DocStateStore()

    targets: list[DocState] = []
    if body.force:
        targets.extend(
            store.list_by_status(
                DocStatus.PARSED,
                limit=body.limit,
                cursor_key="reindex-pending",
            )
        )
        remain = body.limit - len(targets)
        if remain > 0:
            targets.extend(
                store.list_by_status(
                    DocStatus.INDEXED,
                    limit=remain,
                    cursor_key="reindex-pending",
                )
            )
    else:
        targets = store.list_by_status(
            DocStatus.PARSED,
            limit=body.limit,
            cursor_key="reindex-pending",
        )

    totals = {
        "candidates": len(targets),
        "withUris": 0,
        "indexed": 0,
        "skippedNoUri": 0,
        "failed": 0,
    }

    for dept_settings, group in _group_docs_by_drive(targets, base_settings):
        _reindex_group(body, dept_settings, store, group, totals)

    logger.info("reindex-pending done totals=%s", totals)
    return {
        "mode": "reindex-pending",
        "totals": totals,
        "ok": totals["failed"] == 0,
    }


class RetryFailedBody(BaseModel):
    limit: int = Field(default=100, ge=1, le=1000)
    parser_url: str = Field(default="", alias="parserUrl")
    max_attempts: int = Field(default=3, alias="maxAttempts", ge=1, le=10)
    index_batch_size: int = Field(default=10, alias="indexBatchSize", ge=1, le=50)

    model_config = {"populate_by_name": True}


def _drive_link(source_uri: str | None) -> str | None:
    uri = (source_uri or "").strip()
    return uri if uri.startswith(("http://", "https://")) else None


@app.post("/sync/retry-failed")
def retry_failed(body: RetryFailedBody) -> dict[str, Any]:
    store = DocStateStore()
    targets = store.list_by_status(
        DocStatus.FAILED,
        limit=body.limit,
        cursor_key="retry-failed",
    )

    base_settings = get_settings() if targets else None
    drive = DriveClient() if targets else None

    _dept_cache: dict[str, tuple[Settings, Any]] = {}

    def _dept_ctx(drive_id: str) -> tuple[Settings, Any]:
        key = drive_id or ""
        if key not in _dept_cache:
            dept_settings = _settings_for_drive(base_settings, drive_id)
            _dept_cache[key] = (dept_settings, GcsClient(dept_settings))
        return _dept_cache[key]

    totals = {
        "candidates": len(targets),
        "retried": 0,
        "recovered": 0,
        "stillFailed": 0,
        "exhausted": 0,
        "indexed": 0,
    }
    pending_uris: list[str] = []
    pending_ids: list[str] = []

    def flush() -> None:
        nonlocal pending_uris, pending_ids
        if not pending_uris:
            return
        uris, ids = pending_uris, pending_ids
        uniq_ids = list(dict.fromkeys(ids))
        idx = index_gcs(IndexGcsBody(gcsUris=uris, fileIds=uniq_ids))
        indexed = int(idx.get("count", 0))
        if indexed != len(uris):
            raise RuntimeError(
                f"retry index count mismatch: requested={len(uris)} indexed={indexed}"
            )
        for fid in uniq_ids:
            try:
                store.clear_dlq(fid)
            except Exception:  # noqa: BLE001
                logger.exception("failed to clear recovered DLQ entry: %s", fid)
        totals["recovered"] += len(uniq_ids)
        totals["indexed"] += indexed
        pending_uris, pending_ids = [], []

    for doc in targets:
        if is_empty_ocr_error(getattr(doc, "error", None), doc.mime_type):
            store.upsert(replace(doc, status=DocStatus.SKIPPED, error="OCR_EMPTY_TEXT"))
            store.clear_dlq(doc.file_id)
            totals["held"] = totals.get("held", 0) + 1
            continue
        if store.get_dlq_attempts(doc.file_id) >= body.max_attempts:
            totals["exhausted"] += 1
            continue

        try:
            settings, gcs = _dept_ctx(doc.drive_id)
        except UnknownDriveError:
            logger.warning("학과 판정 불가로 회수 보류: %s", doc.file_id)
            totals["stillFailed"] += 1
            continue

        store.record_dlq_attempt(doc.file_id)
        totals["retried"] += 1
        try:
            res = _ingest_with(
                IngestBody(
                    fileId=doc.file_id,
                    driveId=doc.drive_id,
                    name=doc.name,
                    mimeType=doc.mime_type,
                    modifiedTime=doc.modified_time,
                    webViewLink=_drive_link(doc.source_uri),
                    parserUrl=body.parser_url,
                ),
                store=store,
                settings=settings,
                gcs=gcs,
                drive=drive,
            )
        except Exception:  # noqa: BLE001
            logger.exception("retry-failed ingest raised: %s", doc.file_id)
            totals["stillFailed"] += 1
            continue

        status = res.get("status")
        if status == "GCS_READY":
            uris = list(res.get("gcsUris") or [])
            if not uris and res.get("gcsUri"):
                uris = [res["gcsUri"]]
            if not uris:
                logger.error("retry-failed got GCS_READY without URI: %s", doc.file_id)
                totals["stillFailed"] += 1
                continue
            pending_uris.extend(uris)
            pending_ids.extend([doc.file_id] * len(uris))
            if len(pending_uris) >= body.index_batch_size:
                try:
                    flush()
                except Exception:  # noqa: BLE001
                    logger.exception("retry-failed flush failed")
                    totals["stillFailed"] += len(set(pending_ids))
                    pending_uris, pending_ids = [], []
        elif status in ("SKIPPED", "UNCHANGED", "HASH_UNCHANGED"):
            totals["recovered"] += 1
            store.clear_dlq(doc.file_id)
        else:
            totals["stillFailed"] += 1

    try:
        flush()
    except Exception:  # noqa: BLE001
        logger.exception("retry-failed final flush failed")
        totals["stillFailed"] += len(set(pending_ids))
        pending_uris, pending_ids = [], []

    if totals["exhausted"]:
        logger.warning(
            "retry-failed parked %s document(s) at maxAttempts=%s — manual review needed",
            totals["exhausted"],
            body.max_attempts,
        )
    logger.info("retry-failed done totals=%s", totals)
    return {
        "mode": "retry-failed",
        "totals": totals,
        "ok": totals["stillFailed"] == 0,
        "parked": totals["exhausted"],
    }


@app.post("/sync/delete")
def delete_file(body: DeleteBody) -> dict[str, Any]:
    store = DocStateStore()

    settings = get_settings()
    if getattr(settings, "departments", ()):
        drive_id = body.drive_id or ""
        if not drive_id:
            state = store.get(body.file_id)
            drive_id = state.drive_id if state else ""
        settings = _settings_for_drive(settings, drive_id)
    with document_mutation(store, settings, [body.file_id]):
        return _delete_file_locked(body, settings, store)


def _delete_file_locked(body: DeleteBody, settings: Settings, store: DocStateStore) -> dict[str, Any]:
    gcs = GcsClient(settings)
    try:
        ok = RagEngineClient(settings).delete_by_file_id(body.file_id)
        if settings.audience_split_enabled:
            RagEngineClient(
                settings, corpus_name=settings.rag_corpus_name_student
            ).delete_by_file_id(body.file_id)
    except RagImportThrottledError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc

    failures: list[Exception] = []
    counts = {"source": 0, "hwpOriginal": 0}
    for bucket, kind in (
        (settings.gcs_source_bucket, "source"),
        (settings.gcs_hwp_original_bucket, "hwpOriginal"),
    ):
        if not bucket:
            continue
        try:
            counts[kind] = len(gcs.delete_for_file(bucket, body.file_id))
        except Exception as exc:
            failures.append(exc)
            logger.exception("delete %s GCS cleanup failed: %s", kind, body.file_id)

    store.mark_deleted(body.file_id)
    logger.info(
        "deleted fileId=%s corpus=%s source=%s hwpOriginal=%s",
        body.file_id, ok, counts["source"], counts["hwpOriginal"],
    )
    if failures:
        raise HTTPException(
            status_code=500,
            detail=f"gcs cleanup failed for {body.file_id}: {failures[0]}"[:500],
        ) from failures[0]

    return {
        "fileId": body.file_id,
        "deleted": ok,
        "gcsDeleted": counts["source"] + counts["hwpOriginal"],
        "status": DocStatus.DELETED.value,
        "sourceDeleted": counts["source"],
        "hwpOriginalDeleted": counts["hwpOriginal"],
    }


@app.post("/sync/reconcile")
def reconcile(body: ReconcileBody) -> dict[str, Any]:
    accounted = (
        body.gcs_uploaded
        + body.failed
        + body.parked
        + body.skipped
        + body.deleted
        + body.unchanged
    )
    listed = body.listed - body.excluded
    index_baseline = body.uris if body.uris > 0 else body.gcs_uploaded
    deferred_verified = body.deferred_uris == 0
    if body.deferred_uris and body.index_job_id:
        settings = get_settings()
        _, job_ref, _ = _index_job_refs(settings, body.index_job_id)
        job = job_ref.get().to_dict() or {}
        result = job.get("result") or {}
        deferred_verified = (
            job.get("status") == "DONE" and job.get("driveId") == body.drive_id
            and result.get("deferredUris") == body.deferred_uris
            and result.get("count") == body.indexed
            and len(job.get("gcsUris") or []) == index_baseline
        )
    index_ok = deferred_verified and body.indexed + body.deferred_uris == index_baseline
    delta = listed - accounted
    ok = delta == 0 and index_ok
    summary = {
        "driveId": body.drive_id,
        "listed": body.listed,
        "excluded": body.excluded,
        "listedInScope": listed,
        "gcsUploaded": body.gcs_uploaded,
        "uris": body.uris,
        "indexed": body.indexed,
        "failed": body.failed,
        "parked": body.parked,
        "skipped": body.skipped,
        "deleted": body.deleted,
        "unchanged": body.unchanged,
        "dlq": body.dlq,
        "splitQueued": body.split_queued,
        "unaccounted": delta,
        "indexConsistent": index_ok,
        "ok": ok,
    }
    if not ok:
        logger.error("Reconciliation mismatch: %s", summary)
    else:
        logger.info("Reconciliation OK: %s", summary)
    if body.parked:
        logger.warning(
            "parked %s docs on drive=%s (dlq=%s splitQueued=%s) — 별도 조치 필요",
            body.parked, body.drive_id, body.dlq, body.split_queued,
        )
    return summary


@app.post("/sync/reprocess-file")
def reprocess_file(body: ReprocessFileBody) -> dict[str, Any]:
    from google.cloud import firestore
    from google.cloud.firestore_v1.base_query import FieldFilter

    base = get_settings()
    settings = _settings_for_drive(base, body.drive_id)
    if not base.cloud_tasks_enabled:
        raise HTTPException(409, "Cloud Tasks indexing is disabled")
    store = DocStateStore(base)
    _, receipt, _ = _index_job_refs(base, "reprocess-" + body.request_id)
    with document_mutation(store, settings, [body.file_id]):
        previous = receipt.get().to_dict() or {}
        if previous:
            if previous.get("fileIds") != [body.file_id] or previous.get("driveId") != body.drive_id:
                raise HTTPException(409, "requestId already belongs to another document")
            if previous.get("response"):
                return previous["response"]
            raise HTTPException(409, "Previous request outcome is uncertain; inspect its execution first")
        state = store.get(body.file_id)
        if not state or state.drive_id != body.drive_id:
            raise HTTPException(404, "Document does not belong to this drive")
        if state.status in {DocStatus.DELETED, DocStatus.EXCLUDED}:
            raise HTTPException(409, "Document is no longer a reprocessing target")
        if state.status == DocStatus.INDEXED and not str(state.error or "").startswith("NO_BODY_EXTRACTOR:"):
            raise HTTPException(409, "Document is already indexed; refresh the issue list")
        jobs = list(receipt.parent.where(filter=FieldFilter(
            "fileIds", "array_contains", body.file_id,
        )).limit(1001).stream())
        if len(jobs) > 1000:
            raise HTTPException(409, "Too many related jobs to safely verify; operator review required")
        now = datetime.now(UTC)
        for job in jobs:
            data = job.to_dict() or {}
            if (data.get("kind") == "INDEX_GCS" and data.get("status") == "RUNNING"
                    and (not data.get("deadlineAt") or data["deadlineAt"] > now)):
                raise HTTPException(409, "Document already has an active indexing job")
        drive = DriveClient()
        meta = drive.get_file(body.file_id)
        if meta.get("id") != body.file_id or meta.get("driveId") != body.drive_id or meta.get("trashed"):
            raise HTTPException(409, "Drive document moved or was deleted; run normal synchronization")
        if settings.sync_folder_id_list and not drive.is_in_sync_scope(body.file_id, settings.sync_folder_id_list):
            raise HTTPException(409, "Document is outside the current synchronization folders")
        receipt.set({"kind": "REPROCESS_FILE", "fileIds": [body.file_id],
                     "driveId": body.drive_id, "status": "STARTED",
                     "createdAt": firestore.SERVER_TIMESTAMP})
        result = _ingest_with(IngestBody(
            fileId=body.file_id, driveId=body.drive_id,
            name=meta.get("name", ""), mimeType=meta.get("mimeType", ""),
            modifiedTime=meta.get("modifiedTime"), webViewLink=meta.get("webViewLink"),
            sizeBytes=meta.get("size"), parserUrl=body.parser_url, refreshContent=True,
        ), store=store, settings=settings, drive=drive, gcs=GcsClient(settings))
        response = {"fileId": body.file_id, "name": meta.get("name", body.file_id),
                    "status": result.get("status", "UNKNOWN"), "jobId": ""}
        if result.get("status") == "GCS_READY":
            uris = result.get("gcsUris") or ([result["gcsUri"]] if result.get("gcsUri") else [])
            if not uris:
                raise HTTPException(502, "Ingestion returned no indexable objects")
            response.update(index_gcs_async(IndexGcsBody(
                gcsUris=uris, fileIds=[body.file_id], driveId=body.drive_id,
            )))
        elif result.get("status") in {"UNCHANGED", "HASH_UNCHANGED"}:
            current = store.get(body.file_id)
            response["status"] = "INDEXED" if current and current.status == DocStatus.INDEXED else "UNCONFIRMED"
        current = store.get(body.file_id)
        response["reason"] = str((current.error if current else "") or result.get("reason") or "")[:2000]
        receipt.set({"status": "DISPATCHED", "response": response,
                     "updatedAt": firestore.SERVER_TIMESTAMP}, merge=True)
        return response


@app.post("/sync/process")
def process_legacy(body: IngestBody) -> dict[str, Any]:
    if body.removed or (body.route == RouteKind.DELETE.value):
        return delete_file(DeleteBody(fileId=body.file_id, driveId=body.drive_id))
    result = ingest(body)
    if result.get("status") == "GCS_READY":
        uris = list(result.get("gcsUris") or [])
        if not uris and result.get("gcsUri"):
            uris = [result["gcsUri"]]
        if uris:
            idx = index_gcs(
                IndexGcsBody(gcsUris=uris, fileIds=[body.file_id] * len(uris))
            )
            result["index"] = idx
            result["status"] = DocStatus.INDEXED.value
    return result


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", "8081"))
    uvicorn.run("services.sync.main:app", host="0.0.0.0", port=port)
