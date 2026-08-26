"""로컬 학과 관리 GUI의 생성·검증·상태 API."""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from scripts import dept_config, dept_gui


@pytest.fixture()
def isolated_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_dir = tmp_path / "config"
    dept_dir = config_dir / "departments"
    dept_dir.mkdir(parents=True)
    (config_dir / "common.yaml").write_text(
        yaml.safe_dump(
            {
                "GCP_PROJECT_ID": "project-test",
                "GCP_REGION": "asia-northeast3",
                "GCS_HWP_ORIGINAL_BUCKET": "common-hwp-test",
                "GCS_SOURCE_BUCKET": "common-source-test",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(dept_gui, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(dept_gui, "DEPT_DIR", dept_dir)
    monkeypatch.setattr(dept_config, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(dept_config, "DEPT_DIR", dept_dir)
    monkeypatch.setattr(
        dept_gui,
        "_department_resource_options",
        lambda common: {
            "corpora": [
                {
                    "name": "projects/project-test/locations/asia-northeast3/ragCorpora/staff-1",
                    "displayName": "교직원 코퍼스",
                    "description": "",
                },
                {
                    "name": "projects/project-test/locations/asia-northeast3/ragCorpora/student-1",
                    "displayName": "학생 코퍼스",
                    "description": "",
                },
            ],
            "buckets": [
                {"name": "rag-ee-hwp-project-test", "location": "asia-northeast3"},
                {"name": "rag-ee-source-project-test", "location": "asia-northeast3"},
            ],
            "error": "",
        },
    )
    dept_gui._LATEST.clear()
    dept_gui._RUNS.clear()
    return dept_dir


def _payload() -> dict:
    return {
        "code": "ee",
        "name": "전자공학과",
        "corpora": {
            "staff": (
                "projects/project-test/locations/asia-northeast3/ragCorpora/staff-1"
            ),
            "student": (
                "projects/project-test/locations/asia-northeast3/ragCorpora/student-1"
            ),
        },
        "buckets": {
            "hwpOriginal": "rag-ee-hwp-project-test",
            "source": "rag-ee-source-project-test",
        },
        "drive": {
            "driveIds": "DRIVE-1, DRIVE-1",
            "syncFolderIds": "STAFF-1\nSTUDENT-1",
            "studentFolderIds": ["STUDENT-1"],
        },
        "minInstances": {"staff": 0, "student": 0},
    }


def _client() -> tuple[TestClient, dict[str, str]]:
    client = TestClient(dept_gui.app)
    nonce = client.get("/api/v1/session").json()["nonce"]
    return client, {"X-Local-Session": nonce, "Origin": "http://testserver"}


def test_preview_normalises_ids_and_never_returns_secret(isolated_config: Path) -> None:
    client, headers = _client()
    response = client.post("/api/v1/departments/preview", headers=headers, json=_payload())

    assert response.status_code == 200
    body = response.json()
    assert body["valid"] is True
    assert "<자동 생성>" in body["yamlPreview"]
    assert "MCP_API_KEY" not in response.text


def test_department_resource_options_include_display_names(isolated_config: Path) -> None:
    client, _ = _client()

    response = client.get("/api/v1/departments/resource-options")

    assert response.status_code == 200
    assert response.json()["corpora"][0]["displayName"] == "교직원 코퍼스"
    assert response.json()["buckets"][0]["name"] == "rag-ee-hwp-project-test"
    assert response.json()["buckets"][0]["usedBy"] == []


def test_bucket_options_list_departments_already_using_them(isolated_config: Path) -> None:
    client, headers = _client()
    assert client.post("/api/v1/departments", headers=headers, json=_payload()).status_code == 201
    (isolated_config / "cs.yaml").write_text(
        (isolated_config / "ee.yaml").read_text(encoding="utf-8"), encoding="utf-8"
    )

    response = client.get("/api/v1/departments/resource-options")

    buckets = {item["name"]: item for item in response.json()["buckets"]}
    assert buckets["rag-ee-hwp-project-test"]["usedBy"] == ["cs", "ee"]
    assert buckets["rag-ee-source-project-test"]["usedBy"] == ["cs", "ee"]


def test_same_hwp_and_source_bucket_is_rejected(isolated_config: Path) -> None:
    client, headers = _client()
    payload = _payload()
    payload["buckets"]["source"] = payload["buckets"]["hwpOriginal"]

    response = client.post("/api/v1/departments/preview", headers=headers, json=payload)

    assert response.status_code == 200
    assert response.json()["valid"] is False
    assert "buckets.source" in response.json()["fieldErrors"]


def test_environment_exposes_drive_service_account(
    isolated_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        dept_gui,
        "_gcloud_bootstrap_state",
        lambda **kwargs: {
            "installed": True,
            "authenticated": True,
            "account": "te***@example.com",
            "currentProject": "project-test",
            "projects": [],
            "regions": dept_gui.SETUP_REGIONS,
        },
    )
    monkeypatch.setattr(
        dept_gui,
        "_gcloud_json",
        lambda args: (True, {"projectNumber": "123456789"}),
    )
    client, _ = _client()

    response = client.get("/api/v1/environment")

    assert response.status_code == 200
    assert (
        response.json()["serviceAccount"]
        == "123456789-compute@developer.gserviceaccount.com"
    )


def test_missing_common_config_can_be_bootstrapped(
    isolated_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    common_path = isolated_config.parent / "common.yaml"
    common_path.unlink()
    monkeypatch.setattr(
        dept_gui,
        "_gcloud_bootstrap_state",
        lambda **kwargs: {
            "installed": True,
            "authenticated": True,
            "account": "te***@example.com",
            "currentProject": "project-test",
            "projects": [{"id": "project-test", "name": "테스트 프로젝트"}],
            "regions": dept_gui.SETUP_REGIONS,
        },
    )
    monkeypatch.setattr(
        dept_gui,
        "_gcloud_project_resources",
        lambda project, region: {
            "artifactRepositories": [{"id": "rag-mcp", "format": "DOCKER"}],
            "firestoreDatabases": [
                {"id": "rag-sync-state", "location": region, "type": "FIRESTORE_NATIVE"}
            ],
            "artifactError": "",
            "firestoreError": "",
        },
    )
    client, headers = _client()

    environment = client.get("/api/v1/environment")
    assert environment.status_code == 200
    assert environment.json()["commonExists"] is False

    resources = client.get(
        "/api/v1/common-config/resources?project=project-test&region=asia-northeast3"
    )
    assert resources.status_code == 200
    assert resources.json()["artifactRepositories"][0]["id"] == "rag-mcp"

    payload = {
        "projectId": "project-test",
        "region": "asia-northeast3",
        "artifactRepo": "rag-mcp",
        "firestoreDatabase": "rag-sync-state",
    }
    created = client.post("/api/v1/common-config", headers=headers, json=payload)

    assert created.status_code == 201
    saved = yaml.safe_load(common_path.read_text(encoding="utf-8"))
    assert saved["GCP_PROJECT_ID"] == "project-test"
    assert "GCS_HWP_ORIGINAL_BUCKET" not in saved
    assert "GCS_SOURCE_BUCKET" not in saved
    assert saved["TOP_K_DEFAULT"] == 5
    assert saved["ALLOW_UNAUTH"] is True
    assert client.get("/api/v1/environment").json()["commonValid"] is True

    duplicate = client.post("/api/v1/common-config", headers=headers, json=payload)
    assert duplicate.status_code == 409


def test_common_bootstrap_requires_gcloud_login(
    isolated_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (isolated_config.parent / "common.yaml").unlink()
    monkeypatch.setattr(
        dept_gui,
        "_gcloud_bootstrap_state",
        lambda **kwargs: {
            "installed": True,
            "authenticated": False,
            "account": "",
            "currentProject": "",
            "projects": [],
            "regions": dept_gui.SETUP_REGIONS,
        },
    )
    monkeypatch.setattr(dept_gui, "_start_gcloud_login", lambda: True)
    client, headers = _client()

    login = client.post("/api/v1/gcloud-auth/login", headers=headers)
    assert login.status_code == 202
    assert login.json()["started"] is True

    response = client.post(
        "/api/v1/common-config",
        headers=headers,
        json={
            "projectId": "project-test",
            "region": "asia-northeast3",
            "artifactRepo": "rag-mcp",
            "firestoreDatabase": "rag-sync-state",
        },
    )

    assert response.status_code == 412
    assert response.json()["error"]["code"] == "GCLOUD_AUTH_REQUIRED"


def test_gcloud_login_restarts_stale_waiting_process(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeProcess:
        def __init__(self, pid: int) -> None:
            self.pid = pid

        def poll(self):
            return None

    old_process = FakeProcess(1234)
    new_process = FakeProcess(5678)
    killed: list[list[str]] = []
    monkeypatch.setattr(dept_gui, "_AUTH_PROCESS", old_process)
    monkeypatch.setattr(dept_gui, "_gcloud_executable", lambda: "gcloud.cmd")
    monkeypatch.setattr(
        dept_gui.subprocess,
        "run",
        lambda args, **kwargs: killed.append(args),
    )
    monkeypatch.setattr(
        dept_gui.subprocess,
        "Popen",
        lambda *args, **kwargs: new_process,
    )

    assert dept_gui._start_gcloud_login() is True
    assert killed[0][:3] == ["taskkill.exe", "/PID", "1234"]
    assert dept_gui._AUTH_PROCESS is new_process


def test_preview_rejects_student_folder_outside_sync(isolated_config: Path) -> None:
    client, headers = _client()
    payload = _payload()
    payload["drive"]["studentFolderIds"] = ["OUTSIDE"]

    body = client.post(
        "/api/v1/departments/preview", headers=headers, json=payload
    ).json()

    assert body["valid"] is False
    assert "drive.studentFolderIds" in body["fieldErrors"]


def test_create_generates_distinct_keys_and_will_not_overwrite(
    isolated_config: Path,
) -> None:
    client, headers = _client()

    first = client.post("/api/v1/departments", headers=headers, json=_payload())
    assert first.status_code == 201
    assert "keys" not in first.text
    target = isolated_config / "ee.yaml"
    before = target.read_bytes()
    data = yaml.safe_load(before)
    assert len(data["keys"]["staff"]) >= 24
    assert len(data["keys"]["student"]) >= 24
    assert data["keys"]["staff"] != data["keys"]["student"]
    assert data["drive"]["driveIds"] == ["DRIVE-1"]

    second = client.post("/api/v1/departments", headers=headers, json=_payload())
    assert second.status_code == 409
    assert target.read_bytes() == before


def test_secret_input_is_rejected(isolated_config: Path) -> None:
    client, headers = _client()
    payload = _payload()
    payload["keys"] = {"staff": "supplied", "student": "supplied"}

    response = client.post("/api/v1/departments", headers=headers, json=payload)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "UNSUPPORTED_SECRET_INPUT"


def test_offline_status_run_completes_without_gcloud(isolated_config: Path) -> None:
    client, headers = _client()
    assert client.post("/api/v1/departments", headers=headers, json=_payload()).status_code == 201

    started = client.post(
        "/api/v1/status-runs",
        headers=headers,
        json={"departments": ["ee"], "offline": True},
    )
    assert started.status_code == 202
    run_id = started.json()["runId"]
    for _ in range(50):
        run = client.get(f"/api/v1/status-runs/{run_id}").json()
        if run["status"] != "RUNNING":
            break
        time.sleep(0.02)

    assert run["status"] == "COMPLETED"
    result = run["departments"][0]
    assert result["code"] == "ee"
    assert any(item["layer"] == "LOCAL" for item in result["checks"])
    assert any(item["status"] == "SKIP" for item in result["checks"])


def test_status_run_cache_deduplicates_concurrent_gcloud_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0
    call_lock = threading.Lock()

    def fake_gcloud_json(args: list[str], timeout: int = 12) -> tuple[bool, dict]:
        nonlocal calls
        with call_lock:
            calls += 1
        time.sleep(0.04)
        return True, {"args": args, "timeout": timeout}

    monkeypatch.setattr(dept_gui, "_gcloud_json", fake_gcloud_json)
    cache = dept_gui._StatusRunCache()
    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(lambda _: cache.gcloud_json(["shared"]), range(4)))

    assert calls == 1
    assert all(result[0] is True for result in results)


def test_status_run_checks_departments_in_parallel(
    isolated_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    active = 0
    max_active = 0
    active_lock = threading.Lock()

    def fake_status(
        code: str, offline: bool, cache: dept_gui._StatusRunCache | None = None
    ) -> dict:
        nonlocal active, max_active
        assert offline is False
        assert cache is not None
        with active_lock:
            active += 1
            max_active = max(max_active, active)
        time.sleep(0.04)
        with active_lock:
            active -= 1
        return {"code": code, "checks": []}

    monkeypatch.setattr(dept_gui, "_run_department_status", fake_status)
    monkeypatch.setattr(dept_gui, "_warm_status_cache", lambda cache, common: None)
    run_id = "parallel-test"
    dept_gui._RUNS[run_id] = {
        "status": "RUNNING",
        "cancelRequested": False,
        "departments": [],
    }

    dept_gui._execute_run(run_id, ["ai", "cs"], False)

    assert max_active == 2
    assert dept_gui._RUNS[run_id]["status"] == "COMPLETED"
    assert [item["code"] for item in dept_gui._RUNS[run_id]["departments"]] == ["ai", "cs"]


def test_mutation_requires_local_session(isolated_config: Path) -> None:
    client = TestClient(dept_gui.app)

    response = client.post("/api/v1/departments", json=_payload())

    assert response.status_code == 403


def test_update_preserves_keys_and_hides_them_from_config_api(isolated_config: Path) -> None:
    client, headers = _client()
    assert client.post("/api/v1/departments", headers=headers, json=_payload()).status_code == 201
    target = isolated_config / "ee.yaml"
    original_keys = yaml.safe_load(target.read_text(encoding="utf-8"))["keys"]

    public = client.get("/api/v1/departments/ee/config")
    assert public.status_code == 200
    assert "keys" not in public.text

    payload = public.json()
    payload["name"] = "전자·AI공학과"
    payload["drive"]["studentFolderIds"] = ["STAFF-1", "STUDENT-1"]
    updated = client.put("/api/v1/departments/ee", headers=headers, json=payload)

    assert updated.status_code == 200
    saved = yaml.safe_load(target.read_text(encoding="utf-8"))
    assert saved["name"] == "전자·AI공학과"
    assert saved["keys"] == original_keys
    assert saved["drive"]["studentFolderIds"] == ["STAFF-1", "STUDENT-1"]


def test_update_rejects_stale_revision(isolated_config: Path) -> None:
    client, headers = _client()
    assert client.post("/api/v1/departments", headers=headers, json=_payload()).status_code == 201
    payload = client.get("/api/v1/departments/ee/config").json()
    payload["configRevision"] = "sha256:stale"

    response = client.put("/api/v1/departments/ee", headers=headers, json=payload)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "REVISION_CONFLICT"


def test_drive_service_account_status_confirms_direct_access(monkeypatch) -> None:
    monkeypatch.setattr(
        dept_gui,
        "_gcloud_json",
        lambda args: (True, {"projectNumber": "123456789"}),
    )
    monkeypatch.setattr(
        dept_gui,
        "_http_post_json",
        lambda *args, **kwargs: (200, {"accessToken": "sa-token"}, 10),
    )

    def fake_http(url: str, token: str = "", timeout: int = 10):
        if "/drives/" in url:
            return 200, {"id": "DRIVE-1", "name": "AI 공유드라이브"}, 5
        return 200, {"startPageToken": "123"}, 5

    monkeypatch.setattr(dept_gui, "_http_json", fake_http)

    result = dept_gui._drive_service_account_status(
        {"drive": {"driveIds": ["DRIVE-1"]}}, "project-test", "caller-token"
    )

    assert result["status"] == "OK"
    assert "AI 공유드라이브" in result["detail"]


def test_drive_preflight_checks_unsaved_ids(
    isolated_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, headers = _client()
    monkeypatch.setattr(dept_gui, "_gcloud_executable", lambda: "gcloud")
    monkeypatch.setattr(dept_gui, "_run_command", lambda args: (True, "caller-token"))
    monkeypatch.setattr(
        dept_gui,
        "_drive_service_account_status",
        lambda cfg, project, token: {
            "status": "OK",
            "detail": "1개 Drive SA 실접근 확인 · AI 공유드라이브",
            "latencyMs": 20,
        },
    )

    response = client.post(
        "/api/v1/departments/drive-preflight",
        headers=headers,
        json={"driveIds": ["DRIVE-1"]},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "OK"
    assert response.json()["driveIds"] == ["DRIVE-1"]
    assert "AI 공유드라이브" in response.json()["detail"]


def test_drive_preflight_requires_an_id(isolated_config: Path) -> None:
    client, headers = _client()

    response = client.post(
        "/api/v1/departments/drive-preflight", headers=headers, json={"driveIds": []}
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "DRIVE_ID_REQUIRED"


def test_drive_service_account_status_reports_missing_share(monkeypatch) -> None:
    monkeypatch.setattr(
        dept_gui,
        "_gcloud_json",
        lambda args: (True, {"projectNumber": "123456789"}),
    )
    monkeypatch.setattr(
        dept_gui,
        "_http_post_json",
        lambda *args, **kwargs: (200, {"accessToken": "sa-token"}, 10),
    )
    monkeypatch.setattr(
        dept_gui,
        "_http_json",
        lambda *args, **kwargs: (404, {}, 5),
    )

    result = dept_gui._drive_service_account_status(
        {"drive": {"driveIds": ["DRIVE-1"]}}, "project-test", "caller-token"
    )

    assert result["status"] == "FAIL"
    assert "123456789-compute@developer.gserviceaccount.com" in result["action"]
