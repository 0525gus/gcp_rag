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
import sys
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from shared.config import Settings, get_settings  # noqa: E402
from shared.drive import DriveClient  # noqa: E402
from shared.firestore_state import DocStateStore  # noqa: E402
from shared.gcs import GcsClient  # noqa: E402
from shared.hashing import sha256_bytes, sha256_text  # noqa: E402
from shared.logging_config import setup_logging  # noqa: E402
from shared.mime_types import (  # noqa: E402
    GOOGLE_EXPORT_MAP,
    RouteKind,
    classify_route,
    is_hwpx,
)
from shared.models import DocState, DocStatus, ParseRoute  # noqa: E402
from shared.path_context import (  # noqa: E402
    PathContext,
    build_breadcrumb_markdown,
)
from shared.rag_engine import RagEngineClient  # noqa: E402

setup_logging()
logger = logging.getLogger("sync_service")

# FILE_COPY 중 텍스트는 본문에 breadcrumb를 직접 삽입
_TEXT_COPY_MIMES = frozenset(
    {
        "text/plain",
        "text/markdown",
        "text/html",
        "text/csv",
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
    indexed: int
    failed: int
    skipped: int
    deleted: int
    unchanged: int = 0
    dlq: int = 0
    split_queued: int = Field(default=0, alias="splitQueued")

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
            kind = RouteKind.SKIP
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
        "indexed": 0,
        "failed": 0,
        "skipped": 0,
        "deleted": 0,
        "unchanged": 0,
        "dlq": 0,
        "splitQueued": 0,
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
        idx = index_gcs(IndexGcsBody(gcsUris=uris, fileIds=ids))
        totals["indexed"] += int(idx.get("count") or len(uris))

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
                        if len(pending_uris) >= body.index_batch_size:
                            try:
                                flush_index()
                            except Exception:  # noqa: BLE001
                                logger.exception("backfill index flush failed")
                                totals["failed"] += len(pending_uris)
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
        logger.exception("backfill final index failed")
        with lock:
            totals["failed"] += len(pending_uris)

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
                kind = RouteKind.SKIP
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
    store = DocStateStore()
    settings = get_settings()
    gcs = GcsClient(settings)
    drive = DriveClient()

    folder_ids = settings.sync_folder_id_list
    if folder_ids and not body.removed:
        if not drive.is_in_sync_scope(body.file_id, folder_ids):
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
                    error="out_of_folder_scope",
                )
            )
            return {
                "fileId": body.file_id,
                "status": "skipped",
                "reason": "out_of_folder_scope",
            }

    route = RouteKind(body.route) if body.route else classify_route(
        body.mime_type, body.name, removed=body.removed
    )

    if route == RouteKind.DELETE or body.removed:
        return {"fileId": body.file_id, "status": "DELETE_PENDING", "route": "DELETE"}

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
            return _ingest_file_copy(body, store, gcs, drive, settings)
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


def _size_gate(
    store: DocStateStore,
    settings: Settings,
    body: IngestBody,
    data: bytes,
    *,
    splittable: bool,
) -> dict[str, Any] | None:
    """업로드 직전 크기 체크. 초과 시 FAILED/분할 큐. None이면 통과."""
    size = len(data)
    if size <= settings.max_gcs_bytes:
        return None
    reason = f"SIZE_EXCEEDED:{size}>{settings.max_gcs_bytes}"
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


def _state_fields(
    body: IngestBody,
    *,
    content_hash: str | None,
    status: DocStatus,
    parse_route: ParseRoute,
    source_uri: str | None,
    path_ctx: PathContext,
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

    gated = _size_gate(store, settings, body, md_bytes, splittable=True)
    if gated:
        gated["route"] = "HWP_PARSE"
        return gated

    if store.content_unchanged(body.file_id, content_hash):
        store.upsert(
            _state_fields(
                body,
                content_hash=content_hash,
                status=DocStatus.PARSED,
                parse_route=route,
                source_uri=body.web_view_link or md_uri,
                path_ctx=path_ctx,
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
    export_mime, ext = GOOGLE_EXPORT_MAP[body.mime_type]
    data = drive.export_file(body.file_id, export_mime)
    gated = _size_gate(store, settings, body, data, splittable=True)
    if gated:
        gated["route"] = "GOOGLE_EXPORT"
        return gated

    sidecar = build_breadcrumb_markdown(
        path=path_ctx.path,
        bundle=path_ctx.bundle,
        title=body.name or body.file_id,
    )
    content_hash = sha256_text(f"{sha256_bytes(data)}|{path_ctx.path}|{sidecar}")
    if store.content_unchanged(body.file_id, content_hash):
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


def _ingest_file_copy(
    body: IngestBody,
    store: DocStateStore,
    gcs: GcsClient,
    drive: DriveClient,
    settings: Settings,
) -> dict[str, Any]:
    path_ctx = _resolve_path_ctx(drive, body)
    data = drive.download_file(body.file_id)
    gated = _size_gate(store, settings, body, data, splittable=True)
    if gated:
        gated["route"] = "FILE_COPY"
        return gated

    name = body.name or body.file_id
    mime = (body.mime_type or "").lower()
    ext = Path(name).suffix or _ext_for_mime(body.mime_type)

    if mime in _TEXT_COPY_MIMES:
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            text = data.decode("utf-8", errors="replace")
        md_text = build_breadcrumb_markdown(
            path=path_ctx.path,
            bundle=path_ctx.bundle,
            title=name,
            body=text,
        )
        content_hash = sha256_text(md_text)
        if store.content_unchanged(body.file_id, content_hash):
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
    else:
        sidecar = build_breadcrumb_markdown(
            path=path_ctx.path,
            bundle=path_ctx.bundle,
            title=name,
        )
        content_hash = sha256_text(f"{sha256_bytes(data)}|{path_ctx.path}|{sidecar}")
        if store.content_unchanged(body.file_id, content_hash):
            return {
                "fileId": body.file_id,
                "status": "HASH_UNCHANGED",
                "route": "FILE_COPY",
                "contentHash": content_hash,
                "path": path_ctx.path,
                "bundle": path_ctx.bundle,
            }
        blob = f"normalized/{body.file_id}{ext}"
        gcs_uri = gcs.upload_bytes(
            data,
            settings.gcs_normalized_bucket,
            blob,
            content_type=body.mime_type or "application/octet-stream",
        )
        meta_uri = gcs.upload_path_sidecar_md(sidecar, body.file_id)
        uris = [gcs_uri, meta_uri]

    store.upsert(
        _state_fields(
            body,
            content_hash=content_hash,
            status=DocStatus.PARSED,
            parse_route=ParseRoute.GCS_COPY,
            source_uri=body.web_view_link or uris[0],
            path_ctx=path_ctx,
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


@app.post("/sync/index-gcs")
def index_gcs(body: IndexGcsBody) -> dict[str, Any]:
    """GCS URI만 RAG Engine에 증분 import. Drive 커넥터 미사용."""
    if not body.gcs_uris:
        return {"imported": [], "count": 0, "status": "EMPTY"}

    rag = RagEngineClient()
    store = DocStateStore()

    # upsert: 동일 fileId 기존 청크 제거 후 import
    for fid in body.file_ids:
        try:
            rag.delete_by_file_id(fid)
        except Exception:  # noqa: BLE001
            logger.warning("pre-delete failed for %s", fid)

    imported = rag.import_from_gcs(body.gcs_uris)

    for fid in body.file_ids:
        existing = store.get(fid)
        if existing:
            existing.status = DocStatus.INDEXED
            store.upsert(existing)
        else:
            store._col.document(fid).set(  # noqa: SLF001
                {"fileId": fid, "status": DocStatus.INDEXED.value}, merge=True
            )

    return {"imported": imported, "count": len(imported), "status": "INDEXED"}


class ReindexPendingBody(BaseModel):
    """PARSED(색인 누락) 문서를 GCS URI로 재인덱싱."""

    limit: int = Field(default=200, ge=1, le=2000)
    index_batch_size: int = Field(default=10, alias="indexBatchSize", ge=1, le=50)
    # true면 INDEXED도 다시 import (기본은 PARSED만)
    force: bool = False

    model_config = {"populate_by_name": True}


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


def _normalized_uris_for_file(settings: Settings, file_id: str) -> list[str]:
    """존재하는 정규화 객체 중 인덱싱 가능 URI만 반환. xlsx 원본은 제외(meta만)."""
    from google.cloud import storage

    client = storage.Client(project=settings.gcp_project_id)
    bucket = client.bucket(settings.gcs_normalized_bucket)
    prefix = f"normalized/{file_id}"
    uris: list[str] = []
    for blob in client.list_blobs(bucket, prefix=prefix):
        name = blob.name
        # fileId 접두 뒤에 다른 파일이 붙지 않게
        rest = name[len(f"normalized/{file_id}") :]
        if not rest.startswith(".") and rest != "":
            continue
        lower = name.lower()
        if lower.endswith(".xlsx") or lower.endswith(".xls"):
            continue
        if any(lower.endswith(suf) for suf in _INDEXABLE_SUFFIXES):
            uris.append(f"gs://{settings.gcs_normalized_bucket}/{name}")
    return uris


@app.post("/sync/reindex-pending")
def reindex_pending(body: ReindexPendingBody) -> dict[str, Any]:
    """Firestore PARSED(또는 force 시 전체) → GCS URI 모아 index-gcs.

    백필이 ingest만 하고 색인이 끊긴 경우 복구용.
    """
    settings = get_settings()
    store = DocStateStore()

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
        # PARSED 복구는 대부분 코퍼스 미존재 → 전량 list+delete 생략(속도)
        if body.force:
            idx = index_gcs(IndexGcsBody(gcsUris=uris, fileIds=uniq_ids))
            totals["indexed"] += int(idx.get("count") or len(uris))
            return
        rag = RagEngineClient()
        imported = rag.import_from_gcs(uris)
        for fid in uniq_ids:
            existing = store.get(fid)
            if existing:
                existing.status = DocStatus.INDEXED
                store.upsert(existing)
            else:
                store._col.document(fid).set(  # noqa: SLF001
                    {"fileId": fid, "status": DocStatus.INDEXED.value},
                    merge=True,
                )
        totals["indexed"] += len(imported)

    for doc in targets:
        uris = _normalized_uris_for_file(settings, doc.file_id)
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
                logger.exception("reindex-pending flush failed")
                totals["failed"] += len(pending_uris)
                pending_uris, pending_ids = [], []

    try:
        flush()
    except Exception:  # noqa: BLE001
        logger.exception("reindex-pending final flush failed")
        totals["failed"] += len(pending_uris)

    logger.info("reindex-pending done totals=%s", totals)
    return {"mode": "reindex-pending", "totals": totals, "ok": totals["failed"] == 0}


@app.post("/sync/delete")
def delete_file(body: DeleteBody) -> dict[str, Any]:
    rag = RagEngineClient()
    store = DocStateStore()
    settings = get_settings()
    gcs = GcsClient(settings)

    ok = rag.delete_by_file_id(body.file_id)
    # 정규화 객체 정리 (확장자 불명 → prefix 삭제는 서비스 계정 권한에 따라 제한될 수 있음)
    for suffix in (
        ".md",
        ".meta.md",
        ".pdf",
        ".docx",
        ".pptx",
        ".xlsx",
        ".txt",
        ".html",
        ".csv",
        ".bin",
    ):
        try:
            gcs.delete(
                f"gs://{settings.gcs_normalized_bucket}/normalized/{body.file_id}{suffix}"
            )
        except Exception:  # noqa: BLE001
            pass
    store.mark_deleted(body.file_id)
    return {"fileId": body.file_id, "deleted": ok, "status": DocStatus.DELETED.value}


@app.post("/sync/reconcile")
def reconcile(body: ReconcileBody) -> dict[str, Any]:
    """Drive 조회 건수 vs GCS 업로드·색인·스킵·실패·삭제 정합성."""
    accounted = (
        body.gcs_uploaded
        + body.failed
        + body.skipped
        + body.deleted
        + body.unchanged
        + body.dlq
        + body.split_queued
    )
    # indexed는 gcs_uploaded의 하위 집합이어야 함
    index_ok = body.indexed <= body.gcs_uploaded
    delta = body.listed - accounted
    ok = delta == 0 and index_ok
    summary = {
        "driveId": body.drive_id,
        "listed": body.listed,
        "gcsUploaded": body.gcs_uploaded,
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
