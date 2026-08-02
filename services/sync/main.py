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
from shared.drive import DriveClient, parse_drive_size  # noqa: E402
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


class BootstrapBody(DriveIdBody):
    # 기존 스냅샷을 의도적으로 건너뛰는 운영자 전용 동작. 기본은 fail closed.
    baseline_only: bool = Field(default=False, alias="baselineOnly")


class BackfillBody(BaseModel):
    drive_id: str = Field(..., alias="driveId")
    # true면 기존 pageToken이 있어도 전체 스캔 (초기 셋업용)
    force: bool = False

    model_config = {"populate_by_name": True}


class ChangesBody(DriveIdBody):
    # 한 번에 반환할 최대 변경 건수 (미지정 시 SYNC_MAX_CHANGES).
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
    removed: bool = False
    web_view_link: str | None = Field(default=None, alias="webViewLink")
    # Drive 가 알려준 원본 크기. 다운로드 전에 거르는 데 쓴다(없으면 사후 검사만).
    size_bytes: int | None = Field(default=None, alias="sizeBytes")
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
    """현재 Drive 스냅샷과 커밋 후보 pageToken을 반환한다.

    후보 토큰은 호출자가 스냅샷 처리를 성공한 뒤에만 저장해야 한다. 여기서 먼저
    저장하면 중간 실패 후 다음 실행이 미처리 스냅샷을 건너뛰게 된다.
    """
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
def bootstrap(body: BootstrapBody) -> dict[str, str]:
    """향후 변경만 추적할 기준점을 명시적으로 생성한다.

    일반 초기 동기화는 /sync/changes의 backfill 경로를 사용해야 한다. 기존 문서를
    건너뛰는 위험한 동작은 baselineOnly=true를 지정한 경우에만 허용한다.
    """
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
    import threading
    from concurrent.futures import ThreadPoolExecutor, as_completed

    settings = get_settings()
    store = DocStateStore()
    drive = DriveClient()
    # Firestore/Storage 클라이언트는 스레드 안전하므로 워커들이 공유한다.
    gcs = GcsClient(settings)
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
        # 색인 실패는 failed 와 분리한다 — 아래 커밋 게이트 주석 참고.
        "indexFailed": 0,
    }
    pending_uris: list[str] = []
    pending_ids: list[str] = []
    # lock: totals·pending 접근용(짧게). index_lock: import 직렬화용(길게).
    # 하나로 합치면 import 가 도는 수십 초 동안 워커 8개가 집계조차 못 하고 멈춘다
    # — 동시성이 사실상 1로 붕괴하고, 그 지연이 Cloud Run 타임아웃까지 이어진다.
    lock = threading.Lock()
    index_lock = threading.Lock()

    # 스냅샷 전체를 미리 지우면 안 된다. 재백필에서는 대부분의 파일이 UNCHANGED 로
    # 빠져 재import 되지 않으므로, 미리 지운 청크가 그대로 유실된다 — 그러고도
    # totals 는 unchanged 로 세고 ok=true 로 보고했다. 삭제는 import 하는 배치에서만
    # 한다(_import_and_mark). 클라이언트를 공유해 코퍼스 순회는 1회로 유지한다.
    rag = RagEngineClient()

    def _take_batch(min_size: int) -> tuple[list[str], list[str]] | None:
        """조건을 만족하면 대기열을 통째로 떼어 온다. 반드시 lock 밖에서 import 할 것."""
        nonlocal pending_uris, pending_ids
        with lock:
            if len(pending_uris) < min_size or not pending_uris:
                return None
            uris, ids = pending_uris, pending_ids
            pending_uris, pending_ids = [], []
            return uris, ids

    def flush_index(min_size: int = 1) -> int:
        """떼어 온 배치를 import 한다. 실패하면 그 배치의 파일 수를 돌려준다."""
        batch = _take_batch(min_size)
        if batch is None:
            return 0
        uris, ids = batch
        try:
            # import 는 직렬화하되(Vertex RPM), 집계 lock 은 쥐지 않는다.
            with index_lock:
                indexed = len(_import_and_mark(store, uris, ids, rag=rag))
        except Exception:  # noqa: BLE001
            logger.exception("backfill index flush failed")
            return len(dict.fromkeys(ids))
        with lock:
            totals["indexed"] += indexed
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
                    totals["failed"] += 1
                elif status == "SPLIT_QUEUED":
                    totals["splitQueued"] += 1
                    totals["failed"] += 1
                else:
                    totals["failed"] += 1

            index_failed = flush_index(body.index_batch_size)
            if index_failed:
                with lock:
                    totals["indexFailed"] += index_failed

    index_failed = flush_index()
    if index_failed:
        totals["indexFailed"] += index_failed

    # 색인 실패를 failed 에 더하면 이중 집계다 — 그 파일은 이미 gcsUploaded 로
    # 세어져 있어서 reconcile 의 listed 항등식이 깨진다(unaccounted 가 음수).
    # 별도 지표로 두고, 커밋 게이트에서는 워크플로우와 같은 항등식으로 막는다.
    index_complete = totals["indexed"] == totals["uris"]
    ok = totals["failed"] == 0 and totals["indexFailed"] == 0 and index_complete

    if pending_page_token and ok:
        store.set_start_page_token(body.drive_id, pending_page_token)

    logger.info("backfill-run done drive=%s totals=%s", body.drive_id, totals)
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
    """델타 조회. pageToken은 commit-token 전까지 커밋하지 않음.

    한 번에 최대 maxChanges 건만 반환하고, 남으면 hasMore=true 로 알린다. 호출측은
    hasMore 가 false 가 될 때까지 (처리 → 커밋 → 재호출) 을 반복한다. 배치마다
    토큰을 커밋할 수 있으므로 중간에 실패해도 앞 배치는 확정된다.

    토큰이 없으면(최초) 목록 대신 mode=backfill_required 를 돌려준다. 드라이브 전체
    스냅샷은 크기가 델타와 달리 상한이 없고 재개 지점도 없어서, 서버 안에서 끝내는
    /sync/backfill-run 이 유일하게 안전한 경로다.

    SYNC_FOLDER_IDS가 있으면 해당 폴더 트리 밖 파일은 SKIP.
    삭제(removed)는 이전에 색인됐을 수 있어 범위 밖이어도 DELETE 유지.
    """
    store = DocStateStore()
    drive = DriveClient()
    settings = get_settings()
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
            "sizeBytes": ch.size_bytes,
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
        "hasMore": has_more,
        "mode": "delta",
    }


@app.post("/sync/commit-token")
def commit_token(body: CommitTokenBody) -> dict[str, str]:
    store = DocStateStore()
    store.set_start_page_token(body.drive_id, body.page_token)
    return {"driveId": body.drive_id, "pageToken": body.page_token, "status": "committed"}


_OUT_OF_SCOPE_CLEANUP_ERROR = "out_of_folder_scope_cleanup_failed"
_OUT_OF_SCOPE_REASON = "out_of_folder_scope"


def _already_evicted(existing: DocState | None) -> bool:
    """이 파일의 범위 밖 정리가 이미 성공적으로 끝났는가.

    ``SKIPPED`` 만으로는 판단할 수 없다 — 지원하지 않는 MIME 으로 바뀐 문서도
    코퍼스에 청크를 남긴 채 SKIPPED 가 된다. 정리를 마친 뒤에만 찍히는
    ``error=out_of_folder_scope`` 마커까지 맞을 때만 생략한다.
    """
    return bool(
        existing
        and existing.status == DocStatus.SKIPPED
        and (existing.error or "") == _OUT_OF_SCOPE_REASON
    )


def _delete_gcs_prefix_for_file(gcs: GcsClient, bucket: str, prefix: str) -> int:
    """Delete one file's objects below a prefix without matching longer file ids."""
    if not bucket:
        return 0
    blobs = list(
        gcs._client.list_blobs(
            bucket,
            prefix=prefix,
        )
    )
    deleted = 0
    failures: list[Exception] = []
    for blob in blobs:
        rest = blob.name[len(prefix) :]
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
            f"failed to delete {len(failures)} GCS object(s) under {prefix}"
        ) from failures[0]
    return deleted


def _cleanup_out_of_scope_file(
    gcs: GcsClient, settings: Settings, file_id: str
) -> tuple[bool, int, int]:
    """Best-effort both cleanup targets, then fail if either target errored."""
    failures: list[Exception] = []
    rag_deleted = False
    normalized_deleted = 0
    raw_deleted = 0
    try:
        # False means no matching RAG file remained, which is an idempotent success.
        rag_deleted = RagEngineClient().delete_by_file_id(file_id)
    except Exception as exc:
        failures.append(exc)
        logger.exception("out-of-scope RAG cleanup failed: %s", file_id)
    for bucket, prefix, kind in (
        (
            settings.gcs_normalized_bucket,
            f"normalized/{file_id}",
            "normalized",
        ),
        (settings.gcs_raw_bucket, f"raw/{file_id}", "raw"),
    ):
        try:
            count = _delete_gcs_prefix_for_file(gcs, bucket, prefix)
            if kind == "normalized":
                normalized_deleted = count
            else:
                raw_deleted = count
        except Exception as exc:
            failures.append(exc)
            logger.exception("out-of-scope %s GCS cleanup failed: %s", kind, file_id)
    if failures:
        raise RuntimeError(
            f"out-of-scope cleanup failed for {file_id} ({len(failures)} target(s))"
        ) from failures[0]
    return rag_deleted, normalized_deleted, raw_deleted


@app.post("/sync/ingest")
def ingest(body: IngestBody) -> dict[str, Any]:
    """Drive 문서를 GCS 정규화 버킷에 적재. RAG import는 /sync/index-gcs."""
    settings = get_settings()
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
    """ingest 본체. 클라이언트를 주입받아 대량 경로에서 재사용할 수 있게 한다.

    파일마다 DriveClient 를 새로 만들면 인증 + discovery build 가 파일 수만큼 돌고,
    parents/name 캐시가 인스턴스 단위라 조상 폴더를 파일마다 다시 조회하게 된다
    (N × 폴더깊이 회의 files.get). 백필에서 이 비용이 지배적이다.
    """
    folder_ids = settings.sync_folder_id_list
    if (
        folder_ids
        and not body.removed
        and not drive.is_in_sync_scope(body.file_id, folder_ids)
    ):
        rag_deleted = False
        normalized_deleted = 0
        raw_deleted = 0
        if _already_evicted(store.get(body.file_id)):
            # 이미 이 사유로 정리를 마친 파일이다. 다시 부르면 코퍼스를 파일마다
            # 전수 순회하는데(정리할 것도 없이), 범위 밖 파일은 바뀔 때마다 델타에
            # 다시 실려 오므로 그 비용이 매 실행 반복된다.
            return {
                "fileId": body.file_id,
                "status": DocStatus.SKIPPED.value,
                "reason": _OUT_OF_SCOPE_REASON,
                "ragDeleted": False,
                "normalizedDeleted": 0,
                "rawDeleted": 0,
                "cleanupSkipped": True,
            }
        try:
            rag_deleted, normalized_deleted, raw_deleted = (
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
                status=DocStatus.SKIPPED,
                parse_route=ParseRoute.NONE,
                source_uri=body.web_view_link,
                error=_OUT_OF_SCOPE_REASON,
            )
        )
        return {
            "fileId": body.file_id,
            "status": DocStatus.SKIPPED.value,
            "reason": "out_of_folder_scope",
            "ragDeleted": rag_deleted,
            "normalizedDeleted": normalized_deleted,
            "rawDeleted": raw_deleted,
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
            # 재시도가 Drive 링크를 복원할 수 있도록 남긴다. 여기서 빠뜨리면
            # retry-failed 가 sourceUri 를 gs:// 로 덮어쓴다.
            sourceUri=body.web_view_link,
        )
        return {
            "fileId": body.file_id,
            "status": "DLQ",
            "route": route.value,
            "error": str(exc)[:500],
        }


_MB = 1024 * 1024

# Vertex AI RAG Engine 의 파일 타입별 import 상한.
# 초과분을 잘라 주는 게 아니라 import 자체가 실패한다 — 청킹(chunk_size)은 한도
# 안에 들어온 파일에만 적용된다. 게이트를 통과시키면 index-gcs 가 배치 전체를
# 실패로 돌리고, 그 문서는 PARSED 에 머물며 매일 재시도만 반복한다.
# 출처: cloud.google.com/vertex-ai/generative-ai/docs/supported-documents
_RAG_SIZE_LIMITS: dict[str, int] = {
    ".md": 10 * _MB,
    ".txt": 10 * _MB,
    ".html": 10 * _MB,
    ".pptx": 10 * _MB,
    ".docx": 50 * _MB,
    ".pdf": 50 * _MB,
}
# 문서에 상한이 없는 형식(.xlsx/.csv/.bin 등)은 보수적으로 낮은 쪽을 쓴다.
_RAG_SIZE_LIMIT_DEFAULT = 10 * _MB


def _size_limit_for(settings: Settings, ext: str) -> int:
    """RAG import 상한과 운영 상한(MAX_GCS_BYTES) 중 엄격한 쪽."""
    rag_limit = _RAG_SIZE_LIMITS.get((ext or "").lower(), _RAG_SIZE_LIMIT_DEFAULT)
    return min(settings.max_gcs_bytes, rag_limit)


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
    """크기 체크. 초과 시 FAILED/분할 큐. None이면 통과(크기 미상 포함).

    ``ext`` 는 **실제로 GCS 에 올라갈** 확장자다. 텍스트 FILE_COPY 는 breadcrumb 를
    붙여 .md 로 올라가므로 원본 MIME 이 아니라 업로드 형식으로 재야 한다.

    ``limit`` 을 주면 RAG 한도 대신 그 값을 쓴다 — HWP 원본처럼 '업로드물이 아니라
    메모리에 올릴 바이트'를 막을 때 사용한다.
    """
    if size is None:
        return None
    if limit is None:
        limit = _size_limit_for(settings, ext)
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

    # 원본을 통째로 메모리에 올리기 전에 막는다. 여기 한도는 RAG import 상한이
    # 아니라 운영 메모리 상한이다 — 60MB HWP 가 2MB 마크다운이 되는 일도 흔하다.
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
    raw = drive.download_file(body.file_id)
    ext = src_ext
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
            # 파서는 서로 다른 4가지를 모두 422 로 낸다 — PARSE_FAILED(코드/라이브러리
            # 결함), EMPTY_TEXT, QUALITY_GATE(임계 미달), FALLBACK_FAILED. 전부
            # "QUALITY_GATE" 로 적으면 DLQ 에서 '파서가 깨진 것'과 '문서 품질이 낮은
            # 것'을 구분할 수 없다. 파서가 준 분류를 그대로 쓴다.
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
                # 하드코딩하면 HWPX 문서가 DLQ 에 RHWP 로 남는다.
                parseRoute=(
                    ParseRoute.HWPX.value
                    if is_hwpx(body.mime_type, body.name)
                    else ParseRoute.RHWP.value
                ),
                sourceUri=body.web_view_link,
                path=path_ctx.path,
                bundle=path_ctx.bundle,
            )
            # 거부된 원본은 아무도 다시 읽지 않는다 — 재시도도 Drive 에서 새로
            # 내려받아 같은 경로를 덮어쓴다. 안 지우면 영구 실패 문서만큼 raw/ 가
            # 단조 증가한다. 실패해도 DLQ 결과를 뒤집지는 않는다.
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
        title=body.name or body.file_id,
        body=md_bytes.decode("utf-8", errors="replace"),
    )
    md_bytes = md_text.encode("utf-8")
    # 업로드 전에 잰다 — 파서가 이미 같은 경로에 써 둔 객체는 별개지만,
    # 한도 초과분을 다시 올릴 이유는 없다.
    gated = _size_gate(store, settings, body, len(md_bytes), splittable=True, ext=".md")
    if gated:
        gated["route"] = "HWP_PARSE"
        return gated

    content_hash = sha256_text(md_text)
    md_uri = gcs.upload_normalized_md(md_text, body.file_id)

    if store.should_skip_reindex(body.file_id, content_hash):
        # 이미 INDEXED 이고 내용도 그대로다 — modifiedTime 만 전진시킨다.
        # 여기서 PARSED 로 덮어쓰면 색인된 문서가 '색인 누락'으로 강등된다.
        store.touch_modified_time(body.file_id, body.modified_time)
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
    gated = _size_gate(store, settings, body, len(data), splittable=True, ext=ext)
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
        # 전진시키지 않으면 should_reparse 가 매 실행 참이 되어 같은 문서를
        # 매일 다시 export 한다(내용이 그대로인 걸 확인하려고).
        store.touch_modified_time(body.file_id, body.modified_time)
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
    name = body.name or body.file_id
    mime = (body.mime_type or "").lower()
    ext = Path(name).suffix or _ext_for_mime(body.mime_type)
    # 텍스트는 breadcrumb 를 붙여 .md 로 나가므로 상한도 .md 기준이다.
    upload_ext = ".md" if mime in _TEXT_COPY_MIMES else ext

    # 다운로드 전 1차 차단. 크기 미상이면 통과하고 아래 사후 검사가 잡는다.
    gated = _size_gate(
        store, settings, body, body.size_bytes, splittable=True, ext=upload_ext
    )
    if gated:
        gated["route"] = "FILE_COPY"
        return gated

    path_ctx = _resolve_path_ctx(drive, body)
    data = drive.download_file(body.file_id)

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
        # 텍스트는 원본 확장자와 무관하게 breadcrumb 를 붙여 .md 로 올라간다.
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
        gcs_uri = gcs.upload_normalized_md(md_text, body.file_id)
        uris = [gcs_uri]
    else:
        gated = _size_gate(store, settings, body, len(data), splittable=True, ext=ext)
        if gated:
            gated["route"] = "FILE_COPY"
            return gated
        sidecar = build_breadcrumb_markdown(
            path=path_ctx.path,
            bundle=path_ctx.bundle,
            title=name,
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


def _import_and_mark(
    store: DocStateStore,
    gcs_uris: list[str],
    file_ids: list[str],
    *,
    rag: Any | None = None,
) -> list[str]:
    """이번 배치의 기존 청크만 제거 → import → 성공분만 INDEXED 로 전환.

    **삭제 대상은 반드시 이번에 import 할 파일과 같아야 한다.** 실행 시작 시
    전체를 미리 지우면, 중간에 UNCHANGED 로 빠져 재import 되지 않는 문서까지
    코퍼스에서 사라진다(백필이 정상 코퍼스를 비우고 성공으로 보고했다).
    삭제를 import 바로 앞에 두면 지우는 집합과 넣는 집합이 정의상 같아진다.

    ``rag`` 를 넘겨 **run 전체가 클라이언트 하나를 공유**하면 코퍼스 순회는
    첫 배치에서 한 번만 일어난다(RagEngineClient.delete_files_by_ids 참고).
    넘기지 않으면 이 호출만의 클라이언트를 쓴다.
    """
    client = rag if rag is not None else RagEngineClient()
    # 삭제 실패 뒤 import 를 계속하면 이전 청크와 새 청크가 함께 남으므로 fail closed.
    client.delete_files_by_ids(list(dict.fromkeys(file_ids)))
    imported = client.import_from_gcs(gcs_uris)
    if len(imported) != len(gcs_uris):
        raise RuntimeError(
            "RAG import result mismatch: "
            f"requested={len(gcs_uris)} completed={len(imported)}"
        )
    for fid in dict.fromkeys(file_ids):
        existing = store.get(fid)
        if existing:
            existing.status = DocStatus.INDEXED
            store.upsert(existing)
        else:
            store.mark_indexed(fid)
    return imported


@app.post("/sync/index-gcs")
def index_gcs(body: IndexGcsBody) -> dict[str, Any]:
    """GCS URI만 RAG Engine에 증분 import. Drive 커넥터 미사용."""
    if not body.gcs_uris:
        return {"imported": [], "count": 0, "status": "EMPTY"}

    store = DocStateStore()
    # 기존 청크 제거는 _import_and_mark 안에서 import 바로 앞에 한다. 여기서 한 번
    # 더 지우면 같은 배치에 코퍼스를 두 번 순회하게 된다.
    imported = _import_and_mark(store, body.gcs_uris, body.file_ids)
    return {"imported": imported, "count": len(imported), "status": "INDEXED"}


class ReindexPendingBody(BaseModel):
    """PARSED(색인 누락) 문서를 GCS URI로 재인덱싱."""

    limit: int = Field(default=200, ge=1, le=2000)
    index_batch_size: int = Field(default=10, alias="indexBatchSize", ge=1, le=50)
    # true면 INDEXED도 다시 import (기본은 PARSED만)
    force: bool = False

    model_config = {"populate_by_name": True}


# ingest가 RAG import 대상으로 방출하는 확장자 (+ sidecar meta.md).
# 복구에서 원본을 빼면 meta.md만 색인하고 문서 전체를 INDEXED로 오인하므로,
# 지원 여부와 무관하게 최초 import와 동일한 URI 집합을 다시 제출해 fail closed 한다.
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


def _normalized_uris_for_file(settings: Settings, file_id: str) -> list[str]:
    """존재하는 정규화 객체 중 최초 ingest가 방출한 형식의 URI를 반환."""
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

    pending_uris: list[str] = []
    pending_ids: list[str] = []
    totals = {
        "candidates": len(targets),
        "withUris": 0,
        "indexed": 0,
        "skippedNoUri": 0,
        "failed": 0,
    }

    # run 전체가 공유 → 코퍼스 순회는 첫 배치에서 1회.
    rag = RagEngineClient()

    def flush() -> None:
        nonlocal pending_uris, pending_ids
        if not pending_uris:
            return
        uris, ids = pending_uris, pending_ids
        uniq_ids = list(dict.fromkeys(ids))
        indexed = len(_import_and_mark(store, uris, uniq_ids, rag=rag))
        for fid in uniq_ids:
            try:
                store.clear_dlq(fid)
            except Exception:  # noqa: BLE001
                logger.exception("failed to clear recovered DLQ entry: %s", fid)
        totals["indexed"] += indexed
        pending_uris, pending_ids = [], []

    resolved: list[tuple[str, list[str]]] = []
    for doc in targets:
        uris = _normalized_uris_for_file(settings, doc.file_id)
        if not uris:
            totals["skippedNoUri"] += 1
            totals["failed"] += 1
            try:
                store.enqueue_dlq(
                    doc.file_id,
                    "reindex_no_normalized_uri",
                    driveId=doc.drive_id,
                    name=doc.name,
                    mimeType=doc.mime_type,
                    modifiedTime=doc.modified_time,
                    sourceUri=doc.source_uri,
                )
            except Exception:  # noqa: BLE001
                # 상태 전이 실패 시에도 라운드로빈 커서가 다시 이 문서로 돌아온다.
                logger.exception(
                    "failed to enqueue no-URI document for ingest retry: %s",
                    doc.file_id,
                )
            continue
        totals["withUris"] += 1
        resolved.append((doc.file_id, uris))

    # 기존 청크 제거는 배치마다 _import_and_mark 가 한다 — 지우는 집합과 넣는
    # 집합이 항상 같아야 하기 때문이다. 여기서 resolved 전체를 미리 지우면 뒤에서
    # 플러시가 실패한 배치의 문서가 청크 없이 남는다.
    # 삭제 자체를 생략해선 안 된다: '과거 INDEXED → 재파싱 → 색인 실패' 문서에서
    # Vertex 가 같은 파일을 skip 하고, import_from_gcs 는 skipped 를 성공으로 세므로
    # 구버전 내용인 채 INDEXED 로 확정된다(조용한 스테일).
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

    try:
        flush()
    except Exception:  # noqa: BLE001
        logger.exception("reindex-pending final flush failed")
        totals["failed"] += len(pending_uris)
        pending_uris, pending_ids = [], []

    logger.info("reindex-pending done totals=%s", totals)
    # skippedNoUri 는 failed 에도 함께 더해지므로 따로 조건에 넣을 필요가 없다.
    return {
        "mode": "reindex-pending",
        "totals": totals,
        "ok": totals["failed"] == 0,
    }


class RetryFailedBody(BaseModel):
    """FAILED(DLQ) 문서를 ingest부터 재시도."""

    limit: int = Field(default=100, ge=1, le=1000)
    parser_url: str = Field(default="", alias="parserUrl")
    # 이 횟수만큼 재시도해도 실패하면 영구 실패로 두고 건너뜀 (무한 재시도 방지)
    max_attempts: int = Field(default=3, alias="maxAttempts", ge=1, le=10)
    index_batch_size: int = Field(default=10, alias="indexBatchSize", ge=1, le=50)

    model_config = {"populate_by_name": True}


def _drive_link(source_uri: str | None) -> str | None:
    """저장된 sourceUri 가 Drive 링크일 때만 재사용한다.

    ingest 계열은 ``web_view_link or <gcs uri>`` 로 sourceUri 를 정하므로, 링크를
    넘기지 않으면 merge 로 기존 Drive 링크가 gs:// 로 덮여 인용이 열리지 않는다.
    gs:// 는 링크가 아니므로 넘기지 않는다(그 경우 기존 동작 유지).
    """
    uri = (source_uri or "").strip()
    return uri if uri.startswith(("http://", "https://")) else None


@app.post("/sync/retry-failed")
def retry_failed(body: RetryFailedBody) -> dict[str, Any]:
    """FAILED 문서를 ingest부터 재구동하고 GCS_READY면 색인까지 이어붙인다.

    일시적 오류(Drive 429/5xx, 파서 타임아웃)로 DLQ에 빠진 문서를 자동 회수한다.
    max_attempts 초과 문서는 실제 결함으로 보고 건너뛴다.
    """
    store = DocStateStore()
    targets = store.list_by_status(
        DocStatus.FAILED,
        limit=body.limit,
        cursor_key="retry-failed",
    )

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
                    webViewLink=_drive_link(doc.source_uri),
                    parserUrl=body.parser_url,
                )
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
    # exhausted 는 maxAttempts 를 넘겨 '영구 보류'로 둔 문서다 — 이번 실행의 실패가
    # 아니다. ok 에 포함하면 손상된 문서 1건 때문에 배치가 매일 실패로 보고돼
    # 진짜 장애가 묻힌다. 별도 지표(parked)로 올리고 경고 로그로만 남긴다.
    return {
        "mode": "retry-failed",
        "totals": totals,
        "ok": totals["stillFailed"] == 0,
        "parked": totals["exhausted"],
    }


@app.post("/sync/delete")
def delete_file(body: DeleteBody) -> dict[str, Any]:
    rag = RagEngineClient()
    store = DocStateStore()
    settings = get_settings()
    gcs = GcsClient(settings)

    ok = rag.delete_by_file_id(body.file_id)

    # 확장자 목록으로 지우면 새는 경로가 많다 — FILE_COPY 는 Path(name).suffix 를
    # 그대로 쓰므로 ".DOCX"·".hwp"·".rtf" 같은 값이 얼마든지 나온다. prefix 나열로
    # 실제 존재하는 객체만 지운다(_delete_gcs_prefix_for_file 이 fileId 경계를 지킴).
    # raw/ 도 함께 지운다 — 안 지우면 삭제한 원본이 버킷에 영구히 남는다.
    failures: list[Exception] = []
    counts = {"normalized": 0, "raw": 0}
    for bucket, prefix, kind in (
        (settings.gcs_normalized_bucket, f"normalized/{body.file_id}", "normalized"),
        (settings.gcs_raw_bucket, f"raw/{body.file_id}", "raw"),
    ):
        try:
            counts[kind] = _delete_gcs_prefix_for_file(gcs, bucket, prefix)
        except Exception as exc:
            failures.append(exc)
            logger.exception("delete %s GCS cleanup failed: %s", kind, body.file_id)

    # 코퍼스는 이미 정리됐으므로 상태는 DELETED 가 맞다. 다만 GCS 가 남았으면
    # 성공으로 위장하지 않고 올려 보내 재시도(=삭제 change 재생)되게 한다.
    store.mark_deleted(body.file_id)
    if failures:
        raise HTTPException(
            status_code=500,
            detail=f"gcs cleanup failed for {body.file_id}: {failures[0]}"[:500],
        ) from failures[0]

    return {
        "fileId": body.file_id,
        "deleted": ok,
        "status": DocStatus.DELETED.value,
        "normalizedDeleted": counts["normalized"],
        "rawDeleted": counts["raw"],
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
    # indexed(=처리 완료된 URI 수)는 업로드된 URI 수와 정확히 같아야 함.
    # gcs_uploaded 는 '파일 수' 라 파일당 URI가 2개(원본+.meta.md)면 어긋난다 →
    # uris(업로드된 URI 총수)와 비교. uris 미제공 시 gcs_uploaded 로 폴백.
    index_baseline = body.uris if body.uris > 0 else body.gcs_uploaded
    index_ok = body.indexed == index_baseline
    delta = body.listed - accounted
    ok = delta == 0 and index_ok
    summary = {
        "driveId": body.drive_id,
        "listed": body.listed,
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
