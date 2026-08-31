from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

import services.sync.main as sync_main
from shared import rag_engine
from shared.rag_engine import ImportOutcome
from shared.models import DocState, DocStatus
from shared.rag_import_result import parse_import_results
from shared.rag_mapping import RagFileMapping


DRIVE_FILE_ID = "1eetIrUEnpmrFEn9ui2fQ6Yfr2piiBKrv"


def test_mapping_id_is_stable_and_corpus_scoped() -> None:
    base = {
        "file_id": DRIVE_FILE_ID,
        "corpus_name": "projects/p/locations/r/ragCorpora/c",
        "rag_file_name": "projects/p/locations/r/ragCorpora/c/ragFiles/r1",
        "gcs_uri": f"gs://bucket/{DRIVE_FILE_ID}.md",
        "generation": "sha256:abc",
    }
    faculty = RagFileMapping(corpus_type="FACULTY", **base)
    student = RagFileMapping(corpus_type="STUDENT", **base)
    assert faculty.mapping_id == faculty.mapping_id
    assert faculty.mapping_id != student.mapping_id


def test_mapping_roundtrip_preserves_resource_identity() -> None:
    mapping = RagFileMapping(
        file_id=DRIVE_FILE_ID,
        corpus_type="faculty",
        corpus_name="projects/p/locations/r/ragCorpora/c",
        rag_file_name="projects/p/locations/r/ragCorpora/c/ragFiles/r1",
        gcs_uri=f"gs://bucket/{DRIVE_FILE_ID}.md",
        generation="sha256:abc",
        import_result_sink="gs://metadata/result.ndjson",
    )
    restored = RagFileMapping.from_firestore(mapping.to_firestore())
    assert restored.corpus_type == "FACULTY"
    assert restored.rag_file_name == mapping.rag_file_name
    assert restored.gcs_uri == mapping.gcs_uri
    assert restored.generation == mapping.generation


@pytest.mark.parametrize(
    "row",
    [
        {
            "gcs_uri": f"gs://bucket/{DRIVE_FILE_ID}.md",
            "rag_file_name": "projects/p/locations/r/ragCorpora/c/ragFiles/r1",
            "status": "SUCCEEDED",
        },
        {
            "result": {
                "sourceUri": f"gs://bucket/{DRIVE_FILE_ID}.md",
                "ragFile": {
                    "name": "projects/p/locations/r/ragCorpora/c/ragFiles/r1"
                },
                "state": "ACTIVE",
            }
        },
    ],
)
def test_import_result_parser_accepts_sdk_field_variants(row: dict) -> None:
    result = parse_import_results(json.dumps(row))[0]
    assert result.gcs_uri == f"gs://bucket/{DRIVE_FILE_ID}.md"
    assert result.rag_file_name.endswith("/ragFiles/r1")


def test_import_result_parser_rejects_non_json_rows() -> None:
    with pytest.raises(json.JSONDecodeError):
        parse_import_results("not-json")


def test_import_result_parser_accepts_real_vertex_sink_shape() -> None:
    row = {
        "OperationId": 5214282437000953856,
        "CreateTimestamp": "2026-08-31T04:11:04.315317",
        "Filename": f"gs://bucket/{DRIVE_FILE_ID}.md",
        "Status": "OK",
        "FileId": 5776712888744724230,
    }
    corpus = "projects/p/locations/r/ragCorpora/c"
    result = parse_import_results(json.dumps(row), corpus_name=corpus)[0]
    assert result.gcs_uri == f"gs://bucket/{DRIVE_FILE_ID}.md"
    assert result.rag_file_name == f"{corpus}/ragFiles/5776712888744724230"
    assert result.status == "OK"


def test_import_result_parser_marks_success_and_sink_uri() -> None:
    sink = "gs://metadata/import-results/c/result.ndjson"
    result = parse_import_results(
        json.dumps(
            {
                "Filename": f"gs://bucket/{DRIVE_FILE_ID}.md",
                "Status": "OK",
                "FileId": 123,
            }
        ),
        corpus_name="projects/p/locations/r/ragCorpora/c",
        sink_uri=sink,
    )[0]

    assert result.succeeded is True
    assert result.import_result_sink == sink


def test_rag_import_uses_and_reads_result_sink(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = SimpleNamespace(
        rag_mapping_write_enabled=True,
        rag_metadata_bucket="metadata-bucket",
        gcp_project_id="p",
    )
    client = object.__new__(rag_engine.RagEngineClient)
    client.settings = settings
    client.corpus_name = "projects/p/locations/r/ragCorpora/c"
    captured: dict[str, str] = {}

    def fake_import(_corpus, _uris, **kwargs):
        captured["sink"] = kwargs["import_result_sink"]
        return SimpleNamespace(
            imported_rag_files_count=1,
            failed_rag_files_count=0,
            skipped_rag_files_count=0,
        )

    class FakeGcs:
        def __init__(self, _settings) -> None:
            pass

        def download_bytes(self, uri: str) -> bytes:
            assert uri == captured["sink"]
            return (
                b'{"Filename":"gs://source/1eetIrUEnpmrFEn9ui2fQ6Yfr2piiBKrv.md","Status":"OK",'
                b'"FileId":456}\n'
            )

    monkeypatch.setattr(rag_engine.rag, "import_files", fake_import)
    monkeypatch.setattr(rag_engine, "GcsClient", FakeGcs)

    outcome = client._import_batch(
        [f"gs://source/{DRIVE_FILE_ID}.md"], object(), 1
    )

    assert captured["sink"].startswith(
        "gs://metadata-bucket/import-results/c/"
    )
    assert captured["sink"].endswith(".ndjson")
    assert outcome.results[0].rag_file_name.endswith("/ragFiles/456")
    assert outcome.result_sinks == (captured["sink"],)


def test_mapping_write_replaces_only_complete_sink_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str, list[RagFileMapping]]] = []

    class FakeMappingStore:
        def __init__(self, _settings) -> None:
            pass

        def replace_for_corpus(self, file_id, corpus_type, mappings) -> None:
            calls.append((file_id, corpus_type, list(mappings)))

    class FakeStateStore:
        def get(self, file_id: str) -> DocState:
            return DocState(
                file_id=file_id,
                drive_id="drive",
                status=DocStatus.PARSED,
                content_hash="sha256:generation",
            )

    monkeypatch.setattr(sync_main, "RagFileMappingStore", FakeMappingStore)
    settings = SimpleNamespace(rag_mapping_write_enabled=True)
    parsed = parse_import_results(
        '{"Filename":"gs://source/1eetIrUEnpmrFEn9ui2fQ6Yfr2piiBKrv.md",'
        '"Status":"OK","FileId":456}',
        corpus_name="projects/p/locations/r/ragCorpora/c",
        sink_uri="gs://metadata/result.ndjson",
    )
    outcome = ImportOutcome(
        [f"gs://source/{DRIVE_FILE_ID}.md"],
        imported=1,
        failed=0,
        skipped=0,
        results=tuple(parsed),
    )

    result = sync_main._write_rag_mappings(
        state_store=FakeStateStore(),
        settings=settings,
        corpus_type="FACULTY",
        corpus_name="projects/p/locations/r/ragCorpora/c",
        gcs_uris=[f"gs://source/{DRIVE_FILE_ID}.md"],
        file_ids=[DRIVE_FILE_ID],
        outcome=outcome,
    )

    assert result == {"enabled": True, "written": 1, "deferred": 0, "failed": 0}
    assert calls[0][0:2] == (DRIVE_FILE_ID, "FACULTY")
    assert calls[0][2][0].generation == "sha256:generation"
    assert calls[0][2][0].import_result_sink == "gs://metadata/result.ndjson"


def test_missing_sink_row_defers_without_deleting_old_mapping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailIfCalled:
        def __init__(self, _settings) -> None:
            pass

        def replace_for_corpus(self, *_args, **_kwargs) -> None:
            raise AssertionError("incomplete sink must preserve the old mapping")

    monkeypatch.setattr(sync_main, "RagFileMappingStore", FailIfCalled)
    result = sync_main._write_rag_mappings(
        state_store=SimpleNamespace(get=lambda _fid: None),
        settings=SimpleNamespace(rag_mapping_write_enabled=True),
        corpus_type="FACULTY",
        corpus_name="corpus",
        gcs_uris=[f"gs://source/{DRIVE_FILE_ID}.md"],
        file_ids=[DRIVE_FILE_ID],
        outcome=ImportOutcome(
            [f"gs://source/{DRIVE_FILE_ID}.md"], 1, 0, 0, results=()
        ),
    )

    assert result["deferred"] == 1
    assert result["written"] == 0


def test_backfill_mapping_accepts_rag_file_without_source_uri() -> None:
    mapping = sync_main._mapping_from_existing_rag_file(
        SimpleNamespace(
            name="projects/p/locations/r/ragCorpora/c/ragFiles/123",
            display_name=f"{DRIVE_FILE_ID}.md",
        ),
        corpus_type="FACULTY",
        corpus_name="projects/p/locations/r/ragCorpora/c",
    )

    assert mapping is not None
    assert mapping.file_id == DRIVE_FILE_ID
    assert mapping.gcs_uri == ""
    assert mapping.mapping_id.startswith("faculty__")


def test_backfill_mapping_rejects_unidentifiable_rag_file() -> None:
    mapping = sync_main._mapping_from_existing_rag_file(
        SimpleNamespace(
            name="projects/p/locations/r/ragCorpora/c/ragFiles/123",
            display_name="bad.md",
        ),
        corpus_type="FACULTY",
        corpus_name="projects/p/locations/r/ragCorpora/c",
    )
    assert mapping is None


def test_mapping_store_bulk_upsert_batches(monkeypatch: pytest.MonkeyPatch) -> None:
    committed: list[int] = []

    class FakeBatch:
        def __init__(self) -> None:
            self.count = 0

        def set(self, *_args, **_kwargs) -> None:
            self.count += 1

        def commit(self) -> None:
            committed.append(self.count)

    class FakeDb:
        def batch(self) -> FakeBatch:
            return FakeBatch()

    store = object.__new__(sync_main.RagFileMappingStore)
    store._db = FakeDb()
    store._mappings = lambda _fid: SimpleNamespace(  # type: ignore[method-assign]
        document=lambda mapping_id: mapping_id
    )
    mappings = [
        RagFileMapping(
            file_id=f"{DRIVE_FILE_ID}{i}",
            corpus_type="FACULTY",
            corpus_name="corpus",
            rag_file_name=f"corpus/ragFiles/{i}",
            gcs_uri=f"gs://source/{DRIVE_FILE_ID}{i}.md",
            generation="",
        )
        for i in range(5)
    ]

    assert store.upsert_many(mappings, batch_size=2) == 5
    assert committed == [2, 2, 1]


def test_backfill_endpoint_dry_run_then_writes(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = SimpleNamespace(
        rag_mapping_write_enabled=True,
        rag_corpus_name="faculty-corpus",
        rag_corpus_name_student="student-corpus",
        audience_split_enabled=True,
        departments=(),
    )
    listed = {
        "faculty-corpus": [
            SimpleNamespace(
                name="faculty-corpus/ragFiles/1",
                display_name=f"{DRIVE_FILE_ID}.md",
                source_uri=f"gs://source/{DRIVE_FILE_ID}.md",
            )
        ],
        "student-corpus": [],
    }
    writes: list[list[RagFileMapping]] = []

    class FakeRag:
        def __init__(self, _settings, *, corpus_name: str) -> None:
            self.corpus_name = corpus_name

        def list_files(self):
            return listed[self.corpus_name]

    class FakeMappingStore:
        def __init__(self, _settings) -> None:
            pass

        def upsert_many(self, mappings) -> int:
            rows = list(mappings)
            writes.append(rows)
            return len(rows)

    monkeypatch.setattr(sync_main, "get_settings", lambda: settings)
    monkeypatch.setattr(sync_main, "RagEngineClient", FakeRag)
    monkeypatch.setattr(sync_main, "RagFileMappingStore", FakeMappingStore)

    dry = sync_main.backfill_rag_mappings(
        sync_main.BackfillRagMappingsBody(driveId="drive", dryRun=True)
    )
    assert dry["totals"] == {
        "listed": 1,
        "mappable": 1,
        "skipped": 0,
        "written": 0,
    }
    assert writes == []

    applied = sync_main.backfill_rag_mappings(
        sync_main.BackfillRagMappingsBody(driveId="drive", dryRun=False)
    )
    assert applied["totals"]["written"] == 1
    assert len(writes) == 2  # 교직원 1회 + 비어 있는 학생 코퍼스 1회
    assert writes[0][0].corpus_type == "FACULTY"


def test_delete_uses_mapping_without_corpus_scan(monkeypatch: pytest.MonkeyPatch) -> None:
    resource_by_project = (
        "projects/p/locations/r/ragCorpora/c/ragFiles/vertex-file-1"
    )
    resource_by_number = (
        "projects/123/locations/r/ragCorpora/c/ragFiles/vertex-file-1"
    )
    rows = [
        RagFileMapping(
            file_id=DRIVE_FILE_ID,
            corpus_type="FACULTY",
            corpus_name="projects/p/locations/r/ragCorpora/c",
            rag_file_name=resource_by_project,
            gcs_uri=f"gs://source/{DRIVE_FILE_ID}.md",
            generation="hash",
        ),
        RagFileMapping(
            file_id=DRIVE_FILE_ID,
            corpus_type="FACULTY",
            corpus_name="projects/123/locations/r/ragCorpora/c",
            rag_file_name=resource_by_number,
            gcs_uri="",
            generation="",
            status="BACKFILLED",
        ),
    ]
    deleted_api: list[str] = []
    deleted_mappings: list[str] = []

    class FakeMappingStore:
        def __init__(self, _settings) -> None:
            pass

        def list_for_file(self, file_id: str):
            assert file_id == DRIVE_FILE_ID
            return rows

        def delete(self, mapping: RagFileMapping) -> None:
            deleted_mappings.append(mapping.mapping_id)

    monkeypatch.setattr(rag_engine, "RagFileMappingStore", FakeMappingStore)
    monkeypatch.setattr(
        rag_engine.rag,
        "list_files",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("mapping hit must not scan the corpus")
        ),
    )
    monkeypatch.setattr(
        rag_engine.rag,
        "delete_file",
        lambda *, name: deleted_api.append(name),
    )

    client = object.__new__(rag_engine.RagEngineClient)
    client.settings = SimpleNamespace(
        rag_mapping_read_enabled=True,
        rag_mapping_fallback_scan_enabled=True,
        rag_delete_concurrency=1,
        rag_delete_pacing_seconds=0,
    )
    client.corpus_name = "projects/p/locations/r/ragCorpora/c"

    assert client.delete_files_by_ids([DRIVE_FILE_ID]) == 1
    assert deleted_api == [resource_by_project]
    assert len(deleted_mappings) == 2


def test_backfill_preserves_vertex_quota_as_http_429(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = SimpleNamespace(
        rag_mapping_write_enabled=True,
        rag_corpus_name="faculty-corpus",
        rag_corpus_name_student="",
        audience_split_enabled=False,
        departments=(),
    )

    class ThrottledRag:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def list_files(self):
            raise rag_engine.RagImportThrottledError("quota")

    monkeypatch.setattr(sync_main, "get_settings", lambda: settings)
    monkeypatch.setattr(sync_main, "RagEngineClient", ThrottledRag)

    with pytest.raises(sync_main.HTTPException) as caught:
        sync_main.backfill_rag_mappings(
            sync_main.BackfillRagMappingsBody(driveId="drive", dryRun=True)
        )
    assert caught.value.status_code == 429
