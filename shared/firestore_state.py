"""Firestore doc_state / sync_token 저장소."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from google.cloud import firestore
from google.cloud.firestore_v1.field_path import FieldPath

from shared.config import Settings, get_settings
from shared.models import Audience, DocState, DocStatus

logger = logging.getLogger(__name__)


class DocStateStore:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._db = firestore.Client(
            project=self.settings.gcp_project_id,
            database=self.settings.firestore_database,
        )
        self._col = self._db.collection(self.settings.firestore_collection)
        self._tokens = self._db.collection(self.settings.sync_token_collection)

    def get(self, file_id: str) -> DocState | None:
        snap = self._col.document(file_id).get()
        if not snap.exists:
            return None
        data = snap.to_dict() or {}
        data.setdefault("fileId", file_id)
        return DocState.from_firestore(data)

    def upsert(self, state: DocState) -> None:
        state.last_synced_at = datetime.now(timezone.utc)
        self._col.document(state.file_id).set(state.to_firestore(), merge=True)

    def mark_failed(self, file_id: str, error: str, **fields: Any) -> None:
        payload: dict[str, Any] = {
            "fileId": file_id,
            "status": DocStatus.FAILED.value,
            "error": error[:2000],
            "lastSyncedAt": datetime.now(timezone.utc),
            **fields,
        }
        self._col.document(file_id).set(payload, merge=True)

    def touch_modified_time(
        self,
        file_id: str,
        modified_time: str | None,
        *,
        audience: Audience | str | None = None,
    ) -> None:
        """내용이 그대로일 때 modifiedTime 만 전진시킨다.

        재파싱 결과가 이전과 같으면(HASH_UNCHANGED) 색인은 건드릴 게 없지만,
        modifiedTime 을 남겨두지 않으면 ``should_reparse`` 가 매 실행 참이 되어
        같은 파일을 매일 다시 내려받고 다시 파싱한다.

        **status 를 쓰지 않는 것이 핵심이다.** 여기서 PARSED 를 쓰면 이미 색인된
        문서가 '색인 누락'으로 강등돼 reindex-pending 이 매일 헛돌고, 복구 예산을
        진짜 끊긴 문서 대신 이 문서들이 차지한다. sourceUri/path 도 건드리지
        않는다 — merge 로 None 을 쓰면 기존 Drive 링크가 지워진다.

        ``audience`` 는 예외다. 내용이 그대로여도 문서가 학생↔교직원 폴더 사이를
        옮겨 다니면 대상 코퍼스가 바뀌므로, 여기서 갱신하지 않으면 옛 코퍼스에
        영영 남는다. None 이면 기존 값을 그대로 둔다.
        """
        if not modified_time:
            return
        payload: dict[str, Any] = {
            "fileId": file_id,
            "modifiedTime": modified_time,
            "lastSyncedAt": datetime.now(timezone.utc),
        }
        if audience is not None:
            payload["audience"] = (
                audience.value if isinstance(audience, Audience) else audience
            )
        self._col.document(file_id).set(payload, merge=True)

    def mark_deleted(self, file_id: str) -> None:
        self._col.document(file_id).set(
            {
                "fileId": file_id,
                "status": DocStatus.DELETED.value,
                "lastSyncedAt": datetime.now(timezone.utc),
                "error": None,
            },
            merge=True,
        )

    def mark_indexed(self, file_id: str) -> None:
        """색인 완료 기록. 실패에서 회복한 경우 실패 큐도 함께 비운다.

        상태 문서가 없으면(코퍼스에만 있고 doc_state 가 유실된 경우) 최소 필드만
        만들어 둔다 — 다음 동기화가 이 문서를 '처음 본 것'으로 다시 처리하지
        않게 하려면 status 는 남아 있어야 한다. ingest 는 GCS_READY 전에 항상
        upsert 하므로 정상 흐름에서는 일어나지 않고, driveId 를 모르는 채로
        만들어져 검색의 driveId 필터에서 빠질 수 있어 경고를 남긴다.
        """
        existing = self.get(file_id)
        if existing is None:
            logger.warning(
                "indexed a file with no doc_state — creating a stub: %s", file_id
            )
            self._col.document(file_id).set(
                {
                    "fileId": file_id,
                    "status": DocStatus.INDEXED.value,
                    "lastSyncedAt": datetime.now(timezone.utc),
                },
                merge=True,
            )
            return
        was_failed = existing.status == DocStatus.FAILED
        existing.status = DocStatus.INDEXED
        self.upsert(existing)
        if was_failed:
            # 큐에 남은 잔재가 신규 실패를 가리지 않도록 여기서 치운다.
            # 실패였던 문서만 건드리므로 정상 경로에는 쓰기가 늘지 않는다.
            self.clear_dlq(file_id)

    def should_reparse(self, file_id: str, change_modified_time: str | None) -> bool:
        """change.modifiedTime > doc_state.modifiedTime 인 경우에만 재파싱."""
        if not change_modified_time:
            return True
        existing = self.get(file_id)
        if existing is None or not existing.modified_time:
            return True
        if existing.status in (DocStatus.FAILED, DocStatus.PENDING):
            return True
        return change_modified_time > existing.modified_time

    def should_skip_reindex(self, file_id: str, content_hash: str) -> bool:
        """색인 스킵 판단: 내용이 같고 '이미 INDEXED' 인 경우에만 True.

        PARSED(색인만 실패)인데 해시가 같다는 이유로 스킵하면 색인 누락이
        영구 고착된다 — 그 경우 URI 재방출해 색인을 복구해야 하므로 False.
        """
        existing = self.get(file_id)
        return bool(
            existing
            and existing.content_hash == content_hash
            and existing.status == DocStatus.INDEXED
        )

    # --- Changes API pageToken ---

    def get_start_page_token(self, drive_id: str) -> str | None:
        snap = self._tokens.document(drive_id).get()
        if not snap.exists:
            return None
        data = snap.to_dict() or {}
        return data.get("pageToken")

    def set_start_page_token(self, drive_id: str, page_token: str) -> None:
        self._tokens.document(drive_id).set(
            {
                "driveId": drive_id,
                "pageToken": page_token,
                "updatedAt": datetime.now(timezone.utc),
            },
            merge=True,
        )

    def all_statuses(self) -> dict[str, str]:
        """fileId → status 전량. 정리·감사 스크립트가 대조 기준으로 쓴다."""
        return {
            snap.id: ((snap.to_dict() or {}).get("status") or "")
            for snap in self._col.stream()
        }

    # --- 단일 실행 잠금 ---

    def try_acquire_lock(self, name: str, *, ttl_seconds: int) -> bool:
        """이미 살아 있는 잠금이 없을 때만 잡는다. 트랜잭션으로 경합을 막는다.

        워크플로우 스텝 타임아웃이 Cloud Run 요청 타임아웃보다 짧으면, 워크플로우가
        포기한 뒤에도 서버는 계속 돌고 그 위에 재시도가 새 요청을 얹는다 — 같은
        드라이브에 백필이 둘 돈다. 타임아웃을 정렬해도 수동 실행·스케줄러 중복은
        남으므로 서버가 스스로 막는다.

        ``ttl_seconds`` 는 프로세스가 죽어 release 를 못 했을 때 영구 잠김을 막는
        안전판이다. 정상 종료는 release_lock 이 즉시 푼다.
        """
        ref = self._tokens.document(f"__lock__{name}")
        now = datetime.now(timezone.utc)
        expires = now + timedelta(seconds=ttl_seconds)

        @firestore.transactional
        def _acquire(txn: Any) -> bool:
            snap = ref.get(transaction=txn)
            if snap.exists:
                held = (snap.to_dict() or {}).get("expiresAt")
                if held is not None and held > now:
                    return False
            txn.set(
                ref,
                {"name": name, "acquiredAt": now, "expiresAt": expires},
            )
            return True

        return bool(_acquire(self._db.transaction()))

    def release_lock(self, name: str) -> None:
        try:
            self._tokens.document(f"__lock__{name}").delete()
        except Exception:
            # 놓지 못해도 TTL 이 만료시킨다 — 본 작업 결과를 뒤집지는 않는다.
            logger.exception("failed to release lock: %s", name)

    def list_by_status(
        self,
        status: DocStatus,
        limit: int = 100,
        *,
        cursor_key: str | None = None,
    ) -> list[DocState]:
        """상태별 문서를 조회한다.

        ``cursor_key``를 지정한 복구 작업은 문서 ID 순서의 영속 라운드로빈
        페이지를 사용한다. 처리할 수 없는 문서가 상태를 계속 점유하더라도 다음
        실행은 그 뒤에서 시작하므로 ``limit`` 뒤의 문서가 영구 고착되지 않는다.

        커서는 반환할 페이지를 정한 직후 저장한다. 이후 작업이 중단되면 해당
        페이지는 한 바퀴 뒤에 다시 선택될 수 있지만 영구 누락되지는 않는다.
        """
        query = self._col.where("status", "==", status.value).order_by(
            FieldPath.document_id()
        )

        cursor_ref = None
        last_file_id: str | None = None
        if cursor_key:
            cursor_ref = self._tokens.document(
                f"__status_scan_cursor__{cursor_key}__{status.value}"
            )
            try:
                cursor_snap = cursor_ref.get()
                if cursor_snap.exists:
                    raw_cursor = (cursor_snap.to_dict() or {}).get("lastFileId")
                    if isinstance(raw_cursor, str) and raw_cursor:
                        last_file_id = raw_cursor
            except Exception:
                # 커서 장애 때문에 복구 자체를 중단하지는 않는다. 이번 실행은
                # 컬렉션 앞에서 시작하고 다음 성공한 쓰기부터 순환을 재개한다.
                logger.exception(
                    "failed to load status scan cursor key=%s status=%s",
                    cursor_key,
                    status.value,
                )

        snapshots: list[Any]
        if last_file_id:
            snapshots = list(query.start_after([last_file_id]).limit(limit).stream())
            if len(snapshots) < limit:
                # 컬렉션 끝에 닿으면 앞에서부터 남은 칸을 채운다. 두 구간이
                # 겹칠 수 있으므로 ID로 중복을 제거한다.
                seen = {snap.id for snap in snapshots}
                for snap in query.limit(limit).stream():
                    if snap.id in seen:
                        continue
                    snapshots.append(snap)
                    seen.add(snap.id)
                    if len(snapshots) >= limit:
                        break
        else:
            snapshots = list(query.limit(limit).stream())

        if cursor_ref is not None and snapshots:
            try:
                cursor_ref.set(
                    {
                        "cursorKey": cursor_key,
                        "status": status.value,
                        "lastFileId": snapshots[-1].id,
                        "updatedAt": datetime.now(timezone.utc),
                    },
                    merge=True,
                )
            except Exception:
                logger.exception(
                    "failed to save status scan cursor key=%s status=%s",
                    cursor_key,
                    status.value,
                )

        results: list[DocState] = []
        for snap in snapshots:
            data = snap.to_dict() or {}
            data.setdefault("fileId", snap.id)
            results.append(DocState.from_firestore(data))
        return results

    def enqueue_dlq(self, file_id: str, reason: str, **fields: Any) -> None:
        """반복 실패·품질 게이트 미달 → 수동 점검 큐 (PoC: DocAI 폴백 대체)."""
        self._db.collection(self.settings.dlq_collection).document(file_id).set(
            {
                "fileId": file_id,
                "reason": reason[:2000],
                "enqueuedAt": datetime.now(timezone.utc),
                **fields,
            },
            merge=True,
        )
        self.mark_failed(file_id, reason, **fields)

    def get_dlq_attempts(self, file_id: str) -> int:
        snap = self._db.collection(self.settings.dlq_collection).document(file_id).get()
        if not snap.exists:
            return 0
        return int((snap.to_dict() or {}).get("retryCount") or 0)

    def record_dlq_attempt(self, file_id: str) -> None:
        self._db.collection(self.settings.dlq_collection).document(file_id).set(
            {
                "fileId": file_id,
                "retryCount": firestore.Increment(1),
                "lastRetryAt": datetime.now(timezone.utc),
            },
            merge=True,
        )

    def clear_dlq(self, file_id: str) -> None:
        """재처리 성공 → DLQ·분할 큐 항목 제거 (다음 실패 시 카운트 0부터).

        분할 큐도 같이 비운다. 크기 초과로 들어간 문서가 나중에 다른 경로로
        색인돼도(예: xlsx 를 마크다운으로 변환) 큐 항목은 그대로 남는데,
        큐 적재를 알림 신호로 쓸 계획이라 잔재가 실제 실패를 가린다.
        DLQ 892건이 신규 실패를 묻어버린 것과 같은 유형이다
        (docs/OPS_AUDIT.md Ⅱ.3).
        """
        self._db.collection(self.settings.dlq_collection).document(file_id).delete()
        self._db.collection(self.settings.split_queue_collection).document(
            file_id
        ).delete()

    def enqueue_split(self, file_id: str, reason: str, size_bytes: int, **fields: Any) -> None:
        """크기 초과 → 분할 업로드 대기 큐."""
        self._db.collection(self.settings.split_queue_collection).document(file_id).set(
            {
                "fileId": file_id,
                "reason": reason[:2000],
                "sizeBytes": size_bytes,
                "enqueuedAt": datetime.now(timezone.utc),
                **fields,
            },
            merge=True,
        )
        self.mark_failed(file_id, reason, **fields)
