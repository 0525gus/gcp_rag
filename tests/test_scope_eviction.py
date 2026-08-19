"""Out-of-folder-scope documents must be removed before becoming SKIPPED."""

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
    # No RAG match is already clean and must remain an idempotent success.
    assert result["ragDeleted"] is False
    assert result["sourceDeleted"] == 1
    assert result["hwpOriginalDeleted"] == 0
    assert orphan.deleted is True
    assert rag_calls == ["f1"]


def test_already_evicted_file_does_not_rescan_the_corpus(monkeypatch) -> None:
    """정리를 마친 범위 밖 파일이 다시 델타에 실려도 코퍼스를 또 순회하지 않는다.

    delete_by_file_id 는 파일 하나를 찾으려고 코퍼스를 전수 순회한다. 범위 밖
    파일은 바뀔 때마다 델타에 다시 오므로, 생략하지 않으면 그 비용이 매 실행
    반복되고 대량 변경 시 list 쿼터를 넘겨 SKIP 분기를 실패시킨다.
    """
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
    """SKIPPED 만으로 생략하면 안 된다 — 미지원 MIME 로 바뀐 문서는 청크가 남는다."""
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
    """코퍼스별 삭제 호출을 기록한다. 인자 없는 생성 = 기본(교직원) 코퍼스."""

    calls: list[tuple[str | None, str]] = []
    # 값이 있으면 학생 코퍼스 삭제만 실패시킨다 (교직원 쪽은 성공).
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
    """분리가 켜지면 학생 코퍼스에서도 내려야 한다.

    기본 클라이언트는 교직원 코퍼스만 본다. 학생 쪽을 빼면 범위 밖으로 나간
    문서가 교직원 검색에서만 사라지고 **학생에게는 계속 검색된다**.
    """
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
    """학생 코퍼스 삭제 실패를 삼키면 안 된다 — 내려야 할 자료가 남는다."""
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
    assert blob.deleted is True  # GCS is still cleaned even when RAG cleanup fails.
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
    assert blobs[1].deleted is True  # Remaining objects are attempted after one failure.
    assert store.upserts == []
    assert len(store.dlq) == 1
