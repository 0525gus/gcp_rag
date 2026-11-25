from __future__ import annotations

import pytest

from scripts import dept_gui


def _fake_gcloud(monkeypatch: pytest.MonkeyPatch, results: list[tuple[bool, str]]) -> list[list[str]]:
    calls: list[list[str]] = []

    def run(args: list[str], timeout: int = 12) -> tuple[bool, str]:
        calls.append(args)
        return results.pop(0)

    monkeypatch.setattr(dept_gui, "_gcloud_executable", lambda: "gcloud")
    monkeypatch.setattr(dept_gui, "_run_command", run)
    return calls


def test_token_is_reused_without_another_gcloud_process(monkeypatch):
    calls = _fake_gcloud(monkeypatch, [(True, "t1\n")])

    assert dept_gui._provision_access_token() == "t1"
    assert dept_gui._provision_access_token() == "t1"
    assert len(calls) == 1


def test_token_is_fetched_again_after_ttl(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(dept_gui.time, "monotonic", lambda: now[0])
    _fake_gcloud(monkeypatch, [(True, "t1"), (True, "t2")])

    assert dept_gui._provision_access_token() == "t1"
    now[0] += dept_gui._ACCESS_TOKEN_TTL_SECONDS
    assert dept_gui._provision_access_token() == "t2"


def test_ttl_is_shorter_than_the_lifetime_gcloud_guarantees():
    assert dept_gui._ACCESS_TOKEN_TTL_SECONDS < 300, (
        "gcloud 는 남은 수명이 300초 이하일 때만 갱신한다. 그 이상 캐시하면 만료 토큰을 쓴다"
    )


def test_logout_forgets_the_cached_token(monkeypatch):
    _fake_gcloud(monkeypatch, [(True, "old-account"), (True, "new-account")])

    dept_gui._provision_access_token()
    dept_gui._clear_cloud_caches()

    assert dept_gui._provision_access_token() == "new-account"


def test_failed_token_is_not_cached(monkeypatch):
    _fake_gcloud(monkeypatch, [(False, ""), (True, "t1")])

    with pytest.raises(RuntimeError):
        dept_gui._provision_access_token()
    assert dept_gui._provision_access_token() == "t1"
