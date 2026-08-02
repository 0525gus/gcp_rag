"""델타 페이징 계약 — 워크플로우 변수 한도(512KB)를 넘기지 않는지 검증한다.

변경 전체를 한 번에 반환하면 Cloud Workflows 실행이 변수 한도에서 죽고, 토큰이
커밋되지 않아 다음 실행의 델타가 더 커진다. 한 번 넘으면 자력 회복이 불가능하므로
'끊어서 주고, 배치마다 커밋한다'가 이 파이프라인의 핵심 불변식이다.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

import services.sync.main as sync_main
from shared.drive import DriveClient

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / "workflows" / "daily_sync.yaml"


# ------------------------------------------------------------------ 가짜 Drive
class _FakeChangesApi:
    """changes.list 를 페이지 단위로 흉내낸다 (pageSize=100 고정)."""

    def __init__(self, pages: list[dict[str, Any]]) -> None:
        self._pages = pages
        self.requested_tokens: list[str] = []

    def changes(self):  # noqa: D102
        return self

    def list(self, **kwargs: Any):  # noqa: D102
        self.requested_tokens.append(kwargs["pageToken"])
        idx = int(kwargs["pageToken"].removeprefix("t"))
        return _FakeExec(self._pages[idx])


class _FakeExec:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def execute(self, **_kwargs: Any) -> dict[str, Any]:
        return self._payload


def _page(n: int, *, next_token: str | None, new_start: str | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "changes": [
            {"fileId": f"f{i}", "file": {"id": f"f{i}", "name": f"{i}.hwp"}}
            for i in range(n)
        ]
    }
    if next_token:
        payload["nextPageToken"] = next_token
    if new_start:
        payload["newStartPageToken"] = new_start
    return payload


def _client(pages: list[dict[str, Any]]) -> tuple[DriveClient, _FakeChangesApi]:
    client = object.__new__(DriveClient)
    api = _FakeChangesApi(pages)
    client._service = api  # noqa: SLF001
    return client, api


# ------------------------------------------------------------------ ① drive.py
def test_stops_at_max_changes_and_returns_resume_token() -> None:
    # 100건짜리 페이지 3장. 상한 200이면 2장만 읽고 멈춰야 한다.
    pages = [
        _page(100, next_token="t1"),
        _page(100, next_token="t2"),
        _page(100, next_token=None, new_start="final"),
    ]
    client, api = _client(pages)

    changes, token, has_more = client.list_changes("d", "t0", max_changes=200)

    assert len(changes) == 200
    assert has_more is True
    # 재개 지점은 nextPageToken — 3번째 페이지를 아직 안 읽었다
    assert token == "t2"
    assert api.requested_tokens == ["t0", "t1"]


def test_resumes_from_returned_token_without_gap_or_overlap() -> None:
    pages = [
        _page(100, next_token="t1"),
        _page(100, next_token="t2"),
        _page(60, next_token=None, new_start="final"),
    ]
    client, _ = _client(pages)

    first, token, has_more = client.list_changes("d", "t0", max_changes=200)
    assert has_more is True

    second, final_token, still_more = client.list_changes("d", token, max_changes=200)

    assert still_more is False
    assert final_token == "final"
    # 260건을 정확히 한 번씩 — 누락도 중복도 없어야 한다
    ids = [c.file_id for c in first] + [c.file_id for c in second]
    assert len(ids) == 260


def test_no_cap_reads_everything_and_returns_new_start_token() -> None:
    pages = [
        _page(100, next_token="t1"),
        _page(5, next_token=None, new_start="final"),
    ]
    client, _ = _client(pages)

    changes, token, has_more = client.list_changes("d", "t0")

    assert len(changes) == 105
    assert has_more is False
    assert token == "final"


def test_exact_boundary_on_last_page_is_not_has_more() -> None:
    # 정확히 상한만큼 읽었는데 다음 페이지가 없으면 완료다.
    pages = [_page(100, next_token="t1"), _page(100, next_token=None, new_start="final")]
    client, _ = _client(pages)

    changes, token, has_more = client.list_changes("d", "t0", max_changes=200)

    assert len(changes) == 200
    assert has_more is False, "다음 페이지가 없으면 hasMore 는 False 여야 한다"
    assert token == "final"


def test_no_changes_keeps_existing_token() -> None:
    client, _ = _client([_page(0, next_token=None, new_start="final")])

    changes, token, has_more = client.list_changes("d", "t0", max_changes=200)

    assert changes == []
    assert has_more is False
    assert token == "final"


# ------------------------------------------------------------- ② /sync/changes
class _Store:
    def __init__(self, token: str | None) -> None:
        self._token = token

    def get_start_page_token(self, _drive_id: str) -> str | None:
        return self._token


class _Settings:
    sync_folder_id_list: list[str] = []
    sync_max_changes = 200


def _wire(monkeypatch: pytest.MonkeyPatch, token: str | None, drive: Any) -> None:
    monkeypatch.setattr(sync_main, "get_settings", lambda: _Settings())
    monkeypatch.setattr(sync_main, "DocStateStore", lambda: _Store(token))
    monkeypatch.setattr(sync_main, "DriveClient", lambda: drive)


class _StubDrive:
    def __init__(self, has_more: bool) -> None:
        self.has_more = has_more
        self.max_changes: int | None = None

    def list_changes(self, _drive_id: str, _token: str, *, max_changes: int | None = None):
        self.max_changes = max_changes
        return [], "next-token", self.has_more


def test_missing_token_delegates_to_backfill_instead_of_dumping_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 최초 실행에 드라이브 전체 목록을 반환하면 워크플로우가 변수 한도에서 죽는다.
    called = False

    def _boom(*_a: Any, **_k: Any) -> dict[str, Any]:
        nonlocal called
        called = True
        return {}

    monkeypatch.setattr(sync_main, "_build_backfill_changes", _boom)
    _wire(monkeypatch, None, _StubDrive(False))

    result = sync_main.list_changes(sync_main.ChangesBody(driveId="d"))

    assert result["mode"] == "backfill_required"
    assert result["changes"] == []
    assert result["hasMore"] is False
    assert not called, "전체 스냅샷을 워크플로우로 넘겨서는 안 된다"


def test_has_more_is_surfaced_to_caller(monkeypatch: pytest.MonkeyPatch) -> None:
    drive = _StubDrive(True)
    _wire(monkeypatch, "t0", drive)

    result = sync_main.list_changes(sync_main.ChangesBody(driveId="d"))

    assert result["hasMore"] is True
    assert result["mode"] == "delta"
    assert result["pendingPageToken"] == "next-token"


def test_max_changes_defaults_to_settings_and_is_overridable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    drive = _StubDrive(False)
    _wire(monkeypatch, "t0", drive)

    sync_main.list_changes(sync_main.ChangesBody(driveId="d"))
    assert drive.max_changes == 200

    sync_main.list_changes(sync_main.ChangesBody(driveId="d", maxChanges=50))
    assert drive.max_changes == 50


# ------------------------------------------------------------------ ③ workflow
def _drive_steps() -> list[dict[str, Any]]:
    data = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    return next(
        step["for_each_drive"] for step in data["main"]["steps"] if "for_each_drive" in step
    )["for"]["steps"]


def _named(steps: list[dict[str, Any]], name: str) -> dict[str, Any]:
    return next(step[name] for step in steps if name in step)


def test_page_loop_target_exists_and_resets_accumulators() -> None:
    steps = _drive_steps()
    names = [next(iter(s)) for s in steps]

    assert "sync_page" in names, "페이지 루프 진입점이 있어야 한다"
    assert names.index("reset_drive_state") < names.index("sync_page")

    assigned = {k for entry in _named(steps, "sync_page")["assign"] for k in entry}
    # 페이지마다 초기화돼야 직전 페이지 수치가 새 페이지 정합성 검사를 오염시키지 않는다
    for key in ("change_list", "pending_uris", "drive_listed", "drive_failed", "drive_uris"):
        assert key in assigned


def test_next_page_only_loops_when_token_was_committed() -> None:
    steps = _drive_steps()
    loop_branch = _named(steps, "next_page")["switch"][0]

    assert loop_branch["next"] == "sync_page"

    commit_cond = _named(steps, "commit_token")["switch"][0]["condition"]
    loop_cond = loop_branch["condition"]
    # 커밋 조건이 하나라도 빠지면 미커밋 상태로 되돌아가 같은 페이지를 무한 반복한다
    for clause in (
        'pending_page_token != ""',
        "drive_failed == 0",
        "drive_indexed == drive_uris",
        "drive_reconciled == true",
    ):
        assert clause in commit_cond
        assert clause in loop_cond, f"next_page 조건에 {clause} 가 빠졌다"

    assert "drive_has_more == true" in loop_cond
    assert "drive_page < max_pages" in loop_cond, "무한 루프 방지 상한이 필요하다"


def test_changes_request_sends_max_changes() -> None:
    steps = _drive_steps()
    branches = _named(steps, "fetch_source")["switch"]
    fetch_changes = _named(branches[1]["steps"], "fetch_changes")

    assert fetch_changes["try"]["args"]["body"]["maxChanges"] == "${max_changes}"


def test_backfill_required_reroutes_to_backfill_run() -> None:
    steps = _drive_steps()
    branch = _named(steps, "check_backfill_required")["switch"][0]

    assert "backfill_required" in branch["condition"]
    inner = branch["steps"]
    assert _named(inner, "switch_to_backfill")["assign"] == [{"drive_backfill": True}]
    assert _named(inner, "restart_as_backfill")["next"] == "fetch_source"


def test_backfill_branch_terminates_the_page_loop() -> None:
    steps = _drive_steps()
    branches = _named(steps, "fetch_source")["switch"]
    totals = _named(branches[0]["steps"], "apply_backfill_totals")["assign"]
    assigned = {k: v for entry in totals for k, v in entry.items()}

    # backfill-run 이 드라이브 전체를 끝내므로 다시 돌면 무한 루프다
    assert assigned["drive_has_more"] is False
    assert assigned["drive_backfill"] is False


def test_page_size_never_overshoots_the_requested_limit() -> None:
    """한 페이지를 통째로 받은 뒤 재면 최대 pageSize-1 건을 초과해 돌려준다.

    maxChanges 를 낮춰 잡으려는 조정이 반대로 도는 함정이었다 — 150 을 주면
    100 을 받고 아직 모자라서 100 을 더 받아 200 이 나왔다.
    """

    class _SizeAwareApi(_FakeChangesApi):
        def __init__(self) -> None:
            super().__init__([])
            self.page_sizes: list[int] = []

        def list(self, **kwargs: Any):
            self.page_sizes.append(kwargs["pageSize"])
            n = kwargs["pageSize"]
            idx = len(self.page_sizes)
            payload: dict[str, Any] = {
                "changes": [
                    {"fileId": f"p{idx}f{i}", "file": {"id": f"p{idx}f{i}"}}
                    for i in range(n)
                ],
                "nextPageToken": f"t{idx}",
            }
            return _FakeExec(payload)

    client = object.__new__(DriveClient)
    api = _SizeAwareApi()
    client._service = api  # noqa: SLF001

    changes, token, has_more = client.list_changes("drive", "t0", max_changes=150)

    assert len(changes) == 150, f"{len(changes)}건 — 요청한 150건을 넘겼다"
    assert api.page_sizes == [100, 50]
    assert has_more is True
    assert token == "t2"
