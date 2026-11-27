from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from scripts import dept_gui, gcp_cli
from scripts.console import cloud_registry, environment_setup


def _client() -> tuple[TestClient, dict[str, str]]:
    client = TestClient(dept_gui.app)
    nonce = client.get("/api/v1/session").json()["nonce"]
    return client, {"X-Local-Session": nonce, "Origin": "http://testserver"}


@pytest.fixture
def gcloud(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    calls: list[list[str]] = []
    monkeypatch.setattr(gcp_cli, "gcloud_executable", lambda: "gcloud.cmd")

    def fake_run(args: list[str], timeout: int = 12) -> tuple[bool, str]:
        calls.append(args)
        return True, ""

    monkeypatch.setattr(gcp_cli, "run_command", fake_run)
    return calls


def test_logout_requires_local_session(gcloud: list[list[str]]) -> None:
    client, _ = _client()
    assert client.post("/api/v1/gcloud-auth/logout").status_code == 403
    assert gcloud == []


def test_logout_revokes_all_accounts_and_clears_cached_credentials(
    gcloud: list[list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gcp_cli, "_token", "old-account-token")
    monkeypatch.setattr(gcp_cli, "_token_expires", 9e18)
    cloud_registry._MCP_REGISTRY_READ_CACHE[("p", "r", "d")] = {"created": 0, "record": {}, "configs": {}}
    client, headers = _client()

    response = client.post("/api/v1/gcloud-auth/logout", headers=headers)

    assert response.status_code == 200
    assert response.json() == {"authenticated": False}
    assert ["gcloud.cmd", "auth", "revoke", "--all", "--quiet"] in gcloud
    assert gcp_cli._token == ""
    assert cloud_registry._MCP_REGISTRY_READ_CACHE == {}


def test_logout_when_already_logged_out_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gcp_cli, "gcloud_executable", lambda: "gcloud.cmd")
    monkeypatch.setattr(gcp_cli, "run_command", lambda args, timeout=12: (False, "No credentials"))
    monkeypatch.setattr(environment_setup, "_gcloud_bootstrap_state", lambda **kw: {"authenticated": False})
    client, headers = _client()
    assert client.post("/api/v1/gcloud-auth/logout", headers=headers).status_code == 200


def test_logout_failure_is_reported_when_still_authenticated(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gcp_cli, "gcloud_executable", lambda: "gcloud.cmd")
    monkeypatch.setattr(gcp_cli, "run_command", lambda args, timeout=12: (False, "revoke failed"))
    monkeypatch.setattr(environment_setup, "_gcloud_bootstrap_state", lambda **kw: {"authenticated": True})
    client, headers = _client()
    response = client.post("/api/v1/gcloud-auth/logout", headers=headers)
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "LOGOUT_FAILED"


def test_logout_without_gcloud_returns_404(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gcp_cli, "gcloud_executable", lambda: None)
    client, headers = _client()
    assert client.post("/api/v1/gcloud-auth/logout", headers=headers).status_code == 404
