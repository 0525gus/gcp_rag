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
    dept_gui._RESOURCE_PLANS.clear()
    dept_gui._PROVISION_RUNS.clear()
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


def test_department_mcp_servers_return_actual_cloud_run_urls(
    isolated_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, headers = _client()
    assert client.post("/api/v1/departments", headers=headers, json=_payload()).status_code == 201

    def fake_gcloud(args: list[str], timeout: int = 12):
        assert args[:3] == ["run", "services", "list"]
        return (
            True,
            [
                {
                    "metadata": {"name": "rag-mcp-ee-staff"},
                    "status": {
                        "url": "https://rag-mcp-ee-staff.example.run.app",
                        "conditions": [{"type": "Ready", "status": "True"}],
                    },
                },
                {
                    "metadata": {"name": "rag-mcp-ee-student"},
                    "status": {
                        "url": "https://rag-mcp-ee-student.example.run.app",
                        "conditions": [{"type": "Ready", "status": "True"}],
                    },
                },
            ],
        )

    monkeypatch.setattr(dept_gui, "_gcloud_json", fake_gcloud)

    response = client.get("/api/v1/departments/ee/mcp-servers")

    assert response.status_code == 200
    servers = response.json()["servers"]
    assert [item["audience"] for item in servers] == ["staff", "student"]
    assert all(item["status"] == "READY" for item in servers)
    assert servers[0]["url"] == "https://rag-mcp-ee-staff.example.run.app"
    assert servers[1]["healthUrl"].endswith("/health")


def test_corpus_query_uses_selected_department_corpus(
    isolated_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, headers = _client()
    assert client.post("/api/v1/departments", headers=headers, json=_payload()).status_code == 201
    captured: dict = {}
    monkeypatch.setattr(dept_gui, "_provision_access_token", lambda: "caller-token")

    def fake_post(url: str, payload: dict, token: str = "", timeout: int = 10):
        captured.update(url=url, payload=payload, token=token, timeout=timeout)
        return (
            200,
            {
                "contexts": {
                    "contexts": [
                        {
                            "sourceDisplayName": "학사 안내.md",
                            "sourceUri": "gs://source/notice.md",
                            "text": "수강신청 안내 본문",
                            "score": 0.12,
                        }
                    ]
                }
            },
            12,
        )

    monkeypatch.setattr(dept_gui, "_http_post_json", fake_post)
    response = client.post(
        "/api/v1/corpus-query",
        headers=headers,
        json={"code": "ee", "audience": "student", "query": "수강신청", "topK": 3},
    )

    assert response.status_code == 200
    assert response.json()["contexts"][0]["text"] == "수강신청 안내 본문"
    assert captured["token"] == "caller-token"
    assert captured["payload"]["vertexRagStore"]["ragResources"][0]["ragCorpus"].endswith(
        "/student-1"
    )
    assert captured["payload"]["query"]["ragRetrievalConfig"]["topK"] == 3


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


def test_department_code_availability_blocks_existing_code(isolated_config: Path) -> None:
    client, headers = _client()
    assert client.post("/api/v1/departments", headers=headers, json=_payload()).status_code == 201

    response = client.get("/api/v1/departments/code-availability?code=EE")

    assert response.status_code == 200
    assert response.json()["code"] == "ee"
    assert response.json()["available"] is False
    assert "이미" in response.json()["reason"]

    plan = client.post(
        "/api/v1/departments/resource-plans",
        headers=headers,
        json={"code": "ee", "name": "중복 학과", "resources": ["bucketHwp"]},
    )
    assert plan.status_code == 409
    assert plan.json()["error"]["code"] == "DEPARTMENT_CODE_EXISTS"


def test_resource_plan_and_provision_run_create_selected_resources(
    isolated_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, headers = _client()
    created_buckets: list[tuple[str, str, str]] = []
    created_corpora: list[tuple[str, str, str]] = []
    monkeypatch.setattr(dept_gui, "_provision_access_token", lambda: "caller-token")

    def fake_bucket(name: str, project: str, region: str) -> str:
        created_buckets.append((name, project, region))
        return name

    def fake_corpus(display_name: str, project: str, region: str, token: str) -> str:
        assert token == "caller-token"
        created_corpora.append((display_name, project, region))
        corpus_id = "staff-created" if "교직원" in display_name else "student-created"
        return f"projects/{project}/locations/{region}/ragCorpora/{corpus_id}"

    monkeypatch.setattr(dept_gui, "_create_bucket_resource", fake_bucket)
    monkeypatch.setattr(dept_gui, "_create_corpus_resource", fake_corpus)

    planned = client.post(
        "/api/v1/departments/resource-plans",
        headers=headers,
        json={
            "code": "cs",
            "name": "컴퓨터공학부",
            "resources": ["bucketHwp", "bucketSource", "corpusStaff", "corpusStudent"],
        },
    )
    assert planned.status_code == 201
    plan = planned.json()
    assert len(plan["resources"]) == 4
    assert plan["resources"][0]["value"].startswith("rag-cs-hwp-")
    corpus_names = {
        item["key"]: item["displayName"]
        for item in plan["resources"]
        if item["kind"] == "corpus"
    }
    assert corpus_names == {
        "corpusStaff": "cs-rag-corpus-staff",
        "corpusStudent": "cs-rag-corpus-student",
    }
    assert plan["bucketProtection"]["publicAccessPrevention"] == "enforced"

    overrides = {
        "bucketHwp": "custom-cs-hwp",
        "bucketSource": "custom-cs-source",
        "corpusStaff": "cs-rag-corpus",
        "corpusStudent": "cs-rag-corpus-student-custom",
    }
    started = client.post(
        "/api/v1/departments/resource-provisioning",
        headers=headers,
        json={"planId": plan["planId"], "overrides": overrides},
    )
    assert started.status_code == 202
    run_id = started.json()["runId"]
    for _ in range(50):
        run = client.get(f"/api/v1/departments/resource-provisioning/{run_id}").json()
        if run["status"] != "RUNNING":
            break
        time.sleep(0.01)

    assert run["status"] == "COMPLETED"
    assert all(item["status"] == "COMPLETE" for item in run["resources"])
    assert len(created_buckets) == 2
    assert len(created_corpora) == 2
    assert {item[0] for item in created_buckets} == {"custom-cs-hwp", "custom-cs-source"}
    assert {item[0] for item in created_corpora} == {
        "cs-rag-corpus",
        "cs-rag-corpus-student-custom",
    }
    assert {item[1] for item in created_buckets} == {"project-test"}


def test_corpus_creation_uses_multilingual_embedding(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    def fake_post(url: str, payload: dict, token: str = "", timeout: int = 10):
        captured.update(payload)
        return (
            200,
            {
                "done": True,
                "response": {
                    "name": "projects/project-test/locations/asia-northeast3/ragCorpora/created"
                },
            },
            5,
        )

    monkeypatch.setattr(dept_gui, "_http_post_json", fake_post)

    name = dept_gui._create_corpus_resource(
        "컴퓨터공학부 · 교직원", "project-test", "asia-northeast3", "caller-token"
    )

    assert name.endswith("/ragCorpora/created")
    endpoint = captured["vectorDbConfig"]["ragEmbeddingModelConfig"][
        "vertexPredictionEndpoint"
    ]["endpoint"]
    assert endpoint == (
        "projects/project-test/locations/asia-northeast3/publishers/google/models/"
        "text-multilingual-embedding-002"
    )
    assert captured["vectorDbConfig"]["ragManagedDb"] == {}
    assert "ragVectorDbConfig" not in captured


def test_corpus_creation_surfaces_google_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        dept_gui,
        "_http_post_json",
        lambda *args, **kwargs: (
            400,
            {"error": {"message": "Embedding model is not available in this location."}},
            5,
        ),
    )

    with pytest.raises(RuntimeError, match="Embedding model is not available"):
        dept_gui._create_corpus_resource(
            "cs-rag-corpus-staff", "project-test", "asia-northeast3", "caller-token"
        )


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
