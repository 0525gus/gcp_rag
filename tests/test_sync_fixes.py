from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import services.sync.main as sync_main  # noqa: E402
from services.sync.main import ReconcileBody, reconcile  # noqa: E402
from shared import rag_engine  # noqa: E402
from settings_presets import NO_LEASES_OR_MAPPINGS
from shared.config import Settings  # noqa: E402
from shared.firestore_state import DocStateStore  # noqa: E402
from shared.models import DocState, DocStatus  # noqa: E402
from shared.rag_engine import RagEngineClient  # noqa: E402


class _FakeStore(DocStateStore):
    def __init__(self, doc: DocState | None) -> None:
        self._doc = doc

    def get(self, file_id: str) -> DocState | None:  # type: ignore[override]
        return self._doc


def test_should_skip_reindex_indexed_same_hash() -> None:
    doc = DocState(file_id="f1", drive_id="d", content_hash="h1", status=DocStatus.INDEXED)
    assert _FakeStore(doc).should_skip_reindex("f1", "h1") is True


def test_should_skip_reindex_parsed_not_indexed() -> None:
    doc = DocState(file_id="f1", drive_id="d", content_hash="h1", status=DocStatus.PARSED)
    assert _FakeStore(doc).should_skip_reindex("f1", "h1") is False


def test_should_skip_reindex_diff_hash() -> None:
    doc = DocState(file_id="f1", drive_id="d", content_hash="h1", status=DocStatus.INDEXED)
    assert _FakeStore(doc).should_skip_reindex("f1", "h2") is False


def test_should_skip_reindex_missing() -> None:
    assert _FakeStore(None).should_skip_reindex("f1", "h1") is False


class _FakeRagFile:
    def __init__(self, display: str, name: str) -> None:
        self.display_name = display
        self.name = name


def _bare_client(corpus: str) -> RagEngineClient:
    client = object.__new__(RagEngineClient)
    client.corpus_name = corpus
    client.settings = get_settings_for_test()
    rag_engine._CORPUS_INDEX_CACHE.pop(corpus, None)
    rag_engine._CORPUS_DIRTY_IDS.pop(corpus, None)
    return client


def get_settings_for_test() -> Settings:
    return Settings(
        gcp_project_id="p",
        rag_delete_concurrency=1,
        rag_delete_pacing_seconds=0.0,
        **NO_LEASES_OR_MAPPINGS,
    )


def test_delete_files_by_ids_single_list(monkeypatch) -> None:
    client = _bare_client("corpus-single-list")
    calls = {"list": 0, "deleted": []}

    def fake_list() -> list[_FakeRagFile]:
        calls["list"] += 1
        return [
            _FakeRagFile("f1.md", "rn1"),
            _FakeRagFile("f1.meta.md", "rn2"),
            _FakeRagFile("f2.pdf", "rn3"),
            _FakeRagFile("f10.pdf", "rn-collision"),
            _FakeRagFile("prefix-f1.md", "rn-substring"),
            _FakeRagFile("other.md", "rn4"),
        ]

    client.list_files = fake_list  # type: ignore[method-assign]
    monkeypatch.setattr(
        rag_engine.rag, "delete_file", lambda name: calls["deleted"].append(name)
    )

    n = client.delete_files_by_ids(["f1", "f2"])
    assert calls["list"] == 1
    assert n == 3
    assert set(calls["deleted"]) == {"rn1", "rn2", "rn3"}


def test_shared_client_scans_the_corpus_once_across_batches(monkeypatch) -> None:
    client = object.__new__(RagEngineClient)
    client.corpus_name = "corpora/shared-scan"
    client.settings = get_settings_for_test()
    rag_engine._CORPUS_INDEX_CACHE.pop(client.corpus_name, None)
    rag_engine._CORPUS_DIRTY_IDS.pop(client.corpus_name, None)
    calls = {"list": 0, "deleted": []}

    def fake_list() -> list[_FakeRagFile]:
        calls["list"] += 1
        return [
            _FakeRagFile("f1.md", "rn1"),
            _FakeRagFile("f2.pdf", "rn2"),
            _FakeRagFile("f3.md", "rn3"),
        ]

    client.list_files = fake_list  # type: ignore[method-assign]
    monkeypatch.setattr(
        rag_engine.rag, "delete_file", lambda name: calls["deleted"].append(name)
    )

    assert client.delete_files_by_ids(["f1"]) == 1
    assert client.delete_files_by_ids(["f2"]) == 1
    assert client.delete_files_by_ids(["f3"]) == 1
    assert calls["list"] == 1
    assert calls["deleted"] == ["rn1", "rn2", "rn3"]
    assert client.delete_files_by_ids(["f1"]) == 0
    assert calls["list"] == 1


def test_delete_files_by_ids_empty(monkeypatch) -> None:
    client = _bare_client("corpus-empty")

    def boom() -> list:
        raise AssertionError("list_files should not be called for empty ids")

    client.list_files = boom  # type: ignore[method-assign]
    assert client.delete_files_by_ids([]) == 0


def test_reconcile_no_double_count_dlq() -> None:
    body = ReconcileBody(
        driveId="d", listed=5, gcsUploaded=2, uris=2, indexed=2,
        failed=1, skipped=1, deleted=0, unchanged=1, dlq=1, splitQueued=0,
    )
    r = reconcile(body)
    assert r["unaccounted"] == 0
    assert r["ok"] is True


def test_reconcile_index_ok_uses_uris() -> None:
    body = ReconcileBody(
        driveId="d", listed=1, gcsUploaded=1, uris=2, indexed=2,
        failed=0, skipped=0, deleted=0, unchanged=0,
    )
    r = reconcile(body)
    assert r["indexConsistent"] is True
    assert r["ok"] is True


def test_reconcile_uris_fallback_to_gcs() -> None:
    body = ReconcileBody(
        driveId="d", listed=1, gcsUploaded=1, indexed=1,
        failed=0, skipped=0, deleted=0, unchanged=0,
    )
    r = reconcile(body)
    assert r["indexConsistent"] is True


def test_reconcile_detects_missing_indexed_uri() -> None:
    body = ReconcileBody(
        driveId="d", listed=2, gcsUploaded=2, uris=2, indexed=1,
        failed=0, skipped=0, deleted=0, unchanged=0,
    )
    r = reconcile(body)
    assert r["indexConsistent"] is False
    assert r["ok"] is False


def test_reconcile_detects_real_gap() -> None:
    body = ReconcileBody(
        driveId="d", listed=5, gcsUploaded=2, uris=2, indexed=2,
        failed=1, skipped=1, deleted=0, unchanged=0,
    )
    r = reconcile(body)
    assert r["unaccounted"] == 1
    assert r["ok"] is False


def test_ingest_out_of_scope_returns_uppercase_excluded(monkeypatch) -> None:
    from services.sync.main import IngestBody, ingest

    class FakeSettings:
        sync_folder_id_list = ["folderX"]

    class FakeStore:
        def get(self, _file_id):
            return None

        def upsert(self, *a, **k) -> None:
            pass

    class FakeDrive:
        def is_in_sync_scope(self, file_id, folder_ids) -> bool:
            return False

    monkeypatch.setattr(sync_main, "get_settings", lambda: FakeSettings())
    monkeypatch.setattr(sync_main, "DocStateStore", lambda *a, **k: FakeStore())
    monkeypatch.setattr(sync_main, "GcsClient", lambda *a, **k: object())
    monkeypatch.setattr(sync_main, "DriveClient", lambda *a, **k: FakeDrive())
    monkeypatch.setattr(
        sync_main, "_cleanup_out_of_scope_file", lambda *a, **k: (False, 0, 0)
    )

    res = ingest(IngestBody(fileId="f1", driveId="d", mimeType="application/pdf"))
    assert res["status"] == DocStatus.EXCLUDED.value == "EXCLUDED"
    assert res["reason"] == "out_of_folder_scope"


class _FakeBlob:
    def __init__(self, name: str, sink: list[str]) -> None:
        self.name = name
        self._sink = sink

    def delete(self) -> None:
        self._sink.append(self.name)


class _FakeGcsBackend:
    def __init__(self, names: list[str] | dict[str, list[str]]) -> None:
        self.names = names
        self.deleted: list[str] = []

    def _names_for(self, bucket) -> list[str]:
        if isinstance(self.names, dict):
            return self.names.get(getattr(bucket, "name", bucket), [])
        return self.names

    def bucket(self, name: str) -> object:
        backend = self

        class _B:
            def __init__(self) -> None:
                self.name = name

            @staticmethod
            def blob(n: str) -> _FakeBlob:
                return _FakeBlob(n, backend.deleted)

        return _B()

    def list_blobs(self, bucket, prefix: str = "") -> list[_FakeBlob]:
        return [
            _FakeBlob(n, self.deleted)
            for n in self._names_for(bucket)
            if n.startswith(prefix)
        ]


def _fake_gcs(names: list[str] | dict[str, list[str]]):
    from shared.gcs import GcsClient

    client = object.__new__(GcsClient)
    backend = _FakeGcsBackend(names)
    client._client = backend  # noqa: SLF001
    return client, backend


def test_delete_for_file_catches_suffixes_a_hardcoded_list_missed() -> None:
    client, backend = _fake_gcs(
        [
            "abc123.part1.pdf",
            "abc123.part2.pdf",
            "abc123.meta.md",
            "abc123.rtf",
        ]
    )
    removed = client.delete_for_file("b", "abc123")
    assert len(removed) == 4
    assert sorted(backend.deleted) == sorted(removed)


def test_delete_for_file_respects_file_id_boundary() -> None:
    client, backend = _fake_gcs(
        [
            "abc123.md",
            "abc123456.md",
            "abc123",
        ]
    )
    removed = client.delete_for_file("b", "abc123")
    assert sorted(removed) == ["abc123", "abc123.md"]
    assert "abc123456.md" not in backend.deleted


def test_delete_file_also_clears_the_raw_original(monkeypatch) -> None:
    from services.sync.main import DeleteBody, delete_file

    class FakeSettings:
        gcs_source_bucket = "norm"
        gcs_hwp_original_bucket = "raw-b"
        audience_split_enabled = False
        rag_corpus_name_student = ""

    client, backend = _fake_gcs(
        {"norm": ["abc123.md"], "raw-b": ["abc123.hwp"]}
    )
    monkeypatch.setattr(sync_main, "get_settings", lambda: FakeSettings())
    monkeypatch.setattr(sync_main, "GcsClient", lambda *a, **k: client)
    monkeypatch.setattr(
        sync_main, "RagEngineClient", lambda *a, **k: type(
            "_R", (), {"delete_by_file_id": staticmethod(lambda _f: True)}
        )()
    )
    monkeypatch.setattr(
        sync_main, "DocStateStore", lambda *a, **k: type(
            "_S", (), {"mark_deleted": staticmethod(lambda _f: None)}
        )()
    )

    res = delete_file(DeleteBody(fileId="abc123"))
    assert res["gcsDeleted"] == 2
    assert "abc123.hwp" in backend.deleted


def test_delete_preserves_vertex_quota_as_http_429(monkeypatch) -> None:
    import pytest

    class Store:
        pass

    class ThrottledRag:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def delete_by_file_id(self, _file_id: str) -> bool:
            raise rag_engine.RagImportThrottledError("quota")

    settings = type(
        "SettingsStub",
        (),
        {"departments": (), "audience_split_enabled": False},
    )()
    monkeypatch.setattr(sync_main, "DocStateStore", Store)
    monkeypatch.setattr(sync_main, "get_settings", lambda: settings)
    monkeypatch.setattr(sync_main, "GcsClient", lambda _settings: object())
    monkeypatch.setattr(sync_main, "RagEngineClient", ThrottledRag)

    with pytest.raises(sync_main.HTTPException) as caught:
        sync_main.delete_file(sync_main.DeleteBody(fileId="abc123"))
    assert caught.value.status_code == 429


def test_ingest_rejects_blank_file_id() -> None:
    import pytest
    from fastapi import HTTPException

    from services.sync.main import IngestBody, ingest

    with pytest.raises(HTTPException) as exc:
        ingest(IngestBody(fileId="", driveId="d", mimeType="application/pdf"))
    assert exc.value.status_code == 400


def test_list_changes_drops_entries_without_file_id(monkeypatch) -> None:
    from shared.drive import DriveClient

    client = object.__new__(DriveClient)

    class _Changes:
        @staticmethod
        def list(**_kwargs):
            class _Req:
                @staticmethod
                def execute(**_k):
                    return {
                        "newStartPageToken": "t2",
                        "changes": [
                            {"fileId": "", "file": {}},
                            {
                                "fileId": "realid1234",
                                "file": {"id": "realid1234", "name": "a.pdf"},
                            },
                        ],
                    }

            return _Req()

    client._service = type("_S", (), {"changes": staticmethod(lambda: _Changes())})()

    changes, token, has_more = client.list_changes("drive1", "t1")
    assert [c.file_id for c in changes] == ["realid1234"]
    assert token == "t2"


def test_workflow_yaml_parses_and_uses_uris_gate() -> None:
    import yaml

    p = ROOT / "workflows" / "daily_sync.yaml"
    txt = p.read_text(encoding="utf-8")
    data = yaml.safe_load(txt)
    assert "main" in data
    assert (
        "drive_failed == 0 and drive_indexed + drive_deferred_uris == drive_uris"
        in txt
    )
    assert "drive_reconciled == true" in txt
    assert "drive_indexed == drive_gcs" not in txt
    assert "uris: ${drive_uris}" in txt
    assert "/sync/index-gcs-async" in txt
    assert "/sync/index-jobs/" in txt
    assert "index_status_resp.body.status == \"DONE\"" in txt
    assert "index_status_resp.body.status == \"FAILED\"" in txt


def test_workflow_runs_explicit_recovery_after_all_drives() -> None:
    import yaml

    data = yaml.safe_load((ROOT / "workflows" / "daily_sync.yaml").read_text(encoding="utf-8"))
    names = [next(iter(step)) for step in data["main"]["steps"]]

    assert names.index("for_each_drive") < names.index("recover_pending_index")
    assert names.index("for_each_drive") < names.index("check_recovery_enabled")
    gate = next(step["check_recovery_enabled"] for step in data["main"]["steps"] if "check_recovery_enabled" in step)
    assert gate["switch"][0]["condition"] == "${run_recovery == false}"
    assert gate["switch"][0]["next"] == "finalize_summary"
    assert names.index("recover_pending_index") < names.index("recover_failed")
    assert names.index("recover_failed") < names.index("finalize_summary")
    assert names.index("finalize_summary") < names.index("return_summary")


def test_workflow_recovery_calls_both_endpoints() -> None:
    txt = (ROOT / "workflows" / "daily_sync.yaml").read_text(encoding="utf-8")
    assert "/sync/reindex-pending" in txt
    assert "/sync/retry-failed" in txt


def test_workflow_flattens_delta_index_payload() -> None:
    txt = (ROOT / "workflows" / "daily_sync.yaml").read_text(encoding="utf-8")

    assert "list.concat(pending_uris, pending_uri)" in txt
    assert "list.concat(pending_file_ids, change.fileId)" in txt
    assert "list.concat(pending_uris, ingest_resp.body.gcsUris)" not in txt
    assert "list.concat(pending_file_ids, [change.fileId])" not in txt


def test_throttle_retry_unwraps_runtime_error_args(monkeypatch) -> None:
    from google.api_core import exceptions as gcp_exceptions

    calls: list[int] = []

    def flaky():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("wrapped", gcp_exceptions.ResourceExhausted("quota"))
        return "ok"

    monkeypatch.setattr(rag_engine.time, "sleep", lambda *_: None)
    assert rag_engine._with_throttle_retry(flaky, what="list_files") == "ok"
    assert len(calls) == 2


def test_list_files_retries_current_page_without_restarting(monkeypatch) -> None:
    from google.api_core import exceptions as gcp_exceptions

    calls: list[str | None] = []

    class Page:
        def __init__(self, files, next_token: str) -> None:
            self.rag_files = files
            self.next_page_token = next_token

    class Pager:
        def __init__(self, page: Page) -> None:
            self.pages = iter([page])

    def lazy_list_files(*_args, page_size=None, page_token=None, **_kwargs):
        assert page_size == 100
        calls.append(page_token)
        if page_token is None:
            return Pager(Page(["first-page"], "page-2"))
        if calls.count("page-2") == 1:
            class FailedPager:
                @property
                def pages(self):
                    def failed_page():
                        raise gcp_exceptions.ResourceExhausted("quota")
                        yield None

                    return failed_page()

            return FailedPager()
        return Pager(Page(["second-page"], ""))

    monkeypatch.setattr(rag_engine.rag, "list_files", lazy_list_files)
    monkeypatch.setattr(rag_engine.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(rag_engine.random, "uniform", lambda _a, _b: 0)
    client = object.__new__(rag_engine.RagEngineClient)
    client.corpus_name = "corpus/test"

    assert client.list_files() == ["first-page", "second-page"]
    assert calls == [None, "page-2", "page-2"]


def test_import_retries_on_corpus_busy(monkeypatch) -> None:
    from google.api_core import exceptions as gcp_exceptions

    calls: list[int] = []

    def _flaky(corpus, uris, transformation_config=None):  # noqa: ANN001
        calls.append(1)
        if len(calls) < 3:
            raise RuntimeError(
                "Failed in importing the RagFiles due to: ",
            ) from gcp_exceptions.FailedPrecondition("other operations running")
        return type("R", (), {"imported_rag_files_count": len(uris)})()

    monkeypatch.setattr(rag_engine.rag, "import_files", _flaky)
    monkeypatch.setattr(rag_engine.time, "sleep", lambda *_: None)

    client = RagEngineClient.__new__(RagEngineClient)
    client.corpus_name = "corpora/x"  # type: ignore[attr-defined]
    out = client._import_batch(["gs://b/a.md"], None, max_retries=5)

    assert out.uris == ["gs://b/a.md"]
    assert out.ok
    assert len(calls) == 3, "재시도 없이 즉시 실패하면 안 된다"


def _fake_response(imported: int, failed: int = 0, skipped: int = 0):
    return type(
        "R",
        (),
        {
            "imported_rag_files_count": imported,
            "failed_rag_files_count": failed,
            "skipped_rag_files_count": skipped,
        },
    )()


def _client() -> RagEngineClient:
    client = RagEngineClient.__new__(RagEngineClient)
    client.corpus_name = "corpora/x"  # type: ignore[attr-defined]
    return client


def test_import_reports_partial_failure(monkeypatch) -> None:
    uris = [f"gs://b/f{i}.md" for i in range(3)]
    monkeypatch.setattr(
        rag_engine.rag, "import_files",
        lambda *a, **k: _fake_response(imported=2, failed=1),
    )
    out = _client()._import_batch(uris, None, max_retries=1)

    assert out.imported == 2
    assert out.failed == 1
    assert not out.ok, "거부된 파일이 있는데 성공으로 보고하면 안 된다"


def test_import_counts_skipped_as_ok(monkeypatch) -> None:
    monkeypatch.setattr(
        rag_engine.rag, "import_files",
        lambda *a, **k: _fake_response(imported=0, skipped=1),
    )
    assert _client()._import_batch(["gs://b/a.md"], None, max_retries=1).ok


def test_import_failure_still_not_ok_despite_skipped(monkeypatch) -> None:
    monkeypatch.setattr(
        rag_engine.rag, "import_files",
        lambda *a, **k: _fake_response(imported=1, failed=1, skipped=1),
    )
    out = _client()._import_batch(
        ["gs://b/a.md", "gs://b/b.md", "gs://b/c.md"], None, max_retries=1
    )
    assert not out.ok


def test_import_outcome_survives_missing_counts(monkeypatch) -> None:
    monkeypatch.setattr(
        rag_engine.rag, "import_files", lambda *a, **k: type("R", (), {})()
    )
    out = _client()._import_batch(["gs://b/a.md"], None, max_retries=1)
    assert out.imported == 1
    assert out.ok


def test_import_aggregates_across_subbatches(monkeypatch) -> None:
    seen: list[int] = []

    def _import(corpus, uris, transformation_config=None):
        seen.append(len(uris))
        return _fake_response(imported=len(uris) - (1 if len(seen) == 2 else 0),
                              failed=1 if len(seen) == 2 else 0)

    monkeypatch.setattr(rag_engine.rag, "import_files", _import)
    monkeypatch.setattr(rag_engine.time, "sleep", lambda *_: None)

    client = _client()
    client.settings = Settings(gcp_project_id="p", rag_corpus_name="corpora/x")
    out = client.import_from_gcs([f"gs://b/f{i}.md" for i in range(30)])

    assert seen == [25, 5], "25개 상한으로 쪼개져야 한다"
    assert out.failed == 1
    assert not out.ok
    assert len(out.uris) == 30


def test_index_gcs_keeps_parsed_on_partial_failure(monkeypatch) -> None:
    marked: list[str] = []

    class _Rag:
        def __init__(self, settings=None, *, corpus_name=None) -> None:
            pass

        def delete_files_by_ids(self, ids):
            return 0

        def import_from_gcs(self, uris):
            return rag_engine.ImportOutcome(
                uris=list(uris), imported=len(uris) - 1, failed=1, skipped=0
            )

    class _Store:
        def get(self, _fid):
            return None

        def mark_indexed(self, fid):
            marked.append(fid)

    monkeypatch.setattr(sync_main, "RagEngineClient", _Rag)
    monkeypatch.setattr(sync_main, "DocStateStore", lambda *a, **k: _Store())
    monkeypatch.setattr(
        sync_main, "get_settings",
        lambda: Settings(gcp_project_id="p", rag_corpus_name="corpora/x", **NO_LEASES_OR_MAPPINGS),
    )

    res = sync_main.index_gcs(
        sync_main.IndexGcsBody(
            gcsUris=["gs://b/aaaaaaaaaaaa.md", "gs://b/bbbbbbbbbbbb.md"],
            fileIds=["aaaaaaaaaaaa", "bbbbbbbbbbbb"],
        )
    )

    assert marked == [], "부분 실패인데 INDEXED 로 찍으면 안 된다"
    assert res["ok"] is False
    assert res["status"] == "PARTIAL"
    assert res["count"] == 1, "count 는 보낸 수가 아니라 실제 색인 수여야 한다"


def test_index_gcs_marks_indexed_on_full_success(monkeypatch) -> None:
    marked: list[str] = []

    class _Rag:
        def __init__(self, settings=None, *, corpus_name=None) -> None:
            pass

        def delete_files_by_ids(self, ids):
            return 0

        def import_from_gcs(self, uris):
            return rag_engine.ImportOutcome(
                uris=list(uris), imported=len(uris), failed=0, skipped=0
            )

    class _Store:
        def get(self, _fid):
            return None

        def mark_indexed(self, fid):
            marked.append(fid)

    monkeypatch.setattr(sync_main, "RagEngineClient", _Rag)
    monkeypatch.setattr(sync_main, "DocStateStore", lambda *a, **k: _Store())
    monkeypatch.setattr(
        sync_main, "get_settings",
        lambda: Settings(gcp_project_id="p", rag_corpus_name="corpora/x", **NO_LEASES_OR_MAPPINGS),
    )

    res = sync_main.index_gcs(
        sync_main.IndexGcsBody(
            gcsUris=["gs://b/aaaaaaaaaaaa.md"], fileIds=["aaaaaaaaaaaa"]
        )
    )

    assert marked == ["aaaaaaaaaaaa"]
    assert res["ok"] is True
    assert res["status"] == "INDEXED"
