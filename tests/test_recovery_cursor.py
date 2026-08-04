"""복구 상태 조회의 영속 라운드로빈 커서 회귀 테스트."""

from __future__ import annotations

from typing import Any

import services.sync.main as sync_main
from services.sync.main import (
    ReindexPendingBody,
    RetryFailedBody,
    _reindex_pending_sync,
    retry_failed,
)
from shared.firestore_state import DocStateStore
from shared.models import DocStatus


class _Snapshot:
    def __init__(self, doc_id: str, data: dict[str, Any] | None) -> None:
        self.id = doc_id
        self._data = data
        self.exists = data is not None

    def to_dict(self) -> dict[str, Any] | None:
        return dict(self._data) if self._data is not None else None


class _DocumentRef:
    def __init__(self, documents: dict[str, dict[str, Any]], doc_id: str) -> None:
        self._documents = documents
        self._doc_id = doc_id

    def get(self) -> _Snapshot:
        return _Snapshot(self._doc_id, self._documents.get(self._doc_id))

    def set(self, data: dict[str, Any], merge: bool = False) -> None:
        if merge:
            self._documents.setdefault(self._doc_id, {}).update(data)
        else:
            self._documents[self._doc_id] = dict(data)


class _Query:
    def __init__(
        self,
        documents: dict[str, dict[str, Any]],
        status: str,
        *,
        after: str | None = None,
        query_limit: int | None = None,
    ) -> None:
        self._documents = documents
        self._status = status
        self._after = after
        self._limit = query_limit

    def order_by(self, field: str) -> _Query:
        assert field == "__name__"
        return self

    def start_after(self, values: list[str]) -> _Query:
        return _Query(
            self._documents,
            self._status,
            after=values[0],
            query_limit=self._limit,
        )

    def limit(self, value: int) -> _Query:
        return _Query(
            self._documents,
            self._status,
            after=self._after,
            query_limit=value,
        )

    def stream(self) -> list[_Snapshot]:
        ids = sorted(
            doc_id
            for doc_id, data in self._documents.items()
            if data.get("status") == self._status
            and (self._after is None or doc_id > self._after)
        )
        if self._limit is not None:
            ids = ids[: self._limit]
        return [_Snapshot(doc_id, self._documents[doc_id]) for doc_id in ids]


class _Collection:
    def __init__(self, documents: dict[str, dict[str, Any]] | None = None) -> None:
        self.documents = documents or {}

    def document(self, doc_id: str) -> _DocumentRef:
        return _DocumentRef(self.documents, doc_id)

    def where(self, field: str, op: str, value: str) -> _Query:
        assert (field, op) == ("status", "==")
        return _Query(self.documents, value)


class _RecoveryStore(DocStateStore):
    def __init__(
        self,
        statuses: dict[str, DocStatus],
        attempts: dict[str, int] | None = None,
    ) -> None:
        # 실제 Firestore 클라이언트를 만들지 않고 쿼리/커서 동작만 모사한다.
        self._col = _Collection(
            {
                file_id: {
                    "fileId": file_id,
                    "driveId": "drive-1",
                    "name": f"{file_id}.pdf",
                    "mimeType": "application/pdf",
                    "status": status.value,
                }
                for file_id, status in statuses.items()
            }
        )
        self._tokens = _Collection()
        self.attempts = attempts or {}
        self.recorded: list[str] = []
        self.cleared: list[str] = []

    def get_dlq_attempts(self, file_id: str) -> int:
        return self.attempts.get(file_id, 0)

    def record_dlq_attempt(self, file_id: str) -> None:
        self.recorded.append(file_id)
        self.attempts[file_id] = self.attempts.get(file_id, 0) + 1

    def clear_dlq(self, file_id: str) -> None:
        self.cleared.append(file_id)


def _ids(page: list[Any]) -> list[str]:
    return [doc.file_id for doc in page]


def test_status_cursor_advances_and_wraps_without_duplicates() -> None:
    store = _RecoveryStore(
        {f"f{i}": DocStatus.FAILED for i in range(1, 6)}
    )

    assert _ids(
        store.list_by_status(DocStatus.FAILED, limit=2, cursor_key="retry")
    ) == ["f1", "f2"]
    assert _ids(
        store.list_by_status(DocStatus.FAILED, limit=2, cursor_key="retry")
    ) == ["f3", "f4"]
    assert _ids(
        store.list_by_status(DocStatus.FAILED, limit=2, cursor_key="retry")
    ) == ["f5", "f1"]


def test_retry_failed_moves_past_exhausted_first_page(monkeypatch) -> None:
    store = _RecoveryStore(
        {file_id: DocStatus.FAILED for file_id in ("f1", "f2", "f3")},
        attempts={"f1": 3, "f2": 3},
    )
    ingested: list[str] = []

    def fake_ingest(body: Any, **_clients: Any) -> dict[str, str]:
        ingested.append(body.file_id)
        return {"status": "SKIPPED"}

    monkeypatch.setattr(sync_main, "DocStateStore", lambda: store)
    # retry_failed 는 클라이언트를 run 당 하나만 만들려고 _ingest_with 를 직접 부른다.
    monkeypatch.setattr(sync_main, "get_settings", lambda: object())
    monkeypatch.setattr(sync_main, "GcsClient", lambda *a, **k: object())
    monkeypatch.setattr(sync_main, "DriveClient", lambda *a, **k: object())
    monkeypatch.setattr(sync_main, "_ingest_with", fake_ingest)

    first = retry_failed(RetryFailedBody(limit=2, maxAttempts=3))
    second = retry_failed(RetryFailedBody(limit=2, maxAttempts=3))

    assert first["totals"]["exhausted"] == 2
    assert second["totals"]["retried"] == 1
    assert ingested == ["f3"]


def test_reindex_pending_moves_past_no_uri_first_page(monkeypatch) -> None:
    store = _RecoveryStore(
        {file_id: DocStatus.PARSED for file_id in ("f1", "f2", "f3")}
    )
    imported: list[str] = []

    class FakeRag:
        def import_from_gcs(self, uris: list[str]) -> list[str]:
            imported.extend(uris)
            return uris

        def delete_files_by_ids(self, _file_ids: list[str]) -> int:
            # 재색인 전 기존 청크 일괄 제거 (스테일 방지). 여기서는 검증 대상 아님.
            return 0

    monkeypatch.setattr(sync_main, "DocStateStore", lambda: store)
    monkeypatch.setattr(sync_main, "get_settings", lambda: object())
    monkeypatch.setattr(sync_main, "RagEngineClient", FakeRag)
    monkeypatch.setattr(sync_main, "GcsClient", lambda _s: object())
    monkeypatch.setattr(
        sync_main,
        "_normalized_uris_for_file",
        lambda settings, file_id, _c=None: (
            ["gs://bucket/f3.pdf"] if file_id == "f3" else []
        ),
    )

    first = _reindex_pending_sync(ReindexPendingBody(limit=2))
    second = _reindex_pending_sync(ReindexPendingBody(limit=2))

    assert first["totals"]["skippedNoUri"] == 2
    assert second["totals"]["withUris"] == 1
    assert imported == ["gs://bucket/f3.pdf"]


def test_retry_failed_builds_no_clients_when_there_is_nothing_to_retry(
    monkeypatch,
) -> None:
    """회수할 문서가 없는 날(대부분)에 인증 + discovery build 를 치르면 안 된다."""
    built: list[str] = []

    class _EmptyStore:
        def list_by_status(self, *_a, **_k):
            return []

    def _boom(*_a, **_k):
        built.append("client")
        raise AssertionError("대상이 없는데 클라이언트를 만들었다")

    monkeypatch.setattr(sync_main, "DocStateStore", lambda: _EmptyStore())
    monkeypatch.setattr(sync_main, "DriveClient", _boom)
    monkeypatch.setattr(sync_main, "GcsClient", _boom)

    res = retry_failed(RetryFailedBody())

    assert built == []
    assert res["totals"]["candidates"] == 0
