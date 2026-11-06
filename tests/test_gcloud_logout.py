"""콘솔 환경 화면의 gcloud 로그아웃: 세션 필수, 자격 증명 철회, 이전 계정 캐시 제거."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from scripts import dept_gui


def _client() -> tuple[TestClient, dict[str, str]]:
    client = TestClient(dept_gui.app)
    nonce = client.get("/api/v1/session").json()["nonce"]
    return client, {"X-Local-Session": nonce, "Origin": "http://testserver"}


@pytest.fixture
def gcloud(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    calls: list[list[str]] = []
    monkeypatch.setattr(dept_gui, "_gcloud_executable", lambda: "gcloud.cmd")

    def fake_run(args: list[str], timeout: int = 12) -> tuple[bool, str]:
        calls.append(args)
        return True, ""

    monkeypatch.setattr(dept_gui, "_run_command", fake_run)
    return calls


def test_logout_requires_local_session(gcloud: list[list[str]]) -> None:
    client, _ = _client()
    assert client.post("/api/v1/gcloud-auth/logout").status_code == 403
    assert gcloud == []


def test_logout_revokes_all_accounts_and_clears_cached_credentials(
    gcloud: list[list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(dept_gui, "_SYNC_AUTH_TOKEN", "old-account-token")
    monkeypatch.setattr(dept_gui, "_SYNC_AUTH_TOKEN_EXPIRES", 9e18)
    dept_gui._MCP_REGISTRY_READ_CACHE[("p", "r", "d")] = {"created": 0, "record": {}, "configs": {}}
    client, headers = _client()

    response = client.post("/api/v1/gcloud-auth/logout", headers=headers)

    assert response.status_code == 200
    assert response.json() == {"authenticated": False}
    assert ["gcloud.cmd", "auth", "revoke", "--all", "--quiet"] in gcloud
    assert dept_gui._SYNC_AUTH_TOKEN == ""
    assert dept_gui._MCP_REGISTRY_READ_CACHE == {}


def test_logout_when_already_logged_out_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dept_gui, "_gcloud_executable", lambda: "gcloud.cmd")
    monkeypatch.setattr(dept_gui, "_run_command", lambda args, timeout=12: (False, "No credentials"))
    monkeypatch.setattr(dept_gui, "_gcloud_bootstrap_state", lambda **kw: {"authenticated": False})
    client, headers = _client()
    assert client.post("/api/v1/gcloud-auth/logout", headers=headers).status_code == 200


def test_logout_failure_is_reported_when_still_authenticated(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dept_gui, "_gcloud_executable", lambda: "gcloud.cmd")
    monkeypatch.setattr(dept_gui, "_run_command", lambda args, timeout=12: (False, "revoke failed"))
    monkeypatch.setattr(dept_gui, "_gcloud_bootstrap_state", lambda **kw: {"authenticated": True})
    client, headers = _client()
    response = client.post("/api/v1/gcloud-auth/logout", headers=headers)
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "LOGOUT_FAILED"


def test_logout_without_gcloud_returns_404(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dept_gui, "_gcloud_executable", lambda: None)
    client, headers = _client()
    assert client.post("/api/v1/gcloud-auth/logout", headers=headers).status_code == 404
