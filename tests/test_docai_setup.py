"""Setup uses metadata-only calls; no document processing or paid resources in tests."""
from unittest.mock import MagicMock

import pytest
import yaml
from fastapi.testclient import TestClient

from scripts import dept_gui, docai_setup


@pytest.fixture
def setup():
    get = MagicMock()
    post = MagicMock()
    obj = docai_setup.ProcessorSetup(get, post, lambda: "test-token")
    get.return_value = (200, {"type": "OCR_PROCESSOR", "state": "ENABLED"}, 1)
    return obj


def available():
    return (200, {"allowCreation": True, "availableLocations": [{"locationId": "us"}]}, 1)


def empty_plan(setup):
    setup.get.side_effect = [(200, {}, 1), available()]
    plan = setup.lookup("project-test", "ocr", "us")
    setup.get.side_effect = None
    return plan


def test_list_checks_every_page_and_does_not_create_if_existing(setup):
    setup.get.side_effect = [
        (200, {"nextPageToken": "page2"}, 1),
        (200, {"processors": [{"name": "projects/project-test/locations/us/processors/123",
                               "type": "OCR_PROCESSOR", "state": "DISABLED"}]}, 1),
    ]
    result = setup.lookup("project-test", "ocr", "us")
    assert not result["canCreate"] and result["processors"][0]["id"] == "123"
    assert "pageToken=page2" in setup.get.call_args.args[0]
    setup.post.assert_not_called()


@pytest.mark.parametrize("status", [0, 403, 404, 429, 500])
def test_lookup_failure_never_means_absent(setup, status):
    setup.get.return_value = (status, {}, 1)
    with pytest.raises(RuntimeError):
        setup.lookup("project-test", "ocr", "us")
    assert setup.plans == {}
    setup.post.assert_not_called()


def test_creation_rechecks_then_posts_only_pretrained_type_once(setup):
    plan = empty_plan(setup)
    setup.get.side_effect = [(200, {}, 1), available(), (200, {"type": "OCR_PROCESSOR", "state": "ENABLED"}, 1)]
    setup.post.return_value = (200, {"name": "projects/project-test/locations/us/processors/123"}, 1)
    result = setup.create(plan["planId"])
    assert result["processorId"] == "123"
    assert setup.post.call_args.args[1] == {"type": "OCR_PROCESSOR", "displayName": "rag-ocr"}
    with pytest.raises(ValueError, match="이미"):
        setup.create(plan["planId"])
    assert setup.post.call_count == 1


def test_creation_race_detects_new_existing_processor(setup):
    plan = empty_plan(setup)
    setup.get.return_value = (200, {"processors": [{"name": "processors/123", "type": "OCR_PROCESSOR"}]}, 1)
    with pytest.raises(ValueError, match="기존"):
        setup.create(plan["planId"])
    setup.post.assert_not_called()


def test_timed_out_create_is_not_automatically_retried(setup):
    plan = empty_plan(setup)
    setup.get.side_effect = [(200, {}, 1), available()]
    setup.post.return_value = (0, {}, 1)
    with pytest.raises(RuntimeError):
        setup.create(plan["planId"])
    with pytest.raises(ValueError):
        setup.create(plan["planId"])
    assert setup.post.call_count == 1


def test_expired_or_unknown_plan_never_creates(setup):
    plan = empty_plan(setup)
    setup.plans[plan["planId"]]["time"] = 0
    for plan_id in (plan["planId"], "unknown"):
        with pytest.raises(ValueError):
            setup.create(plan_id)
    setup.post.assert_not_called()


@pytest.mark.parametrize("kind,wrong_type", [("ocr", "LAYOUT_PARSER_PROCESSOR"), ("fallback", "OCR_PROCESSOR")])
def test_processors_cannot_be_swapped(setup, kind, wrong_type):
    setup.get.return_value = (200, {"type": wrong_type, "state": "ENABLED"}, 1)
    with pytest.raises(ValueError, match="종류"):
        setup.verify("project-test", kind, {"processorId": "123", "location": "us"})


@pytest.mark.parametrize("location", ["asia-northeast3", "https://evil", "", "us/../../"])
def test_invalid_location_never_calls_google(setup, location):
    with pytest.raises(ValueError):
        setup.lookup("project-test", "ocr", location)
    setup.get.assert_not_called()


@pytest.fixture
def gui(tmp_path, monkeypatch):
    monkeypatch.setattr(dept_gui, "CONFIG_DIR", tmp_path)
    path = tmp_path / "common.yaml"
    path.write_text(yaml.safe_dump({"GCP_PROJECT_ID": "project-test", "CUSTOM_VALUE": "preserve-me",
                                    "DOCAI_PROCESSOR_ID": "layout", "DOCAI_LOCATION": "us"}), encoding="utf-8")
    verify = MagicMock()
    monkeypatch.setattr(dept_gui._DOCAI_SETUP, "verify", verify)
    client = TestClient(dept_gui.app)
    headers = {"X-Local-Session": client.get("/api/v1/session").json()["nonce"], "Origin": "http://testserver"}
    return client, headers, path, verify


def test_save_preserves_other_processor_and_common_fields(gui):
    client, headers, path, verify = gui
    current = client.get("/api/v1/common-config/docai").json()
    assert "CUSTOM_VALUE" not in current
    payload = {"configRevision": current["configRevision"], "docai": {"ocr": {"processorId": "ocr", "location": "eu"}}}
    result = client.put("/api/v1/common-config/docai", headers=headers, json=payload)
    assert result.status_code == 200 and result.json()["deploymentRequired"]
    saved = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert saved["CUSTOM_VALUE"] == "preserve-me" and saved["DOCAI_PROCESSOR_ID"] == "layout"
    assert saved["DOCAI_OCR_PROCESSOR_ID"] == "ocr" and saved["DOCAI_OCR_LOCATION"] == "eu"
    verify.assert_called_once_with("project-test", "ocr", payload["docai"]["ocr"])
    assert client.put("/api/v1/common-config/docai", headers=headers, json=payload).status_code == 409


def test_failed_verification_does_not_write(gui):
    client, headers, path, verify = gui
    original = path.read_bytes()
    revision = client.get("/api/v1/common-config/docai").json()["configRevision"]
    verify.side_effect = ValueError("wrong processor type")
    response = client.put("/api/v1/common-config/docai", headers=headers, json={
        "configRevision": revision, "docai": {"ocr": {"processorId": "123", "location": "us"}},
    })
    assert response.status_code == 422 and path.read_bytes() == original


@pytest.mark.parametrize("endpoint", ["lookup", "check", "create", "enable-api"])
def test_setup_actions_require_local_session(gui, endpoint):
    client, _, _, _ = gui
    assert client.post(f"/api/v1/common-config/docai/{endpoint}", json={}).status_code == 403


def test_initial_setup_keeps_both_processors_optional_and_independent():
    payload = {"projectId": "project-test", "region": "asia-northeast3"}
    candidate, result = dept_gui.validate_common_candidate(payload)
    assert result["valid"] and "DOCAI_OCR_PROCESSOR_ID" not in candidate
    candidate, result = dept_gui.validate_common_candidate({**payload, "docai": {
        "ocr": {"processorId": "ocr", "location": "us"},
        "fallback": {"processorId": "", "location": ""},
    }})
    assert result["valid"] and candidate["DOCAI_OCR_PROCESSOR_ID"] == "ocr"
    assert candidate["DOCAI_PROCESSOR_ID"] == ""


def test_runtime_drift_detects_cleared_processor_and_deploy_location_default(monkeypatch):
    monkeypatch.setattr(dept_gui, "_common", lambda: {
        "GCP_PROJECT_ID": "project-test", "GCP_REGION": "asia-northeast3",
        "DOCAI_PROCESSOR_ID": "", "DOCAI_LOCATION": "", "DOCAI_OCR_PROCESSOR_ID": "new-ocr",
    })
    monkeypatch.setattr(dept_gui, "_expected_runtime_env", lambda: ("cs", {}, "{}"))
    deployed = {"DOCAI_PROCESSOR_ID": "old-layout", "DOCAI_LOCATION": "asia-northeast3",
                "DOCAI_OCR_PROCESSOR_ID": "old-ocr", "DEPARTMENTS_JSON": "{}"}
    monkeypatch.setattr(dept_gui, "_gcloud_json", lambda *a, **kw: (True, {
        "spec": {"template": {"spec": {"containers": [{"env": [
            {"name": key, "value": value} for key, value in deployed.items()
        ]}]}}},
    }))
    result = dept_gui.runtime_env_drift()
    parser = next(row for row in result["services"] if row["serviceName"] == "rag-parser")
    assert {row["key"] for row in parser["staleKeys"]} == {"DOCAI_PROCESSOR_ID", "DOCAI_OCR_PROCESSOR_ID"}
