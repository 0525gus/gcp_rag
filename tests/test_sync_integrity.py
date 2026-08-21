"""동기화 커밋·색인 실패가 성공으로 위장되지 않는지 검증한다."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

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
    # 복구 경로가 이제 **이 설정으로** RagEngineClient 를 만든다.
    # 예전에는 인자 없이 만들어 전역 기본 코퍼스를 봤다 — 학과를 갈라 놓고도
    # 전부 한 코퍼스로 들어가던 원인이다.
    rag_corpus_name = "corpus-under-test"


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
    """백필 시작 시의 기존 청크 일괄 제거 (코퍼스 1회 순회).

    백필은 이제 학과(=드라이브) 설정을 넘겨 클라이언트를 만든다
    (`RagEngineClient(settings)`) — 안 넘기면 전역 기본 코퍼스를 보게 된다.
    """

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
    # 백필은 클라이언트를 재사용하려고 _ingest_with 를 직접 부른다.
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
        sync_main, "_import_and_mark", lambda _store, uris, _ids, **_k: ImportOutcome(
            uris=list(uris), imported=len(uris), failed=0, skipped=0
        )
    )

    result = sync_main.backfill_run(
        sync_main.BackfillRunBody(driveId="drive", indexBatchSize=3)
    )

    assert result["ok"] is True
    assert store.committed == [("drive", "candidate-token")]


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
    """백필은 /sync/index-gcs 를 거치지 않는다.

    학생 코퍼스를 여기서 안 맞추면 초기 적재 직후 학생 코퍼스가 통째로 비고,
    그 문서들은 이미 INDEXED 라 이후 델타에도 안 걸려 영영 안 채워진다.
    """
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
    """학생 코퍼스 동기화 실패를 삼키면 안 된다.

    교직원 import 만 성공한 채 토큰이 커밋되면, 그 배치는 다시 안 돌아
    학생 코퍼스에 영구 구멍이 남는다.
    """
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
    """재백필에서 UNCHANGED 로 빠지는 문서의 청크를 지우면 그대로 유실된다.

    스냅샷 전체를 미리 지우던 시절, 3천 건 재백필은 코퍼스를 비우고도
    totals.unchanged 로 세어 ok=true 로 보고했다 — ERROR 로그 한 줄 없이.
    """
    deleted: list[list[str]] = []
    imported: list[list[str]] = []

    class _Rag:
        # 백필이 학과(=드라이브) 설정을 넘겨 만든다 — RagEngineClient(settings).
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

    # 예외가 아니라 outcome 으로 돌려준다 — 호출측이 배치를 PARSED 로 남겨
    # 다음 주기에 회수할 수 있어야 하기 때문이다(_import_and_mark 참고).
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

    # imported + skipped 가 보낸 수에 못 미치면 성공으로 세지 않는다.
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

    # skipped 는 이미 코퍼스에 있어 재사용된 정상 결과라 성공으로 센다.
    outcome = _rag_client().import_from_gcs(uris)
    assert outcome.ok is True
    assert outcome.uris == uris


def test_index_gcs_does_not_mark_state_on_incomplete_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeRag:
        # index-gcs 가 학과(=드라이브)별 클라이언트를 만든다 — RagEngineClient(settings).
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

    # 예외 대신 PARTIAL 로 돌려준다 — 배치는 PARSED 로 남아 다음 주기가 회수한다.
    # 핵심 계약은 그대로다: 불완전한 결과로는 상태를 건드리지 않는다(FakeStore.get).
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
        # index-gcs 가 학과(=드라이브)별 클라이언트를 만든다 — RagEngineClient(settings).
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

        # 복구·색인 경로가 학과(=드라이브) 설정을 넘겨 만든다 — RagEngineClient(settings).

        def __init__(self, *_a: object, **_kw: object) -> None:

            pass


        def delete_files_by_ids(self, _file_ids: list[str]) -> int:
            # 사전 일괄 삭제는 성공하고 import 만 실패하는 상황을 만든다.
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


# ------------------------------------------------- parked 는 커밋을 막지 않는다
def test_parked_docs_are_accounted_and_do_not_break_reconcile() -> None:
    """DLQ·분할 대기는 '처리를 마친 것'이라 accounted 에 들어가야 한다.

    안 넣으면 그만큼 unaccounted 로 잡혀 reconcile 이 실패하고, 워크플로우는
    drive_reconciled=false 로 pageToken 을 커밋하지 않는다 — parked 를 failed 에서
    뺀 의미가 사라진다.
    """
    body = sync_main.ReconcileBody(
        driveId="d",
        listed=5,
        gcsUploaded=2,
        uris=2,
        indexed=2,
        failed=0,
        parked=2,          # DLQ 1 + 분할 대기 1
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
    """parked 와 failed 는 서로 다른 문서다 — 한쪽을 다른 쪽에 겹쳐 세면 안 된다.

    reconcile 이 보는 것은 '전부 설명됐는가'(항등식)뿐이고, 실패 때문에 커밋을
    막는 판단은 워크플로우의 `drive_failed == 0` 이 한다. 그래서 failed 가 있어도
    항등식이 맞으면 여기서는 ok 다 — 그 구분이 parked 분리의 전제다.
    """
    body = sync_main.ReconcileBody(
        driveId="d",
        listed=3,
        gcsUploaded=1,
        uris=1,
        indexed=1,
        failed=1,          # 진짜 일시 실패 — 커밋은 워크플로우가 막는다
        parked=1,          # 처리를 마친 것 — 커밋을 막지 않는다
        skipped=0,
        deleted=0,
        unchanged=0,
    )

    r = sync_main.reconcile(body)

    # 1(gcs) + 1(failed) + 1(parked) = 3 = listed
    assert r["unaccounted"] == 0
    assert (r["failed"], r["parked"]) == (1, 1)


def test_backfill_dlq_does_not_block_token_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """처리 불가 문서 1건이 백필의 pageToken 을 영구히 세우면 안 된다."""
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
