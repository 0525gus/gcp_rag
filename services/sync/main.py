"""
동기화 HTTP 서비스 — GCS 단일 진입점 파이프라인.

Workflows 호출 순서:
  1) POST /sync/changes       — 델타 + MIME 라우팅 (pageToken 미커밋)
  2) POST /sync/ingest        — Drive→GCS (HWP 파싱 / Google export / 원본 복사)
  3) POST /sync/index-gcs     — RAG Engine이 GCS만 import
  4) POST /sync/delete        — 코퍼스·GCS 정리
  5) POST /sync/commit-token  — 배치 성공 후 pageToken 커밋
  6) POST /sync/reconcile     — Drive 조회 vs GCS 업로드·색인 정합성
"""

from __future__ import annotations

import logging
import os
import re
import sys
import uuid
from pathlib import Path
from typing import Any

import httpx
from fastapi import BackgroundTasks, FastAPI, HTTPException
from pydantic import BaseModel, Field

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from shared.config import Settings, get_settings  # noqa: E402
from shared.drive import DriveClient  # noqa: E402
from shared.firestore_state import DocStateStore  # noqa: E402
from shared.gcs import GcsClient, gs_uri  # noqa: E402
from shared.hashing import sha256_bytes, sha256_text  # noqa: E402
from shared.logging_config import setup_logging  # noqa: E402
from shared.mime_types import (  # noqa: E402
    GOOGLE_EXPORT_MAP,
    RouteKind,
    classify_route,
    is_hwpx,
    rag_size_limit,
)
from shared.pdf_split import PdfSplitError, split_pdf  # noqa: E402
from shared.xlsx_md import XlsxParseError, xlsx_to_markdown  # noqa: E402
from shared.models import Audience, DocState, DocStatus, ParseRoute  # noqa: E402
from shared.path_context import (  # noqa: E402
    PathContext,
    build_breadcrumb_markdown,
)
from shared.rag_engine import RagEngineClient  # noqa: E402
from shared.search_postprocess import extract_file_id  # noqa: E402

setup_logging()
logger = logging.getLogger("sync_service")

# 직접 경로(_ingest_direct) 중 텍스트는 본문에 breadcrumb를 직접 삽입
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

app = FastAPI(title="Drive Sync Service (GCS-only index)", version="2.0.0")


class DriveIdBody(BaseModel):
    drive_id: str = Field(..., alias="driveId")

    model_config = {"populate_by_name": True}


class BackfillBody(BaseModel):
    drive_id: str = Field(..., alias="driveId")
    # true면 기존 pageToken이 있어도 전체 스캔 (초기 셋업용)
    force: bool = False

    model_config = {"populate_by_name": True}


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
    removed: bool = False
    web_view_link: str | None = Field(default=None, alias="webViewLink")
    route: str | None = None
    parser_url: str = Field(default="", alias="parserUrl")

    model_config = {"populate_by_name": True}


class IndexGcsBody(BaseModel):
    gcs_uris: list[str] = Field(..., alias="gcsUris")
    file_ids: list[str] = Field(default_factory=list, alias="fileIds")

    model_config = {"populate_by_name": True}


class DeleteBody(BaseModel):
    file_id: str = Field(..., alias="fileId")
    drive_id: str | None = Field(default=None, alias="driveId")

    model_config = {"populate_by_name": True}


class ReconcileBody(BaseModel):
    drive_id: str = Field(..., alias="driveId")
    listed: int
    gcs_uploaded: int = Field(..., alias="gcsUploaded")
    # 업로드된 GCS URI 총수(파일당 원본+.meta.md면 2). indexed 정합성 비교 기준.
    uris: int = 0
    indexed: int
    failed: int
    skipped: int
    deleted: int
    unchanged: int = 0
    dlq: int = 0
    split_queued: int = Field(default=0, alias="splitQueued")
    # 동기화 지정 폴더 밖 — 애초에 우리 대상이 아니므로 listed 에서 차감한다.
    excluded: int = 0

    model_config = {"populate_by_name": True}


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "indexPath": "gcs-only"}


def _route_file_meta(
    *,
    drive_id: str,
    file_meta: dict[str, Any],
    folder_ids: list[str],
    drive: DriveClient,
) -> dict[str, Any] | None:
    """Drive files.list 항목 → workflow change entry. 폴더는 None."""
    mime = file_meta.get("mimeType") or ""
    if mime == "application/vnd.google-apps.folder":
        return None
    file_id = file_meta.get("id") or ""
    name = file_meta.get("name") or ""
    parents = list(file_meta.get("parents") or [])
    kind = classify_route(mime, name, removed=False)
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
        "removed": False,
        "webViewLink": file_meta.get("webViewLink"),
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
    """현재 Drive 스냅샷을 changes와 같은 형태로 반환. pageToken은 '지금' 기준으로 확보."""
    folder_ids = settings.sync_folder_id_list
    token = store.get_start_page_token(drive_id)
    if not token:
        token = drive.get_start_page_token(drive_id)
        store.set_start_page_token(drive_id, token)

    routed: list[dict[str, Any]] = []
    skipped_out_of_scope = 0
    for meta in drive.iter_backfill_files(drive_id, folder_ids):
        entry = _route_file_meta(
            drive_id=drive_id,
            file_meta=meta,
            folder_ids=folder_ids,
            drive=drive,
        )
        if entry is None:
            continue
        if entry.get("skipReason") == "out_of_folder_scope":
            skipped_out_of_scope += 1
            # backfill에서는 범위 밖은 목록에 넣지 않음
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
def bootstrap(body: DriveIdBody) -> dict[str, str]:
    store = DocStateStore()
    drive = DriveClient()
    existing = store.get_start_page_token(body.drive_id)
    if existing:
        return {"driveId": body.drive_id, "pageToken": existing, "status": "exists"}
    token = drive.get_start_page_token(body.drive_id)
    store.set_start_page_token(body.drive_id, token)
    return {"driveId": body.drive_id, "pageToken": token, "status": "created"}


@app.post("/sync/backfill")
def backfill(body: BackfillBody) -> dict[str, Any]:
    """초기 셋업용 파일 목록만 반환 (소량/디버그). 대량은 /sync/backfill-run 사용."""
    store = DocStateStore()
    drive = DriveClient()
    settings = get_settings()
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
    index_batch_size: int = Field(default=10, alias="indexBatchSize", ge=1, le=50)

    model_config = {"populate_by_name": True}


@app.post("/sync/backfill-run")
def backfill_run(body: BackfillRunBody) -> dict[str, Any]:
    """초기 전체 적재: Drive 스냅샷 → ingest(병렬) → index-gcs 배치.

    Workflow에 수천 개 change를 올리면 메모리 한도에 걸리므로 init은 여기서 수행.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    import threading

    settings = get_settings()
    store = DocStateStore()
    drive = DriveClient()
    parser_url = body.parser_url or os.environ.get("PARSER_URL", "")
    workers = settings.raw_upload_concurrency

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
        # 백필은 범위 밖 파일을 목록에 넣지 않으므로 스냅샷 집계를 그대로 쓴다
        "excluded": int(snapshot.get("skippedOutOfScope") or 0),
    }
    pending_uris: list[str] = []
    pending_ids: list[str] = []
    lock = threading.Lock()

    def flush_index() -> None:
        nonlocal pending_uris, pending_ids
        if not pending_uris:
            return
        uris, ids = pending_uris, pending_ids
        pending_uris, pending_ids = [], []
        # 실패 카운트는 uris 를 비우기 전이 아니라 여기서 직접 세야 한다.
        try:
            idx = index_gcs(IndexGcsBody(gcsUris=uris, fileIds=ids))
            totals["indexed"] += int(idx.get("count") or len(uris))
        except Exception:  # noqa: BLE001
            logger.exception("backfill index flush failed for %s uris", len(uris))
            totals["failed"] += len(uris)
            raise

    def _ingest_one(ch: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        ingest_body = IngestBody(
            fileId=ch["fileId"],
            driveId=ch.get("driveId") or body.drive_id,
            name=ch.get("name") or "",
            mimeType=ch.get("mimeType") or "",
            modifiedTime=ch.get("modifiedTime"),
            removed=False,
            webViewLink=ch.get("webViewLink"),
            route=ch.get("route"),
            parserUrl=parser_url,
        )
        return ch, ingest(ingest_body)

    logger.info(
        "backfill-run parallel workers=%s files=%s", workers, len(changes)
    )

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(_ingest_one, ch) for ch in changes]
        for fut in as_completed(futures):
            try:
                ch, result = fut.result()
            except Exception:  # noqa: BLE001
                logger.exception("backfill ingest worker failed")
                with lock:
                    totals["failed"] += 1
                    totals["dlq"] += 1
                continue

            status = result.get("status")
            with lock:
                if status == "GCS_READY":
                    uris = list(result.get("gcsUris") or [])
                    if not uris and result.get("gcsUri"):
                        uris = [result["gcsUri"]]
                    if uris:
                        pending_uris.extend(uris)
                        pending_ids.extend([ch["fileId"]] * len(uris))
                        totals["gcsUploaded"] += 1
                        totals["uris"] += len(uris)
                        if len(pending_uris) >= body.index_batch_size:
                            try:
                                flush_index()
                            except Exception:  # noqa: BLE001
                                pending_uris, pending_ids = [], []
                elif status in {"UNCHANGED", "HASH_UNCHANGED"}:
                    totals["unchanged"] += 1
                elif status in {"SKIPPED", "skipped"}:
                    totals["skipped"] += 1
                elif status == "DLQ":
                    totals["dlq"] += 1
                    totals["failed"] += 1
                elif status == "SPLIT_QUEUED":
                    totals["splitQueued"] += 1
                    totals["failed"] += 1
                else:
                    totals["failed"] += 1

    try:
        with lock:
            flush_index()
    except Exception:  # noqa: BLE001
        pass

    if pending_page_token and totals["failed"] == 0:
        store.set_start_page_token(body.drive_id, pending_page_token)

    logger.info("backfill-run done drive=%s totals=%s", body.drive_id, totals)
    return {
        "driveId": body.drive_id,
        "mode": "backfill-run",
        "pendingPageToken": pending_page_token,
        "workers": workers,
        "totals": totals,
        "ok": totals["failed"] == 0,
    }


@app.post("/sync/changes")
def list_changes(body: DriveIdBody) -> dict[str, Any]:
    """델타 조회. pageToken은 commit-token 전까지 커밋하지 않음.

    토큰이 없으면(최초) backfill 스냅샷을 반환해 초기 셋업에도 동일 파이프라인 적용.
    SYNC_FOLDER_IDS가 있으면 해당 폴더 트리 밖 파일은 SKIP.
    삭제(removed)는 이전에 색인됐을 수 있어 범위 밖이어도 DELETE 유지.
    """
    store = DocStateStore()
    drive = DriveClient()
    settings = get_settings()
    folder_ids = settings.sync_folder_id_list
    token = store.get_start_page_token(body.drive_id)
    if not token:
        # 최초: pageToken 확정 + 현재 파일 전체 적재 대상으로 반환
        result = _build_backfill_changes(
            body.drive_id, store=store, drive=drive, settings=settings
        )
        result["message"] = "bootstrapped with full backfill snapshot"
        return result

    changes, new_token = drive.list_changes(body.drive_id, token)
    routed: list[dict[str, Any]] = []
    skipped_out_of_scope = 0
    for ch in changes:
        kind = classify_route(ch.mime_type, ch.name, removed=ch.removed)
        skip_reason: str | None = None
        if folder_ids and kind != RouteKind.DELETE:
            in_scope = drive.is_in_sync_scope(
                ch.file_id, folder_ids, parents=ch.parents or None
            )
            if not in_scope:
                # 백필과 달리 델타에서는 목록에서 빼지 않고 EXCLUDE 로 흘린다.
                # 이전에 색인된 문서가 폴더 밖으로 나간 경우, 목록에서 빼버리면
                # 코퍼스·GCS 잔존물을 회수할 기회 자체가 사라지기 때문이다.
                kind = RouteKind.EXCLUDE
                skip_reason = "out_of_folder_scope"
                skipped_out_of_scope += 1
        entry: dict[str, Any] = {
            "fileId": ch.file_id,
            "driveId": ch.drive_id,
            "name": ch.name,
            "mimeType": ch.mime_type,
            "modifiedTime": ch.modified_time,
            "removed": ch.removed,
            "webViewLink": ch.web_view_link,
            "route": kind.value,
        }
        if skip_reason:
            entry["skipReason"] = skip_reason
        routed.append(entry)

    return {
        "driveId": body.drive_id,
        "changes": routed,
        "pendingPageToken": new_token,
        "count": len(routed),
        "syncFolderIds": folder_ids,
        "skippedOutOfScope": skipped_out_of_scope,
        "mode": "delta",
    }


@app.post("/sync/commit-token")
def commit_token(body: CommitTokenBody) -> dict[str, str]:
    store = DocStateStore()
    store.set_start_page_token(body.drive_id, body.page_token)
    return {"driveId": body.drive_id, "pageToken": body.page_token, "status": "committed"}


@app.post("/sync/ingest")
def ingest(body: IngestBody) -> dict[str, Any]:
    """Drive 문서를 GCS 정규화 버킷에 적재. RAG import는 /sync/index-gcs."""
    # 빈 fileId 는 Firestore 문서 경로를 `doc_state/` 로 만들어 400 을 던진다.
    # 호출측을 고쳐도(shared/drive.py) 이 엔드포인트가 500 으로 죽을 이유는 없다.
    if not body.file_id.strip():
        raise HTTPException(status_code=400, detail="fileId is required")

    store = DocStateStore()
    settings = get_settings()
    gcs = GcsClient(settings)
    drive = DriveClient()

    def _mark_excluded() -> dict[str, Any]:
        """대상 폴더 밖 — 우리가 할 일이 없는 문서.

        SKIPPED 로 찍으면 '대상인데 처리 못 함'과 섞여 집계가 흐려진다.
        EXCLUDED 는 reconcile 의 listed 에서 차감되고, cleanup 이 잔존물을
        회수할 수 있게 살아있는 상태 목록에서도 빠진다.
        """
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
                error="out_of_folder_scope",
            )
        )
        return {
            "fileId": body.file_id,
            "status": DocStatus.EXCLUDED.value,
            "reason": "out_of_folder_scope",
        }

    folder_ids = settings.sync_folder_id_list
    if folder_ids and not body.removed:
        if not drive.is_in_sync_scope(body.file_id, folder_ids):
            return _mark_excluded()

    route = RouteKind(body.route) if body.route else classify_route(
        body.mime_type, body.name, removed=body.removed
    )

    if route == RouteKind.DELETE or body.removed:
        return {"fileId": body.file_id, "status": "DELETE_PENDING", "route": "DELETE"}

    if route == RouteKind.EXCLUDE:
        return _mark_excluded()

    if route == RouteKind.SKIP:
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
            )
        )
        return {"fileId": body.file_id, "status": DocStatus.SKIPPED.value, "route": route.value}

    if not store.should_reparse(body.file_id, body.modified_time):
        existing = store.get(body.file_id)
        # path 있고 이미 INDEXED면 스킵. PARSED만 된 문서는 재처리(색인 누락 복구).
        if (
            existing
            and (existing.path or "").strip()
            and existing.status == DocStatus.INDEXED
        ):
            return {"fileId": body.file_id, "status": "UNCHANGED", "route": route.value}

    try:
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
        )
        return {
            "fileId": body.file_id,
            "status": "DLQ",
            "route": route.value,
            "error": str(exc)[:500],
        }


def _effective_limit(settings: Settings, ext: str) -> int:
    """RAG Engine 타입별 한도와 우리 상한 중 작은 쪽.

    RAG Engine 한도를 넘겨 올려봐야 import 에서 거부되므로, 올려도 의미가 없다.
    MAX_GCS_BYTES 는 그보다 더 조이고 싶을 때만 쓰인다.
    """
    return min(settings.max_gcs_bytes, rag_size_limit(ext))


def _size_gate(
    store: DocStateStore,
    settings: Settings,
    body: IngestBody,
    data: bytes,
    *,
    splittable: bool,
    ext: str = "",
    limit: int | None = None,
) -> dict[str, Any] | None:
    """업로드 직전 크기 체크. 초과 시 FAILED/분할 큐. None이면 통과."""
    size = len(data)
    limit = limit if limit is not None else _effective_limit(settings, ext)
    if size <= limit:
        return None
    reason = f"SIZE_EXCEEDED:{size}>{limit}"
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
    """학생자료 폴더 트리 안이면 STUDENT, 아니면 STAFF.

    판정에 필요한 부모 조회는 `_resolve_path_ctx` 가 이미 훑어 캐시해 둔 것을
    재사용하므로(DriveClient._parent_cache) Drive 호출이 추가로 들지 않는다.

    실패는 전부 STAFF 로 떨어진다. `is_in_sync_scope` 자체가 부모를 못 읽으면
    False 를 주므로 이미 그쪽으로 기울어 있지만, 여기서 한 번 더 감싼다 —
    이 함수가 틀리는 방향은 '학생에게 안 보인다' 여야 하고, 절대 그 반대가
    되어서는 안 된다.
    """
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
    """동일 프로젝트 Cloud Run 호출용 ID 토큰 (sync → parser)."""
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

    path_ctx = _resolve_path_ctx(drive, body)
    audience = _resolve_audience(drive, settings, body)
    raw = drive.download_file(body.file_id)
    ext = ".hwpx" if is_hwpx(body.mime_type, body.name) else ".hwp"
    raw_uri = gcs.upload_raw(raw, body.file_id, ext)

    headers = _cloud_run_auth_headers(body.parser_url)
    with httpx.Client(timeout=600.0) as client:
        resp = client.post(
            body.parser_url.rstrip("/") + "/parse",
            headers=headers,
            json={
                "gcsUri": raw_uri,
                "mimeType": body.mime_type,
                "fileId": body.file_id,
            },
        )
        if resp.status_code == 422:
            detail = (
                resp.json()
                if resp.headers.get("content-type", "").startswith("application/json")
                else {"detail": resp.text}
            )
            reason = f"QUALITY_GATE:{detail}"
            store.enqueue_dlq(
                body.file_id,
                reason,
                driveId=body.drive_id,
                name=body.name,
                mimeType=body.mime_type,
                modifiedTime=body.modified_time,
                parseRoute=ParseRoute.RHWP.value,
                path=path_ctx.path,
                bundle=path_ctx.bundle,
            )
            return {
                "fileId": body.file_id,
                "status": "DLQ",
                "route": "HWP_PARSE",
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
        title=body.name or body.file_id,
        body=md_bytes.decode("utf-8", errors="replace"),
    )
    content_hash = sha256_text(md_text)
    md_uri = gcs.upload_normalized_md(md_text, body.file_id)
    md_bytes = md_text.encode("utf-8")

    # 파서 결과는 마크다운이므로 md 한도(10MB)가 적용된다
    gated = _size_gate(store, settings, body, md_bytes, splittable=True, ext=".md")
    if gated:
        gated["route"] = "HWP_PARSE"
        return gated

    if store.should_skip_reindex(body.file_id, content_hash):
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
    gated = _size_gate(store, settings, body, data, splittable=True, ext=ext)
    if gated:
        gated["route"] = "GOOGLE_EXPORT"
        return gated

    sidecar = build_breadcrumb_markdown(
        path=path_ctx.path,
        bundle=path_ctx.bundle,
        title=body.name or body.file_id,
    )
    content_hash = sha256_text(f"{sha256_bytes(data)}|{path_ctx.path}|{sidecar}")
    if store.should_skip_reindex(body.file_id, content_hash):
        return {
            "fileId": body.file_id,
            "status": "HASH_UNCHANGED",
            "route": "GOOGLE_EXPORT",
            "contentHash": content_hash,
            "path": path_ctx.path,
            "bundle": path_ctx.bundle,
        }

    blob = f"normalized/{body.file_id}{ext}"
    gcs_uri = gcs.upload_bytes(
        data, settings.gcs_normalized_bucket, blob, content_type=export_mime
    )
    meta_uri = gcs.upload_path_sidecar_md(sidecar, body.file_id)
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


def _ingest_direct(
    body: IngestBody,
    store: DocStateStore,
    gcs: GcsClient,
    drive: DriveClient,
    settings: Settings,
) -> dict[str, Any]:
    """파서 서비스를 거치지 않고 Drive→GCS 로 바로 가는 포맷들.

    한때는 이름 그대로 '복사'만 했으나 지금은 포맷마다 다르게 손본다.
    라우트 키(`FILE_COPY`)는 워크플로가 문자열로 비교하고 API 응답에도
    나가므로 그대로 두고, 함수 이름만 실제 동작에 맞춘다.

        PDF          한도 초과분을 페이지 경계로 분할
        XLSX         셀을 마크다운 표로 변환
        TXT/HTML/CSV 머리말을 본문 앞에 심음
        그 외        원본 복사 + 경로 사이드카

    크기 게이트가 두 번 나오는데 **재는 대상이 다르다**. 위쪽은 원본 바이트,
    아래쪽은 RAG 로 실제 올라갈 산출물이다. 둘을 섞으면 색인되지도 않을
    원본 크기 때문에 문서를 잃는다(실측 사고 있었음).
    """
    path_ctx = _resolve_path_ctx(drive, body)
    audience = _resolve_audience(drive, settings, body)
    data = drive.download_file(body.file_id)

    name = body.name or body.file_id
    mime = (body.mime_type or "").lower()
    ext = Path(name).suffix or _ext_for_mime(body.mime_type)

    # PDF 는 한도를 넘으면 버리지 말고 페이지 경계로 쪼갠다.
    pdf_parts: list[bytes] | None = None
    if ext.lower() == ".pdf" and len(data) > _effective_limit(settings, ext):
        try:
            pdf_parts = split_pdf(data, _effective_limit(settings, ext))
            logger.info(
                "oversized PDF split %s: %sB -> %s parts",
                body.file_id, len(data), len(pdf_parts),
            )
        except PdfSplitError as exc:
            logger.warning("PDF split failed %s: %s", body.file_id, exc)
            pdf_parts = None  # 아래 게이트가 큐로 보낸다

    if pdf_parts is None:
        # 스프레드시트 원본은 RAG 로 가지 않는다 — 색인되는 건 변환된 .md 뿐이다.
        # 원본을 RAG 한도로 재면 색인되지도 않을 크기 때문에 문서를 통째로 잃는다.
        # (실측: 27.7MB xlsx 가 여기서 떨어져 사이드카로도 못 찾게 됐다)
        # 변환 결과는 아래에서 .md 기준으로 따로 잰다. 여기서는 우리 저장 상한만 본다.
        raw_limit = (
            settings.max_gcs_bytes if mime in _SPREADSHEET_COPY_MIMES else None
        )
        gated = _size_gate(
            store, settings, body, data, splittable=True, ext=ext, limit=raw_limit
        )
        if gated:
            gated["route"] = "FILE_COPY"
            return gated

    body_text: str | None = None
    # 원본 바이트를 RAG 로 보낼지. 스프레드시트는 변환에 실패하면 보내지 않는다.
    index_original = True
    skip_reason: str | None = None
    if mime in _SPREADSHEET_COPY_MIMES:
        try:
            body_text = xlsx_to_markdown(data)
        except XlsxParseError as exc:
            # 암호 걸린 파일 등 — 사이드카만 남긴다(경로·파일명으로는 찾힌다).
            #
            # 원본 xlsx 를 색인 목록에 넣으면 안 된다. RAG Engine 기본 파서는
            # xlsx 를 못 읽어 **매번 import 에서 거부**되는데, 지금까지는 그
            # 실패가 성공으로 집계돼 보이지 않았다(실측 27건이 상시 실패 중).
            # 재색인 경로(_normalized_uris_for_file)는 이미 .xlsx 를 빼고
            # 있었다 — ingest 경로만 빠져 있었던 것이다.
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

    if body_text is not None:
        md_text = build_breadcrumb_markdown(
            path=path_ctx.path,
            bundle=path_ctx.bundle,
            title=name,
            body=body_text,
        )
        # 원본 바이트가 아니라 머리말까지 붙인 최종 산출물로 잰다.
        # 변환으로 늘어난 분량(xlsx→표)은 원본 크기로는 안 보인다.
        gated = _size_gate(
            store, settings, body, md_text.encode("utf-8"), splittable=True, ext=".md"
        )
        if gated:
            gated["route"] = "FILE_COPY"
            return gated
        content_hash = sha256_text(md_text)
        if store.should_skip_reindex(body.file_id, content_hash):
            return {
                "fileId": body.file_id,
                "status": "HASH_UNCHANGED",
                "route": "FILE_COPY",
                "contentHash": content_hash,
                "path": path_ctx.path,
                "bundle": path_ctx.bundle,
            }
        gcs_uri = gcs.upload_normalized_md(md_text, body.file_id)
        uris = [gcs_uri]
        _drop_stale_sidecar(gcs, settings, body.file_id)
    else:
        sidecar = build_breadcrumb_markdown(
            path=path_ctx.path,
            bundle=path_ctx.bundle,
            title=name,
        )
        content_hash = sha256_text(f"{sha256_bytes(data)}|{path_ctx.path}|{sidecar}")
        if store.should_skip_reindex(body.file_id, content_hash):
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
            # 읽을 수 없는 스프레드시트 — 원본은 GCS 에도 올리지 않는다.
            # 아무도 읽지 않는데(재색인 경로가 .xlsx 를 제외한다) 자리만 차지하고,
            # 색인에 넣으면 매번 import 를 실패시킨다.
            uris = []
        elif pdf_parts and len(pdf_parts) > 1:
            # {fileId}.part1.pdf ... — extract_file_id 가 .partN 을 떼어내므로
            # 검색 결과에서는 원본 한 문서로 합쳐진다.
            uris = []
            for i, part in enumerate(pdf_parts, start=1):
                uris.append(
                    gcs.upload_bytes(
                        part,
                        settings.gcs_normalized_bucket,
                        f"normalized/{body.file_id}.part{i}{ext}",
                        content_type=ctype,
                    )
                )
        else:
            uris = [
                gcs.upload_bytes(
                    data,
                    settings.gcs_normalized_bucket,
                    f"normalized/{body.file_id}{ext}",
                    content_type=ctype,
                )
            ]
        uris.append(gcs.upload_path_sidecar_md(sidecar, body.file_id))

    store.upsert(
        _state_fields(
            body,
            content_hash=content_hash,
            status=DocStatus.PARSED,
            parse_route=ParseRoute.GCS_COPY,
            source_uri=body.web_view_link or uris[0],
            path_ctx=path_ctx,
            audience=audience,
            # 본문 추출 실패 사유를 남긴다. 상태는 INDEXED 로 가지만(사이드카는
            # 색인됨) 본문이 없는 문서라는 사실은 조회 가능해야 한다.
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
    """본문 md 가 생긴 문서의 옛 경로 사이드카를 치운다.

    xlsx 는 예전에 `.meta.md` 한 줄만 색인했다. 본문이 생긴 뒤에도 남겨두면
    같은 fileId 로 청크가 두 벌 잡혀, 문서당 청크 상한을 사이드카가 잡아먹는다.
    """
    uri = gs_uri(settings.gcs_normalized_bucket, f"normalized/{file_id}.meta.md")
    try:
        gcs.delete(uri)
    except Exception as exc:  # noqa: BLE001
        # 처음부터 없었으면 그만이다. 삭제 실패로 색인을 막지 않는다.
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
    """(정상 fileId, 버린 값). 파일명/URI 로 들어와도 fileId 로 되돌린다.

    여기서 거르지 않으면 아래 upsert 가 이상한 문서를 새로 만들어 버린다
    (예: '{fileId}.meta.md' 를 순진하게 잘라 만든 '{fileId}.meta').
    """
    ok: list[str] = []
    bad: list[str] = []
    for raw in raw_ids:
        fid = extract_file_id(raw)
        (ok if _FILE_ID_RE.match(fid) else bad).append(fid)
    return list(dict.fromkeys(ok)), bad


def _sync_student_corpus(
    gcs_uris: list[str], file_ids: list[str], settings: Settings, store: DocStateStore
) -> dict[str, Any]:
    """학생 코퍼스를 doc_state 의 audience 에 맞춘다.

    교직원 코퍼스(=기본 코퍼스)는 전량을 담으므로 기존 경로가 그대로 처리한다.
    여기서는 **학생 코퍼스만** 따로 맞춘다.

    핵심은 대상이 아닌 문서까지 **먼저 지운다**는 점이다. 학생자료 → 교직원자료로
    옮긴 문서는 삭제 대상이 아니라 소속 변경 대상이고, 그 이동이 반영되는 지점이
    여기뿐이다. 지우지 않으면 옮긴 뒤에도 학생에게 계속 노출된다.

    URI → fileId 는 이름에서 되돌린다(`extract_file_id`). 워크플로가 넘기는
    fileIds 는 URI 와 1:1 이 아니라(파일 하나가 본문+.meta.md 로 URI 2개를
    만든다) URI 별 판정에는 쓸 수 없기 때문이다.
    """
    if not settings.audience_split_enabled:
        return {"enabled": False}

    student_uris: list[str] = []
    touched: set[str] = {fid for fid in file_ids if fid}
    for uri in gcs_uris:
        fid = extract_file_id(uri)
        if not _FILE_ID_RE.match(fid):
            # fileId 로 못 되돌리는 URI 는 소속을 판정할 수 없다 → 학생에게 안 준다
            logger.warning("학생 코퍼스: fileId 판정 불가라 제외 %s", uri)
            continue
        touched.add(fid)
        state = store.get(fid)
        if state and state.audience == Audience.STUDENT:
            student_uris.append(uri)

    rag = RagEngineClient(settings, corpus_name=settings.rag_corpus_name_student)
    try:
        rag.delete_files_by_ids(sorted(touched))
    except Exception:  # noqa: BLE001
        # 삭제가 실패한 채로 import 하면 옛 청크가 남는다. 학생 코퍼스에서 그것은
        # '내려야 할 문서가 안 내려간' 상태이므로 조용히 넘기지 않는다.
        logger.exception("학생 코퍼스 pre-delete 실패 files=%s", len(touched))
        raise

    imported = rag.import_from_gcs(student_uris) if student_uris else []
    logger.info(
        "학생 코퍼스 동기화: 대상 %s / 검토 %s URI", len(imported), len(gcs_uris)
    )
    return {"enabled": True, "imported": len(imported), "removed": len(touched)}


@app.post("/sync/index-gcs")
def index_gcs(body: IndexGcsBody) -> dict[str, Any]:
    """GCS URI만 RAG Engine에 증분 import. Drive 커넥터 미사용."""
    if not body.gcs_uris:
        return {"imported": [], "count": 0, "status": "EMPTY"}

    settings = get_settings()
    rag = RagEngineClient(settings)
    store = DocStateStore()

    file_ids, bad_ids = _clean_file_ids(body.file_ids)
    if bad_ids:
        logger.warning(
            "index-gcs dropped %s malformed fileIds: %s", len(bad_ids), bad_ids[:10]
        )

    # upsert: 동일 fileId 기존 청크 제거 후 import (코퍼스 1회 순회로 일괄 삭제)
    try:
        rag.delete_files_by_ids(file_ids)
    except Exception:  # noqa: BLE001
        logger.warning("pre-delete failed for batch %s", file_ids)

    imported = rag.import_from_gcs(body.gcs_uris)

    for fid in file_ids:
        store.mark_indexed(fid)

    # 교직원 코퍼스가 끝난 뒤에 학생 코퍼스를 맞춘다. 순서가 중요하다 — 학생
    # 코퍼스가 실패해도 교직원 쪽 색인과 doc_state 는 이미 확정돼 있어야
    # 다음 배치가 같은 일을 처음부터 다시 하지 않는다.
    student = _sync_student_corpus(body.gcs_uris, file_ids, settings, store)

    return {
        "imported": imported,
        "count": len(imported),
        "droppedFileIds": len(bad_ids),
        "student": student,
        "status": "INDEXED",
    }


class ReindexPendingBody(BaseModel):
    """PARSED(색인 누락) 문서를 GCS URI로 재인덱싱."""

    limit: int = Field(default=200, ge=1, le=2000)
    # URI 개수 기준이다(문서 수 아님 — 아래 루프의 len(pending_uris) 비교).
    # rag.import_files 는 호출 1회당 URI 25개까지만 받고(_MAX_IMPORT_URIS),
    # 호출 지연이 URI 수와 거의 무관하게 ~21초다. 즉 **호출 횟수가 곧 시간**이라
    # 한 번에 최대한 담아야 한다. 실측(1,211건 재색인): 10 이면 ~140회 42분.
    #
    # 상한이 25인데 기본값을 24로 두는 이유: 문서 하나가 URI 를 최대 2개
    # 만든다(본문 + .meta.md, 실측 892건이 1개 / 317건이 2개). 임계값이 25면
    # pending 이 24일 때 2-URI 문서가 와서 26이 되고, 25+1 로 쪼개져 호출이
    # 한 번 더 든다. 그 1개짜리 호출도 21초를 그대로 먹는다.
    # 24면 pending 이 23 이하이므로 2를 더해도 25 — 항상 한 번에 끝난다.
    index_batch_size: int = Field(default=24, alias="indexBatchSize", ge=1, le=25)
    # true면 INDEXED도 다시 import (기본은 PARSED만)
    force: bool = False
    # true면 즉시 jobId를 반환하고 뒤에서 계속 돈다.
    # 전량 재색인은 수십 분이 걸려 클라이언트가 먼저 끊기므로 이쪽을 쓸 것.
    background: bool = False
    # 내부용 — background 실행 시 진행률을 기록할 잡 ID
    job_id: str | None = Field(default=None, alias="jobId")

    model_config = {"populate_by_name": True}


_JOB_COLLECTION = "sync_jobs"


def _job_set(job_id: str, **fields: Any) -> None:
    from google.cloud import firestore

    store = DocStateStore()
    store._db.collection(_JOB_COLLECTION).document(job_id).set(  # noqa: SLF001
        {"jobId": job_id, "updatedAt": firestore.SERVER_TIMESTAMP, **fields},
        merge=True,
    )


@app.get("/sync/jobs/{job_id}")
def job_status(job_id: str) -> dict[str, Any]:
    store = DocStateStore()
    snap = store._db.collection(_JOB_COLLECTION).document(job_id).get()  # noqa: SLF001
    if not snap.exists:
        raise HTTPException(status_code=404, detail=f"unknown job: {job_id}")
    return snap.to_dict() or {}


# RAG Engine 기본 파서가 안정적으로 받는 확장자 (+ sidecar meta.md)
_INDEXABLE_SUFFIXES = (
    ".md",
    ".meta.md",
    ".pdf",
    ".txt",
    ".html",
    ".docx",
    ".pptx",
    ".csv",
)


def _normalized_uris_for_file(
    settings: Settings, file_id: str, gcs: GcsClient | None = None
) -> list[str]:
    """존재하는 정규화 객체 중 인덱싱 가능 URI만 반환. xlsx 원본은 제외(meta만).

    fileId 경계 검사는 GcsClient 쪽에 모아 두었다 — 삭제 경로와 같은 규칙을
    써야 '색인은 됐는데 삭제는 안 되는' 확장자가 생기지 않는다.
    """
    client = gcs or GcsClient(settings)
    uris: list[str] = []
    for name in client.list_blob_names_for_file(
        settings.gcs_normalized_bucket, "normalized", file_id
    ):
        lower = name.lower()
        if lower.endswith((".xlsx", ".xls")):
            continue
        if any(lower.endswith(suf) for suf in _INDEXABLE_SUFFIXES):
            uris.append(gs_uri(settings.gcs_normalized_bucket, name))
    return uris


@app.post("/sync/reindex-pending")
def reindex_pending(
    body: ReindexPendingBody, tasks: BackgroundTasks
) -> dict[str, Any]:
    """Firestore PARSED(또는 force 시 전체) → GCS URI 모아 index-gcs.

    백필이 ingest만 하고 색인이 끊긴 경우 복구용.
    전량 재색인은 수십 분이 걸려 클라이언트가 먼저 끊기므로 background=true 권장.
    """
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
        # totals 는 루프 도중 스냅샷이라 완주해도 중간값이 남는다. 최종값으로
        # 덮어써야 /sync/jobs/{id} 가 실패처럼 읽히지 않는다 (실측:
        # reindex-0f06ea24ff7a 가 candidates=115 indexed=48 로 보였으나 완주였다).
        _job_set(job_id, status="DONE", result=result, totals=result.get("totals"))
        logger.info("reindex job %s done: %s", job_id, result.get("totals"))
    except Exception as exc:  # noqa: BLE001
        logger.exception("reindex job %s failed", job_id)
        _job_set(job_id, status="FAILED", error=str(exc)[:1000])


def _reindex_pending_sync(body: ReindexPendingBody) -> dict[str, Any]:
    settings = get_settings()
    store = DocStateStore()
    # 문서마다 storage.Client 를 새로 만들면 200건에 200번 만든다 — 한 번만.
    gcs = GcsClient(settings)

    targets: list[DocState] = []
    if body.force:
        # stream 제한 — status 필터 두 번
        targets.extend(store.list_by_status(DocStatus.PARSED, limit=body.limit))
        remain = body.limit - len(targets)
        if remain > 0:
            targets.extend(store.list_by_status(DocStatus.INDEXED, limit=remain))
    else:
        targets = store.list_by_status(DocStatus.PARSED, limit=body.limit)

    pending_uris: list[str] = []
    pending_ids: list[str] = []
    totals = {
        "candidates": len(targets),
        "withUris": 0,
        "indexed": 0,
        "skippedNoUri": 0,
        "failed": 0,
    }

    def flush() -> None:
        nonlocal pending_uris, pending_ids
        if not pending_uris:
            return
        uris, ids = pending_uris, pending_ids
        pending_uris, pending_ids = [], []
        uniq_ids = list(dict.fromkeys(ids))
        # 실패 카운트는 uris 를 비우기 전이 아니라 여기서 직접 세야 한다.
        # (바깥 except 시점엔 이미 pending_uris 가 [] 라 len() 이 0으로 잡힘)
        try:
            # PARSED 복구는 대부분 코퍼스 미존재 → 전량 list+delete 생략(속도)
            if body.force:
                idx = index_gcs(IndexGcsBody(gcsUris=uris, fileIds=uniq_ids))
                totals["indexed"] += int(idx.get("count") or len(uris))
                return
            rag = RagEngineClient(settings)
            imported = rag.import_from_gcs(uris)
            for fid in uniq_ids:
                store.mark_indexed(fid)
            # 교직원 쪽은 위처럼 pre-delete 를 생략하지만(대부분 코퍼스 미존재),
            # 학생 코퍼스는 생략하지 않는다 — 소속 이동이 반영되는 지점이라
            # 속도보다 정확성이 우선이다.
            _sync_student_corpus(uris, uniq_ids, settings, store)
            totals["indexed"] += len(imported)
        except Exception:  # noqa: BLE001
            logger.exception("reindex-pending flush failed for %s uris", len(uris))
            totals["failed"] += len(uris)
            raise

    for doc in targets:
        uris = _normalized_uris_for_file(settings, doc.file_id, gcs)
        if not uris:
            totals["skippedNoUri"] += 1
            continue
        totals["withUris"] += 1
        pending_uris.extend(uris)
        pending_ids.extend([doc.file_id] * len(uris))
        if len(pending_uris) >= body.index_batch_size:
            try:
                flush()
            except Exception:  # noqa: BLE001
                pending_uris, pending_ids = [], []
            if body.job_id:
                _job_set(body.job_id, status="RUNNING", totals=dict(totals))

    try:
        flush()
    except Exception:  # noqa: BLE001
        pass

    logger.info("reindex-pending done totals=%s", totals)
    return {"mode": "reindex-pending", "totals": totals, "ok": totals["failed"] == 0}


class RetryFailedBody(BaseModel):
    """FAILED(DLQ) 문서를 ingest부터 재시도."""

    limit: int = Field(default=100, ge=1, le=1000)
    parser_url: str = Field(default="", alias="parserUrl")
    # 이 횟수만큼 재시도해도 실패하면 영구 실패로 두고 건너뜀 (무한 재시도 방지)
    max_attempts: int = Field(default=3, alias="maxAttempts", ge=1, le=10)
    index_batch_size: int = Field(default=10, alias="indexBatchSize", ge=1, le=50)

    model_config = {"populate_by_name": True}


@app.post("/sync/retry-failed")
def retry_failed(body: RetryFailedBody) -> dict[str, Any]:
    """FAILED 문서를 ingest부터 재구동하고 GCS_READY면 색인까지 이어붙인다.

    일시적 오류(Drive 429/5xx, 파서 타임아웃)로 DLQ에 빠진 문서를 자동 회수한다.
    max_attempts 초과 문서는 실제 결함으로 보고 건너뛴다.
    """
    store = DocStateStore()
    targets = store.list_by_status(DocStatus.FAILED, limit=body.limit)

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
        pending_uris, pending_ids = [], []
        idx = index_gcs(IndexGcsBody(gcsUris=uris, fileIds=list(dict.fromkeys(ids))))
        totals["indexed"] += int(idx.get("count") or len(uris))

    for doc in targets:
        if store.get_dlq_attempts(doc.file_id) >= body.max_attempts:
            totals["exhausted"] += 1
            continue

        store.record_dlq_attempt(doc.file_id)
        totals["retried"] += 1
        try:
            res = ingest(
                IngestBody(
                    fileId=doc.file_id,
                    driveId=doc.drive_id,
                    name=doc.name,
                    mimeType=doc.mime_type,
                    modifiedTime=doc.modified_time,
                    parserUrl=body.parser_url,
                )
            )
        except Exception:  # noqa: BLE001
            logger.exception("retry-failed ingest raised: %s", doc.file_id)
            totals["stillFailed"] += 1
            continue

        status = res.get("status")
        if status == "GCS_READY":
            totals["recovered"] += 1
            store.clear_dlq(doc.file_id)
            uris = list(res.get("gcsUris") or [])
            pending_uris.extend(uris)
            pending_ids.extend([doc.file_id] * len(uris))
            if len(pending_uris) >= body.index_batch_size:
                try:
                    flush()
                except Exception:  # noqa: BLE001
                    logger.exception("retry-failed flush failed")
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

    logger.info("retry-failed done totals=%s", totals)
    return {"mode": "retry-failed", "totals": totals, "ok": totals["stillFailed"] == 0}


@app.post("/sync/delete")
def delete_file(body: DeleteBody) -> dict[str, Any]:
    store = DocStateStore()
    settings = get_settings()
    gcs = GcsClient(settings)

    ok = RagEngineClient(settings).delete_by_file_id(body.file_id)
    # 학생 코퍼스에서도 반드시 빼야 한다. 여기를 빠뜨리면 Drive 에서 지운 문서가
    # 학생에게만 계속 검색되는, 가장 알아채기 어려운 형태의 잔존이 된다.
    if settings.audience_split_enabled:
        try:
            RagEngineClient(
                settings, corpus_name=settings.rag_corpus_name_student
            ).delete_by_file_id(body.file_id)
        except Exception:  # noqa: BLE001
            logger.exception("학생 코퍼스 삭제 실패 fileId=%s", body.file_id)
            raise

    # 정규화 산출물 + raw 원본을 prefix 로 훑어 지운다.
    #
    # 예전에는 확장자 목록을 손으로 적었는데, 목록에 없는 것을 조용히 놓쳤다:
    #   .partN.pdf  분할 PDF 조각이 전량 남는다(실측 6건 존재)
    #   .rtf / .doc  FILE_COPY_MIME 에 있는데 목록에는 없었다
    #
    # raw 는 아예 건드리지도 않았다. 그래서 Drive 에서 지운 문서의 **원본이
    # GCS 에 영구 잔존**했다(실측: DELETED 100건 중 52건의 .hwp 원본이 남아 있었다).
    # raw 에는 명단·인사발령 같은 원문이 그대로 있어(docs/OPS_DEFERRED.md 6번)
    # 삭제가 이행되지 않는 것 자체가 문제다.
    removed: list[str] = []
    for bucket in (settings.gcs_normalized_bucket, settings.gcs_raw_bucket):
        if not bucket:
            continue
        prefix = "normalized" if bucket == settings.gcs_normalized_bucket else "raw"
        try:
            removed.extend(gcs.delete_for_file(bucket, prefix, body.file_id))
        except Exception:  # noqa: BLE001
            # GCS 정리 실패로 코퍼스·상태 정리를 막지는 않는다
            logger.warning(
                "GCS 정리 실패 bucket=%s fileId=%s", bucket, body.file_id, exc_info=True
            )

    store.mark_deleted(body.file_id)
    logger.info(
        "deleted fileId=%s corpus=%s gcsObjects=%s", body.file_id, ok, len(removed)
    )
    return {
        "fileId": body.file_id,
        "deleted": ok,
        "gcsDeleted": len(removed),
        "status": DocStatus.DELETED.value,
    }


@app.post("/sync/reconcile")
def reconcile(body: ReconcileBody) -> dict[str, Any]:
    """Drive 조회 건수 vs GCS 업로드·색인·스킵·실패·삭제 정합성."""
    # dlq/splitQueued 는 failed 의 하위 분류(집계 시 failed 도 함께 증가)이므로
    # accounted 에 다시 더하면 이중 집계된다 — failed 만 합산한다.
    accounted = (
        body.gcs_uploaded
        + body.failed
        + body.skipped
        + body.deleted
        + body.unchanged
    )
    # EXCLUDED 는 대상 폴더 밖이라 처리할 일이 없다. accounted 에 더하는 대신
    # listed 에서 뺀다 — 그래야 남은 skipped 가 '대상인데 처리 못 한 것'만
    # 가리킨다. (예전에는 폴더 밖 393건이 skipped 로 잡혀 그 신호를 덮었다)
    listed = body.listed - body.excluded
    # indexed(=import된 URI 수)는 업로드된 URI 수의 하위 집합이어야 함.
    # gcs_uploaded 는 '파일 수' 라 파일당 URI가 2개(원본+.meta.md)면 어긋난다 →
    # uris(업로드된 URI 총수)와 비교. uris 미제공 시 gcs_uploaded 로 폴백.
    index_baseline = body.uris if body.uris > 0 else body.gcs_uploaded
    index_ok = body.indexed <= index_baseline
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
    return summary


# 하위 호환: 구 process 엔드포인트는 ingest+즉시 index (비권장)
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
