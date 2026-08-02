"""동기화 커밋·색인 실패가 성공으로 위장되지 않는지 검증한다."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import services.sync.main as sync_main
import shared.rag_engine as rag_module
from shared.models import DocState, DocStatus
from shared.rag_engine import RagEngineClient, RagImportError


class _Settings:
    sync_folder_id_list: list[str] = []
    raw_upload_concurrency = 1
    gcp_project_id = "project"
    gcs_normalized_bucket = "normalized"
    rag_chunk_size = 1024
    rag_chunk_overlap = 256


class _LockableStore:
    """backfill-run 은 드라이브당 하나만 돌도록 잠금을 잡는다."""

    def try_acquire_lock(self, _name: str, *, ttl_seconds: int) -> bool:
        return True

    def release_lock(self, _name: str) -> None:
        pass


class _BackfillStore(_LockableStore):
    def __init__(self) -> None:
        self.committed: list[tuple[str, str]] = []

    def get_start_page_token(self, _drive_id: str) -> None:
        return None

    def set_start_page_token(self, drive_id: str, token: str) -> None:
        self.committed.append((drive_id, token))


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
    """백필 시작 시의 기존 청크 일괄 제거 (코퍼스 1회 순회)."""

    def delete_files_by_ids(self, _file_ids: list[str]) -> int:
        return 0


def _wire_backfill(monkeypatch: pytest.MonkeyPatch) -> _BackfillStore:
    store = _BackfillStore()
    monkeypatch.setattr(sync_main, "get_settings", lambda: _Settings())
    monkeypatch.setattr(sync_main, "DocStateStore", lambda: store)
    monkeypatch.setattr(sync_main, "DriveClient", _BackfillDrive)
    monkeypatch.setattr(sync_main, "GcsClient", lambda _s=None: object())
    monkeypatch.setattr(sync_main, "RagEngineClient", _NoopRag)
    # 백필은 클라이언트를 재사용하려고 _ingest_with 를 직접 부른다.
    monkeypatch.setattr(
        sync_main,
        "_ingest_with",
        lambda body, **_clients: {
            "status": "GCS_READY",
            "gcsUris": [f"gs://normalized/{body.file_id}.txt"],
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
    # 백필은 배치마다 _import_and_mark 로 (삭제 → import) 를 함께 수행한다.
    monkeypatch.setattr(
        sync_main,
        "_import_and_mark",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("RAG unavailable")),
    )

    result = sync_main.backfill_run(
        sync_main.BackfillRunBody(driveId="drive", indexBatchSize=3)
    )

    # 색인 실패는 indexFailed 로 센다 — failed 에 더하면 그 파일이 gcsUploaded 와
    # 겹쳐 reconcile 의 listed 항등식이 깨진다(unaccounted 음수).
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
        sync_main, "_import_and_mark", lambda _store, uris, _ids, **_k: list(uris)
    )

    result = sync_main.backfill_run(
        sync_main.BackfillRunBody(driveId="drive", indexBatchSize=3)
    )

    assert result["ok"] is True
    assert store.committed == [("drive", "candidate-token")]


def test_backfill_does_not_delete_chunks_of_documents_it_will_not_reimport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """재백필에서 UNCHANGED 로 빠지는 문서의 청크를 지우면 그대로 유실된다.

    스냅샷 전체를 미리 지우던 시절, 3천 건 재백필은 코퍼스를 비우고도
    totals.unchanged 로 세어 ok=true 로 보고했다 — ERROR 로그 한 줄 없이.
    """
    deleted: list[list[str]] = []
    imported: list[list[str]] = []

    class _Rag:
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
    # 이미 INDEXED 이고 modifiedTime 이 그대로면 ingest 는 UNCHANGED 를 돌려준다.
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

    with pytest.raises(RagImportError) as exc_info:
        _rag_client().import_from_gcs(["gs://b/1", "gs://b/2", "gs://b/3"])

    assert exc_info.value.requested == 3
    assert exc_info.value.failed == 1


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

    with pytest.raises(RagImportError):
        _rag_client().import_from_gcs(["gs://b/1", "gs://b/2"])


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

    assert _rag_client().import_from_gcs(uris) == uris


def test_index_gcs_does_not_mark_state_on_incomplete_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeRag:
        def delete_files_by_ids(self, _file_ids: list[str]) -> int:
            return 0

        def import_from_gcs(self, _uris: list[str]) -> list[str]:
            return ["gs://b/1"]

    class FakeStore:
        def get(self, _file_id: str):
            raise AssertionError("state must not be read for an incomplete import")

    monkeypatch.setattr(sync_main, "RagEngineClient", FakeRag)
    monkeypatch.setattr(sync_main, "DocStateStore", FakeStore)

    with pytest.raises(RuntimeError, match="result mismatch"):
        sync_main.index_gcs(
            sync_main.IndexGcsBody(
                gcsUris=["gs://b/1", "gs://b/2"],
                fileIds=["f1", "f2"],
            )
        )


def test_index_gcs_stops_when_predelete_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    class FakeRag:
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

    with pytest.raises(RuntimeError, match="delete unavailable"):
        sync_main.index_gcs(
            sync_main.IndexGcsBody(gcsUris=["gs://b/f1.md"], fileIds=["f1"])
        )

    assert calls == ["delete"]


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

        def delete_files_by_ids(self, _file_ids: list[str]) -> int:
            # 사전 일괄 삭제는 성공하고 import 만 실패하는 상황을 만든다.
            return 0

    monkeypatch.setattr(sync_main, "get_settings", lambda: _Settings())
    monkeypatch.setattr(sync_main, "DocStateStore", FakeStore)
    monkeypatch.setattr(sync_main, "RagEngineClient", FailingRag)
    monkeypatch.setattr(sync_main, "_new_storage_client", lambda _s: object())
    monkeypatch.setattr(
        sync_main,
        "_normalized_uris_for_file",
        lambda _settings, file_id, _c=None: [f"gs://normalized/{file_id}.txt"],
    )

    result = sync_main.reindex_pending(
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
    monkeypatch.setattr(sync_main, "_new_storage_client", lambda _s: object())
    monkeypatch.setattr(sync_main, "_new_storage_client", lambda _s: object())
    monkeypatch.setattr(sync_main, "_normalized_uris_for_file", lambda *_args: [])

    result = sync_main.reindex_pending(sync_main.ReindexPendingBody(limit=1))

    assert result["totals"]["skippedNoUri"] == 1
    assert result["totals"]["failed"] == 1
    assert result["ok"] is False
    assert store.enqueued == [("f1", "reindex_no_normalized_uri")]


def test_reindex_replays_original_office_uri_with_sidecar(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from google.cloud import storage

    blobs = [
        SimpleNamespace(name="normalized/f1.xlsx"),
        SimpleNamespace(name="normalized/f1.meta.md"),
        SimpleNamespace(name="normalized/f10.xlsx"),
    ]

    class FakeClient:
        def bucket(self, name: str) -> str:
            return name

        def list_blobs(self, _bucket: str, *, prefix: str):
            return [blob for blob in blobs if blob.name.startswith(prefix)]

    monkeypatch.setattr(storage, "Client", lambda **_kwargs: FakeClient())

    assert sync_main._normalized_uris_for_file(_Settings(), "f1") == [
        "gs://normalized/normalized/f1.xlsx",
        "gs://normalized/normalized/f1.meta.md",
    ]


# ------------------------------------------------- 백필 단일 실행 잠금
def test_backfill_rejects_a_second_concurrent_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """워크플로우 스텝 타임아웃이 Cloud Run 요청보다 짧으면 재시도가 겹친다.

    첫 요청이 아직 살아 있는데 두 번째가 들어오면 같은 드라이브를 두 번 훑고
    같은 코퍼스에 삭제·import 를 교차시킨다. 서버가 스스로 막아야 한다.
    """

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
