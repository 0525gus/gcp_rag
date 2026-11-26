from __future__ import annotations

import base64
import copy
import io
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

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
                "MCP_UNIFIED_ENABLED": True,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(dept_gui, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(dept_config, "CONFIG_DIR", config_dir)
    departments: dict[str, dict] = {}
    record = {"serviceUrl": "https://shared-mcp.example", "revision": "v1"}

    def mutate(method: str, code: str, argument=None):
        if method == "update_department":
            replacement = copy.deepcopy(argument)
            for flag in ("mcpDisabledAudiences", "syncDisabled"):
                if flag not in replacement and flag in departments.get(code, {}):
                    replacement[flag] = copy.deepcopy(departments[code][flag])
            departments[code] = replacement
            (dept_dir / f"{code}.yaml").write_text(
                yaml.safe_dump(replacement, allow_unicode=True), encoding="utf-8"
            )
        elif method == "disable_audience":
            disabled = set(departments[code].get("mcpDisabledAudiences", []))
            departments[code]["mcpDisabledAudiences"] = sorted(disabled | {argument})
        elif method == "remove_department":
            departments.pop(code, None)
            (dept_dir / f"{code}.yaml").unlink(missing_ok=True)

    monkeypatch.setattr(
        dept_gui,
        "_mcp_registry_read",
        lambda **_kwargs: (copy.deepcopy(record), copy.deepcopy(departments)),
    )
    monkeypatch.setattr(dept_gui, "_mcp_registry_mutate", mutate)
    monkeypatch.setattr(
        dept_gui,
        "_unified_mcp_service",
        lambda: {
            "status": {
                "url": "https://shared-mcp.example",
                "conditions": [{"type": "Ready", "status": "True"}],
                "latestReadyRevisionName": "rag-mcp-00001",
            }
        },
    )
    monkeypatch.setattr(dept_gui, "_deployed_sync_department_map", dict)
    monkeypatch.setattr(dept_config, "registry_configs", lambda: copy.deepcopy(departments))
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
    dept_gui._MCP_DEPLOY_CONFIGS.clear()
    dept_gui._COMMON_RUNTIME_DEPLOY_RUNS.clear()
    dept_gui._TEARDOWN_RUNS.clear()
    dept_gui._SYNC_AUTH_TOKEN = ""
    dept_gui._SYNC_AUTH_TOKEN_EXPIRES = 0.0
    dept_gui._SYNC_TARGET_CACHE.clear()
    dept_gui._CLOUD_DEPARTMENT_CONFIG_CACHE.clear()
    dept_gui._MCP_REGISTRY_READ_CACHE.clear()
    dept_gui._UNIFIED_MCP_SERVICE_CACHE.clear()
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


def _cloud_v2_service(code: str = "ee") -> tuple[dict, dict]:
    config = {
        "name": "전자공학과",
        "corpora": {
            "staff": "projects/project-test/locations/asia-northeast3/ragCorpora/staff-1",
            "student": "projects/project-test/locations/asia-northeast3/ragCorpora/student-1",
        },
        "keys": {"staff": "cloud-staff-secret-key-value", "student": "cloud-student-secret-key-value"},
        "buckets": {"hwpOriginal": "rag-ee-hwp-project-test", "source": "rag-ee-source-project-test"},
        "drive": {
            "driveIds": ["DRIVE-1"],
            "syncFolderIds": ["STAFF-1", "STUDENT-1"],
            "studentFolderIds": ["STUDENT-1"],
        },
        "minInstances": {"staff": 1, "student": 0},
        "futureOption": {"enabled": True},
    }
    metadata = {
        "schemaVersion": 2,
        "managedBy": "gcp-rag",
        "code": code,
        "name": config["name"],
        "audience": "staff",
        "corpusMode": "split",
        "yaml": config,
    }
    encoded = base64.urlsafe_b64encode(
        json.dumps(metadata, ensure_ascii=False).encode("utf-8")
    ).decode("ascii")
    service = {
        "metadata": {"name": f"rag-mcp-{code}-staff"},
        "spec": {"template": {"metadata": {"annotations": {dept_gui.DEPLOYMENT_METADATA_ANNOTATION: encoded}}}},
        "status": {"url": f"https://rag-mcp-{code}-staff.example.run"},
    }
    return config, service


class _DeferredThread:
    def __init__(self, *args, **kwargs) -> None:
        pass

    def start(self) -> None:
        pass


class _DeferredThreading:
    Thread = _DeferredThread


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


def test_common_runtime_deployment_tracks_steps_and_follow_up_department(
    isolated_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, headers = _client()
    assert client.post("/api/v1/departments", headers=headers, json=_payload()).status_code == 201

    monkeypatch.setattr(dept_gui, "threading", _DeferredThreading)
    run = dept_gui.start_common_runtime_deployment("ee")

    def fake_deploy(*, on_line, env_only=False) -> int:
        for line in (
            "== Build & push images ==",
            "== Deploy Cloud Run ==",
            "== Deploy Workflow ==",
            "== Ensure Scheduler SA / App Engine ==",
            "== Cloud Scheduler (00:00 Asia/Seoul) ==",
        ):
            on_line(line)
        return 0

    monkeypatch.setattr(dept_gui, "_run_common_runtime_deploy_script", fake_deploy)
    monkeypatch.setattr(
        dept_gui,
        "_verify_common_runtime_deployment",
        lambda: [
            {"serviceName": "rag-parser", "url": "https://parser.example"},
            {"serviceName": "rag-sync", "url": "https://sync.example"},
        ],
    )

    dept_gui._execute_common_runtime_deployment(run["runId"])
    result = dept_gui._COMMON_RUNTIME_DEPLOY_RUNS[run["runId"]]

    assert result["status"] == "COMPLETED"
    assert result["followUpDepartmentCode"] == "ee"
    assert all(step["status"] == "COMPLETE" for step in result["steps"])
    assert [item["serviceName"] for item in result["services"]] == ["rag-parser", "rag-sync"]


def test_common_runtime_deploy_script_reuses_existing_images(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, list[str]] = {}

    class FakeProcess:
        stdout = io.StringIO("")
        returncode = 0

        def wait(self) -> int:
            return 0

    def fake_popen(args, **kwargs):
        captured["args"] = list(args)
        return FakeProcess()

    monkeypatch.setattr(dept_gui.shutil, "which", lambda name: "pwsh.exe")
    monkeypatch.setattr(dept_gui.subprocess, "Popen", fake_popen)
    dept_gui._run_common_runtime_deploy_script(on_line=lambda line: None)

    assert "-SkipMcp" in captured["args"]
    assert "-ReuseExisting" in captured["args"]


def test_missing_common_runtime_status_offers_deployment_action(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(dept_gui, "_gcloud_executable", lambda: "gcloud")

    def fake_gcloud(args: list[str], timeout: int = 12):
        service = args[3] if args[:3] == ["run", "services", "describe"] else ""
        if service in {"rag-parser", "rag-sync"}:
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
    parser = next(item for item in checks if item["name"] == "parser")
    sync = next(item for item in checks if item["name"] == "sync")

    assert parser["status"] == "WARN"
    assert parser["actionType"] == "COMMON_RUNTIME_DEPLOY"
    assert sync["actionType"] == "COMMON_RUNTIME_DEPLOY"


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


def test_common_runtime_deployment_api_starts_and_returns_run(
    isolated_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, headers = _client()
    expected = {
        "runId": "runtime-123",
        "status": "RUNNING",
        "followUpDepartmentCode": "ee",
        "steps": [],
        "logs": [],
    }
    monkeypatch.setattr(
        dept_gui, "start_common_runtime_deployment", lambda code, env_only=False: expected
    )

    response = client.post(
        "/api/v1/common-runtime-deployments?followUpDepartmentCode=ee",
        headers=headers,
    )

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
    assert saved == {
        "GCP_PROJECT_ID": "project-test",
        "GCP_REGION": "asia-northeast3",
        "ARTIFACT_REPO": "rag-mcp",
        "FIRESTORE_DATABASE": "rag-sync-state",
    }
    env = dept_config.build_env_from_config(
        "cs", "staff", {"corpora": {"staff": "corpus/staff"}, "keys": {"staff": "S" * 30}}
    )
    assert env["CLOUD_TASKS_ENABLED"] == "true"
    assert env["RAG_MAPPING_WRITE_ENABLED"] == "true"
    assert env["TOP_K_DEFAULT"] == "7"
    assert env["SEARCH_FETCH_MULTIPLIER"] == "9"
    assert env["ALLOW_UNAUTH"] == "true"
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


def test_cloud_only_department_status_uses_annotation_config_without_local_yaml(
    isolated_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, _service = _cloud_v2_service()
    revision = dept_gui._mapping_revision(config)
    monkeypatch.setattr(
        dept_gui,
        "cloud_department_status_config",
        lambda code: (config, revision, True),
    )

    result = dept_gui._run_department_status("ee", offline=True)

    assert result["configRevision"] == revision
    assert any(
        item["name"] == "cloud-metadata" and item["status"] == "OK"
        for item in result["checks"]
    )
    assert any(item["name"] == "offline" for item in result["checks"])


def test_status_run_endpoint_accepts_cloud_only_department(
    isolated_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, _service = _cloud_v2_service()
    revision = dept_gui._mapping_revision(config)
    monkeypatch.setattr(
        dept_gui,
        "cloud_department_status_config",
        lambda code: (config, revision, True),
    )
    client, headers = _client()

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
    assert run["scope"] == ["ee"]
    assert run["departments"][0]["code"] == "ee"


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


def test_registry_read_uses_short_cache_and_mutation_invalidates_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "common.yaml").write_text(
        yaml.safe_dump({"GCP_PROJECT_ID": "project-test", "GCP_REGION": "asia-northeast3"}),
        encoding="utf-8",
    )
    monkeypatch.setattr(dept_gui, "CONFIG_DIR", config_dir)
    dept_gui._MCP_REGISTRY_READ_CACHE.clear()
    reads = 0
    mutations: list[tuple] = []

    class Admin:
        def read(self):
            nonlocal reads
            reads += 1
            return object(), {"revision": f"v{reads}"}, {"ee": {"name": "전자공학과"}}

        def update_department(self, *args):
            mutations.append(args)

    monkeypatch.setattr(dept_gui, "_mcp_registry_admin", Admin)

    first = dept_gui._mcp_registry_read()
    second = dept_gui._mcp_registry_read()
    assert first == second
    assert reads == 1

    dept_gui._mcp_registry_mutate("update_department", "ee", {"name": "변경"})
    refreshed = dept_gui._mcp_registry_read()
    assert mutations == [("ee", {"name": "변경"})]
    assert reads == 2
    assert refreshed[0]["revision"] == "v2"


def test_unified_mcp_service_uses_short_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "common.yaml").write_text(
        yaml.safe_dump({"GCP_PROJECT_ID": "project-test", "GCP_REGION": "asia-northeast3"}),
        encoding="utf-8",
    )
    monkeypatch.setattr(dept_gui, "CONFIG_DIR", config_dir)
    dept_gui._UNIFIED_MCP_SERVICE_CACHE.clear()
    calls = 0

    def fake_gcloud(args, timeout=12):
        nonlocal calls
        calls += 1
        return True, {"status": {"url": "https://mcp.example"}, "call": calls}

    monkeypatch.setattr(dept_gui, "_gcloud_json", fake_gcloud)

    first = dept_gui._unified_mcp_service()
    second = dept_gui._unified_mcp_service()
    assert first == second
    assert calls == 1


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
    monkeypatch.setattr(
        dept_gui, "_run_command", lambda args, timeout=12: (True, "caller-token")
    )
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


def test_drive_folder_children_lists_one_level(monkeypatch) -> None:
    captured: dict[str, str] = {}

    def fake_http(url: str, token: str = "", timeout: int = 10):
        captured.update(url=url, token=token)
        return 200, {
            "files": [
                {
                    "id": "FOLDER_CHILD_123",
                    "name": "교직원 자료",
                    "driveId": "DRIVE_123456",
                    "parents": ["FOLDER_PARENT_123"],
                    "mimeType": dept_gui.DRIVE_FOLDER_MIME_TYPE,
                }
            ]
        }, 8

    monkeypatch.setattr(dept_gui, "_http_json", fake_http)
    result = dept_gui._drive_folder_children(
        "DRIVE_123456", "FOLDER_PARENT_123", "sa-token"
    )

    assert result["folders"] == [
        {
            "folderId": "FOLDER_CHILD_123",
            "name": "교직원 자료",
            "driveId": "DRIVE_123456",
            "parentIds": ["FOLDER_PARENT_123"],
        }
    ]
    assert result["truncated"] is False
    assert "corpora=drive" in captured["url"]
    assert "includeItemsFromAllDrives=true" in captured["url"]
    assert captured["token"] == "sa-token"


def test_drive_folder_children_endpoint_uses_compute_service_account(
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
    captured: dict[str, str] = {}

    def fake_children(drive_id: str, parent_id: str, token: str) -> dict[str, object]:
        captured.update(drive_id=drive_id, parent_id=parent_id, token=token)
        return {
            "driveId": drive_id,
            "parentId": parent_id,
            "folders": [],
            "truncated": False,
        }

    monkeypatch.setattr(dept_gui, "_drive_folder_children", fake_children)
    response = client.post(
        "/api/v1/departments/drive-folders",
        headers=headers,
        json={"driveId": "DRIVE_123456", "parentId": "FOLDER_PARENT_123"},
    )

    assert response.status_code == 200
    assert captured == {
        "drive_id": "DRIVE_123456",
        "parent_id": "FOLDER_PARENT_123",
        "token": "sa-token",
    }
    assert response.json()["serviceAccount"] == "compute@example.com"


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


def test_sync_history_reports_missing_workflow_as_empty_state(
    isolated_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _headers = _client()
    monkeypatch.setattr(
        dept_gui,
        "_common",
        lambda: {"GCP_PROJECT_ID": "project-test", "GCP_REGION": "asia-northeast3"},
    )
    monkeypatch.setattr(
        dept_gui,
        "_list_sync_execution_rows",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            dept_gui.WorkflowNotFoundError("rag-daily-sync 워크플로우가 아직 배포되지 않았습니다.")
        ),
    )
    monkeypatch.setattr(
        dept_gui,
        "_sync_department_targets",
        lambda: ({"ee": {"code": "ee", "name": "전자공학과"}}, {}),
    )

    response = client.get("/api/v1/sync-runs")

    assert response.status_code == 200
    body = response.json()
    assert body["workflowStatus"] == "NOT_FOUND"
    assert body["runs"] == []
    assert body["departments"][0]["code"] == "ee"


def test_workflow_not_found_is_distinguished_from_lookup_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        dept_gui,
        "_gcloud_json",
        lambda args, timeout=12: (
            False,
            (
                'ERROR: NOT_FOUND: workflow "projects/1/locations/r/workflows/rag-daily-sync" '
                "does not exist"
            ),
        ),
    )

    with pytest.raises(dept_gui.WorkflowNotFoundError):
        dept_gui._list_sync_execution_rows("project-test", "asia-northeast3")


def test_active_sync_rows_are_described_before_duplicate_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def fake_gcloud(args, timeout=12):
        calls.append(args)
        if args[:3] == ["workflows", "executions", "list"]:
            return True, [
                {
                    "name": "projects/p/locations/r/workflows/w/executions/ex-active",
                    "state": "ACTIVE",
                }
            ]
        return True, {
            "name": "projects/p/locations/r/workflows/w/executions/ex-active",
            "state": "ACTIVE",
            "argument": '{"driveIds":["DRIVE_CS_123456"],"backfill":true,"departmentCode":"cs"}',
            "labels": {"department": "cs", "mode": "backfill"},
        }

    monkeypatch.setattr(dept_gui, "_gcloud_json", fake_gcloud)
    rows = dept_gui._list_sync_execution_rows("project-test", "asia-northeast3")

    assert dept_gui._execution_arguments(rows[0])["driveIds"] == ["DRIVE_CS_123456"]
    assert rows[0]["labels"]["department"] == "cs"
    assert any(call[:3] == ["workflows", "executions", "describe"] for call in calls)


def test_completed_sync_rows_are_described_for_result_and_labels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def fake_gcloud(args, timeout=12):
        calls.append(args)
        if args[:3] == ["workflows", "executions", "list"]:
            return True, [
                {
                    "name": "projects/p/locations/r/workflows/w/executions/ex-done",
                    "state": "SUCCEEDED",
                }
            ]
        return True, {
            "name": "projects/p/locations/r/workflows/w/executions/ex-done",
            "state": "SUCCEEDED",
            "argument": '{"driveIds":["DRIVE_CS_123456"],"departmentCode":"cs"}',
            "labels": {"department": "cs", "mode": "delta"},
            "result": '{"ok":false,"totals":{"listed":25,"indexed":0,"indexFailed":2}}',
        }

    monkeypatch.setattr(dept_gui, "_gcloud_json", fake_gcloud)
    rows = dept_gui._list_sync_execution_rows("project-test", "asia-northeast3")
    record = dept_gui._sync_execution_record(
        rows[0], {"cs": {"name": "컴퓨터공학부"}}, {"DRIVE_CS_123456": "cs"}
    )

    assert record["departmentName"] == "컴퓨터공학부"
    assert record["ok"] is False
    assert record["totals"] == {"listed": 25, "indexed": 0, "indexFailed": 2}
    assert any(call[:3] == ["workflows", "executions", "describe"] for call in calls)


def test_sync_workflow_logs_normalizes_stage_message_and_file_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = (
        'index-gcs failed drive=DRIVE uris=1 {"body":{"detail":['
        '{"input":["gs://bucket/1YUj8gsDZcRuU4b0Pb_jqr_LYVoKai6kW.md"],'
        '"msg":"Input should be a valid string"}]},"code":422}'
    )
    captured: dict[str, object] = {}

    def fake_post(url, payload, token="", timeout=10):
        captured.update(url=url, payload=payload, token=token)
        return (
            200,
            {
                "entries": [
                {
                    "timestamp": "2026-08-30T14:51:17Z",
                    "severity": "ERROR",
                    "textPayload": raw,
                }
                ]
            },
            12,
        )

    monkeypatch.setattr(dept_gui, "_http_post_json", fake_post)

    logs = dept_gui._sync_workflow_logs(
        "project-test",
        "52c4c7ab-da3d-4263-9693-bbd83da8b071",
        "caller-token",
        "2026-08-30T14:50:22Z",
        "2026-08-30T14:56:46Z",
    )

    assert captured["url"] == "https://logging.googleapis.com/v2/entries:list"
    assert captured["token"] == "caller-token"
    assert 'timestamp>="2026-08-30T14:50:22Z"' in captured["payload"]["filter"]
    assert logs[0]["stage"] == "RAG 색인"
    assert logs[0]["message"].endswith("HTTP 422 · Input should be a valid string")
    assert logs[0]["fileIds"] == ["1YUj8gsDZcRuU4b0Pb_jqr_LYVoKai6kW"]


def test_sync_log_message_accepts_plain_text_http_body() -> None:
    raw = (
        'delete failed fileId=12xOd1V0C0egiYrTHxDptbffRoIiBUkfA '
        '{"body":"Internal Server Error","code":500,'
        '"message":"HTTP server responded with error code 500"}'
    )

    assert dept_gui._sync_log_message(raw).endswith(
        "HTTP 500 · Internal Server Error"
    )


def test_sync_log_message_accepts_http_detail_string() -> None:
    raw = (
        'index-gcs failed drive=DRIVE uris=1 '
        '{"body":{"detail":"Vertex RAG quota still exhausted"},"code":429}'
    )

    assert dept_gui._sync_log_message(raw).endswith(
        "HTTP 429 · Vertex RAG quota still exhausted"
    )


def test_sync_workflow_logs_parses_file_result_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = (
        'sync-file-result status=INDEXED {"fileId":"FILE_1234567890",'
        '"name":"안내.xlsx","mimeType":"application/vnd.test",'
        '"operation":"UPDATED","route":"FILE_COPY"}'
    )
    monkeypatch.setattr(
        dept_gui,
        "_http_post_json",
        lambda *args, **kwargs: (
            200,
            {
                "entries": [
                    {
                        "timestamp": "2026-08-30T16:18:03Z",
                        "severity": "INFO",
                        "textPayload": raw,
                    }
                ]
            },
            5,
        ),
    )

    events = dept_gui._sync_workflow_logs(
        "project-test", "8439af79-218b-4dea-8c13-58a0f14e30f0", "token"
    )

    assert events == [
        {
            "eventType": "file",
            "timestamp": "2026-08-30T16:18:03Z",
            "status": "INDEXED",
            "operation": "UPDATED",
            "fileId": "FILE_1234567890",
            "name": "안내.xlsx",
            "mimeType": "application/vnd.test",
            "modifiedTime": "",
            "createdTime": "",
            "route": "FILE_COPY",
        }
    ]


def test_sync_workflow_logs_parses_failed_delete_as_file_item(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = (
        'sync-file-result status=FAILED {"fileId":"12xOd1V0C0egiYrTHxDptbffRoIiBUkfA",'
        '"name":"변경분테스트.txt","mimeType":"text/plain",'
        '"operation":"DELETED","route":"DELETE"}'
    )
    monkeypatch.setattr(
        dept_gui,
        "_http_post_json",
        lambda *args, **kwargs: (
            200,
            {
                "entries": [
                    {
                        "timestamp": "2026-08-31T04:51:00Z",
                        "severity": "INFO",
                        "textPayload": raw,
                    }
                ]
            },
            1,
        ),
    )

    events = dept_gui._sync_workflow_logs(
        "project-test", "0d18dbfd-3c85-4774-acb9-d4da5cbbc84f", "token"
    )

    assert events[0]["status"] == "FAILED"
    assert events[0]["route"] == "DELETE"
    assert events[0]["fileId"] == "12xOd1V0C0egiYrTHxDptbffRoIiBUkfA"


def test_sync_run_detail_returns_result_logs_and_file_names(
    isolated_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _headers = _client()
    execution_id = "52c4c7ab-da3d-4263-9693-bbd83da8b071"
    row = {
        "name": f"projects/p/locations/r/workflows/w/executions/{execution_id}",
        "state": "SUCCEEDED",
        "argument": '{"driveIds":["DRIVE_CS_123456"],"departmentCode":"cs"}',
        "labels": {"department": "cs", "mode": "delta"},
        "result": '{"ok":false,"totals":{"listed":25,"indexed":0,"indexFailed":2}}',
    }
    monkeypatch.setattr(
        dept_gui,
        "_common",
        lambda: {
            "GCP_PROJECT_ID": "project-test",
            "GCP_REGION": "asia-northeast3",
            "FIRESTORE_DATABASE": "rag-sync-state",
        },
    )
    monkeypatch.setattr(dept_gui, "_gcloud_json", lambda *args, **kwargs: (True, row))
    monkeypatch.setattr(
        dept_gui,
        "_sync_department_targets",
        lambda: ({"cs": {"name": "컴퓨터공학부"}}, {"DRIVE_CS_123456": "cs"}),
    )
    monkeypatch.setattr(dept_gui, "_sync_access_token", lambda: "caller-token")
    monkeypatch.setattr(dept_gui, "_firestore_sync_progress", lambda *args: {})
    monkeypatch.setattr(
        dept_gui,
        "_sync_workflow_logs",
        lambda *args: [
            {
                "timestamp": "2026-08-30T14:51:17Z",
                "severity": "ERROR",
                "stage": "RAG 색인",
                "message": "HTTP 422",
                "fileIds": ["FILE_1234567890"],
                "raw": "index-gcs failed",
            },
            {
                "eventType": "file",
                "timestamp": "2026-08-30T14:52:00Z",
                "status": "INDEXED",
                "operation": "UPDATED",
                "fileId": "FILE_1234567890",
                "name": "",
                "mimeType": "text/plain",
                "modifiedTime": "2026-08-30T14:49:58Z",
                "route": "FILE_COPY",
            },
        ],
    )
    monkeypatch.setattr(
        dept_gui,
        "_firestore_document_summaries",
        lambda *args: {
            "FILE_1234567890": {
                "fileId": "FILE_1234567890",
                "name": "변경분_테스트.txt.txt",
                "path": "Drive/변경분_테스트.txt.txt",
                "status": "PARSED",
            }
        },
    )

    response = client.get(f"/api/v1/sync-runs/{execution_id}")

    assert response.status_code == 200
    body = response.json()
    assert body["run"]["ok"] is False
    assert body["run"]["totals"]["indexFailed"] == 2
    assert body["logs"][0]["stage"] == "RAG 색인"
    assert body["logs"][0]["files"][0]["name"] == "변경분_테스트.txt.txt"
    assert body["items"][0]["name"] == "변경분_테스트.txt.txt"
    assert body["items"][0]["path"] == "Drive/변경분_테스트.txt.txt"


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


def _common_sa_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        dept_gui,
        "drive_service_account_status",
        lambda project: {
            "projectId": project,
            "serviceAccount": "42-compute@developer.gserviceaccount.com",
            "account": "tester@example.com",
            "status": "OK",
            "detail": "서비스 계정 연결 확인됨",
            "issues": [],
        },
    )


def test_common_resource_plan_reports_missing_apis_without_touching_gcp(
    isolated_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, headers = _client()
    _bootstrap(monkeypatch)
    monkeypatch.setattr(dept_gui, "_gcloud_project_resources", _no_resources)
    monkeypatch.setattr(dept_gui, "_gcloud_enabled_services", lambda project: (True, set()))
    monkeypatch.setattr(
        dept_gui,
        "drive_service_account_status",
        lambda project: {
            "projectId": project,
            "serviceAccount": "42-compute@developer.gserviceaccount.com",
            "account": "tester@example.com",
            "status": "FAIL",
            "detail": "서비스 계정 임시 접근 토큰 HTTP 403",
            "issues": ["tokenCreator"],
        },
    )
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
        item["name"] for item in dept_gui.COMMON_REQUIRED_SERVICES
    }
    assert "workflowexecutions.googleapis.com" in {item["name"] for item in body["services"]}
    assert all(item["enabled"] is False for item in body["services"])
    assert body["serviceAccount"]["ready"] is False
    assert body["serviceAccount"]["issues"] == ["tokenCreator"]
    assert body["serviceAccount"]["account"] == "tester@example.com"
    resources = {item["key"]: item for item in body["resources"]}
    assert resources["artifactRepo"]["displayName"] == "rag-mcp"
    assert resources["firestoreDatabase"]["displayName"] == "rag-sync-state"
    assert all(item["exists"] is False for item in resources.values())
    assert "변경할 수 없습니다" in resources["firestoreDatabase"]["warning"]


def test_common_resource_plan_marks_existing_resources_as_skipped(
    isolated_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, headers = _client()
    _bootstrap(monkeypatch)
    _common_sa_ready(monkeypatch)
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
    client, headers = _client()
    _bootstrap(monkeypatch)
    _common_sa_ready(monkeypatch)
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


def test_common_provisioning_runs_required_apis_in_declared_order(
    isolated_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, headers = _client()
    _bootstrap(monkeypatch)
    _common_sa_ready(monkeypatch)
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
    monkeypatch.setattr(dept_gui, "_gcloud_enabled_services", lambda project: (True, set()))
    enabled: list[str] = []
    monkeypatch.setattr(
        dept_gui, "_enable_gcloud_service", lambda service, project: enabled.append(service)
    )

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

    assert run["status"] == "COMPLETED"
    assert enabled == [item["name"] for item in dept_gui.COMMON_REQUIRED_SERVICES]


def test_common_provisioning_grants_and_verifies_token_creator(
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
                {"id": "rag-sync-state", "location": region, "type": "FIRESTORE_NATIVE"}
            ],
            "artifactError": "",
            "firestoreError": "",
        },
    )
    all_services = {item["name"] for item in dept_gui.COMMON_REQUIRED_SERVICES}
    monkeypatch.setattr(
        dept_gui, "_gcloud_enabled_services", lambda project: (True, all_services)
    )
    checks = [
        {
            "projectId": "project-test",
            "serviceAccount": "42-compute@developer.gserviceaccount.com",
            "account": "tester@example.com",
            "status": "FAIL",
            "detail": "서비스 계정 임시 접근 토큰 HTTP 403",
            "issues": ["tokenCreator"],
        },
        {
            "projectId": "project-test",
            "serviceAccount": "42-compute@developer.gserviceaccount.com",
            "account": "tester@example.com",
            "status": "FAIL",
            "detail": "서비스 계정 임시 접근 토큰 HTTP 403",
            "issues": ["tokenCreator"],
        },
        {
            "projectId": "project-test",
            "serviceAccount": "42-compute@developer.gserviceaccount.com",
            "account": "tester@example.com",
            "status": "OK",
            "detail": "서비스 계정 연결 확인됨",
            "issues": [],
        },
    ]
    monkeypatch.setattr(
        dept_gui, "drive_service_account_status", lambda project: checks.pop(0)
    )
    grants: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        dept_gui,
        "_grant_service_account_role",
        lambda service_account, member, role, project: grants.append(
            (service_account, member, role)
        ),
    )

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

    assert run["status"] == "COMPLETED"
    assert run["serviceAccount"]["status"] == "COMPLETE"
    assert grants == [
        (
            "42-compute@developer.gserviceaccount.com",
            "user:tester@example.com",
            "roles/iam.serviceAccountTokenCreator",
        )
    ]


def test_common_provisioning_skips_creation_when_api_enable_fails(
    isolated_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, headers = _client()
    _bootstrap(monkeypatch)
    _common_sa_ready(monkeypatch)
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
    _common_sa_ready(monkeypatch)
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
    monkeypatch.setattr(dept_gui, "_enable_gcloud_service", lambda service, project: None)

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
    assert {item["status"] for item in first.json()["resources"]} == {"SKIPPED"}
    assert all("이미 존재" in item["detail"] for item in first.json()["resources"])
    run = _await_run(client, first.json())
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
        lambda project, token, scopes, **kw: (
            "",
            "42-compute@x",
            30,
            "서비스 계정 임시 접근 토큰 HTTP 404",
        ),
    )

    body = client.get("/api/v1/drive-service-account/status").json()

    assert body["status"] == "FAIL"
    assert "tokenCreator" in body["issues"]
    assert "404" in body["detail"]


def test_drive_sa_status_flags_compute_api_on_fresh_project(
    isolated_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _ = _client()
    _sa_common(monkeypatch)
    monkeypatch.setattr(dept_gui, "_gcloud_enabled_services", lambda p: (True, set()))
    monkeypatch.setattr(dept_gui, "_gcloud_json", lambda args, timeout=12: (False, "NOT_FOUND"))

    body = client.get("/api/v1/drive-service-account/status").json()

    assert body["status"] == "FAIL"
    assert "computeApi" in body["issues"]
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
    monkeypatch.setattr(dept_gui.time, "sleep", lambda seconds: None)

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
    assert overlap["peak"] >= 2, "독립 호출이 순차로 돌고 있다"
    assert elapsed < 0.4, f"병렬이 아니다: {elapsed:.2f}s"


def test_bootstrap_state_discards_project_list_when_unauthenticated(
    isolated_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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
    monkeypatch.setattr(dept_gui, "_gcloud_executable", lambda: "gcloud.cmd")

    def fake_json(args, timeout=12):
        if args[:2] == ["projects", "list"]:
            pytest.fail("프로젝트 전량 조회가 되살아났다")
        return True, [{"account": "tester@example.com"}]

    monkeypatch.setattr(dept_gui, "_gcloud_json", fake_json)
    monkeypatch.setattr(dept_gui, "_run_command", lambda *a, **k: (True, "proj-current"))

    state = dept_gui._gcloud_bootstrap_state(include_projects=True)

    assert state["authenticated"] is True
    assert state["projects"] == [{"id": "proj-current", "name": "proj-current"}]


def test_project_search_merges_id_and_name_matches(
    isolated_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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
    assert dept_gui._project_accessible("BAD ID") is False
    assert len(calls) == 2


def _write_twin_department(dept_dir: Path, code: str) -> None:
    payload = _payload()
    (dept_dir / f"{code}.yaml").write_text(
        yaml.safe_dump(
            {
                "name": "기계공학과",
                "corpora": payload["corpora"],
                "keys": {"staff": "k" * 24, "student": "s" * 24},
                "buckets": payload["buckets"],
                "drive": {"driveIds": ["DRIVE-1"], "syncFolderIds": ["STAFF-1"]},
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )


def _wait_for_teardown(timeout: float = 5.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        runs = list(dept_gui._TEARDOWN_RUNS.values())
        if runs and all(run["status"] != "RUNNING" for run in runs):
            return
        time.sleep(0.02)
    raise AssertionError("삭제 작업이 끝나지 않았다")


def _stub_teardown_deletes(monkeypatch: pytest.MonkeyPatch, failures: set[str] | None = None) -> list[str]:
    calls: list[str] = []
    broken = failures or set()

    def record(kind: str):
        def _call(name: str, *args, **kwargs) -> str:
            calls.append(f"{kind}:{name}")
            if name in broken:
                raise RuntimeError(f"{name} 삭제 실패")
            return "삭제 완료"

        return _call

    monkeypatch.setattr(dept_gui, "_delete_cloud_run_service", record("run"))
    monkeypatch.setattr(dept_gui, "_delete_workflow_resource", record("workflow"))
    monkeypatch.setattr(dept_gui, "_delete_scheduler_job", record("scheduler"))
    monkeypatch.setattr(dept_gui, "_delete_bucket_resource", record("bucket"))
    monkeypatch.setattr(dept_gui, "_delete_gcs_prefix", record("prefix"))
    monkeypatch.setattr(
        dept_gui, "_delete_rag_corpus", lambda name, region, token: calls.append(f"corpus:{name}") or "삭제 완료"
    )
    monkeypatch.setattr(
        dept_gui,
        "_delete_department_firestore_state",
        lambda drive_ids, project: calls.append(f"firestore:{','.join(drive_ids)}") or "문서 0건 삭제",
    )
    monkeypatch.setattr(
        dept_gui,
        "_refresh_cloud_teardown_routing",
        lambda code: calls.append("syncEnv:rag-sync") or "",
    )
    monkeypatch.setattr(dept_gui, "_provision_access_token", lambda: "token")
    return calls


@pytest.fixture()
def cloud_teardown_configs(isolated_config: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(dept_gui, "_cloud_department_configs", lambda: {
        path.stem: yaml.safe_load(path.read_text(encoding="utf-8"))
        for path in isolated_config.glob("*.yaml")
    })
    monkeypatch.setattr(dept_gui, "_require_sync_removed", lambda: None)


@pytest.mark.usefixtures("cloud_teardown_configs")
def test_teardown_plan_covers_firestore_history_and_shared_bucket_objects(
    isolated_config: Path,
) -> None:
    (isolated_config.parent / "common.yaml").write_text(
        yaml.safe_dump(
            {
                "GCP_PROJECT_ID": "project-test",
                "GCP_REGION": "asia-northeast3",
                "FIRESTORE_DATABASE": "rag-sync-state",
                "RAG_METADATA_BUCKET": "shared-metadata-test",
            }
        ),
        encoding="utf-8",
    )
    client, headers = _client()
    assert client.post("/api/v1/departments", headers=headers, json=_payload()).status_code == 201

    plan = client.get("/api/v1/departments/ee/teardown-plan").json()
    by_key = {target["key"]: target for target in plan["targets"]}

    assert by_key["firestore-state"]["meta"]["driveIds"] == ["DRIVE-1"]
    assert by_key["metadata-staff"]["name"] == (
        "gs://shared-metadata-test/import-results/staff-1/"
    )
    assert by_key["metadata-student"]["name"] == (
        "gs://shared-metadata-test/import-results/student-1/"
    )


@pytest.mark.usefixtures("cloud_teardown_configs")
def test_teardown_plan_keeps_firestore_history_when_a_drive_is_shared(
    isolated_config: Path,
) -> None:
    client, headers = _client()
    assert client.post("/api/v1/departments", headers=headers, json=_payload()).status_code == 201
    _write_twin_department(isolated_config, "me")

    plan = client.get("/api/v1/departments/ee/teardown-plan").json()
    by_key = {target["key"]: target for target in plan["targets"]}

    assert by_key["firestore-state"]["skipped"] is True
    assert by_key["firestore-state"]["sharedWith"] == ["me"]
    assert by_key["firestore-state"]["selectable"] is False


@pytest.mark.usefixtures("cloud_teardown_configs")
def test_teardown_plan_keeps_resources_shared_with_other_departments(
    isolated_config: Path,
) -> None:
    client, headers = _client()
    assert client.post("/api/v1/departments", headers=headers, json=_payload()).status_code == 201
    _write_twin_department(isolated_config, "me")

    plan = client.get("/api/v1/departments/ee/teardown-plan").json()
    by_key = {target["key"]: target for target in plan["targets"]}

    assert by_key["corpus-staff"]["skipped"] is True
    assert by_key["corpus-staff"]["sharedWith"] == ["me"]
    assert by_key["bucket-source"]["skipped"] is True
    assert by_key["mcp-staff"]["skipped"] is False
    assert "config" not in by_key


@pytest.mark.usefixtures("cloud_teardown_configs")
def test_teardown_refuses_when_confirm_word_does_not_match(
    isolated_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, headers = _client()
    assert client.post("/api/v1/departments", headers=headers, json=_payload()).status_code == 201
    calls = _stub_teardown_deletes(monkeypatch)

    response = client.post(
        "/api/v1/departments/ee/teardown", headers=headers, json={"confirm": "EE "}
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "TEARDOWN_CONFIRM_MISMATCH"
    assert calls == []
    assert (isolated_config / "ee.yaml").exists()


@pytest.mark.usefixtures("cloud_teardown_configs")
def test_teardown_refuses_an_empty_selection(
    isolated_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, headers = _client()
    assert client.post("/api/v1/departments", headers=headers, json=_payload()).status_code == 201
    calls = _stub_teardown_deletes(monkeypatch)

    response = client.post(
        "/api/v1/departments/ee/teardown",
        headers=headers,
        json={"confirm": "ee", "targets": []},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "TEARDOWN_SELECTION_EMPTY"
    assert calls == []


def test_common_runtime_teardown_can_delete_one_service(
    isolated_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, headers = _client()
    calls = _stub_teardown_deletes(monkeypatch)

    response = client.post(
        "/api/v1/common-runtime/teardown",
        headers=headers,
        json={"confirm": "project-test", "targets": ["parser"]},
    )
    assert response.status_code == 202
    _wait_for_teardown()
    run = dept_gui._TEARDOWN_RUNS[response.json()["runId"]]

    assert calls == ["run:rag-parser"]
    assert run["status"] == "COMPLETED"
    assert {target["key"] for target in run["targets"] if target["status"] == "SKIPPED"} == {
        "scheduler",
        "workflow",
        "sync",
    }


class _FakeFirestoreDoc:
    def __init__(self, store: dict, path: str) -> None:
        self.store, self.path = store, path

    @property
    def id(self) -> str:
        return self.path.rsplit("/", 1)[-1]

    def get(self):
        data = self.store.get(self.path)
        return SimpleNamespace(exists=data is not None, id=self.id, reference=self)

    def delete(self) -> None:
        self.store.pop(self.path, None)

    def collection(self, name: str) -> _FakeFirestoreCollection:
        return _FakeFirestoreCollection(self.store, f"{self.path}/{name}")


class _FakeFirestoreCollection:
    def __init__(self, store: dict, path: str, predicate=None) -> None:
        self.store, self.path, self.predicate = store, path, predicate

    def document(self, name: str) -> _FakeFirestoreDoc:
        return _FakeFirestoreDoc(self.store, f"{self.path}/{name}")

    def where(self, field: str = "", op: str = "", value=None, *, filter=None) -> _FakeFirestoreCollection:
        if filter is not None:
            field, op, value = filter.field_path, filter.op_string, filter.value
        assert op == "in"
        return _FakeFirestoreCollection(
            self.store, self.path, lambda data: data.get(field) in value
        )

    def _children(self) -> list[str]:
        depth = self.path.count("/") + 1
        return [
            path
            for path in list(self.store)
            if path.startswith(f"{self.path}/") and path.count("/") == depth
        ]

    def list_documents(self) -> list[_FakeFirestoreDoc]:
        return [_FakeFirestoreDoc(self.store, path) for path in self._children()]

    def stream(self) -> list:
        rows = []
        for path in self._children():
            data = self.store.get(path) or {}
            if self.predicate and not self.predicate(data):
                continue
            reference = _FakeFirestoreDoc(self.store, path)
            rows.append(
                SimpleNamespace(id=reference.id, reference=reference, to_dict=lambda d=data: d)
            )
        return rows


class _FakeFirestoreClient:
    def __init__(self, store: dict) -> None:
        self.store = store
        self.commit_sizes: list[int] = []

    def collection(self, name: str) -> _FakeFirestoreCollection:
        return _FakeFirestoreCollection(self.store, name)

    def batch(self):
        client = self

        class Batch:
            def __init__(self) -> None:
                self.references: list[_FakeFirestoreDoc] = []

            def delete(self, reference: _FakeFirestoreDoc) -> None:
                self.references.append(reference)

            def commit(self) -> None:
                client.commit_sizes.append(len(self.references))
                for reference in self.references:
                    reference.delete()

        return Batch()


def test_firestore_client_uses_active_gcloud_token(
    isolated_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from google.cloud import firestore

    captured: dict = {}

    def fake_client(**kwargs):
        captured.update(kwargs)
        return "client"

    monkeypatch.setattr(dept_gui, "_provision_access_token", lambda: "gcloud-token")
    monkeypatch.setattr(firestore, "Client", fake_client)

    assert dept_gui._firestore_client("project-test") == "client"
    assert captured["project"] == "project-test"
    assert captured["database"] == "rag-sync-state"
    assert captured["credentials"].token == "gcloud-token"


def test_firestore_teardown_removes_history_for_this_departments_drives_only(
    isolated_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = {
        "doc_state/file-a": {"fileId": "file-a", "driveId": "DRIVE-1"},
        "doc_state/file-a/rag_files/m1": {"corpus": "staff"},
        "doc_state/file-a/rag_files/m2": {"corpus": "student"},
        "doc_state/file-b": {"fileId": "file-b", "driveId": "DRIVE-OTHER"},
        "doc_state/file-b/rag_files/m3": {"corpus": "staff"},
        "doc_dlq/file-a": {"fileId": "file-a"},
        "doc_split_queue/file-a": {"fileId": "file-a"},
        "doc_dlq/file-b": {"fileId": "file-b"},
        "sync_tokens/DRIVE-1": {"pageToken": "t1"},
        "sync_tokens/DRIVE-OTHER": {"pageToken": "t2"},
    }
    firestore = _FakeFirestoreClient(store)
    monkeypatch.setattr(dept_gui, "_firestore_client", lambda project: firestore)

    detail = dept_gui._delete_department_firestore_state(["DRIVE-1"], "project-test")

    assert "문서 1건" in detail and "매핑 2건" in detail
    assert firestore.commit_sizes == [6]
    assert set(store) == {
        "doc_state/file-b",
        "doc_state/file-b/rag_files/m3",
        "doc_dlq/file-b",
        "sync_tokens/DRIVE-OTHER",
    }


def test_firestore_teardown_commits_at_configured_batch_size(
    isolated_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = {
        f"doc_state/file-{index}": {"fileId": f"file-{index}", "driveId": "DRIVE-1"}
        for index in range(150)
    }
    firestore = _FakeFirestoreClient(store)
    monkeypatch.setattr(dept_gui, "_firestore_client", lambda project: firestore)
    progress: list[str] = []

    dept_gui._delete_department_firestore_state(
        ["DRIVE-1"], "project-test", progress=progress.append
    )

    assert firestore.commit_sizes == [200, 200, 51]
    assert progress and "문서 150건" in progress[-1]
    assert not store


def test_common_runtime_status_reports_each_service(
    isolated_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_gcloud(args, timeout=12):
        if args[:3] == ["run", "services", "describe"] and args[3] == "rag-sync":
            return True, {
                "metadata": {"creationTimestamp": "2026-09-01T00:00:00Z"},
                "status": {
                    "conditions": [{"type": "Ready", "status": "True"}],
                    "latestReadyRevisionName": "rag-sync-00007",
                    "latestCreatedRevisionName": "rag-sync-00007",
                    "url": "https://rag-sync-test.run.app",
                },
            }
        if args[:3] == ["run", "services", "describe"]:
            return False, "ERROR: NOT_FOUND: Service does not exist"
        if args[:2] == ["workflows", "describe"]:
            return True, {"state": "ACTIVE", "revisionId": "000012"}
        return True, {"state": "ENABLED", "schedule": "0 3 * * *", "timeZone": "Asia/Seoul"}

    monkeypatch.setattr(dept_gui, "_gcloud_json", fake_gcloud)
    client, _ = _client()

    body = client.get("/api/v1/common-runtime/status").json()
    by_key = {item["key"]: item for item in body["services"]}

    assert body["status"] == "DEGRADED"
    assert by_key["sync"]["state"] == "READY"
    assert by_key["sync"]["revision"] == "rag-sync-00007"
    assert by_key["parser"]["state"] == "MISSING"
    assert by_key["workflow"]["state"] == "READY"
    assert by_key["workflow"]["displayName"] == "rag-daily-sync · Workflow"
    assert by_key["scheduler"]["state"] == "READY"
    assert by_key["scheduler"]["displayName"] == "rag-daily-sync · Scheduler"


def test_common_runtime_status_flags_a_revision_that_never_became_ready(
    isolated_config: Path,
) -> None:
    service = {
        "metadata": {},
        "status": {
            "conditions": [{"type": "Ready", "status": "True"}],
            "latestReadyRevisionName": "rag-sync-00007",
            "latestCreatedRevisionName": "rag-sync-00008",
        },
    }

    state = dept_gui._cloud_run_service_state(service)

    assert state["state"] == "DEGRADED"
    assert state["pendingRevision"] == "rag-sync-00008"


def test_common_runtime_teardown_plan_warns_about_remaining_departments(
    isolated_config: Path,
) -> None:
    client, headers = _client()
    assert client.post("/api/v1/departments", headers=headers, json=_payload()).status_code == 201

    plan = client.get("/api/v1/common-runtime/teardown-plan").json()

    assert plan["confirmWord"] == "project-test"
    assert plan["remainingDepartments"] == ["ee"]
    assert [target["key"] for target in plan["targets"]] == [
        "scheduler",
        "workflow",
        "sync",
        "parser",
    ]


def test_common_runtime_teardown_removes_runtime_but_not_departments(
    isolated_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, headers = _client()
    assert client.post("/api/v1/departments", headers=headers, json=_payload()).status_code == 201
    calls = _stub_teardown_deletes(monkeypatch)

    response = client.post(
        "/api/v1/common-runtime/teardown", headers=headers, json={"confirm": "project-test"}
    )
    assert response.status_code == 202
    _wait_for_teardown()
    run = dept_gui._TEARDOWN_RUNS[response.json()["runId"]]

    assert run["status"] == "COMPLETED"
    assert calls == [
        "scheduler:rag-daily-sync",
        "workflow:rag-daily-sync",
        "run:rag-sync",
        "run:rag-parser",
    ]
    assert (isolated_config / "ee.yaml").exists()


def _sync_common() -> dict:
    return {"GCP_PROJECT_ID": "project-test", "GCP_REGION": "asia-northeast3"}


def test_sync_check_defers_to_the_deploy_failure_that_caused_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called: list[list[str]] = []
    monkeypatch.setattr(dept_gui, "_gcloud_json", lambda args, timeout=12: called.append(args) or (False, ""))
    deploy_checks = [
        {"layer": "DEPLOY", "name": "parser", "status": "WARN", "detail": "미배포"},
        {"layer": "DEPLOY", "name": "mcp-cs-staff", "status": "OK", "detail": "ok"},
    ]

    checks = dept_gui._sync_status(_sync_common(), deploy_checks=deploy_checks, code="cs")

    assert len(checks) == 1
    assert checks[0]["status"] == "SKIP"
    assert checks[0]["actionType"] == "COMMON_RUNTIME_DEPLOY"
    assert checks[0]["blockedBy"] == ["parser"]
    assert called == []


def test_missing_workflow_offers_the_common_runtime_deploy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_gcloud(args, timeout=12):
        if args[:2] == ["workflows", "describe"]:
            return False, "ERROR: NOT_FOUND: Workflow does not exist"
        raise AssertionError(f"불필요한 호출: {args}")

    monkeypatch.setattr(dept_gui, "_gcloud_json", fake_gcloud)

    checks = dept_gui._sync_status(_sync_common(), deploy_checks=[], code="cs")

    assert checks[0]["status"] == "WARN"
    assert checks[0]["actionType"] == "COMMON_RUNTIME_DEPLOY"
    assert "rag-daily-sync" in checks[0]["detail"]


def test_deployed_workflow_without_runs_points_at_manual_sync(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_gcloud(args, timeout=12):
        if args[:2] == ["workflows", "describe"]:
            return True, {"name": "rag-daily-sync", "state": "ACTIVE"}
        if args[:3] == ["scheduler", "jobs", "describe"]:
            return True, {"state": "ENABLED", "schedule": "0 0 * * *", "timeZone": "Asia/Seoul"}
        return True, []

    monkeypatch.setattr(dept_gui, "_gcloud_json", fake_gcloud)

    checks = dept_gui._sync_status(_sync_common(), deploy_checks=[], code="cs")
    by_name = {item["name"]: item for item in checks}

    assert by_name["scheduler-job"]["status"] == "OK"
    assert by_name["latest-workflow"]["status"] == "WARN"
    assert by_name["latest-workflow"]["actionType"] == "MANUAL_SYNC"
    assert by_name["latest-workflow"]["departmentCode"] == "cs"


def test_missing_scheduler_job_is_reported_instead_of_a_bare_workflow_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_gcloud(args, timeout=12):
        if args[:2] == ["workflows", "describe"]:
            return True, {"name": "rag-daily-sync"}
        if args[:3] == ["scheduler", "jobs", "describe"]:
            return False, "ERROR: NOT_FOUND: The job does not exist"
        return True, []

    monkeypatch.setattr(dept_gui, "_gcloud_json", fake_gcloud)

    checks = dept_gui._sync_status(_sync_common(), deploy_checks=[], code="cs")
    by_name = {item["name"]: item for item in checks}

    assert by_name["scheduler-job"]["status"] == "WARN"
    assert by_name["scheduler-job"]["actionType"] == "COMMON_RUNTIME_DEPLOY"
    assert by_name["latest-workflow"]["status"] == "SKIP"
    assert by_name["latest-workflow"]["blockedBy"] == ["scheduler-job"]


def test_paused_scheduler_job_shows_the_resume_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_gcloud(args, timeout=12):
        if args[:2] == ["workflows", "describe"]:
            return True, {"name": "rag-daily-sync"}
        if args[:3] == ["scheduler", "jobs", "describe"]:
            return True, {"state": "PAUSED", "schedule": "0 0 * * *"}
        return True, [{"state": "SUCCEEDED", "endTime": "2026-08-29T00:00:00+00:00"}]

    monkeypatch.setattr(dept_gui, "_gcloud_json", fake_gcloud)

    checks = dept_gui._sync_status(_sync_common(), deploy_checks=[], code="cs")
    job = next(item for item in checks if item["name"] == "scheduler-job")

    assert job["status"] == "WARN"
    assert "PAUSED" in job["detail"]
    assert "actionType" not in job
    assert "jobs resume" in job["action"]


def test_execution_lookup_failure_stays_an_error_with_the_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_gcloud(args, timeout=12):
        if args[:2] == ["workflows", "describe"]:
            return True, {"name": "rag-daily-sync"}
        return False, "PERMISSION_DENIED: workflows.executions.list"

    monkeypatch.setattr(dept_gui, "_gcloud_json", fake_gcloud)

    checks = dept_gui._sync_status(_sync_common(), deploy_checks=[], code="cs")

    assert checks[0]["status"] == "FAIL"
    assert "PERMISSION_DENIED" in checks[0]["detail"]
    assert "actionType" not in checks[0]


def test_successful_run_still_reports_freshness(monkeypatch: pytest.MonkeyPatch) -> None:
    finished = (
        datetime.now(UTC).replace(microsecond=0) - timedelta(hours=2)
    ).isoformat()

    def fake_gcloud(args, timeout=12):
        if args[:2] == ["workflows", "describe"]:
            return True, {"name": "rag-daily-sync"}
        if args[:3] == ["scheduler", "jobs", "describe"]:
            return True, {"state": "ENABLED", "schedule": "0 0 * * *"}
        return True, [{"state": "SUCCEEDED", "startTime": finished, "endTime": finished}]

    monkeypatch.setattr(dept_gui, "_gcloud_json", fake_gcloud)

    checks = dept_gui._sync_status(_sync_common(), deploy_checks=[], code="cs")
    latest = next(item for item in checks if item["name"] == "latest-workflow")

    assert latest["status"] == "OK"
    assert "SUCCEEDED" in latest["detail"]


def test_cancelled_duplicate_does_not_hide_a_successful_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    finished = datetime.now(UTC).replace(microsecond=0).isoformat()

    def fake_gcloud(args, timeout=12):
        if args[:2] == ["workflows", "describe"]:
            return True, {"name": "rag-daily-sync"}
        if args[:3] == ["scheduler", "jobs", "describe"]:
            return True, {"state": "ENABLED", "schedule": "0 0 * * *"}
        assert "--limit=10" in args
        return True, [
            {"state": "CANCELLED", "startTime": finished, "endTime": finished},
            {"state": "SUCCEEDED", "startTime": finished, "endTime": finished},
        ]

    monkeypatch.setattr(dept_gui, "_gcloud_json", fake_gcloud)

    checks = dept_gui._sync_status(_sync_common(), deploy_checks=[], code="cs")
    latest = next(item for item in checks if item["name"] == "latest-workflow")

    assert latest["status"] == "OK"
    assert "SUCCEEDED" in latest["detail"]


def _sync_service(env: dict[str, str]) -> dict:
    return {
        "spec": {"template": {"spec": {"containers": [
            {"env": [{"name": key, "value": value} for key, value in env.items()]}
        ]}}}
    }


def test_runtime_env_drift_catches_stale_buckets_after_a_department_rebuild(
    isolated_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, headers = _client()
    assert client.post("/api/v1/departments", headers=headers, json=_payload()).status_code == 201

    def fake_gcloud(args, timeout=12):
        assert args[:3] == ["run", "services", "describe"]
        return True, _sync_service(
            {
                "GCS_HWP_ORIGINAL_BUCKET": "rag-ee-hwp-old",
                "GCS_SOURCE_BUCKET": "rag-ee-source-old",
                "RAG_CORPUS_NAME": (
                    "projects/project-test/locations/asia-northeast3/ragCorpora/staff-1"
                ),
                "DEPARTMENTS_JSON": "{}",
            }
        )

    monkeypatch.setattr(dept_gui, "_gcloud_json", fake_gcloud)
    report = dept_gui.runtime_env_drift()
    by_service = {item["serviceName"]: item for item in report["services"]}

    assert report["status"] == "DRIFT"
    stale_keys = {item["key"] for item in by_service["rag-sync"]["staleKeys"]}
    assert "GCS_HWP_ORIGINAL_BUCKET" in stale_keys
    assert "DEPARTMENTS_JSON" in stale_keys
    assert "RAG_CORPUS_NAME" not in stale_keys


def test_runtime_env_reports_ok_when_deployment_matches_config(
    isolated_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, headers = _client()
    assert client.post("/api/v1/departments", headers=headers, json=_payload()).status_code == 201
    expected = dept_config.build_env("ee", "staff")
    deployed = {
        "GCS_HWP_ORIGINAL_BUCKET": expected["GCS_HWP_ORIGINAL_BUCKET"],
        "GCS_SOURCE_BUCKET": expected["GCS_SOURCE_BUCKET"],
        "RAG_CORPUS_NAME": expected["RAG_CORPUS_NAME"],
        "DEPARTMENTS_JSON": dept_config.departments_json(),
        **{key: expected[key] for key in (
            "DOCAI_PROCESSOR_ID", "DOCAI_LOCATION", "DOCAI_OCR_PROCESSOR_ID", "DOCAI_OCR_LOCATION",
        )},
    }
    monkeypatch.setattr(
        dept_gui, "_gcloud_json", lambda args, timeout=12: (True, _sync_service(deployed))
    )

    report = client.get("/api/v1/runtime-env").json()

    assert report["status"] == "OK"
    assert all(item["status"] == "OK" for item in report["services"])


def test_missing_service_is_not_reported_as_drift(
    isolated_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, headers = _client()
    assert client.post("/api/v1/departments", headers=headers, json=_payload()).status_code == 201
    monkeypatch.setattr(dept_gui, "_gcloud_json", lambda args, timeout=12: (False, "NOT_FOUND"))

    report = dept_gui.runtime_env_drift()

    assert report["status"] == "OK"
    assert all(item["status"] == "UNKNOWN" for item in report["services"])


def test_env_only_run_skips_build_workflow_and_scheduler_steps(
    isolated_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, list[str]] = {}

    class FakeProcess:
        stdout = io.StringIO("")
        returncode = 0

        def wait(self) -> int:
            return 0

    monkeypatch.setattr(dept_gui.shutil, "which", lambda name: "pwsh.exe")
    def fake_popen(args, **kwargs):
        captured["args"] = list(args)
        return FakeProcess()

    monkeypatch.setattr(dept_gui.subprocess, "Popen", fake_popen)
    dept_gui._run_common_runtime_deploy_script(on_line=lambda line: None, env_only=True)

    assert "-EnvOnly" in captured["args"]
    assert "-SkipMcp" not in captured["args"]

    class DeferredThread:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def start(self) -> None:
            pass

    monkeypatch.setattr(dept_gui, "threading", type("T", (), {"Thread": DeferredThread}))
    run = dept_gui.start_common_runtime_deployment(env_only=True)

    assert run["envOnly"] is True
    assert [step["key"] for step in run["steps"]] == ["config", "cloudRun", "ready"]


def test_cloud_teardown_rejects_local_only_department(isolated_config, monkeypatch):
    (isolated_config / "ee.yaml").write_text("name: local", encoding="utf-8")
    monkeypatch.setattr(dept_gui, "_cloud_department_configs", lambda: {"me": _payload()})
    with pytest.raises(FileNotFoundError):
        dept_gui.department_teardown_plan("ee")


def test_cloud_teardown_last_department_blocks_before_deletion(isolated_config, monkeypatch):
    monkeypatch.setattr(dept_gui, "_cloud_department_configs", lambda: {"ee": _payload()})
    monkeypatch.setattr(dept_gui, "_gcloud_json", lambda *args, **kwargs: (True, {"name": "rag-sync"}))
    plan = dept_gui.department_teardown_plan("ee")
    with pytest.raises(ValueError, match="Sync"):
        dept_gui.start_teardown_run(plan, "ee")
    assert not dept_gui._TEARDOWN_RUNS


def test_cloud_teardown_routing_uses_remaining_cloud_configs(isolated_config, monkeypatch):
    deployed = {"ee": _payload(), "me": _payload()}
    for config in deployed.values():
        config["keys"] = {"staff": "k" * 24, "student": "s" * 24}
    monkeypatch.setattr(dept_gui, "_cloud_department_configs", lambda: deployed.copy())
    captured = []
    monkeypatch.setattr(dept_gui, "update_sync_department_map", lambda **kwargs: captured.append(json.loads(kwargs["expected"])) or "me")
    dept_gui._refresh_cloud_teardown_routing("ee")
    assert list(captured[0]) == ["me"]
    assert "ee" in deployed


@pytest.mark.parametrize("result, allowed", [((False, "NOT_FOUND"), True), ((False, "permission denied"), False), ((True, {}), False)])
def test_cloud_teardown_last_department_requires_confirmed_absence(isolated_config, monkeypatch, result, allowed):
    monkeypatch.setattr(dept_gui, "_gcloud_json", lambda *args, **kwargs: result)
    if allowed:
        dept_gui._require_sync_removed()
    else:
        with pytest.raises(ValueError):
            dept_gui._require_sync_removed()

def test_sync_run_review_checks_execution_and_department_scope(monkeypatch):
    run={"executionId":"run-id", "state":"FAILED", "departmentCode":"cs", "driveIds":["drive-cs"]}
    fields={key:{"stringValue":value} for key,value in {"executionId":"run-id", "departmentCode":"cs", "driveId":"drive-cs", "reviewStatus":"REVIEWED"}.items()}
    monkeypatch.setattr(dept_gui, "_http_json", lambda *a, **kw:(200,{"fields":fields},0))
    assert dept_gui._sync_run_file_review("p","db","token",run)["reviewStatus"]=="REVIEWED"
    fields["driveId"]={"stringValue":"drive-ee"}
    assert dept_gui._sync_run_file_review("p","db","token",run) is None
    monkeypatch.setattr(dept_gui, "_http_json", lambda *a, **kw:(403,{},0))
    assert dept_gui._sync_run_file_review("p","db","token",run)["reviewStatus"]=="UNAVAILABLE"

def test_sync_run_review_reads_new_file_checkpoints_when_no_historical_review(monkeypatch):
    run={"executionId":"run-id", "state":"FAILED", "departmentCode":"cs", "driveIds":["drive-cs"], "error":"jobId="+"a"*32}
    def encoded(data):
        def value(item):
            if isinstance(item,dict): return {"mapValue":{"fields":{k:value(v) for k,v in item.items()}}}
            if isinstance(item,list): return {"arrayValue":{"values":[value(v) for v in item]}}
            return {"stringValue":item}
        return {"fields":{k:value(v) for k,v in data.items()}}
    def get(url,*args,**kwargs):
        if 'sync_run_reviews' in url: return 404,{},0
        if '/parts/' in url:
            return 200,encoded({"status":"RETRYING", "fileResults":{"a":{"status":"DONE"},"b":{"status":"FAILED","error":"empty"}}}),0
        return 200,encoded({"driveId":"drive-cs", "fileIds":["a","b"], "expectedParts":["faculty"]}),0
    monkeypatch.setattr(dept_gui,"_http_json",get)
    monkeypatch.setattr(dept_gui,"_firestore_document_summaries",lambda *a:{})
    review=dept_gui._sync_run_file_review("p","db","token",run)
    assert review["counts"]["DONE"]==1
    assert review["counts"]["FAILED"]==1
    assert review["source"]=="FILE_CHECKPOINTS"


def test_sync_file_item_keeps_drive_timestamps_separate_from_log_time():
    result = dept_gui._sync_file_item('sync-file-result status=INDEXED {"fileId":"file", "createdTime":"2026-01-01T00:00:00Z", "modifiedTime":"2026-02-01T00:00:00Z"}', "2026-03-01T00:00:00Z")
    assert result["createdTime"] == "2026-01-01T00:00:00Z"
    assert result["modifiedTime"] == "2026-02-01T00:00:00Z"
    assert result["timestamp"] == "2026-03-01T00:00:00Z"
