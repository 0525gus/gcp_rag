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