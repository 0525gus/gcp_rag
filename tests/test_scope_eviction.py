"""Out-of-folder-scope documents must be removed before becoming SKIPPED."""

from __future__ import annotations

import pytest
from fastapi import HTTPException

import services.sync.main as sync_main
from services.sync.main import IngestBody, ingest
from shared.models import DocState, DocStatus


class _FakeSettings:
    sync_folder_id_list = ("folder-x",)
    gcs_normalized_bucket = "normalized-bucket"
    gcs_raw_bucket = "raw-bucket"


class _FakeBlob:
    def __init__(self, name: str, *, error: Exception | None = None) -> None:
        self.name = name
        self.error = error
        self.deleted = False

    def delete(self) -> None:
        if self.error:
            raise self.error
        self.deleted = True


class _FakeStorageClient:
    def __init__(self, blobs_by_bucket: dict[str, list[_FakeBlob]]) -> None:
        self.blobs_by_bucket = blobs_by_bucket
        self.calls: list[tuple[str, str]] = []

    def list_blobs(self, bucket: str, *, prefix: str):
        self.calls.append((bucket, prefix))
        return [
            blob
            for blob in self.blobs_by_bucket.get(bucket, [])
            if blob.name.startswith(prefix)
        ]


class _FakeGcs:
    def __init__(self, blobs_by_bucket: dict[str, list[_FakeBlob]]) -> None:
        self._client = _FakeStorageClient(blobs_by_bucket)


class _FakeDrive:
    def is_in_sync_scope(self, file_id, folder_ids) -> bool:
        return False


class _FakeStore:
    def __init__(self, existing: DocState | None) -> None:
        self.existing = existing
        self.upserts: list[DocState] = []
        self.dlq: list[tuple[str, str, dict]] = []

    def get(self, file_id: str) -> DocState | None:
        return self.existing

    def upsert(self, state: DocState) -> None:
        self.upserts.append(state)

    def enqueue_dlq(self, file_id: str, reason: str, **fields) -> None:
        self.dlq.append((file_id, reason, fields))


class _FakeRag:
    def __init__(
        self,
        calls: list[str],
        *,
        error: Exception | None = None,
        deleted: bool = True,
    ) -> None:
        self.calls = calls
        self.error = error
        self.deleted = deleted

    def delete_by_file_id(self, file_id: str) -> bool:
        self.calls.append(file_id)
        if self.error:
            raise self.error
        return self.deleted


def _patch_ingest_dependencies(
    monkeypatch,
    *,
    store: _FakeStore,
    gcs: _FakeGcs,
    rag: _FakeRag,
) -> None:
    monkeypatch.setattr(sync_main, "get_settings", lambda: _FakeSettings())
    monkeypatch.setattr(sync_main, "DocStateStore", lambda *a, **k: store)
    monkeypatch.setattr(sync_main, "GcsClient", lambda *a, **k: gcs)
    monkeypatch.setattr(sync_main, "DriveClient", lambda *a, **k: _FakeDrive())
    monkeypatch.setattr(sync_main, "RagEngineClient", lambda *a, **k: rag)


def _body() -> IngestBody:
    return IngestBody(
        fileId="f1",
        driveId="drive-1",
        name="doc.pdf",
        mimeType="application/pdf",
    )


def test_existing_out_of_scope_doc_is_evicted_before_skipped(monkeypatch) -> None:
    normalized_blobs = [
        _FakeBlob("normalized/f1.pdf"),
        _FakeBlob("normalized/f1.meta.md"),
        _FakeBlob("normalized/f10.pdf"),
    ]
    raw_blobs = [_FakeBlob("raw/f1.hwp"), _FakeBlob("raw/f10.hwp")]
    gcs = _FakeGcs(
        {"normalized-bucket": normalized_blobs, "raw-bucket": raw_blobs}
    )
    store = _FakeStore(
        DocState(file_id="f1", drive_id="drive-1", status=DocStatus.INDEXED)
    )
    rag_calls: list[str] = []
    _patch_ingest_dependencies(
        monkeypatch, store=store, gcs=gcs, rag=_FakeRag(rag_calls)
    )

    result = ingest(_body())

    assert rag_calls == ["f1"]
    assert gcs._client.calls == [
        ("normalized-bucket", "normalized/f1"),
        ("raw-bucket", "raw/f1"),
    ]
    assert [blob.deleted for blob in normalized_blobs] == [True, True, False]
    assert [blob.deleted for blob in raw_blobs] == [True, False]
    assert result == {
        "fileId": "f1",
        "status": "SKIPPED",
        "reason": "out_of_folder_scope",
        "ragDeleted": True,
        "normalizedDeleted": 2,
        "rawDeleted": 1,
    }
    assert [state.status for state in store.upserts] == [DocStatus.SKIPPED]
    assert store.dlq == []


def test_missing_state_still_cleans_orphaned_scope_artifacts(monkeypatch) -> None:
    orphan = _FakeBlob("normalized/f1.pdf")
    gcs = _FakeGcs({"normalized-bucket": [orphan], "raw-bucket": []})
    store = _FakeStore(None)
    rag_calls: list[str] = []
    _patch_ingest_dependencies(
        monkeypatch,
        store=store,
        gcs=gcs,
        rag=_FakeRag(rag_calls, deleted=False),
    )

    result = ingest(_body())

    assert result["status"] == "SKIPPED"
    # No RAG match is already clean and must remain an idempotent success.
    assert result["ragDeleted"] is False
    assert result["normalizedDeleted"] == 1
    assert result["rawDeleted"] == 0
    assert orphan.deleted is True
    assert rag_calls == ["f1"]


def test_already_evicted_file_does_not_rescan_the_corpus(monkeypatch) -> None:
    """정리를 마친 범위 밖 파일이 다시 델타에 실려도 코퍼스를 또 순회하지 않는다.

    delete_by_file_id 는 파일 하나를 찾으려고 코퍼스를 전수 순회한다. 범위 밖
    파일은 바뀔 때마다 델타에 다시 오므로, 생략하지 않으면 그 비용이 매 실행
    반복되고 대량 변경 시 list 쿼터를 넘겨 SKIP 분기를 실패시킨다.
    """
    gcs = _FakeGcs({"normalized-bucket": [], "raw-bucket": []})
    store = _FakeStore(
        DocState(
            file_id="f1",
            drive_id="drive-1",
            status=DocStatus.SKIPPED,
            error="out_of_folder_scope",
        )
    )
    rag_calls: list[str] = []
    _patch_ingest_dependencies(
        monkeypatch, store=store, gcs=gcs, rag=_FakeRag(rag_calls)
    )

    result = ingest(_body())

    assert result["status"] == "SKIPPED"
    assert result["cleanupSkipped"] is True
    assert rag_calls == []
    assert gcs._client.calls == []


def test_skipped_for_another_reason_still_cleans_the_corpus(monkeypatch) -> None:
    """SKIPPED 만으로 생략하면 안 된다 — 미지원 MIME 로 바뀐 문서는 청크가 남는다."""
    blob = _FakeBlob("normalized/f1.pdf")
    gcs = _FakeGcs({"normalized-bucket": [blob], "raw-bucket": []})
    store = _FakeStore(
        DocState(file_id="f1", drive_id="drive-1", status=DocStatus.SKIPPED)
    )
    rag_calls: list[str] = []
    _patch_ingest_dependencies(
        monkeypatch, store=store, gcs=gcs, rag=_FakeRag(rag_calls)
    )

    result = ingest(_body())

    assert result["status"] == "SKIPPED"
    assert rag_calls == ["f1"]
    assert blob.deleted is True


def test_rag_cleanup_failure_is_dlq_and_never_skipped(monkeypatch) -> None:
    blob = _FakeBlob("normalized/f1.pdf")
    gcs = _FakeGcs({"normalized-bucket": [blob], "raw-bucket": []})
    store = _FakeStore(
        DocState(file_id="f1", drive_id="drive-1", status=DocStatus.INDEXED)
    )
    _patch_ingest_dependencies(
        monkeypatch,
        store=store,
        gcs=gcs,
        rag=_FakeRag([], error=RuntimeError("rag unavailable")),
    )

    with pytest.raises(HTTPException) as caught:
        ingest(_body())

    assert caught.value.status_code == 500
    assert "out_of_folder_scope_cleanup_failed" in str(caught.value.detail)
    assert blob.deleted is True  # GCS is still cleaned even when RAG cleanup fails.
    assert store.upserts == []
    assert len(store.dlq) == 1
    assert store.dlq[0][0] == "f1"
    assert store.dlq[0][1].startswith("out_of_folder_scope_cleanup_failed")


def test_gcs_cleanup_failure_is_dlq_and_never_skipped(monkeypatch) -> None:
    blobs = [
        _FakeBlob("normalized/f1.pdf", error=RuntimeError("gcs unavailable")),
        _FakeBlob("normalized/f1.meta.md"),
    ]
    gcs = _FakeGcs({"normalized-bucket": blobs, "raw-bucket": []})
    store = _FakeStore(
        DocState(file_id="f1", drive_id="drive-1", status=DocStatus.INDEXED)
    )
    rag_calls: list[str] = []
    _patch_ingest_dependencies(
        monkeypatch, store=store, gcs=gcs, rag=_FakeRag(rag_calls)
    )

    with pytest.raises(HTTPException) as caught:
        ingest(_body())

    assert caught.value.status_code == 500
    assert rag_calls == ["f1"]
    assert blobs[1].deleted is True  # Remaining objects are attempted after one failure.
    assert store.upserts == []
    assert len(store.dlq) == 1
