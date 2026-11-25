from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.cleanup_orphans import _LIVE_STATUSES, classify
from services.sync.main import ReconcileBody, _route_file_meta, reconcile
from shared.mime_types import RouteKind
from shared.models import DocState, DocStatus


class _Drive:
    def __init__(self, in_scope: bool) -> None:
        self._in_scope = in_scope

    def is_in_sync_scope(self, file_id, folder_ids, **kw):
        return self._in_scope


def test_out_of_scope_file_routes_to_exclude() -> None:
    entry = _route_file_meta(
        drive_id="d",
        file_meta={"id": "f1", "name": "a.pdf", "mimeType": "application/pdf"},
        folder_ids=["folderX"],
        drive=_Drive(False),
    )
    assert entry["route"] == RouteKind.EXCLUDE.value
    assert entry["skipReason"] == "out_of_folder_scope"


def test_in_scope_file_keeps_its_route() -> None:
    entry = _route_file_meta(
        drive_id="d",
        file_meta={"id": "f1", "name": "a.pdf", "mimeType": "application/pdf"},
        folder_ids=["folderX"],
        drive=_Drive(True),
    )
    assert entry["route"] == RouteKind.FILE_COPY.value
    assert "skipReason" not in entry


def test_unsupported_mime_is_skip_not_exclude() -> None:
    entry = _route_file_meta(
        drive_id="d",
        file_meta={"id": "f1", "name": "포스터.jpg", "mimeType": "image/jpeg"},
        folder_ids=["folderX"],
        drive=_Drive(True),
    )
    assert entry["route"] == RouteKind.SKIP.value


def _recon(**over):
    base = {
        "driveId": "d", "listed": 0, "gcsUploaded": 0, "indexed": 0,
        "failed": 0, "skipped": 0, "deleted": 0,
    }
    base.update(over)
    return reconcile(ReconcileBody(**base))


def test_excluded_is_subtracted_from_listed() -> None:
    res = _recon(listed=10, excluded=7, gcsUploaded=3, uris=3, indexed=3)
    assert res["listedInScope"] == 3
    assert res["unaccounted"] == 0
    assert res["ok"] is True


def test_old_behaviour_would_have_been_inconsistent() -> None:
    res = _recon(listed=10, excluded=0, gcsUploaded=3, uris=3, indexed=3)
    assert res["unaccounted"] == 7
    assert res["ok"] is False


def test_skipped_still_counts_as_accounted() -> None:
    res = _recon(listed=5, excluded=0, gcsUploaded=3, uris=3, indexed=3, skipped=2)
    assert res["unaccounted"] == 0
    assert res["ok"] is True


def test_excluded_defaults_to_zero_for_old_callers() -> None:
    res = _recon(listed=3, gcsUploaded=3, uris=3, indexed=3)
    assert res["excluded"] == 0
    assert res["ok"] is True


def test_excluded_is_not_a_live_status() -> None:
    assert DocStatus.EXCLUDED.value not in _LIVE_STATUSES
    assert DocStatus.SKIPPED.value in _LIVE_STATUSES


def test_cleanup_targets_excluded_objects() -> None:
    statuses = {"abc123def456": DocStatus.EXCLUDED.value}
    verdict = classify("abc123def456.md", statuses)
    assert verdict is not None
    assert verdict[0] == "abc123def456"


def test_only_deleted_flag_leaves_excluded_alone() -> None:
    statuses = {"abc123def456": DocStatus.EXCLUDED.value}
    assert classify("abc123def456.md", statuses, only_deleted=True) is None


def test_cleanup_leaves_skipped_objects() -> None:
    statuses = {"abc123def456": DocStatus.SKIPPED.value}
    assert classify("abc123def456.md", statuses) is None


def test_excluded_round_trips() -> None:
    src = DocState(file_id="f1", drive_id="d", status=DocStatus.EXCLUDED)
    assert DocState.from_firestore(src.to_firestore()).status is DocStatus.EXCLUDED


def test_unknown_status_still_falls_back_to_pending() -> None:
    state = DocState.from_firestore({"fileId": "f1", "status": "WAT"})
    assert state.status is DocStatus.PENDING


def test_exclude_route_evicts_without_asking_drive_again(monkeypatch) -> None:
    import services.sync.main as sync_main
    from services.sync.main import IngestBody, _ingest_with

    class _Settings:
        sync_folder_id_list = ["folderX"]
        student_folder_id_list: list[str] = []
        audience_split_enabled = False
        rag_corpus_name_student = ""
        gcs_source_bucket = "nb"
        gcs_hwp_original_bucket = "rb"

    class _Store:
        def __init__(self) -> None:
            self.upserts: list[DocState] = []

        def get(self, _fid):
            return None

        def upsert(self, state: DocState) -> None:
            self.upserts.append(state)

    class _Drive2:
        def is_in_sync_scope(self, *_a, **_k):  # noqa: ANN002
            raise AssertionError("EXCLUDE 라우트에서 범위를 다시 묻지 않는다")

    store = _Store()
    monkeypatch.setattr(
        sync_main,
        "_cleanup_out_of_scope_file",
        lambda _gcs, _settings, _fid: (True, 2, 1),
    )

    result = _ingest_with(
        IngestBody(
            fileId="f1",
            driveId="d1",
            name="a.pdf",
            mimeType="application/pdf",
            route=RouteKind.EXCLUDE.value,
        ),
        store=store,
        settings=_Settings(),
        gcs=object(),
        drive=_Drive2(),
    )

    assert result["status"] == DocStatus.EXCLUDED.value
    assert result["reason"] == "out_of_folder_scope"
    assert (result["ragDeleted"], result["sourceDeleted"], result["hwpOriginalDeleted"]) == (
        True,
        2,
        1,
    )
    assert [s.status for s in store.upserts] == [DocStatus.EXCLUDED]
