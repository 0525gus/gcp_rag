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

from shared.config import Settings, UnknownDriveError, get_settings  # noqa: E402
from shared.drive import DriveClient, parse_drive_size  # noqa: E402
from shared.firestore_state import DocStateStore  # noqa: E402
from shared.gcs import GcsClient, gs_uri  # noqa: E402
from shared.hashing import sha256_bytes, sha256_text  # noqa: E402
from shared.logging_config import setup_logging  # noqa: E402
from shared.mime_types import (  # noqa: E402
    GOOGLE_EXPORT_MAP,
    SIDECAR_ONLY_MIME,
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
from shared.rag_engine import ImportOutcome, RagEngineClient  # noqa: E402
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
    # 처리를 **마쳤고** 별도 큐(DLQ·분할 대기)로 보낸 문서 수. dlq/splitQueued 의 합과
    # 같지만 그쪽은 세부 분류라 accounted 에는 이 값만 더한다.
    #
    # failed 와 분리하는 이유는 pageToken 커밋 때문이다. failed 는 '다시 하면 될 수도
    # 있는 것'이라 토큰을 막아야 하지만, parked 는 '다시 해도 같은 결과'다. 막으면
    # 그 한 건 때문에 같은 페이지가 매일 재생되고 드라이브가 영구 정지한다.
    parked: int = 0
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
    index_batch_size: int = Field(default=10, alias="indexBatchSize", ge=1, le=50)

    model_config = {"populate_by_name": True}


# Cloud Run 요청 타임아웃(1800s)보다 넉넉히 잡되, 프로세스가 죽었을 때 다음 실행이
# 하루 안에는 반드시 들어올 수 있어야 한다.
_BACKFILL_LOCK_TTL_SECONDS = 3600


@app.post("/sync/backfill-run")
def backfill_run(body: BackfillRunBody) -> dict[str, Any]:
    """드라이브당 하나만 돌도록 잠근 뒤 실제 백필을 수행한다."""
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
    finally:
        store.release_lock(lock_name)


def _backfill_run_locked(body: BackfillRunBody, store: DocStateStore) -> dict[str, Any]:
    """초기 전체 적재: Drive 스냅샷 → ingest(병렬) → index-gcs 배치.

    Workflow에 수천 개 change를 올리면 메모리 한도에 걸리므로 init은 여기서 수행.
    """
    import threading
    from concurrent.futures import ThreadPoolExecutor, as_completed

    settings = _settings_for_drive(get_settings(), body.drive_id)
    drive = DriveClient()
    # Firestore/Storage 클라이언트는 스레드 안전하므로 워커들이 공유한다.
    gcs = GcsClient(settings)
    parser_url = body.parser_url or os.environ.get("PARSER_URL", "")
    workers = settings.ingest_concurrency

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
        # 처리를 마치고 별도 큐로 보낸 문서. failed 와 달리 커밋을 막지 않는다.
        "parked": 0,
        # 색인이 덜 된 URI 수. failed 와 분리한다 — reconcile 로 가는 값들과 단위가
        # 달라(그쪽 accounted 는 '파일 수' 기준) 섞으면 없는 불일치가 보고된다.
        # pageToken 커밋 판단에만 쓴다.
        # 키 이름은 워크플로우가 읽는 것과 같아야 한다(totals.indexFailed) —
        # 다르면 커밋 게이트가 색인 실패를 못 보고 항상 0 으로 통과한다.
        "indexFailed": 0,
        # 백필은 범위 밖 파일을 목록에 넣지 않으므로 스냅샷 집계를 그대로 쓴다
        "excluded": int(snapshot.get("skippedOutOfScope") or 0),
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
    rag = RagEngineClient(settings)

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
                indexed = _import_and_mark(store, uris, ids, rag=rag).imported
                # 학생 코퍼스도 같은 배치에서 맞춘다. 델타 경로는 /sync/index-gcs
                # 가 대신 해 주지만 백필은 그 엔드포인트를 거치지 않는다. 여기를
                # 빼면 **초기 적재 직후 학생 코퍼스가 통째로 비어 있고**, 그 문서들은
                # 이미 INDEXED 라 이후 델타에도 안 걸려 영영 안 채워진다.
                _sync_student_corpus(uris, ids, settings, store)
        except Exception:  # noqa: BLE001
            logger.exception("backfill index flush failed")
            return len(dict.fromkeys(ids))
        with lock:
            totals["indexed"] += indexed
            # 배치가 통째로 죽는 것(위 except)과 달리, 일부 URI 만 색인이 안 된
            # 경우는 예외가 없다. 여기서 세지 않으면 커밋 게이트가 통과해 버린다.
            totals["indexFailed"] += max(0, len(uris) - indexed)
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
                    # 델타 경로와 같은 이유로 failed 가 아니라 parked 다 — 다시 돌려도
                    # 같은 결과인 문서가 pageToken 커밋을 막으면 백필이 영영 안 끝난다.
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

    index_failed = flush_index()
    if index_failed:
        totals["indexFailed"] += index_failed

    # 색인 실패를 failed 에 더하면 이중 집계다 — 그 파일은 이미 gcsUploaded 로
    # 세어져 있어서 reconcile 의 listed 항등식이 깨진다(unaccounted 가 음수).
    # 별도 지표로 두고, 커밋 게이트에서는 워크플로우와 같은 항등식으로 막는다.
    index_complete = totals["indexed"] == totals["uris"]
    ok = totals["failed"] == 0 and totals["indexFailed"] == 0 and index_complete

    # 색인이 덜 된 채 토큰을 커밋하면 그 변경분을 다시 볼 기회가 사라진다.
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

    상태만으로는 판단할 수 없다 — 지원하지 않는 MIME 으로 바뀐 문서도 코퍼스에
    청크를 남긴 채 SKIPPED 가 된다. 정리를 마친 뒤에만 찍히는
    ``error=out_of_folder_scope`` 마커까지 맞을 때만 생략한다.

    EXCLUDED 가 정상 상태이고, SKIPPED 는 상태 신설 이전에 쓰인 값이다.
    마이그레이션 전 잔여 문서도 정리를 다시 돌리지 않도록 둘 다 받는다.
    """
    return bool(
        existing
        and existing.status in {DocStatus.EXCLUDED, DocStatus.SKIPPED}
        and (existing.error or "") == _OUT_OF_SCOPE_REASON
    )


def _delete_gcs_objects_for_file(gcs: GcsClient, bucket: str, file_id: str) -> int:
    """Delete one file's objects without matching longer file ids."""
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
    """Best-effort both cleanup targets, then fail if either target errored."""
    failures: list[Exception] = []
    rag_deleted = False
    source_deleted = 0
    hwp_original_deleted = 0
    try:
        # False means no matching RAG file remained, which is an idempotent success.
        # **settings 를 넘긴다.** 인자를 빼면 전역 기본 코퍼스를 지우게 되어,
        # 학과를 갈라 놓아도 교직원 쪽 문서는 실제 코퍼스에 그대로 남는다
        # (바로 아래 학생 코퍼스는 처음부터 settings 를 쓰고 있었다 — 비대칭이었다).
        rag_deleted = RagEngineClient(settings).delete_by_file_id(file_id)
    except Exception as exc:
        failures.append(exc)
        logger.exception("out-of-scope RAG cleanup failed: %s", file_id)
    # 분리가 켜져 있으면 학생 코퍼스에서도 내린다. 인자 없는 클라이언트는 기본
    # (=교직원) 코퍼스만 보므로, 여기를 빼면 범위 밖으로 나간 문서가 교직원
    # 검색에서만 사라지고 **학생에게는 계속 검색된다** — 내려야 할 자료가
    # 안 내려가는 쪽이라 조용히 넘기지 않고 실패로 올린다.
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
    """Drive 문서를 GCS 정규화 버킷에 적재. RAG import는 /sync/index-gcs."""
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
    """ingest 본체. 클라이언트를 주입받아 대량 경로에서 재사용할 수 있게 한다.

    파일마다 DriveClient 를 새로 만들면 인증 + discovery build 가 파일 수만큼 돌고,
    parents/name 캐시가 인스턴스 단위라 조상 폴더를 파일마다 다시 조회하게 된다
    (N × 폴더깊이 회의 files.get). 백필에서 이 비용이 지배적이다.
    """
    folder_ids = settings.sync_folder_id_list
    # EXCLUDE 는 list_changes 가 같은 folder_ids 로 이미 판정해 붙인 라우트다.
    # 여기서 is_in_sync_scope 를 다시 부르면 Drive 조회만 한 번 더 든다.
    routed_out_of_scope = body.route == RouteKind.EXCLUDE.value
    if not body.removed and (
        routed_out_of_scope
        or (folder_ids and not drive.is_in_sync_scope(body.file_id, folder_ids))
    ):
        rag_deleted = False
        source_deleted = 0
        hwp_original_deleted = 0
        if _already_evicted(store.get(body.file_id)):
            # 이미 이 사유로 정리를 마친 파일이다. 다시 부르면 코퍼스를 파일마다
            # 전수 순회하는데(정리할 것도 없이), 범위 밖 파일은 바뀔 때마다 델타에
            # 다시 실려 오므로 그 비용이 매 실행 반복된다.
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
        # 잔존물을 회수한 뒤에야 EXCLUDED 로 확정한다. SKIPPED 로 찍으면
        # '대상인데 처리 못 함'과 섞여 집계가 흐려진다 — EXCLUDED 는 reconcile 의
        # listed 에서 차감되고 cleanup 의 살아있는 상태 목록에서도 빠진다.
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
    """크기 체크. 초과 시 FAILED/분할 큐. None이면 통과(크기 미상 포함).

    ``ext`` 는 **실제로 GCS 에 올라갈** 확장자다. 텍스트 FILE_COPY 는 breadcrumb 를
    붙여 .md 로 올라가므로 원본 MIME 이 아니라 업로드 형식으로 재야 한다.

    ``limit`` 을 주면 RAG 한도 대신 그 값을 쓴다 — HWP 원본처럼 '업로드물이 아니라
    메모리에 올릴 바이트'를 막을 때 사용한다.
    """
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
            # 내려받아 같은 경로를 덮어쓴다. 안 지우면 영구 실패 문서만큼 hwp-original 버킷이
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
    # 파서 결과는 마크다운이므로 md 한도(10MB)가 적용된다.
    # 업로드 전에 잰다 — 파서가 이미 같은 경로에 써 둔 객체는 별개지만,
    # 한도 초과분을 다시 올릴 이유는 없다.
    gated = _size_gate(store, settings, body, len(md_bytes), splittable=True, ext=".md")
    if gated:
        gated["route"] = "HWP_PARSE"
        return gated

    content_hash = sha256_text(md_text)
    md_uri = gcs.upload_source_md(md_text, body.file_id)

    if store.should_skip_reindex(body.file_id, content_hash):
        # 이미 INDEXED 이고 내용도 그대로다 — modifiedTime 만 전진시킨다.
        # 여기서 PARSED 로 덮어쓰면 색인된 문서가 '색인 누락'으로 강등된다.
        # audience 는 같이 넘긴다: 내용이 그대로여도 폴더가 학생↔교직원으로
        # 옮겨졌으면 대상 코퍼스가 바뀌므로, 안 넘기면 영영 옛 코퍼스에 남는다.
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
    """본문 추출 수단이 없는 형식 — 경로 사이드카만 색인한다.

    SKIP 으로 두면 파일명으로도 검색되지 않는다. SKIP 라우트는 GCS 업로드도
    사이드카도 만들지 않고, 검색단이 SKIPPED 문서를 결과에서 걸러내기까지 한다
    (services/mcp_server/main.py). "그런 파일이 어디 있다"는 답조차 못 하는 셈이라
    사이드카만이라도 남긴다.

    원본 바이트는 GCS 에 올리지 않는다 — RAG Engine 이 못 읽는 형식을 색인에
    넣으면 매번 import 에서 거부된다(암호 xlsx 27건이 상시 실패하던 전례).

    **파일을 내려받지 않는다.** 쓸 데가 없고, ZIP 은 큰 것이 흔하다. 대신 해시를
    바이트가 아니라 경로·사이드카에서 만든다. 파일명이나 폴더가 바뀌면 사이드카가
    바뀌어 재색인되고, 내용만 바뀐 경우는 어차피 색인할 본문이 없어 재색인이
    무의미하므로 건너뛴다.
    """
    path_ctx = _resolve_path_ctx(drive, body)
    audience = _resolve_audience(drive, settings, body)
    sidecar = build_breadcrumb_markdown(
        path=path_ctx.path,
        bundle=path_ctx.bundle,
        title=body.name or body.file_id,
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
            # 본문 없는 문서라는 사실이 조회 가능해야 한다. 상태는 색인되므로
            # INDEXED 로 가지만, 검색 결과의 근거가 파일명뿐임을 여기서 알린다.
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
    """파서 서비스를 거치지 않고 Drive→GCS 로 바로 가는 포맷들.

    한때는 이름 그대로 '복사'만 했으나 지금은 포맷마다 다르게 손본다.
    라우트 키(`FILE_COPY`)는 워크플로가 문자열로 비교하고 API 응답에도
    나가므로 그대로 두고, 함수 이름만 실제 동작에 맞춘다.

        PDF          한도 초과분을 페이지 경계로 분할
        XLSX/XLSM    셀을 마크다운 표로 변환
        TXT/HTML/CSV 머리말을 본문 앞에 심음
        ZIP/XLS      사이드카만 (본문 추출 수단 없음, 아래 참고)
        그 외        원본 복사 + 경로 사이드카

    크기 게이트가 세 번 나오는데 **재는 대상이 다르다**. 맨 위는 다운로드 전
    Drive 가 알려 준 크기, 가운데는 내려받은 원본 바이트, 아래는 RAG 로 실제
    올라갈 산출물이다. 섞으면 색인되지도 않을 원본 크기 때문에 문서를 잃는다
    (실측 사고 있었음).
    """
    name = body.name or body.file_id
    mime = (body.mime_type or "").lower()
    ext = Path(name).suffix or _ext_for_mime(body.mime_type)

    # 본문을 뽑을 수단이 없는 형식(.zip/.xls)은 여기서 끝낸다. 사이드카만 색인하므로
    # **다운로드도 크기 게이트도 필요 없다.** 그냥 아래로 흘려보내면 원본 크기를
    # RAG 한도(10MB)로 재는 게이트에 걸려 SPLIT_QUEUED 로 영구 정체한다 — 올리지도
    # 않을 바이트 때문에 문서를 잃는, 이 함수가 이미 두 번 겪은 사고다.
    if mime in SIDECAR_ONLY_MIME:
        return _ingest_sidecar_only(body, store, gcs, drive, settings)

    # 텍스트는 breadcrumb 를 붙여 .md 로 나가므로 상한도 .md 기준이다.
    upload_ext = ".md" if mime in _TEXT_COPY_MIMES else ext

    # 다운로드 전 1차 차단. 크기 미상이면 통과하고 아래 사후 검사가 잡는다.
    #
    # **여기서 RAG 한도로 재면 안 되는 두 종류가 있다.** 이 게이트의 목적은
    # '메모리에 올릴 바이트'를 막는 것이지, '색인될 산출물'을 재는 게 아니다.
    # 산출물 크기는 변환·분할이 끝난 뒤 아래에서 따로 잰다.
    #
    #   스프레드시트  원본이 아니라 변환된 .md 가 올라간다. 원본 크기로 재면
    #                변환하면 통과할 문서를 다운로드도 전에 버린다.
    #                (실측: 29MB xlsx 가 SPLIT_QUEUED 로 영구 정체 중)
    #   PDF          한도를 넘으면 버리는 게 아니라 페이지 경계로 쪼갠다.
    #                여기서 막으면 그 분할 로직에 **영영 도달하지 못한다**.
    #                (실측: GCS 에 .partN 조각 6개가 남아 있으나 이 게이트가
    #                 생긴 뒤로는 새로 만들어지지 않는다)
    #
    # 둘 다 '내려받아도 되는 최대치'인 MAX_GCS_BYTES 로만 막는다. 그보다 큰 PDF 를
    # 쪼개려면 MAX_GCS_BYTES 를 올려야 한다 — 기본값은 RAG PDF 한도와 같은 50MB 라
    # 그대로 두면 분할 구간이 열리지 않는다.
    if mime in _SPREADSHEET_COPY_MIMES or ext.lower() == ".pdf":
        pre_ext, pre_limit = ext, settings.max_gcs_bytes
    else:
        pre_ext, pre_limit = upload_ext, None

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
    # _resolve_path_ctx 가 훑어 둔 부모 캐시를 재사용하므로 Drive 호출이 늘지 않는다.
    audience = _resolve_audience(drive, settings, body)
    data = drive.download_file(body.file_id)

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
            store, settings, body, len(data), splittable=True, ext=ext, limit=raw_limit
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
            # 재색인 경로(_source_uris_for_file)는 이미 .xlsx 를 빼고
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
        # 변환으로 늘어난 분량(xlsx→표)은 원본 크기로는 안 보이고, 텍스트는
        # 원본 확장자와 무관하게 breadcrumb 를 붙여 .md 로 올라간다.
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
        # 쪼갠 PDF 는 여기서 재지 않는다. split_pdf 가 파트마다 한도 이하로 만들어
        # 놓았는데 **원본 전체 크기**로 다시 재면 무조건 걸린다 — 위에서 분할에
        # 성공해도 그대로 SPLIT_QUEUED 로 떨어져, 1424행 분할 로직이 통째로
        # 도달 불가가 된다. (로컬 종단 검증에서 잡음: 53MB PDF 가 2파트로 쪼개진
        # 직후 이 게이트에 걸려 파트가 하나도 업로드되지 않았다.)
        if not pdf_parts:
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
    uri = gs_uri(settings.gcs_source_bucket, f"{file_id}.meta.md")
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

    outcome = rag.import_from_gcs(student_uris) if student_uris else None
    if outcome and not outcome.ok:
        # 교직원 쪽과 달리 여기서는 상태를 되돌릴 대상이 없다(audience 는
        # doc_state 에 이미 확정돼 있다). 남길 수 있는 건 신호뿐이다.
        logger.error(
            "학생 코퍼스 import 부분 실패 uris=%s imported=%s failed=%s skipped=%s",
            len(student_uris), outcome.imported, outcome.failed, outcome.skipped,
        )
    logger.info(
        "학생 코퍼스 동기화: 대상 %s / 검토 %s URI",
        outcome.imported if outcome else 0,
        len(gcs_uris),
    )
    return {
        "enabled": True,
        "imported": outcome.imported if outcome else 0,
        "removed": len(touched),
        "ok": outcome.ok if outcome else True,
    }


def _group_docs_by_drive(
    targets: list[DocState], settings: Settings
) -> list[tuple[Settings, list[DocState]]]:
    """복구 대상을 학과(=드라이브)별로 가른다.

    `_split_by_drive` 와 달리 Firestore 를 다시 안 읽는다 — DocState 가 이미
    driveId 를 들고 있다(문서 200건이면 조회 200번을 아낀다).

    driveId 가 비었거나 학과 맵에 없는 문서는 **버린다.** 어느 코퍼스·어느
    버킷인지 모르는 채 전역 기본값으로 처리하면 남의 학과에 섞이거나, 없는
    버킷을 뒤져 DLQ 로 보낸다. 안 건드리면 다음 주기가 다시 집는다.
    (doc_state 가 유실된 문서는 driveId 없이 stub 으로 만들어질 수 있다 —
     DocStateStore.mark_indexed 의 경고 참고.)
    """
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
    """학과별로 나눠 import 한 결과를 하나로 합친다.

    ok 는 **모든 부분이 성공했을 때만** True 다. 한 학과라도 부분 실패면
    워크플로의 커밋 조건(indexed == uris)이 안 맞아 pageToken 이 안 넘어가고,
    다음 주기가 같은 변경을 재생한다 — 그게 의도된 동작이다.
    """
    return ImportOutcome(
        uris=a.uris + b.uris,
        imported=a.imported + b.imported,
        failed=a.failed + b.failed,
        skipped=a.skipped + b.skipped,
    )


def _settings_for_drive(base: Settings, drive_id: str | None) -> Settings:
    """이 드라이브(=학과)용 설정. 학과 맵이 비면 그대로 돌려준다.

    모르는 드라이브는 UnknownDriveError 를 그대로 올린다 — 기본 코퍼스로
    떨어뜨리면 남의 학과 자료가 섞이고, 되돌리려면 코퍼스에서 파일을 골라
    지워야 한다. 호출부가 그 드라이브만 건너뛰고 나머지를 계속 돌리면 된다.
    """
    # getattr 인 이유: 테스트·스크립트가 넘기는 가벼운 설정 대역에는 이 필드가
    # 없다. 없으면 '학과 맵 없음' = 기존 단일 코퍼스 동작으로 본다.
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
    """URI·fileId 를 학과(=드라이브)별로 가른다.

    학과 맵이 없으면 통째로 하나 — 기존 동작 그대로다. 있으면 doc_state 의
    driveId 로 나눈다. **driveId 를 모르는 문서는 버린다**: 어느 코퍼스로 갈지
    모르는 채 기본 코퍼스에 넣으면 남의 학과에 섞이기 때문이다(그쪽이 더 비싼
    사고다 — 안 넣으면 다음 주기가 다시 집는다).
    """
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
) -> ImportOutcome:
    """이번 배치의 기존 청크만 제거 → import → **전량 성공일 때만** INDEXED 전환.

    **삭제 대상은 반드시 이번에 import 할 파일과 같아야 한다.** 실행 시작 시
    전체를 미리 지우면, 중간에 UNCHANGED 로 빠져 재import 되지 않는 문서까지
    코퍼스에서 사라진다(백필이 정상 코퍼스를 비우고 성공으로 보고했다).
    삭제를 import 바로 앞에 두면 지우는 집합과 넣는 집합이 정의상 같아진다.

    ``rag`` 를 넘겨 **run 전체가 클라이언트 하나를 공유**하면 코퍼스 순회는
    첫 배치에서 한 번만 일어난다(RagEngineClient.delete_files_by_ids 참고).
    넘기지 않으면 이 호출만의 클라이언트를 쓴다.

    부분 실패는 예외로 올리지 않고 outcome 으로 돌려준다. 어느 URI 가 거부됐는지는
    응답 카운트로 알 수 없으므로(파일 단위 사유는 import_result_sink 를 걸어야
    나온다) 배치 전체를 PARSED 로 남긴다 — INDEXED 로 찍으면 reindex-pending 이
    PARSED 만 보므로 **자동 회수 경로가 영영 닫힌다.** 성공분을 한 번 더 import
    하는 비용이, 실패분을 영구히 잃는 것보다 싸다.
    """
    client = rag if rag is not None else RagEngineClient()
    # 삭제 실패 뒤 import 를 계속하면 이전 청크와 새 청크가 함께 남으므로 fail closed.
    client.delete_files_by_ids(list(dict.fromkeys(file_ids)))
    outcome = client.import_from_gcs(gcs_uris)
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
    for fid in dict.fromkeys(file_ids):
        existing = store.get(fid)
        if existing:
            existing.status = DocStatus.INDEXED
            store.upsert(existing)
        else:
            store.mark_indexed(fid)
    return outcome


@app.post("/sync/index-gcs")
def index_gcs(body: IndexGcsBody) -> dict[str, Any]:
    """GCS URI만 RAG Engine에 증분 import. Drive 커넥터 미사용."""
    if not body.gcs_uris:
        return {"imported": [], "count": 0, "status": "EMPTY"}

    settings = get_settings()
    store = DocStateStore()

    file_ids, bad_ids = _clean_file_ids(body.file_ids)
    if bad_ids:
        logger.warning(
            "index-gcs dropped %s malformed fileIds: %s", len(bad_ids), bad_ids[:10]
        )

    # 기존 청크 제거는 _import_and_mark 안에서 import 바로 앞에 한다. 여기서 한 번
    # 더 지우면 같은 배치에 코퍼스를 두 번 순회하게 된다.
    # 부분 실패면 INDEXED 로 올리지 않고 PARSED 로 남는다(회수 경로 유지).
    #
    # 이 엔드포인트만 요청에 driveId 가 없다(URI 목록만 받는다). 그래서 학과는
    # doc_state 의 driveId 로 되짚어 **학과별로 갈라서** import 한다 — 안 가르면
    # 전부 전역 기본 코퍼스로 들어가 학과가 둘 이상인 순간 서로 섞인다.
    # 학과 맵이 비면 그룹이 하나뿐이라 예전과 동일한 경로다.
    outcome = ImportOutcome(uris=[], imported=0, failed=0, skipped=0)
    student: dict[str, Any] = {"enabled": False}
    for dept_settings, uris, ids in _split_by_drive(
        store, body.gcs_uris, file_ids, settings
    ):
        part = _import_and_mark(
            store, uris, ids, rag=RagEngineClient(dept_settings)
        )
        outcome = _merge_outcomes(outcome, part)

        # 교직원 코퍼스가 끝난 뒤에 학생 코퍼스를 맞춘다. 순서가 중요하다 — 학생
        # 코퍼스가 실패해도 교직원 쪽 색인과 doc_state 는 이미 확정돼 있어야
        # 다음 배치가 같은 일을 처음부터 다시 하지 않는다.
        student = _sync_student_corpus(uris, ids, dept_settings, store)

    return {
        # 하위 호환: 예전부터 '보낸 URI 목록'이었다. 성공 목록이 아니다.
        "imported": outcome.uris,
        # 예전에는 보낸 URI 수였다. 이제 **실제 색인 건수**다 — 워크플로의
        # 커밋 조건(drive_indexed == drive_uris)이 이 값을 쓰므로, 부분 실패가
        # 나면 pageToken 이 커밋되지 않고 다음 주기가 같은 변경을 재생한다.
        "count": outcome.imported,
        "failed": outcome.failed,
        "skipped": outcome.skipped,
        "ok": outcome.ok,
        "droppedFileIds": len(bad_ids),
        "student": student,
        "status": "INDEXED" if outcome.ok else "PARTIAL",
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


# ingest 가 RAG import 대상으로 방출하는 확장자 (+ sidecar meta.md).
# 복구는 최초 import 와 **같은 URI 집합**을 다시 제출한다. 지원 여부로 걸러내면
# meta.md 만 색인하고 문서 전체를 INDEXED 로 오인하므로 fail closed 한다.
#
# 스프레드시트 원본(.xlsx)을 빼고 싶어지지만 빼면 안 된다. 지금 ingest 는 xlsx 를
# 변환한 .md 만 올리므로 source 버킷에 남은 .xlsx 는 전부 그 변경 이전의 잔재고,
# 그런 문서는 재색인이 아니라 **재ingest** 가 필요하다. 여기서 빼면 import 는
# 통과하지만 본문 없는 INDEXED 가 조용히 확정된다 — 매일 실패하는 편이 낫다.
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
    """존재하는 source 객체 중 인덱싱 가능 URI만 반환. 스프레드시트 원본은 제외.

    fileId 경계 검사는 GcsClient 쪽에 모아 두었다 — 삭제 경로와 같은 규칙을
    써야 '색인은 됐는데 삭제는 안 되는' 확장자가 생기지 않는다.

    ``gcs`` 를 넘기면 재사용한다 — 문서마다 클라이언트를 새로 만들면 복구
    한 번(limit 200)에 클라이언트 200개를 생성한다.
    """
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


def _reindex_group(
    body: ReindexPendingBody,
    settings: Settings,
    store: DocStateStore,
    targets: list[DocState],
    totals: dict[str, int],
) -> None:
    """한 학과분 재색인. totals 는 그룹들이 함께 누적한다."""
    # 문서마다 storage.Client 를 새로 만들면 200건에 200번 만든다 — 한 번만.
    gcs = GcsClient(settings)
    pending_uris: list[str] = []
    pending_ids: list[str] = []

    # 그룹(=학과) 전체가 공유 → 코퍼스 순회는 첫 배치에서 1회.
    # **settings 를 반드시 넘긴다.** 안 넘기면 전역 기본 코퍼스를 보게 되어
    # 학과를 갈라 놓고도 전부 한 코퍼스로 들어간다.
    rag = RagEngineClient(settings)

    def flush() -> None:
        nonlocal pending_uris, pending_ids
        if not pending_uris:
            return
        uris, ids = pending_uris, pending_ids
        uniq_ids = list(dict.fromkeys(ids))
        # 부분 실패면 _import_and_mark 가 PARSED 로 남긴다 — 다음 주기가 다시
        # 집는다. 여기서 INDEXED 로 올리면 이 복구 경로 자체가 그 문서를 두 번
        # 다시 보지 못한다(PARSED 만 대상이므로).
        outcome = _import_and_mark(store, uris, uniq_ids, rag=rag)
        # 교직원 코퍼스가 끝난 뒤에 학생 코퍼스를 맞춘다 — 소속 이동이 반영되는
        # 지점이라 여기서는 속도보다 정확성이 우선이다.
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
        # gcs 는 run 당 하나다 — 문서마다 만들면 limit 200 에 클라이언트 200개다.
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
                # 세는 것이 먼저다 — 비운 뒤에 len() 을 재면 항상 0 이라
                # 배치 전체가 실패해도 ok=true 로 보고된다.
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

    totals = {
        "candidates": len(targets),
        "withUris": 0,
        "indexed": 0,
        "skippedNoUri": 0,
        "failed": 0,
    }

    # 학과(=드라이브)별로 갈라 돌린다. 복구 대상은 status 로만 뽑히므로
    # (list_by_status 에 드라이브 필터가 없다) 전 학과가 한 배치에 섞여 있다.
    # 그대로 돌리면 (1) 전역 기본 코퍼스에 전부 넣고 (2) 전역 버킷에서 URI 를
    # 찾아 학과 버킷 문서를 전부 skippedNoUri → DLQ 로 보낸다.
    # 학과 맵이 비면 그룹이 하나뿐이라 예전과 같은 경로다.
    for dept_settings, group in _group_docs_by_drive(targets, base_settings):
        _reindex_group(body, dept_settings, store, group, totals)

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

    # 문서마다 ingest() 를 부르면 그때마다 DriveClient 를 새로 만든다 — 인증 +
    # discovery build 가 문서 수만큼 돌고, parents/name 캐시가 인스턴스 단위라
    # 같은 폴더의 조상을 문서마다 다시 조회한다. run 당 하나씩만 만든다.
    # 회수할 게 없는 날(대부분)에는 아예 만들지 않는다.
    base_settings = get_settings() if targets else None
    drive = DriveClient() if targets else None

    # 회수 대상은 status 로만 뽑혀 전 학과가 섞여 있다. **쓰기 버킷**이 문제다 —
    # 전역 settings 로 재적재하면 ee 원본이 cs 버킷에 저장되고, 이후 재색인은
    # ee 버킷을 뒤지므로 못 찾아 다시 DLQ 로 돌아온다(회수 장치가 무한 루프).
    # 코퍼스 쪽은 flush() 가 index_gcs() 를 거치므로 거기서 이미 갈라진다.
    #
    # GcsClient 는 학과당 하나만 만든다 — 문서마다 만들면 limit 100 에 100개다.
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
        if store.get_dlq_attempts(doc.file_id) >= body.max_attempts:
            totals["exhausted"] += 1
            continue

        try:
            settings, gcs = _dept_ctx(doc.drive_id)
        except UnknownDriveError:
            # 어느 학과인지 모르는 채 재적재하면 남의 버킷에 쓴다. 시도 횟수도
            # 올리지 않는다 — 설정이 고쳐지면 그대로 회수되어야 한다.
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
    store = DocStateStore()

    # 엉뚱한 코퍼스·버킷을 지우면 아무 일도 안 일어난다 — Drive 에서 지운 문서가
    # 계속 검색되는, 가장 알아채기 어려운 형태의 잔존이 된다.
    # 워크플로는 driveId 를 항상 넘긴다(daily_sync.yaml 의 do_delete). 수동 호출로
    # 빠졌을 때만 doc_state 에서 되짚는다.
    settings = get_settings()
    if getattr(settings, "departments", ()):
        drive_id = body.drive_id or ""
        if not drive_id:
            # 맵이 있을 때만 되짚는다 — 없으면 결과가 같은데 삭제마다 Firestore
            # 읽기가 한 번씩 더 늘 뿐이다.
            state = store.get(body.file_id)
            drive_id = state.drive_id if state else ""
        settings = _settings_for_drive(settings, drive_id)
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

    # RAG import 산출물 + raw 원본을 prefix 로 훑어 지운다.
    #
    # 예전에는 확장자 목록을 손으로 적었는데, 목록에 없는 것을 조용히 놓쳤다:
    #   .partN.pdf  분할 PDF 조각이 전량 남는다(실측 6건 존재)
    #   .rtf / .doc  FILE_COPY_MIME 에 있는데 목록에는 없었다
    #
    # raw 는 아예 건드리지도 않았다. 그래서 Drive 에서 지운 문서의 **원본이
    # GCS 에 영구 잔존**했다(실측: DELETED 100건 중 52건의 .hwp 원본이 남아 있었다).
    # raw 에는 명단·인사발령 같은 원문이 그대로 있어(docs/OPS_DEFERRED.md 6번)
    # 삭제가 이행되지 않는 것 자체가 문제다.
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

    # 코퍼스는 이미 정리됐으므로 상태는 DELETED 가 맞다. 다만 GCS 가 남았으면
    # 성공으로 위장하지 않고 올려 보내 재시도(=삭제 change 재생)되게 한다.
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
    """Drive 조회 건수 vs GCS 업로드·색인·스킵·실패·삭제 정합성."""
    # dlq/splitQueued 는 failed 의 하위 분류(집계 시 failed 도 함께 증가)이므로
    # accounted 에 다시 더하면 이중 집계된다 — failed 만 합산한다.
    accounted = (
        body.gcs_uploaded
        + body.failed
        # parked(DLQ·분할 대기) 도 '처리를 마친 것'이라 accounted 에 든다. 안 더하면
        # 그만큼 unaccounted 로 잡혀 정합성 검사가 실패하고 토큰이 안 커밋된다.
        + body.parked
        + body.skipped
        + body.deleted
        + body.unchanged
    )
    # EXCLUDED 는 대상 폴더 밖이라 처리할 일이 없다. accounted 에 더하는 대신
    # listed 에서 뺀다 — 그래야 남은 skipped 가 '대상인데 처리 못 한 것'만
    # 가리킨다. (예전에는 폴더 밖 393건이 skipped 로 잡혀 그 신호를 덮었다)
    listed = body.listed - body.excluded
    # indexed(=처리 완료된 URI 수)는 업로드된 URI 수와 **정확히 같아야** 한다.
    # 부등호로 두면 색인이 덜 된 배치가 정합성 검사를 통과해 버린다.
    # gcs_uploaded 는 '파일 수' 라 파일당 URI가 2개(원본+.meta.md)면 어긋난다 →
    # uris(업로드된 URI 총수)와 비교. uris 미제공 시 gcs_uploaded 로 폴백.
    index_baseline = body.uris if body.uris > 0 else body.gcs_uploaded
    index_ok = body.indexed == index_baseline
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
        # ok 판정에는 안 들어가므로(토큰을 막지 않는다) 여기서라도 남긴다.
        # 방치되면 '처리된 줄 알았는데 검색에 안 나오는' 문서가 조용히 쌓인다.
        logger.warning(
            "parked %s docs on drive=%s (dlq=%s splitQueued=%s) — 별도 조치 필요",
            body.parked, body.drive_id, body.dlq, body.split_queued,
        )
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
