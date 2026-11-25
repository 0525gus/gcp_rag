from types import SimpleNamespace

import pytest
from googleapiclient.errors import HttpError

from scripts.check_drive_orphans import find_orphans


def http_error(status):
    return HttpError(SimpleNamespace(status=status, reason=""), b"")


def test_live_documents_missing_trashed_or_moved_in_drive_are_reported():
    drive = {
        "kept": {"driveId": "d1"},
        "trashed": {"driveId": "d1", "trashed": True},
        "moved": {"driveId": "d2"},
    }

    def get_file(file_id):
        if file_id == "gone":
            raise http_error(404)
        if file_id == "hidden":
            raise http_error(403)
        return drive[file_id]

    docs = [
        {"fileId": fid, "driveId": "d1", "status": status, "audience": "STUDENT", "path": fid}
        for fid, status in [
            ("kept", "INDEXED"), ("trashed", "INDEXED"), ("moved", "PARSED"),
            ("gone", "INDEXED"), ("hidden", "INDEXED"), ("already", "DELETED"),
        ]
    ]

    found = {o["fileId"]: o["driveState"] for o in find_orphans(docs, get_file, workers=2)}

    assert found == {"trashed": "trashed", "moved": "other-drive", "gone": "missing",
                     "hidden": "no-access"}


def test_unexpected_drive_errors_stop_the_check():
    def get_file(_):
        raise http_error(500)

    with pytest.raises(HttpError):
        find_orphans([{"fileId": "x", "driveId": "d", "status": "INDEXED"}], get_file)
