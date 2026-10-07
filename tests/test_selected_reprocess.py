"""Selected-file retries must preserve scope, receipts and mutation ownership."""
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import yaml
from starlette.exceptions import HTTPException
from fastapi.testclient import TestClient

from fake_firestore import MemoryDb
from scripts import dept_gui
from services.sync import main as sync
from shared.firestore_state import DocStateStore
from shared.models import DocState, DocStatus
from test_index_issues import Db


@pytest.fixture
def runtime(monkeypatch):
    settings = SimpleNamespace(cloud_tasks_enabled=True, sync_folder_id_list=["folder"])
    store = object.__new__(DocStateStore)
    store._db = MemoryDb()
    store._col = store._db.collection("docs")
    store._tokens = store._db.collection("tokens")
    store.upsert(DocState(file_id="file", drive_id="drive", status=DocStatus.FAILED))
    receipt = store._db.collection("jobs").document("receipt")
    receipt.parent = MagicMock()
    receipt.parent.where.return_value.limit.return_value.stream.return_value = []
    drive = MagicMock()
    drive.get_file.return_value = {"id": "file", "driveId": "drive", "name": "current.png",
                                  "mimeType": "image/png", "modifiedTime": "2026-10-06T00:00:00Z",
                                  "webViewLink": "https://drive.google.com/file/d/file/view", "size": "42"}
    drive.is_in_sync_scope.return_value = True
    monkeypatch.setattr(sync, "get_settings", lambda: settings)
    monkeypatch.setattr(sync, "_settings_for_drive", lambda base, did: settings)
    monkeypatch.setattr(sync, "DocStateStore", lambda *args: store)
    monkeypatch.setattr(sync, "_index_job_refs", lambda *args: (store, receipt, None))
    monkeypatch.setattr(sync, "DriveClient", lambda: drive)
    monkeypatch.setattr(sync, "GcsClient", lambda *args: object())
    ingest = MagicMock(return_value={"status": "GCS_READY", "gcsUris": ["gs://source/file.md"]})
    enqueue = MagicMock(return_value={"status": "RUNNING", "jobId": "job"})
    monkeypatch.setattr(sync, "_ingest_with", ingest)
    monkeypatch.setattr(sync, "index_gcs_async", enqueue)
    body = sync.ReprocessFileBody(fileId="file", driveId="drive", requestId="a" * 32, parserUrl="https://parser")
    return SimpleNamespace(store=store, receipt=receipt, drive=drive, ingest=ingest,
                           enqueue=enqueue, body=body, settings=settings)


def test_reprocess_uses_current_metadata_and_returns_same_receipt_without_repeating_ocr(runtime):
    first = sync.reprocess_file(runtime.body)
    second = sync.reprocess_file(runtime.body)
    assert first == second and first["status"] == "RUNNING"
    runtime.ingest.assert_called_once()
    runtime.enqueue.assert_called_once()
    ingested = runtime.ingest.call_args.args[0]
    assert ingested.name == "current.png" and ingested.size_bytes == 42
    assert ingested.refresh_content and ingested.route is None
    assert ingested.modified_time == "2026-10-06T00:00:00Z"
    assert ingested.web_view_link.startswith("https://drive.google.com/")
    indexed = runtime.enqueue.call_args.args[0]
    assert indexed.file_ids == ["file"] and indexed.drive_id == "drive"


@pytest.mark.parametrize("case", ["foreign_state", "foreign_drive", "trashed", "outside_folder", "indexed", "excluded", "busy", "active_job", "uncertain_receipt"])
def test_unsafe_targets_never_ingest_or_enqueue(runtime, case):
    if case == "foreign_state":
        runtime.store._col.document("file").set({"driveId": "other"}, merge=True)
    elif case == "foreign_drive":
        runtime.drive.get_file.return_value["driveId"] = "other"
    elif case == "trashed":
        runtime.drive.get_file.return_value["trashed"] = True
    elif case == "outside_folder":
        runtime.drive.is_in_sync_scope.return_value = False
    elif case in {"indexed", "excluded"}:
        runtime.store._col.document("file").set({"status": case.upper()}, merge=True)
    elif case == "busy":
        runtime.store._tokens.document("__mutation__file").set({"owner": "other", "phase": "MUTATING"})
    elif case == "active_job":
        runtime.receipt.parent.where.return_value.limit.return_value.stream.return_value = [
            SimpleNamespace(to_dict=lambda: {"kind": "INDEX_GCS", "status": "RUNNING",
                                             "deadlineAt": datetime.now(UTC) + timedelta(minutes=5)})]
    else:
        runtime.receipt.set({"fileIds": ["file"], "driveId": "drive", "status": "STARTED"})
    with pytest.raises(HTTPException):
        sync.reprocess_file(runtime.body)
    runtime.ingest.assert_not_called()
    runtime.enqueue.assert_not_called()


@pytest.mark.parametrize("status", ["SKIPPED", "DLQ", "SPLIT_QUEUED"])
def test_parked_outcomes_are_not_reported_as_indexed(runtime, status):
    runtime.ingest.return_value = {"status": status}
    result = sync.reprocess_file(runtime.body)
    assert result["status"] == status and result["jobId"] == ""
    runtime.enqueue.assert_not_called()


def test_interrupted_ingestion_is_not_automatically_reissued(runtime):
    runtime.ingest.side_effect = TimeoutError("uncertain")
    with pytest.raises(TimeoutError):
        sync.reprocess_file(runtime.body)
    with pytest.raises(HTTPException, match="busy"):
        sync.reprocess_file(runtime.body)
    runtime.ingest.assert_called_once()


@pytest.fixture
def gui(monkeypatch):
    db = Db({"docs/file": {"driveId": "drive", "status": "FAILED"}})
    monkeypatch.setattr(dept_gui, "_sync_department_targets", lambda: ({"cs": {"driveIds": ["drive"], "name": "컴공"}}, {"drive": "cs"}))
    monkeypatch.setattr(dept_gui, "_common", lambda: {"GCP_PROJECT_ID": "project", "GCP_REGION": "region"})
    monkeypatch.setattr(dept_gui, "_firestore_client", lambda *args: db)
    monkeypatch.setattr(dept_gui, "_firestore_collection_names", lambda: {"docState": "docs"})
    monkeypatch.setattr(dept_gui, "_gcloud_json", lambda *args, **kwargs: (True, {"sourceContents": "reprocess_document:"}))
    monkeypatch.setattr(dept_gui, "_list_sync_execution_rows", lambda *args, **kwargs: [])
    monkeypatch.setattr(dept_gui, "_sync_access_token", lambda: "test-token")
    monkeypatch.setattr(dept_gui, "_drive_service_account_status", lambda *args: {"status": "OK"})
    monkeypatch.setattr(dept_gui, "_cloud_run_sync_urls", lambda *args: ("https://sync", "https://parser"))
    post = MagicMock(return_value=(201, {"state": "ACTIVE", "name": "executions/id"}, 0))
    monkeypatch.setattr(dept_gui, "_http_post_json", post)
    return db, post


def test_gui_starts_only_one_selected_document_and_authenticates_mutation(gui):
    db, post = gui
    client = TestClient(dept_gui.app)
    url = "/api/v1/departments/cs/index-issues/file/reprocess"
    assert client.post(url, json={"requestId": "a" * 32}).status_code == 403
    assert not post.called
    response = client.post(url, json={"requestId": "a" * 32}, headers={"x-local-session": dept_gui._SESSION_NONCE})
    assert response.status_code == 202
    args = json.loads(post.call_args.args[1]["argument"])
    assert args["reprocessFile"] == {"fileId": "file", "driveId": "drive"}
    assert args["backfill"] is False and args["runId"] == "a" * 32
    assert response.json()["run"]["mode"] == "reprocess"


def test_gui_rejects_foreign_document_and_old_workflow(gui, monkeypatch):
    db, post = gui
    db.rows["docs/file"]["driveId"] = "foreign"
    with pytest.raises(FileNotFoundError):
        dept_gui._start_manual_sync("cs", "reprocess", file_id="file", request_id="a" * 32)
    db.rows["docs/file"]["driveId"] = "drive"
    monkeypatch.setattr(dept_gui, "_gcloud_json", lambda *args, **kwargs: (True, {"sourceContents": "old workflow"}))
    with pytest.raises(ValueError, match="배포"):
        dept_gui._start_manual_sync("cs", "reprocess", file_id="file", request_id="a" * 32)
    post.assert_not_called()


def test_gui_rejects_overlapping_sync(gui, monkeypatch):
    monkeypatch.setattr(dept_gui, "_list_sync_execution_rows", lambda *args, **kwargs: [
        {"state": "ACTIVE", "argument": json.dumps({"driveIds": ["drive"]})}])
    with pytest.raises(FileExistsError):
        dept_gui._start_manual_sync("cs", "reprocess", file_id="file", request_id="a" * 32)
    gui[1].assert_not_called()


def test_reprocess_history_only_returns_the_selected_departments_document(gui, monkeypatch):
    monkeypatch.setattr(dept_gui, "_index_issue_context", lambda code: (
        {"name": "컴공"}, ["drive"], {"GCP_PROJECT_ID": "project"}, {"docState": "docs"}))
    def execution(drive, file_id, state):
        return {"state": state, "name": "executions/id", "labels": {"department": "cs", "mode": "reprocess"},
                "argument": json.dumps({"reprocessFile": {"driveId": drive, "fileId": file_id}})}
    monkeypatch.setattr(dept_gui, "_http_json", lambda *args, **kwargs: (200, {
        "executions": [execution("drive", "file", "ACTIVE"), execution("foreign", "file", "FAILED"),
                       execution("drive", "other", "SUCCEEDED")], "nextPageToken": "next"}, 0))
    response = dept_gui.index_issue_reprocess_history("cs", "file")
    body = json.loads(response.body)
    assert len(body["runs"]) == 1 and body["runs"][0]["state"] == "ACTIVE"
    assert body["limited"] is True
    gui[0].rows["docs/file"]["driveId"] = "foreign"
    with pytest.raises(HTTPException) as exc:
        dept_gui.index_issue_reprocess_history("cs", "file")
    assert exc.value.status_code == 404


def test_workflow_selected_mode_returns_before_changes_and_never_commits_token():
    workflow = yaml.safe_load((Path(__file__).resolve().parents[1] / "workflows/daily_sync.yaml").read_text(encoding="utf-8"))
    steps = {key: value for step in workflow["main"]["steps"] for key, value in step.items()}
    assert list(steps).index("selected_file_mode") < list(steps).index("for_each_drive")
    branch = steps["selected_file_mode"]["switch"][0]["steps"]
    assert branch[-1]["return_selected"] == {"return": "${selected_result}"}
    selected = workflow["reprocess_document"]["steps"]
    assert "retry" not in selected[0]["ingest_selected"]
    text = yaml.safe_dump(selected)
    assert "/sync/changes" not in text and "commit-token" not in text
    assert "selected_incomplete" in text and "raise:" in text and '"DONE"' in text
