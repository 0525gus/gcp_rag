from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import services.sync.main as sync_main
import shared.rag_engine as rag_module
from shared.models import DocState, DocStatus
from shared.rag_engine import ImportOutcome, RagEngineClient


class _Settings:
    sync_folder_id_list: list[str] = []
    student_folder_id_list: list[str] = []
    audience_split_enabled = False
    rag_corpus_name_student = ""
    ingest_concurrency = 1
    gcp_project_id = "project"
    gcp_region = "asia-northeast3"
    gcs_source_bucket = "src-bkt"
    rag_chunk_size = 1024
    rag_chunk_overlap = 256
    rag_corpus_name = "corpus-under-test"


class _LockableStore:
    def try_acquire_lock(self, _name: str, *, ttl_seconds: int) -> bool:
        return True

    def release_lock(self, _name: str) -> None:
        pass


class _BackfillStore(_LockableStore):
    def __init__(self) -> None:
        self.committed: list[tuple[str, str]] = []
        self.progress: list[tuple[str, dict[str, object]]] = []
        self.indexed: list[str] = []

    def get_start_page_token(self, _drive_id: str) -> None:
        return None

    def set_start_page_token(self, drive_id: str, token: str) -> None:
        self.committed.append((drive_id, token))

    def update_sync_run(self, run_id: str, fields: dict[str, object]) -> None:
        self.progress.append((run_id, fields))

    def get(self, _file_id: str) -> None:
        return None

    def mark_indexed(self, file_id: str) -> None:
        self.indexed.append(file_id)


class _BackfillDrive:
    def get_start_page_token(self, _drive_id: str) -> str:
        return "candidate-token"

    def iter_backfill_files(self, drive_id: str, _folder_ids: list[str]):
        for i in range(3):
            yield {
                "id": f"f{i}",
                "driveId": drive_id,
                "name": f"f{i}.txt",
                "mimeType": "text/plain",
            }


class _NoopRag:
    def __init__(self, *_args: object, **_kwargs: object) -> None:
        pass

    def delete_files_by_ids(self, _file_ids: list[str]) -> int:
        return 0


def _wire_backfill(monkeypatch: pytest.MonkeyPatch) -> _BackfillStore:
    store = _BackfillStore()
    monkeypatch.setattr(sync_main, "get_settings", lambda: _Settings())
    monkeypatch.setattr(sync_main, "DocStateStore", lambda: store)
    monkeypatch.setattr(sync_main, "DriveClient", _BackfillDrive)
    monkeypatch.setattr(sync_main, "GcsClient", lambda _s=None: object())
    monkeypatch.setattr(sync_main, "RagEngineClient", _NoopRag)
    monkeypatch.setattr(
        sync_main,
        "_ingest_with",
        lambda body, **_clients: {
            "status": "GCS_READY",
            "gcsUris": [f"gs://{body.file_id}.txt"],
        },
    )
    return store


def test_backfill_snapshot_does_not_persist_candidate_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _BackfillStore()

    result = sync_main._build_backfill_changes(
        "drive",
        store=store,
        drive=_BackfillDrive(),
        settings=_Settings(),
    )

    assert result["pendingPageToken"] == "candidate-token"
    assert store.committed == []


def test_bootstrap_requires_explicit_snapshot_skip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _BackfillStore()
    monkeypatch.setattr(sync_main, "DocStateStore", lambda: store)
    monkeypatch.setattr(sync_main, "DriveClient", _BackfillDrive)

    with pytest.raises(sync_main.HTTPException) as exc_info:
        sync_main.bootstrap(sync_main.BootstrapBody(driveId="drive"))

    assert exc_info.value.status_code == 409
    assert store.committed == []


def test_bootstrap_baseline_only_is_explicit_opt_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _BackfillStore()
    monkeypatch.setattr(sync_main, "DocStateStore", lambda: store)
    monkeypatch.setattr(sync_main, "DriveClient", _BackfillDrive)

    result = sync_main.bootstrap(
        sync_main.BootstrapBody(driveId="drive", baselineOnly=True)
    )

    assert result["status"] == "created_baseline_only"
    assert store.committed == [("drive", "candidate-token")]


def test_backfill_index_failure_is_counted_and_token_is_not_committed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _wire_backfill(monkeypatch)
    monkeypatch.setattr(
        sync_main,
        "_import_and_mark",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("RAG unavailable")),
    )

    result = sync_main.backfill_run(
        sync_main.BackfillRunBody(driveId="drive", indexBatchSize=3)
    )

    assert result["totals"]["indexFailed"] == 3
    assert result["totals"]["failed"] == 0
    assert result["totals"]["gcsUploaded"] == 3
    assert result["totals"]["indexed"] == 0
    assert result["ok"] is False
    assert store.committed == []


def test_backfill_commits_candidate_only_after_complete_index(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _wire_backfill(monkeypatch)
    monkeypatch.setattr(
        sync_main, "_import_and_mark", lambda _store, uris, _ids, **_k: ImportOutcome(
            uris=list(uris), imported=len(uris), failed=0, skipped=0
        )
    )

    result = sync_main.backfill_run(
        sync_main.BackfillRunBody(driveId="drive", indexBatchSize=3)
    )

    assert result["ok"] is True
    assert store.committed == [("drive", "candidate-token")]


def test_backfill_publishes_real_progress_for_manual_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _wire_backfill(monkeypatch)
    _indexes_everything(monkeypatch)

    result = sync_main.backfill_run(
        sync_main.BackfillRunBody(
            driveId="drive",
            indexBatchSize=3,
            runId="a" * 32,
            departmentCode="ee",
            driveIndex=1,
            driveCount=2,
        )
    )

    assert result["ok"] is True
    phases = [fields["phase"] for _run_id, fields in store.progress]
    assert phases[0] == "LISTING"
    assert "INGESTING" in phases
    assert "INDEXING" in phases
    assert phases[-1] == "COMPLETE"
    run_id, final = store.progress[-1]
    assert run_id == "a" * 32
    assert final["departmentCode"] == "ee"
    assert final["driveIndex"] == 1
    assert final["driveCount"] == 2
    assert final["processed"] == 3
    assert final["totals"]["indexed"] == 3


class _SplitSettings(_Settings):
    audience_split_enabled = True
    rag_corpus_name_student = "corpora/student"
    student_folder_id_list = ["folder-student"]


def _indexes_everything(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        sync_main,
        "_import_and_mark",
        lambda _store, uris, _ids, **_k: ImportOutcome(
            uris=list(uris), imported=len(uris), failed=0, skipped=0
        ),
    )


def test_backfill_syncs_student_corpus_for_each_indexed_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _wire_backfill(monkeypatch)
    monkeypatch.setattr(sync_main, "get_settings", lambda: _SplitSettings())
    _indexes_everything(monkeypatch)

    synced: list[tuple[list[str], list[str]]] = []

    def _record(uris, ids, _settings, _store):
        synced.append((list(uris), list(ids)))
        return {"enabled": True}

    monkeypatch.setattr(sync_main, "_sync_student_corpus", _record)

    result = sync_main.backfill_run(
        sync_main.BackfillRunBody(driveId="drive", indexBatchSize=3)
    )

    assert result["ok"] is True
    assert len(synced) == 1, "배치마다 학생 코퍼스를 맞춰야 한다"
    uris, ids = synced[0]
    assert sorted(ids) == ["f0", "f1", "f2"]
    assert len(uris) == 3
    assert store.committed == [("drive", "candidate-token")]


def test_backfill_student_sync_failure_blocks_token_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _wire_backfill(monkeypatch)
    monkeypatch.setattr(sync_main, "get_settings", lambda: _SplitSettings())
    _indexes_everything(monkeypatch)
    monkeypatch.setattr(
        sync_main,
        "_sync_student_corpus",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("student corpus down")),
    )

    result = sync_main.backfill_run(
        sync_main.BackfillRunBody(driveId="drive", indexBatchSize=3)
    )

    assert result["ok"] is False
    assert result["totals"]["indexFailed"] == 3
    assert store.committed == []


def test_backfill_does_not_delete_chunks_of_documents_it_will_not_reimport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deleted: list[list[str]] = []
    imported: list[list[str]] = []

    class _Rag:
        def __init__(self, *_a: object, **_kw: object) -> None:
            pass

        def delete_files_by_ids(self, file_ids: list[str]) -> int:
            deleted.append(sorted(file_ids))
            return len(file_ids)

        def import_from_gcs(self, uris: list[str]) -> list[str]:
            imported.append(list(uris))
            return list(uris)

    store = _BackfillStore()
    monkeypatch.setattr(sync_main, "get_settings", lambda: _Settings())
    monkeypatch.setattr(sync_main, "DocStateStore", lambda: store)
    monkeypatch.setattr(sync_main, "DriveClient", _BackfillDrive)
    monkeypatch.setattr(sync_main, "GcsClient", lambda _s=None: object())
    monkeypatch.setattr(sync_main, "RagEngineClient", _Rag)
    monkeypatch.setattr(
        sync_main, "_ingest_with", lambda body, **_c: {"status": "UNCHANGED"}
    )

    result = sync_main.backfill_run(sync_main.BackfillRunBody(driveId="drive"))

    assert deleted == [], "재import 하지 않을 문서의 청크를 지웠다"
    assert imported == []
    assert result["totals"]["unchanged"] == 3


def _rag_client() -> RagEngineClient:
    client = object.__new__(RagEngineClient)
    client.settings = _Settings()
    client.corpus_name = "projects/p/locations/l/ragCorpora/c"
    return client


def test_rag_import_rejects_partial_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        rag_module.rag,
        "import_files",
        lambda *_args, **_kwargs: SimpleNamespace(
            imported_rag_files_count=2,
            failed_rag_files_count=1,
            skipped_rag_files_count=0,
            partial_failures_gcs_path="gs://bucket/failures.ndjson",
            partial_failures_bigquery_table="",
        ),
    )

    outcome = _rag_client().import_from_gcs(["gs://b/1", "gs://b/2", "gs://b/3"])

    assert outcome.ok is False
    assert len(outcome.uris) == 3
    assert outcome.failed == 1


def test_rag_import_rejects_unaccounted_inputs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        rag_module.rag,
        "import_files",
        lambda *_args, **_kwargs: SimpleNamespace(
            imported_rag_files_count=1,
            failed_rag_files_count=0,
            skipped_rag_files_count=0,
        ),
    )

    outcome = _rag_client().import_from_gcs(["gs://b/1", "gs://b/2"])

    assert outcome.ok is False
    assert outcome.imported == 1


def test_rag_import_accepts_imported_and_deduplicated_files(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        rag_module.rag,
        "import_files",
        lambda *_args, **_kwargs: SimpleNamespace(
            imported_rag_files_count=1,
            failed_rag_files_count=0,
            skipped_rag_files_count=1,
        ),
    )
    uris = ["gs://b/1", "gs://b/2"]

    outcome = _rag_client().import_from_gcs(uris)
    assert outcome.ok is True
    assert outcome.uris == uris


def test_index_gcs_does_not_mark_state_on_incomplete_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeRag:
        def __init__(self, *_a: object, **_kw: object) -> None:
            pass

        def delete_files_by_ids(self, _file_ids: list[str]) -> int:
            return 0

        def import_from_gcs(self, uris: list[str]) -> ImportOutcome:
            return ImportOutcome(uris=list(uris), imported=1, failed=1, skipped=0)

    class FakeStore:
        def get(self, _file_id: str):
            raise AssertionError("state must not be read for an incomplete import")

    monkeypatch.setattr(sync_main, "RagEngineClient", FakeRag)
    monkeypatch.setattr(sync_main, "DocStateStore", FakeStore)
    monkeypatch.setattr(sync_main, "get_settings", lambda: _Settings())

    result = sync_main.index_gcs(
        sync_main.IndexGcsBody(
            gcsUris=["gs://b/1", "gs://b/2"],
            fileIds=["realid00001", "realid00002"],
        )
    )

    assert result["ok"] is False
    assert result["status"] == "PARTIAL"
    assert result["count"] == 1


def test_index_gcs_stops_when_predelete_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    class FakeRag:
        def __init__(self, *_a: object, **_kw: object) -> None:
            pass

        def delete_files_by_ids(self, _file_ids: list[str]) -> int:
            calls.append("delete")
            raise RuntimeError("delete unavailable")

        def import_from_gcs(self, _uris: list[str]) -> list[str]:
            calls.append("import")
            return list(_uris)

    class FakeStore:
        def get(self, _file_id: str):
            raise AssertionError("state must not change when pre-delete fails")

    monkeypatch.setattr(sync_main, "RagEngineClient", FakeRag)
    monkeypatch.setattr(sync_main, "DocStateStore", FakeStore)
    monkeypatch.setattr(sync_main, "get_settings", lambda: _Settings())

    with pytest.raises(RuntimeError, match="delete unavailable"):
        sync_main.index_gcs(
            sync_main.IndexGcsBody(gcsUris=["gs://b/f1.md"], fileIds=["f1"])
        )

    assert calls == ["delete"]


def test_index_gcs_preserves_vertex_quota_as_http_429(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeRag:
        def __init__(self, *_a: object, **_kw: object) -> None:
            pass

        def delete_files_by_ids(self, _file_ids: list[str]) -> int:
            return 0

        def import_from_gcs(self, _uris: list[str]) -> ImportOutcome:
            raise sync_main.RagImportThrottledError("Vertex RAG quota still exhausted")

    class FakeStore:
        def get(self, _file_id: str):
            return None

    monkeypatch.setattr(sync_main, "RagEngineClient", FakeRag)
    monkeypatch.setattr(sync_main, "DocStateStore", FakeStore)
    monkeypatch.setattr(sync_main, "get_settings", lambda: _Settings())

    with pytest.raises(HTTPException) as raised:
        sync_main.index_gcs(
            sync_main.IndexGcsBody(gcsUris=["gs://b/f1.md"], fileIds=["f1"])
        )

    assert raised.value.status_code == 429
    assert "quota" in str(raised.value.detail).lower()


def test_student_corpus_failure_does_not_mark_document_indexed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = DocState(file_id="student-file", drive_id="drive", status=DocStatus.PARSED)
    upserts: list[DocStatus] = []

    class FakeStore:
        def get(self, _file_id: str) -> DocState:
            return state

        def upsert(self, doc: DocState) -> None:
            upserts.append(doc.status)

    class FakeRag:
        def __init__(self, *_a: object, **_kw: object) -> None:
            pass

        def delete_files_by_ids(self, _file_ids: list[str]) -> int:
            return 0

        def import_from_gcs(self, uris: list[str]) -> ImportOutcome:
            return ImportOutcome(uris=list(uris), imported=len(uris), failed=0, skipped=0)

    monkeypatch.setattr(sync_main, "DocStateStore", FakeStore)
    monkeypatch.setattr(sync_main, "RagEngineClient", FakeRag)
    monkeypatch.setattr(sync_main, "get_settings", lambda: _SplitSettings())
    monkeypatch.setattr(
        sync_main,
        "_sync_student_corpus",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("student failed")),
    )

    with pytest.raises(RuntimeError, match="student failed"):
        sync_main.index_gcs(
            sync_main.IndexGcsBody(
                gcsUris=["gs://bucket/student-file.md"],
                fileIds=["student-file"],
            )
        )

    assert state.status == DocStatus.PARSED
    assert upserts == []


@pytest.mark.parametrize("batch_size", [3, 10])
def test_reindex_failure_is_counted_for_threshold_and_final_flush(
    monkeypatch: pytest.MonkeyPatch,
    batch_size: int,
) -> None:
    docs = [
        DocState(file_id=f"f{i}", drive_id="drive", status=DocStatus.PARSED)
        for i in range(3)
    ]

    class FakeStore:
        def list_by_status(self, status, limit=100, *, cursor_key=None):
            assert status == DocStatus.PARSED
            assert cursor_key == "reindex-pending"
            return docs[:limit]

    class FailingRag:
        def import_from_gcs(self, _uris: list[str]) -> list[str]:
            raise RuntimeError("RAG unavailable")

        def __init__(self, *_a: object, **_kw: object) -> None:

            pass


        def delete_files_by_ids(self, _file_ids: list[str]) -> int:
            return 0

    monkeypatch.setattr(sync_main, "get_settings", lambda: _Settings())
    monkeypatch.setattr(sync_main, "DocStateStore", FakeStore)
    monkeypatch.setattr(sync_main, "RagEngineClient", FailingRag)
    monkeypatch.setattr(sync_main, "GcsClient", lambda _s: object())
    monkeypatch.setattr(
        sync_main,
        "_source_uris_for_file",
        lambda _settings, file_id, _c=None: [f"gs://{file_id}.txt"],
    )

    result = sync_main._reindex_pending_sync(
        sync_main.ReindexPendingBody(limit=3, indexBatchSize=batch_size)
    )

    assert result["totals"]["failed"] == 3
    assert result["totals"]["indexed"] == 0
    assert result["ok"] is False


def test_reindex_no_uri_is_not_reported_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    doc = DocState(file_id="f1", drive_id="drive", status=DocStatus.PARSED)

    class FakeStore:
        def __init__(self) -> None:
            self.enqueued: list[tuple[str, str]] = []

        def list_by_status(self, _status, limit=100, *, cursor_key=None):
            return [doc][:limit]

        def enqueue_dlq(self, file_id: str, reason: str, **_fields) -> None:
            self.enqueued.append((file_id, reason))

    store = FakeStore()
    monkeypatch.setattr(sync_main, "get_settings", lambda: _Settings())
    monkeypatch.setattr(sync_main, "DocStateStore", lambda: store)
    monkeypatch.setattr(sync_main, "GcsClient", lambda _s: object())
    monkeypatch.setattr(sync_main, "GcsClient", lambda _s: object())
    monkeypatch.setattr(sync_main, "_source_uris_for_file", lambda *_args: [])

    result = sync_main._reindex_pending_sync(sync_main.ReindexPendingBody(limit=1))

    assert result["totals"]["skippedNoUri"] == 1
    assert result["totals"]["failed"] == 1
    assert result["ok"] is False
    assert store.enqueued == [("f1", "reindex_no_source_uri")]


def test_reindex_replays_original_office_uri_with_sidecar(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from google.cloud import storage

    blobs = [
        SimpleNamespace(name="f1.xlsx"),
        SimpleNamespace(name="f1.meta.md"),
        SimpleNamespace(name="f10.xlsx"),
    ]

    class FakeClient:
        def bucket(self, name: str) -> str:
            return name

        def list_blobs(self, _bucket: str, *, prefix: str):
            return [blob for blob in blobs if blob.name.startswith(prefix)]

    monkeypatch.setattr(storage, "Client", lambda **_kwargs: FakeClient())

    assert sync_main._source_uris_for_file(_Settings(), "f1") == [
        "gs://src-bkt/f1.xlsx",
        "gs://src-bkt/f1.meta.md",
    ]


def test_backfill_rejects_a_second_concurrent_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Busy(_BackfillStore):
        def try_acquire_lock(self, _name: str, *, ttl_seconds: int) -> bool:
            return False

    store = _Busy()
    monkeypatch.setattr(sync_main, "DocStateStore", lambda: store)

    with pytest.raises(sync_main.HTTPException) as exc_info:
        sync_main.backfill_run(sync_main.BackfillRunBody(driveId="drive"))

    assert exc_info.value.status_code == 409


def test_backfill_releases_the_lock_even_when_it_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    released: list[str] = []

    class _Tracking(_BackfillStore):
        def release_lock(self, name: str) -> None:
            released.append(name)

    store = _Tracking()
    monkeypatch.setattr(sync_main, "get_settings", lambda: _Settings())
    monkeypatch.setattr(sync_main, "DocStateStore", lambda: store)
    monkeypatch.setattr(sync_main, "GcsClient", lambda _s=None: object())
    monkeypatch.setattr(sync_main, "RagEngineClient", _NoopRag)
    monkeypatch.setattr(
        sync_main,
        "DriveClient",
        lambda: (_ for _ in ()).throw(RuntimeError("drive down")),
    )

    with pytest.raises(RuntimeError):
        sync_main.backfill_run(sync_main.BackfillRunBody(driveId="drive"))

    assert released == ["backfill:drive"], "실패해도 잠금은 풀려야 한다"


def test_parked_docs_are_accounted_and_do_not_break_reconcile() -> None:
    body = sync_main.ReconcileBody(
        driveId="d",
        listed=5,
        gcsUploaded=2,
        uris=2,
        indexed=2,
        failed=0,
        parked=2,
        dlq=1,
        splitQueued=1,
        skipped=1,
        deleted=0,
        unchanged=0,
    )

    r = sync_main.reconcile(body)

    assert r["unaccounted"] == 0
    assert r["parked"] == 2
    assert r["ok"] is True


def test_parked_and_failed_are_counted_once_each() -> None:
    body = sync_main.ReconcileBody(
        driveId="d",
        listed=3,
        gcsUploaded=1,
        uris=1,
        indexed=1,
        failed=1,
        parked=1,
        skipped=0,
        deleted=0,
        unchanged=0,
    )

    r = sync_main.reconcile(body)

    assert r["unaccounted"] == 0
    assert (r["failed"], r["parked"]) == (1, 1)


def test_backfill_dlq_does_not_block_token_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _wire_backfill(monkeypatch)
    monkeypatch.setattr(
        sync_main,
        "_ingest_with",
        lambda body, **_c: {"status": "DLQ", "error": "PARSE_FAILED"},
    )

    result = sync_main.backfill_run(
        sync_main.BackfillRunBody(driveId="drive", indexBatchSize=3)
    )

    assert result["totals"]["dlq"] == 3
    assert result["totals"]["parked"] == 3
    assert result["totals"]["failed"] == 0, "parked 를 failed 로 세면 커밋이 막힌다"
    assert result["ok"] is True
    assert store.committed == [("drive", "candidate-token")]
