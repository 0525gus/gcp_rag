"""`EXCLUDED`(대상 폴더 밖) 와 `SKIPPED`(대상인데 처리 못 함)의 분리.

핵심은 집계다. 둘이 뭉쳐 있던 동안 폴더 밖 393건이 `accounted` 에 들어가
"대상인데 처리 못 한 것"의 신호를 덮었다.
"""

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


# ------------------------------------------------------------------ 라우팅
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
    # 대상 폴더 안이지만 처리할 방법이 없는 형식 → SKIP 이어야 한다
    entry = _route_file_meta(
        drive_id="d",
        file_meta={"id": "f1", "name": "a.zip", "mimeType": "application/zip"},
        folder_ids=["folderX"],
        drive=_Drive(True),
    )
    assert entry["route"] == RouteKind.SKIP.value


# ------------------------------------------------------------------ 집계
def _recon(**over):
    base = {
        "driveId": "d", "listed": 0, "gcsUploaded": 0, "indexed": 0,
        "failed": 0, "skipped": 0, "deleted": 0,
    }
    base.update(over)
    return reconcile(ReconcileBody(**base))


def test_excluded_is_subtracted_from_listed() -> None:
    # 조회 10건 중 7건이 폴더 밖 → 실제 대상은 3건이고 그 3건이 업로드됨
    res = _recon(listed=10, excluded=7, gcsUploaded=3, uris=3, indexed=3)
    assert res["listedInScope"] == 3
    assert res["unaccounted"] == 0
    assert res["ok"] is True


def test_old_behaviour_would_have_been_inconsistent() -> None:
    # excluded 를 skipped 로 뭉치면 균형은 맞지만 skipped 가 신호를 잃는다.
    # 여기서는 '차감하지 않으면 어긋난다'만 고정한다.
    res = _recon(listed=10, excluded=0, gcsUploaded=3, uris=3, indexed=3)
    assert res["unaccounted"] == 7
    assert res["ok"] is False


def test_skipped_still_counts_as_accounted() -> None:
    # 대상인데 처리 못 한 것은 여전히 집계에 들어간다
    res = _recon(listed=5, excluded=0, gcsUploaded=3, uris=3, indexed=3, skipped=2)
    assert res["unaccounted"] == 0
    assert res["ok"] is True


def test_excluded_defaults_to_zero_for_old_callers() -> None:
    # 필드를 안 보내는 구버전 워크플로도 동작해야 한다
    res = _recon(listed=3, gcsUploaded=3, uris=3, indexed=3)
    assert res["excluded"] == 0
    assert res["ok"] is True


# ------------------------------------------------------------------ 정리 대상
def test_excluded_is_not_a_live_status() -> None:
    # 폴더 밖으로 나간 문서의 산출물은 회수 대상이어야 한다
    assert DocStatus.EXCLUDED.value not in _LIVE_STATUSES
    assert DocStatus.SKIPPED.value in _LIVE_STATUSES


def test_cleanup_targets_excluded_objects() -> None:
    statuses = {"abc123def456": DocStatus.EXCLUDED.value}
    verdict = classify("normalized/abc123def456.md", statuses)
    assert verdict is not None
    assert verdict[0] == "abc123def456"


def test_only_deleted_flag_leaves_excluded_alone() -> None:
    statuses = {"abc123def456": DocStatus.EXCLUDED.value}
    assert classify("normalized/abc123def456.md", statuses, only_deleted=True) is None


def test_cleanup_leaves_skipped_objects() -> None:
    statuses = {"abc123def456": DocStatus.SKIPPED.value}
    assert classify("normalized/abc123def456.md", statuses) is None


# ------------------------------------------------------------------ 상태 호환
def test_excluded_round_trips() -> None:
    src = DocState(file_id="f1", drive_id="d", status=DocStatus.EXCLUDED)
    assert DocState.from_firestore(src.to_firestore()).status is DocStatus.EXCLUDED


def test_unknown_status_still_falls_back_to_pending() -> None:
    state = DocState.from_firestore({"fileId": "f1", "status": "WAT"})
    assert state.status is DocStatus.PENDING
