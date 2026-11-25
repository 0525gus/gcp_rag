from __future__ import annotations

import logging
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from typing import Any

from google.cloud import firestore
from google.cloud.firestore_v1.field_path import FieldPath

from shared.config import Settings, get_settings
from shared.models import Audience, DocState, DocStatus

logger = logging.getLogger(__name__)


class DocStateStore:
    def __init__(self, settings: Settings | None = None, *, credentials: Any | None = None) -> None:
        self.settings = settings or get_settings()
        self._db = firestore.Client(
            project=self.settings.gcp_project_id,
            database=self.settings.firestore_database,
            credentials=credentials,
        )
        self._col = self._db.collection(self.settings.doc_state_collection)
        self._tokens = self._db.collection(self.settings.sync_token_collection)

    def get(self, file_id: str) -> DocState | None:
        snap = self._col.document(file_id).get()
        if not snap.exists:
            return None
        data = snap.to_dict() or {}
        data.setdefault("fileId", file_id)
        return DocState.from_firestore(data)

    def get_many(self, file_ids: Iterable[str]) -> dict[str, DocState | None]:
        found: dict[str, DocState | None] = dict.fromkeys(file_ids)
        if not found:
            return found
        for snap in self._db.get_all([self._col.document(file_id) for file_id in found]):
            if snap.exists:
                data = snap.to_dict() or {}
                data.setdefault("fileId", snap.id)
                found[snap.id] = DocState.from_firestore(data)
        return found

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
            self.clear_dlq(file_id)

    def should_reparse(self, file_id: str, change_modified_time: str | None) -> bool:
        if not change_modified_time:
            return True
        existing = self.get(file_id)
        if existing is None or not existing.modified_time:
            return True
        if existing.status in (DocStatus.FAILED, DocStatus.PENDING):
            return True
        return change_modified_time > existing.modified_time

    def should_skip_reindex(self, file_id: str, content_hash: str) -> bool:
        existing = self.get(file_id)
        return bool(
            existing
            and existing.content_hash == content_hash
            and existing.status == DocStatus.INDEXED
        )

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

    def update_sync_run(self, run_id: str, fields: dict[str, Any]) -> None:
        if not run_id or "/" in run_id or len(run_id) > 80:
            raise ValueError("invalid sync run id")
        payload = dict(fields)
        payload["runId"] = run_id
        payload["updatedAt"] = datetime.now(timezone.utc)
        self._tokens.document(f"__run__{run_id}").set(payload, merge=True)

    def all_statuses(self) -> dict[str, str]:
        return {
            snap.id: ((snap.to_dict() or {}).get("status") or "")
            for snap in self._col.stream()
        }

    def try_acquire_lock(self, name: str, *, ttl_seconds: int) -> bool:
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
            logger.exception("failed to release lock: %s", name)

    def list_by_status(
        self,
        status: DocStatus,
        limit: int = 100,
        *,
        cursor_key: str | None = None,
    ) -> list[DocState]:
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
                logger.exception(
                    "failed to load status scan cursor key=%s status=%s",
                    cursor_key,
                    status.value,
                )

        snapshots: list[Any]
        if last_file_id:
            snapshots = list(query.start_after([last_file_id]).limit(limit).stream())
            if len(snapshots) < limit:
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
        self._db.collection(self.settings.dlq_collection).document(file_id).delete()
        self._db.collection(self.settings.split_queue_collection).document(
            file_id
        ).delete()

    def enqueue_split(self, file_id: str, reason: str, size_bytes: int, **fields: Any) -> None:
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
