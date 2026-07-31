"""Firestore doc_state / sync_token 저장소."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from google.cloud import firestore

from shared.config import Settings, get_settings
from shared.models import DocState, DocStatus


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
        않게 하려면 status 는 남아 있어야 한다.
        """
        existing = self.get(file_id)
        if existing is None:
            self._col.document(file_id).set(
                {"fileId": file_id, "status": DocStatus.INDEXED.value}, merge=True
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

    def content_unchanged(self, file_id: str, content_hash: str) -> bool:
        existing = self.get(file_id)
        return bool(existing and existing.content_hash == content_hash)

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

    def list_by_status(self, status: DocStatus, limit: int = 100) -> list[DocState]:
        query = self._col.where("status", "==", status.value).limit(limit)
        results: list[DocState] = []
        for snap in query.stream():
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