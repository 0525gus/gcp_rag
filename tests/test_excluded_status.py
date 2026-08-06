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
    # 대상 폴더 안이지만 처리할 방법이 없는 형식 → SKIP 이어야 한다.
    # (예시가 .zip 이었으나 지금은 사이드카로라도 색인한다 — 이미지로 바꿨다)
    entry = _route_file_meta(
        drive_id="d",
        file_meta={"id": "f1", "name": "포스터.jpg", "mimeType": "image/jpeg"},
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


# ------------------------------------------------------- EXCLUDE 라우트 처리
def test_exclude_route_evicts_without_asking_drive_again(monkeypatch) -> None:
    """워크플로가 EXCLUDE 로 넘긴 문서도 잔존물 회수까지 마치고 EXCLUDED 가 된다.

    list_changes 가 이미 같은 folder_ids 로 판정해 붙인 라우트라 범위 판정을
    다시 물으면 Drive 호출만 더 든다. 한때 이 분기가 지워진 헬퍼를 부르고 있어
    EXCLUDE 로 들어온 문서가 NameError 로 죽었다 — 정상 경로라 조용했다.
    """
    import services.sync.main as sync_main
    from services.sync.main import IngestBody, _ingest_with

    class _Settings:
        sync_folder_id_list = ["folderX"]
        student_folder_id_list: list[str] = []
        audience_split_enabled = False
        rag_corpus_name_student = ""
        gcs_normalized_bucket = "nb"
        gcs_raw_bucket = "rb"

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
    assert (result["ragDeleted"], result["normalizedDeleted"], result["rawDeleted"]) == (
        True,
        2,
        1,
    )
    assert [s.status for s in store.upserts] == [DocStatus.EXCLUDED]
