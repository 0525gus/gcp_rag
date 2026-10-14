from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import MagicMock
import pytest
from fastapi import HTTPException
from fake_firestore import MemoryDb
from services.sync import metadata_refresh as m
from shared import firestore_state
from shared.config import Settings
from shared.models import DriveChange, DocState, DocStatus
from shared.path_context import build_path_context


def base():
    return Settings(gcp_project_id="p", cloud_tasks_enabled=True, sync_folder_ids="scope")


def store():
    result = MagicMock()
    result.settings = base()
    result._db = MemoryDb()
    return result


def test_metadata_change_schedules_only_its_folder_with_stable_task_identity(monkeypatch):
    s = store()
    drive = MagicMock()
    drive.is_in_sync_scope.return_value = True
    enqueues = []
    monkeypatch.setattr(m, "enqueue_page", lambda *args: enqueues.append(args))
    change = DriveChange(
        file_id="meta", drive_id="drive", name="metadata.json", parents=["post"], modified_time="v1"
    )
    first = m.schedule_change(change, "token", s, base(), drive)
    assert first == m.schedule_change(change, "token", s, base(), drive)
    assert len(first) == 1
    job = s._db.collection("sync_jobs").document(first[0]).get().to_dict()
    assert job["folderId"] == "post" and job["status"] == "RUNNING"
    assert drive.is_in_sync_scope.call_args.args[0] == "post"


def test_metadata_move_and_removal_refresh_previous_folder(monkeypatch):
    s = store()
    drive = MagicMock()
    drive.is_in_sync_scope.return_value = True
    monkeypatch.setattr(m, "enqueue_page", lambda *a: None)
    m.schedule_change(
        DriveChange(file_id="meta", drive_id="drive", name="metadata.json", parents=["old"]),
        "v1",
        s,
        base(),
        drive,
    )
    ids = m.schedule_change(
        DriveChange(file_id="meta", drive_id="drive", name="metadata.json", parents=["new"]),
        "v2",
        s,
        base(),
        drive,
    )
    assert {s._db.collection("sync_jobs").document(i).get().to_dict()["folderId"] for i in ids} == {
        "old",
        "new",
    }
    ids = m.schedule_change(
        DriveChange(file_id="meta", drive_id="drive", removed=True), "v3", s, base(), drive
    )
    assert len(ids) == 1
    assert s._db.collection("sync_jobs").document(ids[0]).get().to_dict()["folderId"] == "new"


def test_failed_enqueue_prevents_change_acknowledgement(monkeypatch):
    s = store()
    drive = MagicMock()
    drive.is_in_sync_scope.return_value = True
    monkeypatch.setattr(
        m, "enqueue_page", lambda *a: (_ for _ in ()).throw(RuntimeError("enqueue failed"))
    )
    with pytest.raises(RuntimeError):
        m.schedule_change(
            DriveChange(file_id="meta", drive_id="drive", name="metadata.json", parents=["post"]),
            "t",
            s,
            base(),
            drive,
        )
    assert not m._source_ref(s, "meta").get().exists


def test_metadata_refresh_reuses_cached_ocr_without_downloading_the_image():
    s = store()
    settings = replace(base(), cloud_tasks_enabled=False)
    state = DocState(
        file_id="file",
        drive_id="drive",
        name="poster.png",
        mime_type="image/png",
        modified_time="v1",
        status=DocStatus.INDEXED,
        content_hash="old",
        source_uri="https://drive.google.com/file/d/file/view",
    )
    s.get.return_value = state
    context = replace(
        build_path_context(["post"], "poster.png"),
        source_title="New title",
        website_url="https://example.org/new",
        metadata_file_id="meta",
    )
    drive = MagicMock()
    drive.resolve_path_context.return_value = context
    gcs = MagicMock()
    gcs.download_bytes.return_value = b"# poster.png\n\nOCR body"
    index = MagicMock(return_value={"ok": True, "count": 1})
    m.refresh_file(
        {"id": "file", "driveId": "drive", "modifiedTime": "v1"},
        store=s,
        settings=settings,
        drive=drive,
        gcs=gcs,
        source_uris=lambda *_: ["gs://source/file.md"],
        index=index,
    )
    drive.download_file.assert_not_called()
    assert "New title" in gcs.upload_bytes.call_args.args[0].decode()
    assert "OCR body" in gcs.upload_bytes.call_args.args[0].decode()
    saved = s.upsert.call_args.args[0]
    assert saved.website_url == "https://example.org/new" and saved.source_uri == state.source_uri
    index.assert_called_once()


def test_metadata_refresh_does_not_reprocess_held_images():
    s = store()
    settings = replace(base(), cloud_tasks_enabled=False)
    s.get.return_value = DocState(
        file_id="file",
        drive_id="drive",
        modified_time="v1",
        status=DocStatus.SKIPPED,
        error="OCR_EMPTY_TEXT",
    )
    drive = MagicMock()
    drive.resolve_path_context.return_value = replace(
        build_path_context(["post"], "photo.png"), source_title="Photo"
    )
    gcs = MagicMock()
    index = MagicMock()
    result = m.refresh_file(
        {"id": "file", "driveId": "drive", "modifiedTime": "v1"},
        store=s,
        settings=settings,
        drive=drive,
        gcs=gcs,
        source_uris=MagicMock(),
        index=index,
    )
    assert result["status"] == "SKIPPED"
    index.assert_not_called()
    gcs.download_bytes.assert_not_called()
    drive.download_file.assert_not_called()


def test_task_pages_are_bounded_and_done_jobs_are_idempotent(monkeypatch):
    s = store()
    s.try_acquire_lock.return_value = True
    ref = s._db.collection("sync_jobs").document("job")
    ref.set(
        {
            "kind": "METADATA_REFRESH",
            "driveId": "drive",
            "folderId": "post",
            "status": "RUNNING",
            "pageToken": "",
        }
    )
    drive = MagicMock()
    drive.is_in_sync_scope.return_value = True
    drive.list_folder_page.side_effect = [
        {"files": [{"id": "a", "driveId": "drive"}], "nextPageToken": "next"},
        {"files": [{"id": "b", "driveId": "drive"}]},
    ]
    monkeypatch.setattr(firestore_state, "DocStateStore", lambda *a: s)
    monkeypatch.setattr(m, "DriveClient", lambda: drive)
    monkeypatch.setattr(m, "GcsClient", lambda *a: MagicMock())
    refresh = MagicMock(return_value={"status": "INDEXED"})
    monkeypatch.setattr(m, "refresh_file", refresh)
    enqueue = MagicMock()
    monkeypatch.setattr(m, "enqueue_page", enqueue)
    args = dict(
        base=base(),
        settings_for_drive=lambda *a: base(),
        source_uris=MagicMock(),
        index=MagicMock(),
    )
    assert m.run_page("job", **args)["status"] == "RUNNING"
    enqueue.assert_called_once_with(base(), "job", "next")
    assert m.run_page("job", **args)["status"] == "DONE"
    assert m.run_page("job", **args)["status"] == "DONE"
    assert refresh.call_count == 2
    assert drive.list_folder_page.call_args.args == ("post", "next")


def test_change_feed_enqueues_metadata_job_without_indexing_the_sidecar(monkeypatch):
    from services.sync import main as sync

    s = store()
    s.get_start_page_token.return_value = "before"
    drive = MagicMock()
    drive.is_in_sync_scope.return_value = True
    drive.list_changes.return_value = (
        [
            DriveChange(
                file_id="meta",
                drive_id="drive",
                name="metadata.json",
                parents=["post"],
                modified_time="v2",
            )
        ],
        "after",
        False,
    )
    monkeypatch.setattr(sync, "get_settings", lambda: s.settings)
    monkeypatch.setattr(sync, "DocStateStore", lambda: s)
    monkeypatch.setattr(sync, "DriveClient", lambda: drive)
    enqueue = MagicMock()
    monkeypatch.setattr(m, "enqueue_page", enqueue)
    result = sync.list_changes(sync.ChangesBody(driveId="drive"))
    assert result["changes"] == [] and result["pendingPageToken"] == "after"
    assert len(result["metadataJobs"]) == 1
    enqueue.assert_called_once()


def test_continuation_enqueue_failure_keeps_page_cursor_for_retry(monkeypatch):
    s = store()
    s.try_acquire_lock.return_value = True
    ref = s._db.collection("sync_jobs").document("job")
    ref.set(
        {
            "kind": "METADATA_REFRESH",
            "driveId": "drive",
            "folderId": "post",
            "status": "RUNNING",
            "pageToken": "before",
        }
    )
    drive = MagicMock()
    drive.is_in_sync_scope.return_value = True
    drive.list_folder_page.return_value = {"files": [], "nextPageToken": "after"}
    monkeypatch.setattr(firestore_state, "DocStateStore", lambda *a: s)
    monkeypatch.setattr(m, "DriveClient", lambda: drive)
    monkeypatch.setattr(m, "GcsClient", lambda *a: MagicMock())
    monkeypatch.setattr(
        m, "enqueue_page", lambda *a: (_ for _ in ()).throw(RuntimeError("queue unavailable"))
    )
    with pytest.raises(HTTPException):
        m.run_page(
            "job",
            base=base(),
            settings_for_drive=lambda *a: base(),
            source_uris=MagicMock(),
            index=MagicMock(),
        )
    job = ref.get().to_dict()
    assert job["pageToken"] == "before" and job["status"] == "FAILED"


@pytest.mark.parametrize(
    "error,status",
    [(ValueError("invalid JSON"), "INVALID_METADATA"), (RuntimeError("Drive timeout"), "RETRY")],
)
def test_metadata_read_failure_retains_valid_state_without_leaving_a_failed_document_lease(
    error, status
):
    s = store()
    settings = replace(base(), cloud_tasks_enabled=False)
    s.get.return_value = DocState(
        file_id="file",
        drive_id="drive",
        modified_time="v1",
        status=DocStatus.INDEXED,
        source_title="Valid title",
    )
    drive = MagicMock()
    drive.resolve_path_context.side_effect = error
    gcs = MagicMock()
    index = MagicMock()
    result = m.refresh_file(
        {"id": "file", "driveId": "drive", "modifiedTime": "v1"},
        store=s,
        settings=settings,
        drive=drive,
        gcs=gcs,
        source_uris=MagicMock(),
        index=index,
    )
    assert result["status"] == status
    s.upsert.assert_not_called()
    index.assert_not_called()
    gcs.upload_bytes.assert_not_called()
