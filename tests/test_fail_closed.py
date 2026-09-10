from dataclasses import replace
from unittest.mock import Mock
from types import SimpleNamespace
import time

import pytest
from google.api_core.exceptions import NotFound, PermissionDenied, ResourceExhausted

import services.sync.main as sync_main
import shared.rag_engine as rag_engine
from shared.config import Department, Settings, _departments_from_json
from shared.models import Audience, DocState
from shared.rag_engine import ImportOutcome, RagEngineClient


def rag_client(workers=1):
    client = object.__new__(RagEngineClient)
    client.settings = Settings(
        gcp_project_id="test",
        rag_corpus_name="staff",
        rag_corpus_name_student="student",
        student_folder_ids="folder",
        rag_delete_concurrency=workers,
        rag_delete_pacing_seconds=0,
    )
    client.corpus_name = "student"
    client._file_index = Mock(return_value={"file1234567890": ["corpus/ragFiles/old"]})
    client.import_from_gcs = Mock(
        return_value=ImportOutcome(["gs://source/file1234567890.md"], 1, 0, 0)
    )
    return client


@pytest.mark.parametrize("workers", [1, 3])
@pytest.mark.parametrize("error", [PermissionDenied("denied"), RuntimeError("unavailable")])
def test_real_delete_failure_blocks_import_and_indexed(monkeypatch, workers, error):
    client = rag_client(workers)
    monkeypatch.setattr(rag_engine.rag, "delete_file", Mock(side_effect=error))
    store = Mock()
    with pytest.raises(type(error)):
        sync_main._import_and_mark(
            store, ["gs://source/file1234567890.md"], ["file1234567890"], rag=client
        )
    client.import_from_gcs.assert_not_called()
    store.upsert.assert_not_called()
    store.mark_indexed.assert_not_called()


def test_exhausted_quota_is_not_treated_as_absent(monkeypatch):
    client = rag_client()
    monkeypatch.setattr(
        rag_engine, "_with_throttle_retry", Mock(side_effect=ResourceExhausted("quota"))
    )
    with pytest.raises(ResourceExhausted):
        client.delete_files_by_ids(["file1234567890"])


def test_distributed_delete_does_not_trust_another_workers_stale_cache(monkeypatch):
    client = rag_client()
    client.settings = replace(client.settings, cloud_tasks_enabled=True)
    client._file_index = RagEngineClient._file_index.__get__(client)
    client.list_files = Mock(
        return_value=[
            SimpleNamespace(name="corpus/ragFiles/current", display_name="file1234567890.md")
        ]
    )
    monkeypatch.setitem(
        rag_engine._CORPUS_INDEX_CACHE,
        "student",
        (time.monotonic(), {"file1234567890": ["corpus/ragFiles/old"]}),
    )
    deleted = Mock()
    monkeypatch.setattr(rag_engine.rag, "delete_file", deleted)
    assert client.delete_files_by_ids(["file1234567890"]) == 1
    deleted.assert_called_once_with(name="corpus/ragFiles/current")


def test_missing_mapping_without_scan_cannot_claim_success(monkeypatch):
    client = rag_client()
    client.settings = replace(
        client.settings, rag_mapping_read_enabled=True, rag_mapping_fallback_scan_enabled=False
    )
    mapping_store = Mock()
    mapping_store.list_for_file.return_value = []
    monkeypatch.setattr(rag_engine, "RagFileMappingStore", lambda *_a: mapping_store)
    with pytest.raises(RuntimeError, match="deletion unverified"):
        client.delete_files_by_ids(["file1234567890"])


def test_wrapped_notfound_is_safe_but_not_counted_as_a_deletion(monkeypatch):
    client = rag_client()
    wrapped = RuntimeError("SDK wrapper")
    wrapped.__cause__ = NotFound("already gone")
    delete = Mock(side_effect=wrapped)
    monkeypatch.setattr(rag_engine.rag, "delete_file", delete)
    assert client.delete_files_by_ids(["file1234567890"]) == 0
    assert client.delete_files_by_ids(["file1234567890"]) == 0
    assert delete.call_count == 1


def test_staff_move_delete_failure_preserves_mapping_and_reports_failure(monkeypatch):
    client = rag_client()
    monkeypatch.setattr(sync_main, "RagEngineClient", lambda *_a, **_kw: client)
    monkeypatch.setattr(rag_engine.rag, "delete_file", Mock(side_effect=PermissionDenied("denied")))
    mapping = Mock()
    monkeypatch.setattr(sync_main, "_write_rag_mappings", mapping)
    store = Mock()
    store.get.return_value = DocState(
        file_id="file1234567890", drive_id="d", audience=Audience.STAFF
    )
    with pytest.raises(PermissionDenied):
        sync_main._sync_student_corpus(
            ["gs://source/file1234567890.md"], ["file1234567890"], client.settings, store
        )
    mapping.assert_not_called()
    client.import_from_gcs.assert_not_called()


def test_student_removed_uses_actual_delete_count(monkeypatch):
    client = rag_client()
    monkeypatch.setattr(sync_main, "RagEngineClient", lambda *_a, **_kw: client)
    monkeypatch.setattr(rag_engine.rag, "delete_file", Mock(side_effect=NotFound("gone")))
    store = Mock()
    store.get.return_value = DocState(
        file_id="file1234567890", drive_id="d", audience=Audience.STAFF
    )
    result = sync_main._sync_student_corpus(
        ["gs://source/file1234567890.md"], ["file1234567890"], client.settings, store
    )
    assert result["ok"] is True
    assert result["removed"] == 0


DEPT = Department(
    code="one",
    drive_ids=("drive1",),
    staff_corpus="staff1",
    hwp_bucket="raw1",
    source_bucket="source1",
    sync_folder_ids=("scope1",),
)


@pytest.mark.parametrize(
    "changes",
    [
        {"drive_ids": ()},
        {"sync_folder_ids": ()},
        {"staff_corpus": ""},
        {"source_bucket": ""},
        {"hwp_bucket": ""},
        {"student_corpus": "student"},
        {"student_folder_ids": ("student-scope",)},
    ],
)
def test_incomplete_department_is_rejected(changes):
    with pytest.raises(ValueError):
        Settings(gcp_project_id="test", departments=(replace(DEPT, **changes),))


@pytest.mark.parametrize("changes", [{"drive_ids": ("drive1",)}, {"staff_corpus": "staff1"}])
def test_duplicate_department_resources_are_rejected(changes):
    other = replace(DEPT, code="two", drive_ids=("drive2",), staff_corpus="staff2")
    other = replace(other, **changes)
    with pytest.raises(ValueError, match="duplicate"):
        Settings(gcp_project_id="test", departments=(DEPT, other))


@pytest.mark.parametrize("raw", [None, "", " ", "{}", "[]", "{broken"])
def test_multidepartment_service_refuses_missing_or_broken_map(monkeypatch, raw):
    for key in (
        "GCP_PROJECT_ID",
        "GCS_HWP_ORIGINAL_BUCKET",
        "GCS_SOURCE_BUCKET",
        "RAG_CORPUS_NAME",
    ):
        monkeypatch.setenv(key, "test")
    monkeypatch.setenv("DEPARTMENTS_REQUIRED", "true")
    monkeypatch.delenv("DEPARTMENTS_JSON", raising=False)
    if raw is not None:
        monkeypatch.setenv("DEPARTMENTS_JSON", raw)
    with pytest.raises(ValueError):
        Settings.from_env()


def test_non_mapping_department_is_not_silently_dropped():
    with pytest.raises(ValueError):
        _departments_from_json('{"dept":null}')


def test_duplicate_json_keys_are_rejected():
    with pytest.raises(ValueError):
        _departments_from_json('{"dept":{},"dept":{}}')


def test_invalid_settings_fail_startup_and_health(monkeypatch):
    from fastapi.testclient import TestClient

    def invalid():
        raise ValueError("invalid departments")

    monkeypatch.setattr(sync_main, "get_settings", invalid)
    with pytest.raises(ValueError, match="invalid departments"):
        with TestClient(sync_main.app):
            pytest.fail("startup should fail")
    with pytest.raises(ValueError, match="invalid departments"):
        sync_main.health()


def test_empty_student_scope_does_not_inherit_common_scope():
    settings = Settings(
        gcp_project_id="test",
        departments=(DEPT,),
        rag_corpus_name_student="shared-student",
        student_folder_ids="shared-folder",
    )
    resolved = settings.for_drive("drive1")
    assert resolved.rag_corpus_name_student == ""
    assert resolved.student_folder_ids == ""
    assert resolved.audience_split_enabled is False
