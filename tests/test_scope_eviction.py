from __future__ import annotations

import pytest
from fastapi import HTTPException

import services.sync.main as sync_main
from services.sync.main import IngestBody, ingest
from shared.models import DocState, DocStatus


class _FakeSettings:
    sync_folder_id_list = ("folder-x",)
    student_folder_id_list: list[str] = []
    audience_split_enabled = False
    rag_corpus_name_student = ""
    gcs_source_bucket = "source-bucket"
    gcs_hwp_original_bucket = "hwp-original-bucket"


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
    source_blobs = [
        _FakeBlob("f1.pdf"),
        _FakeBlob("f1.meta.md"),
        _FakeBlob("f10.pdf"),
    ]
    hwp_original_blobs = [_FakeBlob("f1.hwp"), _FakeBlob("f10.hwp")]
    gcs = _FakeGcs(
        {"source-bucket": source_blobs, "hwp-original-bucket": hwp_original_blobs}
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
        ("source-bucket", "f1"),
        ("hwp-original-bucket", "f1"),
    ]
    assert [blob.deleted for blob in source_blobs] == [True, True, False]
    assert [blob.deleted for blob in hwp_original_blobs] == [True, False]
    assert result == {
        "fileId": "f1",
        "status": "EXCLUDED",
        "reason": "out_of_folder_scope",
        "ragDeleted": True,
        "sourceDeleted": 2,
        "hwpOriginalDeleted": 1,
    }
    assert [state.status for state in store.upserts] == [DocStatus.EXCLUDED]
    assert store.dlq == []


def test_missing_state_still_cleans_orphaned_scope_artifacts(monkeypatch) -> None:
    orphan = _FakeBlob("f1.pdf")
    gcs = _FakeGcs({"source-bucket": [orphan], "hwp-original-bucket": []})
    store = _FakeStore(None)
    rag_calls: list[str] = []
    _patch_ingest_dependencies(
        monkeypatch,
        store=store,
        gcs=gcs,
        rag=_FakeRag(rag_calls, deleted=False),
    )

    result = ingest(_body())

    assert result["status"] == "EXCLUDED"
    assert result["ragDeleted"] is False
    assert result["sourceDeleted"] == 1
    assert result["hwpOriginalDeleted"] == 0
    assert orphan.deleted is True
    assert rag_calls == ["f1"]


def test_already_evicted_file_does_not_rescan_the_corpus(monkeypatch) -> None:
    gcs = _FakeGcs({"source-bucket": [], "hwp-original-bucket": []})
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

    assert result["status"] == "EXCLUDED"
    assert result["cleanupSkipped"] is True
    assert rag_calls == []
    assert gcs._client.calls == []


def test_skipped_for_another_reason_still_cleans_the_corpus(monkeypatch) -> None:
    blob = _FakeBlob("f1.pdf")
    gcs = _FakeGcs({"source-bucket": [blob], "hwp-original-bucket": []})
    store = _FakeStore(
        DocState(file_id="f1", drive_id="drive-1", status=DocStatus.SKIPPED)
    )
    rag_calls: list[str] = []
    _patch_ingest_dependencies(
        monkeypatch, store=store, gcs=gcs, rag=_FakeRag(rag_calls)
    )

    result = ingest(_body())

    assert result["status"] == "EXCLUDED"
    assert rag_calls == ["f1"]
    assert blob.deleted is True


class _SplitSettings(_FakeSettings):
    audience_split_enabled = True
    rag_corpus_name_student = "corpora/student"


class _RecordingRag:
    calls: list[tuple[str | None, str]] = []
    student_error: Exception | None = None

    def __init__(self, *_a, corpus_name: str | None = None, **_k) -> None:
        self.corpus_name = corpus_name

    def delete_by_file_id(self, file_id: str) -> bool:
        type(self).calls.append((self.corpus_name, file_id))
        if self.corpus_name and type(self).student_error:
            raise type(self).student_error
        return True


def _patch_split_dependencies(monkeypatch, *, store: _FakeStore, gcs: _FakeGcs) -> None:
    _RecordingRag.calls = []
    monkeypatch.setattr(_RecordingRag, "student_error", None)
    monkeypatch.setattr(sync_main, "get_settings", lambda: _SplitSettings())
    monkeypatch.setattr(sync_main, "DocStateStore", lambda *a, **k: store)
    monkeypatch.setattr(sync_main, "GcsClient", lambda *a, **k: gcs)
    monkeypatch.setattr(sync_main, "DriveClient", lambda *a, **k: _FakeDrive())
    monkeypatch.setattr(sync_main, "RagEngineClient", _RecordingRag)


def test_out_of_scope_cleanup_also_removes_from_student_corpus(monkeypatch) -> None:
    gcs = _FakeGcs({"source-bucket": [], "hwp-original-bucket": []})
    store = _FakeStore(
        DocState(file_id="f1", drive_id="drive-1", status=DocStatus.INDEXED)
    )
    _patch_split_dependencies(monkeypatch, store=store, gcs=gcs)

    result = ingest(_body())

    assert result["status"] == "EXCLUDED"
    assert _RecordingRag.calls == [(None, "f1"), ("corpora/student", "f1")]
    assert [state.status for state in store.upserts] == [DocStatus.EXCLUDED]


def test_student_corpus_cleanup_failure_is_dlq_and_never_skipped(monkeypatch) -> None:
    gcs = _FakeGcs({"source-bucket": [], "hwp-original-bucket": []})
    store = _FakeStore(
        DocState(file_id="f1", drive_id="drive-1", status=DocStatus.INDEXED)
    )
    _patch_split_dependencies(monkeypatch, store=store, gcs=gcs)
    monkeypatch.setattr(
        _RecordingRag, "student_error", RuntimeError("student corpus unavailable")
    )

    with pytest.raises(HTTPException) as caught:
        ingest(_body())

    assert caught.value.status_code == 500
    assert "out_of_folder_scope_cleanup_failed" in str(caught.value.detail)
    assert store.upserts == []
    assert len(store.dlq) == 1


def test_rag_cleanup_failure_is_dlq_and_never_skipped(monkeypatch) -> None:
    blob = _FakeBlob("f1.pdf")
    gcs = _FakeGcs({"source-bucket": [blob], "hwp-original-bucket": []})
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
    assert blob.deleted is True
    assert store.upserts == []
    assert len(store.dlq) == 1
    assert store.dlq[0][0] == "f1"
    assert store.dlq[0][1].startswith("out_of_folder_scope_cleanup_failed")


def test_gcs_cleanup_failure_is_dlq_and_never_skipped(monkeypatch) -> None:
    blobs = [
        _FakeBlob("f1.pdf", error=RuntimeError("gcs unavailable")),
        _FakeBlob("f1.meta.md"),
    ]
    gcs = _FakeGcs({"source-bucket": blobs, "hwp-original-bucket": []})
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
    assert blobs[1].deleted is True
    assert store.upserts == []
    assert len(store.dlq) == 1
