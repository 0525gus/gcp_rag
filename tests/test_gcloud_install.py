"""콘솔의 gcloud 원클릭 설치: 설치 경로 탐지, 중복 실행 방지, 서명 검증 실패 시 미실행."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from scripts import dept_gui


@pytest.fixture(autouse=True)
def reset_install_state(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dept_gui, "_GCLOUD_INSTALL", {"status": "IDLE"})


def _client() -> tuple[TestClient, dict[str, str]]:
    client = TestClient(dept_gui.app)
    nonce = client.get("/api/v1/session").json()["nonce"]
    return client, {"X-Local-Session": nonce, "Origin": "http://testserver"}


def test_executable_found_in_default_install_dir_without_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    gcloud = tmp_path / "Google" / "Cloud SDK" / "google-cloud-sdk" / "bin" / "gcloud.cmd"
    gcloud.parent.mkdir(parents=True)
    gcloud.write_text("@echo off")
    monkeypatch.setattr(dept_gui.os, "name", "nt")
    monkeypatch.setattr(dept_gui.shutil, "which", lambda name: None)
    monkeypatch.setattr(dept_gui, "_gcloud_install_locations", lambda: [tmp_path / "missing.cmd", gcloud])
    assert dept_gui._gcloud_executable() == str(gcloud)


def test_install_requires_local_session() -> None:
    client, _ = _client()
    assert client.post("/api/v1/gcloud-install").status_code == 403


def test_install_is_noop_when_gcloud_present(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dept_gui, "_gcloud_executable", lambda: "gcloud.cmd")
    started: list[object] = []
    monkeypatch.setattr(dept_gui.threading, "Thread", lambda **kw: started.append(kw))
    client, headers = _client()
    response = client.post("/api/v1/gcloud-install", headers=headers)
    assert response.status_code == 200
    assert response.json()["installed"] is True
    assert started == []


def test_install_rejected_outside_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dept_gui, "_gcloud_executable", lambda: None)
    monkeypatch.setattr(dept_gui.os, "name", "posix")
    client, headers = _client()
    response = client.post("/api/v1/gcloud-install", headers=headers)
    assert response.status_code == 501
    assert response.json()["error"]["code"] == "GCLOUD_INSTALL_UNSUPPORTED"


def test_double_click_starts_one_install(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dept_gui, "_gcloud_executable", lambda: None)
    monkeypatch.setattr(dept_gui.os, "name", "nt")
    threads: list[dict] = []

    class FakeThread:
        def __init__(self, **kwargs) -> None:
            threads.append(kwargs)

        def start(self) -> None:
            return None

    monkeypatch.setattr(dept_gui.threading, "Thread", FakeThread)
    client, headers = _client()
    first = client.post("/api/v1/gcloud-install", headers=headers)
    second = client.post("/api/v1/gcloud-install", headers=headers)
    assert first.status_code == second.status_code == 202
    assert first.json()["status"] == "RUNNING"
    assert len(threads) == 1
    status = client.get("/api/v1/gcloud-install").json()
    assert status == {**status, "status": "RUNNING", "installed": False}


def test_status_reports_done_once_gcloud_appears(monkeypatch: pytest.MonkeyPatch) -> None:
    dept_gui._GCLOUD_INSTALL.update(status="INSTALLER_OPEN", message="창에서 설치")
    monkeypatch.setattr(dept_gui, "_gcloud_executable", lambda: "C:/sdk/gcloud.cmd")
    client, _ = _client()
    body = client.get("/api/v1/gcloud-install").json()
    assert body["installed"] is True
    assert body["status"] == "DONE"


def test_unsigned_installer_is_deleted_and_never_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dept_gui.shutil, "which", lambda name: None)  # winget 없음 → 공식 설치 파일 경로
    monkeypatch.setattr(dept_gui, "_gcloud_executable", lambda: None)
    monkeypatch.setattr(dept_gui.tempfile, "mkdtemp", lambda prefix: str(tmp_path))

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self, size: int = -1) -> bytes:
            data, self._data = getattr(self, "_data", b"MZ fake"), b""
            return data

    monkeypatch.setattr(dept_gui.urllib.request, "urlopen", lambda url, timeout: FakeResponse())
    monkeypatch.setattr(dept_gui, "_installer_signed_by_google", lambda path: False)
    launched: list[object] = []
    monkeypatch.setattr(dept_gui.subprocess, "Popen", lambda *a, **kw: launched.append(a))
    monkeypatch.setattr(dept_gui.subprocess, "CREATE_NO_WINDOW", 0, raising=False)

    dept_gui._run_gcloud_install()

    assert launched == []
    assert not (tmp_path / "GoogleCloudSDKInstaller.exe").exists()
    assert dept_gui._GCLOUD_INSTALL["status"] == "FAILED"


def test_winget_success_skips_installer_download(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []
    present = {"value": False}
    monkeypatch.setattr(dept_gui.shutil, "which", lambda name: "C:/winget.exe" if name.startswith("winget") else None)
    monkeypatch.setattr(dept_gui, "_gcloud_executable", lambda: "gcloud.cmd" if present["value"] else None)

    def fake_run(args, **kwargs):
        calls.append(args)
        present["value"] = True

    monkeypatch.setattr(dept_gui.subprocess, "run", fake_run)
    monkeypatch.setattr(dept_gui.subprocess, "CREATE_NO_WINDOW", 0, raising=False)
    monkeypatch.setattr(dept_gui.urllib.request, "urlopen", lambda *a, **kw: pytest.fail("installer should not download"))

    dept_gui._run_gcloud_install()

    assert calls[0][:4] == ["C:/winget.exe", "install", "--id", "Google.CloudSDK"]
    assert dept_gui._GCLOUD_INSTALL["status"] == "DONE"
