"""로컬 학과 관리 GUI의 생성·검증·상태 API."""

from __future__ import annotations

import os
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
    dept_gui._COMMON_RESOURCE_PLANS.clear()
    dept_gui._COMMON_PROVISION_RUNS.clear()
    dept_gui._SA_REPAIR_PLANS.clear()
    dept_gui._SA_REPAIR_RUNS.clear()
    dept_gui._MCP_DEPLOY_RUNS.clear()
    dept_gui._SYNC_AUTH_TOKEN = ""
    dept_gui._SYNC_AUTH_TOKEN_EXPIRES = 0.0
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
    assert body["driveConflicts"] == []


def test_duplicate_drive_ids_are_preview_warnings_not_errors(isolated_config: Path) -> None:
    client, headers = _client()
    assert client.post("/api/v1/departments", headers=headers, json=_payload()).status_code == 201
    payload = _payload()
    payload["code"] = "cs"
    payload["name"] = "컴퓨터공학과"

    preview = client.post("/api/v1/departments/preview", headers=headers, json=payload)

    assert preview.status_code == 200
    body = preview.json()
    assert body["valid"] is True
    assert "drive.driveIds" not in body["fieldErrors"]
    assert body["driveConflicts"] == [
        {"code": "ee", "name": "전자공학과", "driveIds": ["DRIVE-1"]}
    ]
    assert any("ee" in item for item in body["warnings"])


def test_duplicate_drive_ids_require_explicit_ack_then_create(isolated_config: Path) -> None:
    client, headers = _client()
    assert client.post("/api/v1/departments", headers=headers, json=_payload()).status_code == 201
    payload = _payload()
    payload["code"] = "cs"
    payload["name"] = "컴퓨터공학과"

    blocked = client.post("/api/v1/departments", headers=headers, json=payload)
    assert blocked.status_code == 409
    error = blocked.json()["error"]
    assert error["code"] == "DRIVE_ID_CONFLICT"
    assert error["driveConflicts"][0]["code"] == "ee"
    assert not (isolated_config / "cs.yaml").exists()

    payload["allowDuplicateDriveIds"] = True
    created = client.post("/api/v1/departments", headers=headers, json=payload)
    assert created.status_code == 201
    saved = yaml.safe_load((isolated_config / "cs.yaml").read_text(encoding="utf-8"))
    assert saved["drive"]["driveIds"] == ["DRIVE-1"]


def test_duplicate_drive_ids_require_ack_on_update(isolated_config: Path) -> None:
    client, headers = _client()
    assert client.post("/api/v1/departments", headers=headers, json=_payload()).status_code == 201
    payload = _payload()
    payload["code"] = "cs"
    payload["name"] = "컴퓨터공학과"
    payload["drive"]["driveIds"] = ["DRIVE-2"]
    assert client.post("/api/v1/departments", headers=headers, json=payload).status_code == 201

    current = client.get("/api/v1/departments/cs/config").json()
    payload["drive"]["driveIds"] = ["DRIVE-1"]
    payload["configRevision"] = current["configRevision"]
    blocked = client.put("/api/v1/departments/cs", headers=headers, json=payload)
    assert blocked.status_code == 409
    assert blocked.json()["error"]["code"] == "DRIVE_ID_CONFLICT"

    payload["allowDuplicateDriveIds"] = True
    updated = client.put("/api/v1/departments/cs", headers=headers, json=payload)
    assert updated.status_code == 200
    saved = yaml.safe_load((isolated_config / "cs.yaml").read_text(encoding="utf-8"))
    assert saved["drive"]["driveIds"] == ["DRIVE-1"]


def test_single_corpus_preview_and_create_omit_student_resources(
    isolated_config: Path,
) -> None:
    client, headers = _client()
    payload = _payload()
    payload["corpusMode"] = "single"
    payload["corpora"]["student"] = ""
    payload["drive"]["studentFolderIds"] = []

    preview = client.post("/api/v1/departments/preview", headers=headers, json=payload)
    assert preview.status_code == 200
    assert preview.json()["valid"] is True
    assert "student:" not in preview.json()["yamlPreview"]

    created = client.post("/api/v1/departments", headers=headers, json=payload)
    assert created.status_code == 201
    saved = yaml.safe_load((isolated_config / "ee.yaml").read_text(encoding="utf-8"))
    assert saved["corpora"] == {"staff": payload["corpora"]["staff"]}
    assert saved["keys"].keys() == {"staff"}
    assert "studentFolderIds" not in saved["drive"]
    assert dept_config.configured_audiences("ee") == ("staff",)
    assert client.get("/api/v1/departments/ee/config").json()["corpusMode"] == "single"


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


def test_single_corpus_department_lists_only_default_mcp(
    isolated_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, headers = _client()
    payload = _payload()
    payload["corpusMode"] = "single"
    assert client.post("/api/v1/departments", headers=headers, json=payload).status_code == 201
    monkeypatch.setattr(
        dept_gui,
        "_gcloud_json",
        lambda *args, **kwargs: (
            True,
            [
                {
                    "metadata": {"name": "rag-mcp-ee-staff"},
                    "status": {
                        "url": "https://rag-mcp-ee-staff.example.run.app",
                        "conditions": [{"type": "Ready", "status": "True"}],
                    },
                }
            ],
        ),
    )

    servers = client.get("/api/v1/departments/ee/mcp-servers").json()["servers"]

    assert [item["audience"] for item in servers] == ["staff"]
    assert servers[0]["label"] == "기본"


def test_mcp_deployment_tracks_steps_and_redacts_key(
    isolated_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, headers = _client()
    payload = _payload()
    payload["corpusMode"] = "single"
    assert client.post("/api/v1/departments", headers=headers, json=payload).status_code == 201
    saved = yaml.safe_load((isolated_config / "ee.yaml").read_text(encoding="utf-8"))
    secret = saved["keys"]["staff"]

    class DeferredThread:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def start(self) -> None:
            pass

    class DeferredThreading:
        Thread = DeferredThread

    monkeypatch.setattr(dept_gui, "threading", DeferredThreading)
    run = dept_gui.start_mcp_deployment("ee")
    monkeypatch.setattr(dept_gui, "_gcloud_json", lambda *args, **kwargs: (True, {}))

    def fake_deploy(code: str, *, skip_build: bool, on_line) -> int:
        assert code == "ee"
        assert skip_build is True
        on_line(f"deploying with hidden key {secret}")
        return 0

    monkeypatch.setattr(dept_gui, "_run_mcp_deploy_script", fake_deploy)
    monkeypatch.setattr(
        dept_gui,
        "department_mcp_servers",
        lambda code: {
            "servers": [
                {
                    "serviceName": "rag-mcp-ee-staff",
                    "status": "READY",
                    "healthUrl": "https://mcp.example/health",
                }
            ]
        },
    )
    monkeypatch.setattr(dept_gui, "_http_json", lambda *args, **kwargs: (200, {"ok": True}, 5))

    dept_gui._execute_mcp_deployment(run["runId"])
    result = dept_gui._MCP_DEPLOY_RUNS[run["runId"]]

    assert result["status"] == "COMPLETED"
    assert all(step["status"] == "COMPLETE" for step in result["steps"])
    assert secret not in "\n".join(result["logs"])
    assert "***" in "\n".join(result["logs"])


def test_missing_mcp_status_offers_deployment_action(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dept_gui, "_gcloud_executable", lambda: "gcloud")

    def fake_gcloud(args: list[str], timeout: int = 12):
        service = args[3] if args[:3] == ["run", "services", "describe"] else ""
        if service == "rag-mcp-it-staff":
            return False, "NOT_FOUND: service does not exist"
        return (
            True,
            {
                "status": {
                    "url": f"https://{service}.example.run.app",
                    "latestReadyRevisionName": f"{service}-00001",
                    "latestCreatedRevisionName": f"{service}-00001",
                    "conditions": [{"type": "Ready", "status": "True"}],
                }
            },
        )

    monkeypatch.setattr(dept_gui, "_gcloud_json", fake_gcloud)
    monkeypatch.setattr(dept_gui, "_run_command", lambda *args, **kwargs: (True, "token"))
    monkeypatch.setattr(dept_gui, "_http_json", lambda *args, **kwargs: (200, {"ok": True}, 5))

    checks = dept_gui._deploy_and_runtime_status(
        "it",
        {"GCP_PROJECT_ID": "project-test", "GCP_REGION": "asia-northeast3"},
        {"corpora": {"staff": "corpus"}},
    )
    missing = next(item for item in checks if item["name"] == "mcp-it-staff")

    assert missing["status"] == "WARN"
    assert missing["actionType"] == "MCP_DEPLOY"
    assert missing["departmentCode"] == "it"


def test_mcp_deployment_api_starts_and_returns_run(
    isolated_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, headers = _client()
    expected = {
        "runId": "deploy-123",
        "code": "ee",
        "status": "RUNNING",
        "steps": [],
        "logs": [],
    }
    monkeypatch.setattr(dept_gui, "start_mcp_deployment", lambda code: expected)

    response = client.post("/api/v1/departments/ee/mcp-deployments", headers=headers)

    assert response.status_code == 202
    assert response.json() == expected


def test_mcp_key_copy_returns_only_explicit_audience_key(isolated_config: Path) -> None:
    client, headers = _client()
    assert client.post("/api/v1/departments", headers=headers, json=_payload()).status_code == 201
    saved = yaml.safe_load((isolated_config / "ee.yaml").read_text(encoding="utf-8"))

    response = client.post(
        "/api/v1/departments/ee/mcp-keys/staff",
        headers=headers,
    )

    assert response.status_code == 200
    assert response.json() == {"audience": "staff", "key": saved["keys"]["staff"]}
    assert saved["keys"]["student"] not in response.text
    assert client.get("/api/v1/departments/ee/config").json().get("keys") is None


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
    assert "answer" not in response.json()


def test_corpus_query_generate_uses_gcloud_token_and_returns_answer(
    isolated_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, headers = _client()
    assert client.post("/api/v1/departments", headers=headers, json=_payload()).status_code == 201
    monkeypatch.setattr(dept_gui, "_provision_access_token", lambda: "caller-token")
    urls: list[str] = []

    def fake_post(url: str, payload: dict, token: str = "", timeout: int = 10):
        urls.append(url)
        assert token == "caller-token"
        if url.endswith(":retrieveContexts"):
            return (
                200,
                {
                    "contexts": {
                        "contexts": [
                            {
                                "sourceDisplayName": "학사 안내.md",
                                "text": "수강신청은 2월에 합니다.",
                                "score": 0.1,
                            }
                        ]
                    }
                },
                12,
            )
        assert url.endswith(":generateContent")
        assert "locations/global/publishers/google/models/gemini-2.5-flash-lite" in url
        assert "asia-northeast3" not in url
        assert "수강신청" in payload["contents"][0]["parts"][0]["text"]
        return (
            200,
            {"candidates": [{"content": {"parts": [{"text": "수강신청은 2월입니다. [1]"}]}}]},
            40,
        )

    monkeypatch.setattr(dept_gui, "_http_post_json", fake_post)
    response = client.post(
        "/api/v1/corpus-query",
        headers=headers,
        json={"code": "ee", "audience": "staff", "query": "수강신청", "generate": True},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["answer"] == "수강신청은 2월입니다. [1]"
    assert body["answerModel"] == "gemini-2.5-flash-lite"
    assert body["answerError"] == ""
    assert len(urls) == 2


def test_corpus_query_generate_keeps_contexts_when_model_fails(
    isolated_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, headers = _client()
    assert client.post("/api/v1/departments", headers=headers, json=_payload()).status_code == 201
    monkeypatch.setattr(dept_gui, "_provision_access_token", lambda: "caller-token")

    def fake_post(url: str, payload: dict, token: str = "", timeout: int = 10):
        if url.endswith(":retrieveContexts"):
            return (
                200,
                {"contexts": {"contexts": [{"sourceDisplayName": "안내", "text": "본문"}]}},
                8,
            )
        return (403, {"error": {"message": "permission denied"}}, 5)

    monkeypatch.setattr(dept_gui, "_http_post_json", fake_post)
    response = client.post(
        "/api/v1/corpus-query",
        headers=headers,
        json={"code": "ee", "audience": "staff", "query": "일정", "generate": True},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["contexts"][0]["text"] == "본문"
    assert body["answer"] == ""
    assert "permission denied" in body["answerError"]


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
    monkeypatch.setattr(dept_gui, "_project_accessible", lambda pid: pid == "project-test")
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
            self.terminated = False

        def poll(self):
            return None

        def terminate(self) -> None:
            self.terminated = True

        def wait(self, timeout: float | None = None) -> None:
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
    if os.name == "nt":
        assert killed[0][:3] == ["taskkill.exe", "/PID", "1234"]
    else:
        assert old_process.terminated is True
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
    # 버킷도 코퍼스와 같이 학과 코드가 맨 앞이어야 한다.
    assert plan["resources"][0]["value"].startswith("cs-rag-hwp-")
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


def test_single_corpus_resource_plan_creates_only_base_corpus(
    isolated_config: Path,
) -> None:
    client, headers = _client()

    response = client.post(
        "/api/v1/departments/resource-plans",
        headers=headers,
        json={
            "code": "it",
            "name": "IT정보처",
            "corpusMode": "single",
            "resources": ["corpusStaff"],
        },
    )

    assert response.status_code == 201
    assert response.json()["resources"] == [
        {
            "key": "corpusStaff",
            "kind": "corpus",
            "label": "기본 코퍼스",
            "displayName": "it-rag-corpus",
            "value": "",
        }
    ]


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


def test_update_requires_migration_for_corpus_mode_change(isolated_config: Path) -> None:
    client, headers = _client()
    assert client.post("/api/v1/departments", headers=headers, json=_payload()).status_code == 201
    payload = client.get("/api/v1/departments/ee/config").json()
    payload["corpusMode"] = "single"
    payload["corpora"]["student"] = ""
    payload["drive"]["studentFolderIds"] = []

    response = client.put("/api/v1/departments/ee", headers=headers, json=payload)

    assert response.status_code == 422
    assert "마이그레이션" in response.json()["error"]["fieldErrors"]["corpusMode"][0]


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
    assert response.json()["driveConflicts"] == []
    assert "AI 공유드라이브" in response.json()["detail"]


def test_drive_preflight_includes_duplicate_department_conflicts(
    isolated_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, headers = _client()
    assert client.post("/api/v1/departments", headers=headers, json=_payload()).status_code == 201
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
        json={"driveIds": ["DRIVE-1"], "code": "cs"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "OK"
    assert body["driveConflicts"] == [
        {"code": "ee", "name": "전자공학과", "driveIds": ["DRIVE-1"]}
    ]


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


def test_drive_folder_info_resolves_actual_folder_name(monkeypatch) -> None:
    captured: dict[str, str] = {}

    def fake_http(url: str, token: str = "", timeout: int = 10):
        captured.update(url=url, token=token)
        return (
            200,
            {
                "id": "FOLDER_123456",
                "name": "2026 학생 자료",
                "driveId": "DRIVE_123456",
                "mimeType": dept_gui.DRIVE_FOLDER_MIME_TYPE,
                "parents": ["PARENT_123456"],
            },
            7,
        )

    monkeypatch.setattr(dept_gui, "_http_json", fake_http)

    result = dept_gui._drive_folder_info("FOLDER_123456", "sa-token")

    assert result == {
        "folderId": "FOLDER_123456",
        "status": "OK",
        "name": "2026 학생 자료",
        "driveId": "DRIVE_123456",
        "parentIds": ["PARENT_123456"],
        "latencyMs": 7,
        "reason": "",
    }
    assert "/files/FOLDER_123456?" in captured["url"]
    assert captured["token"] == "sa-token"


def test_drive_folder_info_rejects_non_folder(monkeypatch) -> None:
    monkeypatch.setattr(
        dept_gui,
        "_http_json",
        lambda *args, **kwargs: (
            200,
            {
                "id": "FILE_12345678",
                "name": "일반 문서",
                "mimeType": "application/pdf",
            },
            3,
        ),
    )

    result = dept_gui._drive_folder_info("FILE_12345678", "sa-token")

    assert result["status"] == "FAIL"
    assert result["reason"] == "폴더가 아닌 Drive 항목입니다."


def test_drive_folder_lookup_endpoint_uses_compute_service_account(
    isolated_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, headers = _client()
    monkeypatch.setattr(dept_gui, "_gcloud_executable", lambda: "gcloud")
    monkeypatch.setattr(dept_gui, "_run_command", lambda args: (True, "caller-token"))
    monkeypatch.setattr(
        dept_gui,
        "_service_account_access_token",
        lambda *args, **kwargs: ("sa-token", "compute@example.com", 5, ""),
    )
    captured: dict[str, object] = {}

    def fake_lookup(folder_ids: list[str], token: str) -> dict[str, object]:
        captured.update(folder_ids=folder_ids, token=token)
        return {
            "status": "COMPLETE",
            "folders": [
                {
                    "folderId": folder_ids[0],
                    "status": "OK",
                    "name": "교직원 문서",
                }
            ],
            "stats": {"requested": 1, "resolved": 1, "failed": 0},
        }

    monkeypatch.setattr(dept_gui, "_lookup_drive_folders", fake_lookup)
    response = client.post(
        "/api/v1/departments/folder-lookup",
        headers=headers,
        json={"folderIds": ["FOLDER_123456"]},
    )

    assert response.status_code == 200
    assert captured == {"folder_ids": ["FOLDER_123456"], "token": "sa-token"}
    assert response.json()["folders"][0]["name"] == "교직원 문서"
    assert response.json()["serviceAccount"] == "compute@example.com"


def test_drive_folder_lookup_requires_valid_ids(isolated_config: Path) -> None:
    client, headers = _client()

    response = client.post(
        "/api/v1/departments/folder-lookup",
        headers=headers,
        json={"folderIds": ["short"]},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_FOLDER_IDS"


def test_sync_execution_record_parses_manual_backfill() -> None:
    row = {
        "name": "projects/p/locations/r/workflows/rag-daily-sync/executions/ex-123",
        "state": "SUCCEEDED",
        "argument": '{"driveIds":["DRIVE_123456"],"backfill":true,"runId":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","departmentCode":"ee"}',
        "result": '{"ok":true,"totals":{"listed":12,"indexed":10}}',
        "startTime": "2026-08-27T01:00:00+00:00",
        "endTime": "2026-08-27T01:03:00+00:00",
    }

    result = dept_gui._sync_execution_record(
        row,
        {"ee": {"name": "전자공학과"}},
        {"DRIVE_123456": "ee"},
        {"phase": "COMPLETE", "processed": 12},
    )

    assert result["executionId"] == "ex-123"
    assert result["departmentName"] == "전자공학과"
    assert result["mode"] == "backfill"
    assert result["totals"] == {"listed": 12, "indexed": 10}
    assert result["progress"]["processed"] == 12


def test_firestore_sync_progress_decodes_nested_totals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        dept_gui,
        "_http_json",
        lambda *args, **kwargs: (
            200,
            {
                "fields": {
                    "phase": {"stringValue": "INGESTING"},
                    "processed": {"integerValue": "7"},
                    "totals": {
                        "mapValue": {
                            "fields": {
                                "listed": {"integerValue": "20"},
                                "indexed": {"integerValue": "5"},
                            }
                        }
                    },
                }
            },
            3,
        ),
    )

    result = dept_gui._firestore_sync_progress(
        "project-test",
        "rag-sync-state",
        "caller-token",
        "a" * 32,
    )

    assert result == {
        "phase": "INGESTING",
        "processed": 7,
        "totals": {"listed": 20, "indexed": 5},
    }


def test_start_manual_sync_scopes_workflow_to_selected_department(
    isolated_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for code, drive_id in (("ee", "DRIVE_EE_123456"), ("cs", "DRIVE_CS_123456")):
        (isolated_config / f"{code}.yaml").write_text(
            yaml.safe_dump(
                {
                    "name": code.upper(),
                    "drive": {
                        "driveIds": [drive_id],
                        "syncFolderIds": [f"FOLDER_{code.upper()}_123456"],
                        "studentFolderIds": [f"FOLDER_{code.upper()}_123456"],
                    },
                    "corpora": {"staff": "staff", "student": "student"},
                }
            ),
            encoding="utf-8",
        )
    monkeypatch.setattr(dept_gui, "_list_sync_execution_rows", lambda *a, **k: [])
    monkeypatch.setattr(dept_gui, "_gcloud_executable", lambda: "gcloud")
    monkeypatch.setattr(dept_gui, "_run_command", lambda args: (True, "caller-token"))
    monkeypatch.setattr(
        dept_gui,
        "_drive_service_account_status",
        lambda cfg, project, token: {"status": "OK", "detail": "연결됨"},
    )
    monkeypatch.setattr(
        dept_gui,
        "_cloud_run_sync_urls",
        lambda project, region: ("https://sync.example", "https://parser.example"),
    )
    monkeypatch.setattr(dept_gui.uuid, "uuid4", lambda: type("U", (), {"hex": "b" * 32})())
    captured: dict[str, object] = {}

    def fake_post(url, payload, token, timeout=10):
        captured.update(url=url, payload=payload, token=token)
        return (
            200,
            {
                "name": "projects/p/locations/r/workflows/w/executions/ex-new",
                "state": "ACTIVE",
            },
            5,
        )

    monkeypatch.setattr(dept_gui, "_http_post_json", fake_post)

    result = dept_gui._start_manual_sync("ee", "backfill")

    argument = dept_gui._json_mapping(captured["payload"]["argument"])
    assert argument["driveIds"] == ["DRIVE_EE_123456"]
    assert argument["backfill"] is True
    assert argument["departmentCode"] == "ee"
    assert argument["runId"] == "b" * 32
    assert result["state"] == "ACTIVE"
    assert result["departmentCode"] == "ee"


def test_start_sync_run_endpoint_returns_conflict(
    isolated_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, headers = _client()
    monkeypatch.setattr(
        dept_gui,
        "_start_manual_sync",
        lambda code, mode: (_ for _ in ()).throw(FileExistsError("이미 실행 중")),
    )

    response = client.post(
        "/api/v1/sync-runs",
        headers=headers,
        json={"departmentCode": "ee", "mode": "backfill"},
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "SYNC_ALREADY_RUNNING"


def _bootstrap(monkeypatch: pytest.MonkeyPatch, project: str = "project-test") -> None:
    monkeypatch.setattr(
        dept_gui,
        "_gcloud_bootstrap_state",
        lambda include_projects=True: {
            "installed": True,
            "authenticated": True,
            "account": "tester@example.com",
            "project": project,
            "projects": [{"id": project, "name": project}],
        },
    )
    # 접근 확인은 전량 목록 멤버십이 아니라 단건 조회다.
    monkeypatch.setattr(dept_gui, "_project_accessible", lambda pid: pid == project)


def _no_resources(project: str, region: str) -> dict:
    return {
        "artifactRepositories": [],
        "firestoreDatabases": [],
        "artifactError": "",
        "firestoreError": "",
    }


def _await_run(client: TestClient, run: dict) -> dict:
    deadline = time.time() + 10
    while time.time() < deadline and run["status"] == "RUNNING":
        time.sleep(0.05)
        run_id = run["runId"]
        run = client.get(f"/api/v1/common-config/resource-provisioning/{run_id}").json()
    return run


def test_common_resource_plan_reports_missing_apis_without_touching_gcp(
    isolated_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """계획 단계는 **아무것도 바꾸지 않는다** — 켤 API 와 만들 리소스만 알려준다."""
    client, headers = _client()
    _bootstrap(monkeypatch)
    monkeypatch.setattr(dept_gui, "_gcloud_project_resources", _no_resources)
    monkeypatch.setattr(dept_gui, "_gcloud_enabled_services", lambda project: (True, set()))
    # 계획 단계에서 변경 계열 호출이 일어나면 즉시 실패시킨다.
    monkeypatch.setattr(
        dept_gui,
        "_run_command",
        lambda *args, **kwargs: pytest.fail("계획 단계는 gcloud 를 변경 호출하면 안 된다"),
    )

    response = client.post(
        "/api/v1/common-config/resource-plans",
        headers=headers,
        json={"projectId": "project-test", "region": "asia-northeast3"},
    )

    assert response.status_code == 200
    body = response.json()
    assert {item["name"] for item in body["services"]} == {
        "artifactregistry.googleapis.com",
        "firestore.googleapis.com",
    }
    assert all(item["enabled"] is False for item in body["services"])
    resources = {item["key"]: item for item in body["resources"]}
    assert resources["artifactRepo"]["displayName"] == "rag-mcp"
    assert resources["firestoreDatabase"]["displayName"] == "rag-sync-state"
    assert all(item["exists"] is False for item in resources.values())
    # 위치 불가역 경고가 화면까지 전달돼야 한다.
    assert "변경할 수 없습니다" in resources["firestoreDatabase"]["warning"]


def test_common_resource_plan_marks_existing_resources_as_skipped(
    isolated_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, headers = _client()
    _bootstrap(monkeypatch)
    monkeypatch.setattr(
        dept_gui,
        "_gcloud_project_resources",
        lambda project, region: {
            "artifactRepositories": [{"id": "rag-mcp", "format": "DOCKER"}],
            "firestoreDatabases": [],
            "artifactError": "",
            "firestoreError": "",
        },
    )
    monkeypatch.setattr(
        dept_gui,
        "_gcloud_enabled_services",
        lambda project: (True, {"artifactregistry.googleapis.com"}),
    )

    body = client.post(
        "/api/v1/common-config/resource-plans",
        headers=headers,
        json={"projectId": "project-test", "region": "asia-northeast3"},
    ).json()

    resources = {item["key"]: item for item in body["resources"]}
    assert resources["artifactRepo"]["exists"] is True
    assert resources["firestoreDatabase"]["exists"] is False
    services = {item["name"]: item for item in body["services"]}
    assert services["artifactregistry.googleapis.com"]["enabled"] is True
    assert services["firestore.googleapis.com"]["enabled"] is False


def test_common_resource_plan_rejects_default_firestore_database(
    isolated_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(default) 는 Datastore 모드로 굳으면 못 되돌린다 — 계획 단계에서 막는다."""
    client, headers = _client()
    _bootstrap(monkeypatch)

    response = client.post(
        "/api/v1/common-config/resource-plans",
        headers=headers,
        json={
            "projectId": "project-test",
            "region": "asia-northeast3",
            "firestoreDatabase": "(default)",
        },
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_RESOURCE_NAME"


def test_common_provisioning_enables_apis_before_creating(
    isolated_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """API 를 먼저 켜야 생성이 된다. 순서가 뒤집히면 새 프로젝트에서 그냥 실패한다."""
    client, headers = _client()
    _bootstrap(monkeypatch)
    monkeypatch.setattr(dept_gui, "_gcloud_project_resources", _no_resources)
    monkeypatch.setattr(dept_gui, "_gcloud_enabled_services", lambda project: (True, set()))
    calls: list[str] = []
    monkeypatch.setattr(
        dept_gui,
        "_enable_gcloud_service",
        lambda service, project: calls.append(f"enable:{service}"),
    )

    def fake_repo(name: str, project: str, region: str) -> str:
        calls.append(f"repo:{name}")
        return name

    def fake_db(name: str, project: str, region: str) -> str:
        calls.append(f"db:{name}")
        return name

    monkeypatch.setattr(dept_gui, "_create_artifact_repo_resource", fake_repo)
    monkeypatch.setattr(dept_gui, "_create_firestore_database_resource", fake_db)

    plan = client.post(
        "/api/v1/common-config/resource-plans",
        headers=headers,
        json={"projectId": "project-test", "region": "asia-northeast3"},
    ).json()
    started = client.post(
        "/api/v1/common-config/resource-provisioning",
        headers=headers,
        json={"planId": plan["planId"]},
    )
    assert started.status_code == 202
    run = _await_run(client, started.json())

    assert run["status"] == "COMPLETED"
    assert calls.index("enable:artifactregistry.googleapis.com") < calls.index("repo:rag-mcp")
    assert calls.index("enable:firestore.googleapis.com") < calls.index("db:rag-sync-state")
    assert {item["status"] for item in run["resources"]} == {"COMPLETE"}


def test_common_provisioning_skips_creation_when_api_enable_fails(
    isolated_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """API 를 못 켰으면 생성을 시도하지 않는다 — 실패 원인이 뒤에서 뭉개진다."""
    client, headers = _client()
    _bootstrap(monkeypatch)
    monkeypatch.setattr(dept_gui, "_gcloud_project_resources", _no_resources)
    monkeypatch.setattr(dept_gui, "_gcloud_enabled_services", lambda project: (True, set()))

    def fake_enable(service: str, project: str) -> None:
        if service == "firestore.googleapis.com":
            raise RuntimeError("PERMISSION_DENIED")

    def fail_db(name: str, project: str, region: str) -> str:
        pytest.fail("API 활성화 실패 후에는 생성하면 안 된다")

    monkeypatch.setattr(dept_gui, "_enable_gcloud_service", fake_enable)
    monkeypatch.setattr(
        dept_gui, "_create_artifact_repo_resource", lambda name, project, region: name
    )
    monkeypatch.setattr(dept_gui, "_create_firestore_database_resource", fail_db)

    plan = client.post(
        "/api/v1/common-config/resource-plans",
        headers=headers,
        json={"projectId": "project-test", "region": "asia-northeast3"},
    ).json()
    run = _await_run(
        client,
        client.post(
            "/api/v1/common-config/resource-provisioning",
            headers=headers,
            json={"planId": plan["planId"]},
        ).json(),
    )

    assert run["status"] == "PARTIAL"
    resources = {item["key"]: item for item in run["resources"]}
    assert resources["artifactRepo"]["status"] == "COMPLETE"
    assert resources["firestoreDatabase"]["status"] == "FAILED"
    assert "firestore.googleapis.com" in resources["firestoreDatabase"]["detail"]


def test_common_provisioning_plan_is_single_use(
    isolated_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, headers = _client()
    _bootstrap(monkeypatch)
    monkeypatch.setattr(
        dept_gui,
        "_gcloud_project_resources",
        lambda project, region: {
            "artifactRepositories": [{"id": "rag-mcp", "format": "DOCKER"}],
            "firestoreDatabases": [
                {"id": "rag-sync-state", "location": "asia-northeast3", "type": "FIRESTORE_NATIVE"}
            ],
            "artifactError": "",
            "firestoreError": "",
        },
    )
    monkeypatch.setattr(
        dept_gui,
        "_gcloud_enabled_services",
        lambda project: (True, {"artifactregistry.googleapis.com", "firestore.googleapis.com"}),
    )

    plan = client.post(
        "/api/v1/common-config/resource-plans",
        headers=headers,
        json={"projectId": "project-test", "region": "asia-northeast3"},
    ).json()
    first = client.post(
        "/api/v1/common-config/resource-provisioning",
        headers=headers,
        json={"planId": plan["planId"]},
    )
    assert first.status_code == 202
    run = _await_run(client, first.json())
    # 이미 있는 것만이면 아무것도 만들지 않고 끝난다.
    assert {item["status"] for item in run["resources"]} == {"SKIPPED"}

    repeated = client.post(
        "/api/v1/common-config/resource-provisioning",
        headers=headers,
        json={"planId": plan["planId"]},
    )
    assert repeated.status_code == 409


def test_common_resource_plan_requires_local_session(isolated_config: Path) -> None:
    client, _ = _client()
    response = client.post(
        "/api/v1/common-config/resource-plans",
        json={"projectId": "project-test", "region": "asia-northeast3"},
    )
    assert response.status_code in {401, 403}


def _sa_common(monkeypatch: pytest.MonkeyPatch, project: str = "project-test") -> None:
    _bootstrap(monkeypatch, project)
    monkeypatch.setattr(
        dept_gui, "_default_compute_service_account", lambda p: "42-compute@developer.gserviceaccount.com"
    )
    monkeypatch.setattr(dept_gui, "_sync_access_token", lambda: "caller-token")


def test_drive_sa_status_reports_ok_when_impersonation_succeeds(
    isolated_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _ = _client()
    _sa_common(monkeypatch)
    monkeypatch.setattr(
        dept_gui,
        "_gcloud_enabled_services",
        lambda p: (True, {"compute.googleapis.com", "iamcredentials.googleapis.com"}),
    )
    monkeypatch.setattr(dept_gui, "_gcloud_json", lambda args, timeout=12: (True, {}))
    monkeypatch.setattr(
        dept_gui,
        "_service_account_access_token",
        lambda project, token, scopes, **kw: ("sa-token", "42-compute@x", 12, ""),
    )

    body = client.get("/api/v1/drive-service-account/status").json()

    assert body["status"] == "OK"
    assert body["issues"] == []
    assert body["serviceAccount"] == "42-compute@developer.gserviceaccount.com"


def test_drive_sa_status_flags_token_creator_when_impersonation_404(
    isolated_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """새 프로젝트에서 실제로 나온 증상 — SA 는 있는데 가장이 404 로 막힌다."""
    client, _ = _client()
    _sa_common(monkeypatch)
    monkeypatch.setattr(
        dept_gui,
        "_gcloud_enabled_services",
        lambda p: (True, {"compute.googleapis.com", "iamcredentials.googleapis.com"}),
    )
    monkeypatch.setattr(dept_gui, "_gcloud_json", lambda args, timeout=12: (True, {}))
    monkeypatch.setattr(
        dept_gui,
        "_service_account_access_token",
        lambda project, token, scopes, **kw: ("", "42-compute@x", 30, "서비스 계정 가장 토큰 HTTP 404"),
    )

    body = client.get("/api/v1/drive-service-account/status").json()

    assert body["status"] == "FAIL"
    assert "tokenCreator" in body["issues"]
    assert "404" in body["detail"]


def test_drive_sa_status_flags_compute_api_on_fresh_project(
    isolated_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Compute API 가 꺼져 있으면 기본 SA 자체가 없다 — 권한 문제와 구분해야 한다."""
    client, _ = _client()
    _sa_common(monkeypatch)
    monkeypatch.setattr(dept_gui, "_gcloud_enabled_services", lambda p: (True, set()))
    monkeypatch.setattr(dept_gui, "_gcloud_json", lambda args, timeout=12: (False, "NOT_FOUND"))

    body = client.get("/api/v1/drive-service-account/status").json()

    assert body["status"] == "FAIL"
    assert "computeApi" in body["issues"]
    # SA 가 없는 원인이 Compute API 이면 'SA 없음' 을 따로 또 세지 않는다.
    assert "serviceAccountMissing" not in body["issues"]


def test_drive_sa_repair_plan_lists_actions_without_applying(
    isolated_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, headers = _client()
    _sa_common(monkeypatch)
    monkeypatch.setattr(dept_gui, "_gcloud_enabled_services", lambda p: (True, set()))
    monkeypatch.setattr(dept_gui, "_gcloud_json", lambda args, timeout=12: (False, "NOT_FOUND"))
    monkeypatch.setattr(
        dept_gui,
        "_run_command",
        lambda *a, **k: pytest.fail("계획 단계는 아무것도 바꾸면 안 된다"),
    )

    plan = client.post("/api/v1/drive-service-account/repair-plans", headers=headers)

    assert plan.status_code == 200
    body = plan.json()
    keys = [item["key"] for item in body["steps"]]
    assert "enableCompute" in keys
    assert "grantTokenCreator" in keys
    grant = next(item for item in body["steps"] if item["key"] == "grantTokenCreator")
    assert grant["role"] == "roles/iam.serviceAccountTokenCreator"
    assert grant["member"] == "user:tester@example.com"


def test_drive_sa_repair_plan_refuses_when_already_ok(
    isolated_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, headers = _client()
    _sa_common(monkeypatch)
    monkeypatch.setattr(
        dept_gui,
        "_gcloud_enabled_services",
        lambda p: (True, {"compute.googleapis.com", "iamcredentials.googleapis.com"}),
    )
    monkeypatch.setattr(dept_gui, "_gcloud_json", lambda args, timeout=12: (True, {}))
    monkeypatch.setattr(
        dept_gui,
        "_service_account_access_token",
        lambda project, token, scopes, **kw: ("sa-token", "42-compute@x", 10, ""),
    )

    response = client.post("/api/v1/drive-service-account/repair-plans", headers=headers)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "SA_ALREADY_OK"


def test_drive_sa_repair_applies_then_reverifies(
    isolated_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """조치 후 같은 방법으로 되읽어야 한다 — 명령 성공만 믿으면 안 된다."""
    client, headers = _client()
    _sa_common(monkeypatch)
    enabled: set[str] = set()
    granted: list[str] = []
    monkeypatch.setattr(dept_gui, "_gcloud_enabled_services", lambda p: (True, set(enabled)))
    monkeypatch.setattr(
        dept_gui, "_gcloud_json", lambda args, timeout=12: (bool(enabled), {} if enabled else "NOT_FOUND")
    )
    monkeypatch.setattr(
        dept_gui, "_enable_gcloud_service", lambda service, project: enabled.add(service)
    )
    monkeypatch.setattr(
        dept_gui,
        "_grant_service_account_role",
        lambda sa, member, role, project: granted.append(role),
    )
    monkeypatch.setattr(
        dept_gui,
        "_service_account_access_token",
        lambda project, token, scopes, **kw: (
            ("sa-token", "42-compute@x", 10, "") if granted else ("", "42-compute@x", 10, "HTTP 404")
        ),
    )

    plan = client.post("/api/v1/drive-service-account/repair-plans", headers=headers).json()
    started = client.post(
        "/api/v1/drive-service-account/repairs", headers=headers, json={"planId": plan["planId"]}
    )
    assert started.status_code == 202

    run = started.json()
    deadline = time.time() + 10
    while time.time() < deadline and run["status"] == "RUNNING":
        time.sleep(0.05)
        run = client.get(f"/api/v1/drive-service-account/repairs/{run['runId']}").json()

    assert run["status"] == "COMPLETED"
    assert "roles/iam.serviceAccountTokenCreator" in granted
    assert "compute.googleapis.com" in enabled
    assert run["verification"]["status"] == "OK"


def test_drive_sa_repair_plan_is_single_use(
    isolated_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, headers = _client()
    _sa_common(monkeypatch)
    monkeypatch.setattr(dept_gui, "_gcloud_enabled_services", lambda p: (True, set()))
    monkeypatch.setattr(dept_gui, "_gcloud_json", lambda args, timeout=12: (False, "NOT_FOUND"))
    monkeypatch.setattr(dept_gui, "_enable_gcloud_service", lambda service, project: None)
    monkeypatch.setattr(
        dept_gui, "_grant_service_account_role", lambda sa, member, role, project: None
    )
    monkeypatch.setattr(
        dept_gui,
        "_service_account_access_token",
        lambda project, token, scopes, **kw: ("", "42-compute@x", 10, "HTTP 404"),
    )

    plan = client.post("/api/v1/drive-service-account/repair-plans", headers=headers).json()
    run = client.post(
        "/api/v1/drive-service-account/repairs", headers=headers, json={"planId": plan["planId"]}
    ).json()
    deadline = time.time() + 10
    while time.time() < deadline and run["status"] == "RUNNING":
        time.sleep(0.05)
        run = client.get(f"/api/v1/drive-service-account/repairs/{run['runId']}").json()

    repeated = client.post(
        "/api/v1/drive-service-account/repairs", headers=headers, json={"planId": plan["planId"]}
    )
    assert repeated.status_code == 409


def test_drive_sa_repair_requires_local_session(isolated_config: Path) -> None:
    client, _ = _client()
    response = client.post("/api/v1/drive-service-account/repair-plans")
    assert response.status_code in {401, 403}


def test_session_reports_common_existence_without_touching_gcloud(
    isolated_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """부팅 첫 호출은 gcloud 를 타면 안 된다 — 여기서 막히면 화면이 통째로 늦다.

    공통 설정 화면을 띄울지는 파일 존재 하나로 정해진다. 이 경로에 gcloud 왕복이
    끼어들면 수 초짜리 흰 화면이 된다(실측 9.1s -> 0.01s 로 고친 지점).
    """
    monkeypatch.setattr(
        dept_gui, "_run_command", lambda *a, **k: pytest.fail("세션 응답에 gcloud 가 끼면 안 된다")
    )
    monkeypatch.setattr(
        dept_gui, "_gcloud_json", lambda *a, **k: pytest.fail("세션 응답에 gcloud 가 끼면 안 된다")
    )
    client, _ = _client()

    body = client.get("/api/v1/session").json()

    assert body["commonExists"] is True
    assert body["nonce"]

    (dept_gui.CONFIG_DIR / "common.yaml").unlink()
    assert client.get("/api/v1/session").json()["commonExists"] is False


def test_bootstrap_state_runs_independent_gcloud_calls_concurrently(
    isolated_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """독립 호출은 동시에 돌아야 한다. 순차면 gcloud 왕복이 그대로 더해진다."""
    monkeypatch.setattr(dept_gui, "_gcloud_executable", lambda: "gcloud.cmd")
    overlap = {"peak": 0, "live": 0}
    lock = threading.Lock()

    def slow(fn_result):
        def run(*args, **kwargs):
            with lock:
                overlap["live"] += 1
                overlap["peak"] = max(overlap["peak"], overlap["live"])
            time.sleep(0.15)
            with lock:
                overlap["live"] -= 1
            return fn_result

        return run

    monkeypatch.setattr(
        dept_gui, "_gcloud_json", slow((True, [{"account": "tester@example.com"}]))
    )
    monkeypatch.setattr(dept_gui, "_run_command", slow((True, "proj-a")))

    started = time.time()
    state = dept_gui._gcloud_bootstrap_state(include_projects=False)
    elapsed = time.time() - started

    assert state["authenticated"] is True
    # 3건이 순차면 0.45s 이상 걸린다. 동시에 돌면 0.15s 언저리다.
    assert overlap["peak"] >= 2, "독립 호출이 순차로 돌고 있다"
    assert elapsed < 0.4, f"병렬이 아니다: {elapsed:.2f}s"


def test_bootstrap_state_discards_project_list_when_unauthenticated(
    isolated_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """projects list 를 인증 확인 전에 띄우더라도, 미인증이면 결과를 쓰면 안 된다."""
    monkeypatch.setattr(dept_gui, "_gcloud_executable", lambda: "gcloud.cmd")

    def fake_json(args, timeout=12):
        if args[:2] == ["auth", "list"]:
            return True, []
        return True, [{"projectId": "leaked", "name": "leaked"}]

    monkeypatch.setattr(dept_gui, "_gcloud_json", fake_json)
    monkeypatch.setattr(dept_gui, "_run_command", lambda *a, **k: (False, ""))

    state = dept_gui._gcloud_bootstrap_state(include_projects=True)

    assert state["authenticated"] is False
    assert state["projects"] == []


def test_bootstrap_state_does_not_enumerate_every_project(
    isolated_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """프로젝트 전량 조회를 하면 안 된다 — 수천 개 계정에서 6초를 먹던 지점이다."""
    monkeypatch.setattr(dept_gui, "_gcloud_executable", lambda: "gcloud.cmd")

    def fake_json(args, timeout=12):
        if args[:2] == ["projects", "list"]:
            pytest.fail("프로젝트 전량 조회가 되살아났다")
        return True, [{"account": "tester@example.com"}]

    monkeypatch.setattr(dept_gui, "_gcloud_json", fake_json)
    monkeypatch.setattr(dept_gui, "_run_command", lambda *a, **k: (True, "proj-current"))

    state = dept_gui._gcloud_bootstrap_state(include_projects=True)

    assert state["authenticated"] is True
    # 현재 프로젝트 하나만 싣는다. 나머지는 검색으로 찾는다.
    assert state["projects"] == [{"id": "proj-current", "name": "proj-current"}]


def test_project_search_merges_id_and_name_matches(
    isolated_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ID 와 표시 이름 중 어디에 맞을지 모르므로 둘 다 묻고 합친다."""
    monkeypatch.setattr(dept_gui, "_sync_access_token", lambda: "token")
    seen: list[str] = []

    def fake_http(url, token, timeout=15):
        seen.append(url)
        if "id%3A" in url or "id:" in url:
            return 200, {"projects": [{"projectId": "tuk-mcp-rag", "name": "RAG"}]}, 10
        return 200, {"projects": [{"projectId": "other-rag", "name": "rag-thing"}]}, 10

    monkeypatch.setattr(dept_gui, "_http_json", fake_http)

    result = dept_gui.search_projects("rag")

    ids = [item["id"] for item in result["projects"]]
    assert ids == ["other-rag", "tuk-mcp-rag"] or ids == ["tuk-mcp-rag", "other-rag"]
    assert len(seen) == 2, "ID 와 이름을 둘 다 물어야 한다"


def test_project_search_dedupes_and_prefers_prefix_match(
    isolated_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(dept_gui, "_sync_access_token", lambda: "token")
    monkeypatch.setattr(
        dept_gui,
        "_http_json",
        lambda url, token, timeout=15: (
            200,
            {
                "projects": [
                    {"projectId": "zzz-tuk-old", "name": "old"},
                    {"projectId": "tuk-mcp-rag", "name": "RAG"},
                ]
            },
            10,
        ),
    )

    result = dept_gui.search_projects("tuk")

    ids = [item["id"] for item in result["projects"]]
    assert ids == ["tuk-mcp-rag", "zzz-tuk-old"], "접두어 일치가 위로 와야 한다"


def test_project_search_skips_inactive_projects(
    isolated_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(dept_gui, "_sync_access_token", lambda: "token")
    monkeypatch.setattr(
        dept_gui,
        "_http_json",
        lambda url, token, timeout=15: (
            200,
            {
                "projects": [
                    {"projectId": "gone", "name": "gone", "lifecycleState": "DELETE_REQUESTED"},
                    {"projectId": "live", "name": "live", "lifecycleState": "ACTIVE"},
                ]
            },
            10,
        ),
    )

    result = dept_gui.search_projects("l")

    assert [item["id"] for item in result["projects"]] == ["live"]


def test_project_search_without_term_does_not_call_gcp(
    isolated_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """검색어 없이 상위 N개를 뿌리면 sys-* 가 앞을 채워 쓸모가 없다."""
    monkeypatch.setattr(dept_gui, "_sync_access_token", lambda: "token")
    monkeypatch.setattr(
        dept_gui, "_http_json", lambda *a, **k: pytest.fail("빈 검색어로 GCP 를 부르면 안 된다")
    )
    client, _ = _client()

    body = client.get("/api/v1/projects/search?q=").json()

    assert body["projects"] == []


def test_project_accessible_checks_one_project_not_the_whole_list(
    isolated_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """전량 목록 멤버십 대신 단건 확인 — 빠르고, 목록 누락으로 인한 오탐이 없다."""
    monkeypatch.setattr(dept_gui, "_sync_access_token", lambda: "token")
    calls: list[str] = []

    def fake_http(url, token, timeout=15):
        calls.append(url)
        if url.endswith("/project-ok"):
            return 200, {"projectId": "project-ok", "lifecycleState": "ACTIVE"}, 10
        return 403, {}, 10

    monkeypatch.setattr(dept_gui, "_http_json", fake_http)

    assert dept_gui._project_accessible("project-ok") is True
    assert dept_gui._project_accessible("project-no") is False
    # 형식이 틀리면 왕복조차 하지 않는다.
    assert dept_gui._project_accessible("BAD ID") is False
    assert len(calls) == 2
