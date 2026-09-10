"""Unified MCP Cloud console integration, including destructive-action boundaries."""

from __future__ import annotations

import copy
import json
from types import SimpleNamespace

import pytest
import yaml

from scripts import dept_config, dept_gui


@pytest.fixture
def unified(tmp_path, monkeypatch):
    config_dir = tmp_path / "config"
    dept_dir = config_dir / "departments"
    dept_dir.mkdir(parents=True)
    common = {"GCP_PROJECT_ID": "project-test", "GCP_REGION": "asia-northeast3",
              "MCP_UNIFIED_ENABLED": True, "GCS_HWP_ORIGINAL_BUCKET": "common-hwp",
              "GCS_SOURCE_BUCKET": "common-source", "GCS_METADATA_BUCKET": "shared-metadata"}
    (config_dir / "common.yaml").write_text(yaml.safe_dump(common), encoding="utf-8")
    for module in (dept_config, dept_gui):
        monkeypatch.setattr(module, "CONFIG_DIR", config_dir)
        monkeypatch.setattr(module, "DEPT_DIR", dept_dir)
    departments = {code: {
        "name": f"Cloud {code}",
        "corpora": {aud: f"projects/project-test/locations/asia-northeast3/ragCorpora/{code}-{aud}"
                    for aud in ("staff", "student")},
        "keys": {aud: f"{code}-{aud}-" + "secret" * 8 for aud in ("staff", "student")},
        "drive": {"driveIds": [f"drive-{code}"], "syncFolderIds": [f"folder-{code}"],
                  "studentFolderIds": [f"student-{code}"]},
        "buckets": {"hwpOriginal": f"{code}-hwp", "source": f"{code}-source"},
        "minInstances": {"staff": 0, "student": 0},
    } for code in ("cs", "ee")}
    record = {"serviceUrl": "https://shared-mcp.example", "revision": "v1"}
    service = {"status": {"conditions": [{"type": "Ready", "status": "True"}],
                          "latestReadyRevisionName": "rag-mcp-00001"}}
    mutations = []

    def mutate(method, code, argument=None):
        mutations.append((method, code, copy.deepcopy(argument)))
        if method == "update_department":
            replacement = copy.deepcopy(argument)
            for flag in ("mcpDisabledAudiences", "syncDisabled"):
                if flag not in replacement and flag in departments.get(code, {}):
                    replacement[flag] = copy.deepcopy(departments[code][flag])
            departments[code] = replacement
        elif method == "disable_audience":
            disabled = set(departments[code].get("mcpDisabledAudiences", []))
            departments[code]["mcpDisabledAudiences"] = sorted(disabled | {argument})
        elif method == "remove_department":
            departments.pop(code)

    monkeypatch.setattr(dept_gui, "_mcp_registry_read", lambda: (copy.deepcopy(record), copy.deepcopy(departments)))
    monkeypatch.setattr(dept_gui, "_mcp_registry_mutate", mutate)
    monkeypatch.setattr(dept_gui, "_unified_mcp_service", lambda: service)
    monkeypatch.setattr(dept_gui, "threading", SimpleNamespace(
        Thread=lambda **kwargs: SimpleNamespace(start=lambda: None)))
    monkeypatch.setattr(dept_gui, "_gcloud_json", lambda *a, **kw: pytest.fail("Unexpected live gcloud read"))
    monkeypatch.setattr(dept_gui, "_run_command", lambda *a, **kw: pytest.fail("Unexpected live shell mutation"))
    monkeypatch.setattr(dept_gui, "_run_mcp_deploy_script", lambda *a, **kw: pytest.fail("Must not deploy per-department services"))
    monkeypatch.setattr(dept_gui, "_deployed_sync_department_map", dict)
    dept_gui._LATEST.clear()
    dept_gui._MCP_DEPLOY_RUNS.clear()
    dept_gui._MCP_DEPLOY_CONFIGS.clear()
    dept_gui._TEARDOWN_RUNS.clear()
    yield SimpleNamespace(configs=departments, record=record, service=service,
                          mutations=mutations, dept_dir=dept_dir)
    dept_gui._LATEST.clear()
    dept_gui._MCP_DEPLOY_RUNS.clear()
    dept_gui._MCP_DEPLOY_CONFIGS.clear()
    dept_gui._TEARDOWN_RUNS.clear()


def test_registry_inventory_public_config_and_keys_override_stale_local_yaml(unified):
    stale = copy.deepcopy(unified.configs["cs"])
    stale["name"] = "stale local"
    stale["keys"]["student"] = "stale key"
    (unified.dept_dir / "cs.yaml").write_text(yaml.safe_dump(stale), encoding="utf-8")
    records = dept_gui.cloud_mcp_department_records()
    assert {r["code"] for r in records} == {"cs", "ee"}
    assert all(r["cloudOnly"] and r["cloudEditable"] and r["unifiedMcp"] for r in records)
    config = dept_gui.department_public_config_any("cs")
    assert config["name"] == "Cloud cs"
    assert config["source"] == "cloud" and config["unifiedMcp"]
    assert "keys" not in config
    assert dept_gui.department_mcp_key("cs", "student") == unified.configs["cs"]["keys"]["student"]
    public = json.dumps(records)
    assert all(key not in public for cfg in unified.configs.values() for key in cfg["keys"].values())
    assert not dept_gui.department_code_availability("ee")["available"]


def test_all_departments_and_audiences_share_uri_with_distinct_disabled_status(unified):
    unified.configs["cs"]["mcpDisabledAudiences"] = ["student"]
    servers = [server for code in unified.configs
               for server in dept_gui.department_mcp_servers(code)["servers"]]
    assert len(servers) == 4
    assert {server["serviceName"] for server in servers} == {"rag-mcp"}
    assert {server["mcpUrl"] for server in servers} == {"https://shared-mcp.example/mcp"}
    assert [server["status"] for server in servers].count("DISABLED") == 1
    with pytest.raises(ValueError, match="중지"):
        dept_gui.department_mcp_key("cs", "student")


def test_cloud_registry_failure_does_not_fall_back_to_old_local_config(unified, monkeypatch):
    (unified.dept_dir / "cs.yaml").write_text(yaml.safe_dump(unified.configs["cs"]), encoding="utf-8")
    def offline():
        raise RuntimeError("Registry offline")
    monkeypatch.setattr(dept_gui, "_mcp_registry_read", offline)
    with pytest.raises(RuntimeError):
        dept_gui.department_public_config_any("cs")


def test_delayed_config_edit_cannot_undo_new_scope_or_sync_revocation(unified):
    stale_editor = copy.deepcopy(unified.configs["cs"])
    stale_editor["mcpDisabledAudiences"] = []
    stale_editor["syncDisabled"] = False
    unified.configs["cs"]["mcpDisabledAudiences"] = ["student"]
    unified.configs["cs"]["syncDisabled"] = True
    dept_gui._update_cloud_department_config("cs", stale_editor)
    assert unified.configs["cs"]["mcpDisabledAudiences"] == ["student"]
    assert unified.configs["cs"]["syncDisabled"] is True


def test_teardown_plan_stops_routes_and_sync_before_data_and_never_deletes_common_mcp(unified):
    plan = dept_gui.department_teardown_plan("cs")
    targets = plan["targets"]
    assert plan["unifiedMcp"]
    assert [target["kind"] for target in targets[:3]] == ["mcpRoute", "mcpRoute", "syncEnv"]
    assert targets[-1]["key"] == "registry-config"
    assert not any(target["kind"] == "cloudRun" or target["name"] == "rag-mcp" for target in targets)
    assert all(target["meta"]["audience"] in {"staff", "student"}
               for target in targets if target["kind"] == "mcpRoute")


@pytest.mark.parametrize("selection", [["corpus-staff"], ["registry-config"],
                                       ["mcp-staff", "sync-env", "corpus-staff"]])
def test_data_or_registry_removal_rejects_incomplete_access_stop_selection(unified, selection):
    with pytest.raises(ValueError):
        dept_gui.apply_teardown_selection(dept_gui.department_teardown_plan("cs"), selection)


def test_partial_cleanup_preserves_registry_and_shared_resources(unified):
    unified.configs["ee"]["buckets"]["source"] = unified.configs["cs"]["buckets"]["source"]
    plan = dept_gui.department_teardown_plan("cs")
    selected = dept_gui.apply_teardown_selection(plan, ["mcp-staff", "mcp-student", "sync-env", "corpus-staff", "bucket-source"])
    rows = {row["key"]: row for row in selected["targets"]}
    assert rows["registry-config"]["skipped"]
    assert rows["bucket-source"]["skipped"] and not rows["bucket-source"]["selectable"]


def test_sync_stop_persists_and_stale_local_or_deployed_map_cannot_reactivate(unified, monkeypatch):
    stale = copy.deepcopy(unified.configs["cs"])
    (unified.dept_dir / "cs.yaml").write_text(yaml.safe_dump(stale), encoding="utf-8")
    expected_maps = []
    monkeypatch.setattr(dept_gui, "update_sync_department_map", lambda **kw: expected_maps.append(json.loads(kw["expected"])) or "ee")
    dept_gui._refresh_cloud_teardown_routing("cs")
    assert unified.configs["cs"]["syncDisabled"] is True
    assert set(expected_maps[0]) == {"ee"}
    assert set(json.loads(dept_gui.cloud_departments_json())) == {"ee"}
    monkeypatch.setattr(dept_gui, "_deployed_sync_department_map", lambda: {
        "cs": {"driveIds": ["drive-cs"]}, "ee": {"driveIds": ["drive-ee"]},
        "zz": {"driveIds": ["unregistered"]},
    })
    targets, owners = dept_gui._sync_department_targets()
    assert set(targets) == {"ee"}
    assert owners == {"drive-ee": "ee"}


def test_last_active_sync_department_requires_removed_runtime_even_if_other_configs_remain(unified, monkeypatch):
    unified.configs["ee"]["syncDisabled"] = True
    def runtime_present():
        raise ValueError("Sync runtime still exists")
    monkeypatch.setattr(dept_gui, "_require_sync_removed", runtime_present)
    with pytest.raises(ValueError, match="Sync"):
        dept_gui.start_teardown_run(dept_gui.department_teardown_plan("cs"), "cs", ["sync-env"])
    assert unified.mutations == []


def test_failed_scope_stop_skips_all_resource_deletion_and_retains_registry(unified, monkeypatch):
    plan = dept_gui.department_teardown_plan("cs")
    calls = []
    def fail_scope(method, *args):
        calls.append(method)
        raise RuntimeError("Registry update failure")
    monkeypatch.setattr(dept_gui, "_mcp_registry_mutate", fail_scope)
    run = dept_gui.start_teardown_run(plan, "cs")
    dept_gui._execute_teardown_run(run["runId"])
    result = dept_gui._TEARDOWN_RUNS[run["runId"]]
    assert result["status"] == "FAILED"
    assert set(calls) == {"disable_audience"}
    assert all(row["status"] == "SKIPPED" for row in result["targets"] if row["kind"] != "mcpRoute")
    assert "cs" in unified.configs


def test_shared_service_guard_blocks_even_a_malformed_department_plan(unified, monkeypatch):
    plan = dept_gui.department_teardown_plan("cs")
    plan["targets"] = [dept_gui._teardown_target("common", "cloudRun", "Bad target", "rag-mcp", [])]
    monkeypatch.setattr(dept_gui, "_delete_cloud_run_service", lambda *args: pytest.fail("Shared MCP deletion attempted"))
    run = dept_gui.start_teardown_run(plan, "cs")
    dept_gui._execute_teardown_run(run["runId"])
    assert dept_gui._TEARDOWN_RUNS[run["runId"]]["status"] == "FAILED"


def test_full_cleanup_disables_access_and_sync_before_data_and_removes_config_last(unified, monkeypatch):
    events = []
    original_mutate = dept_gui._mcp_registry_mutate

    def mutate(method, *args):
        events.append(method)
        return original_mutate(method, *args)

    def delete(*args):
        events.append("delete-data")
        assert unified.configs["cs"]["syncDisabled"] is True
        assert set(unified.configs["cs"]["mcpDisabledAudiences"]) == {"staff", "student"}
        return "deleted"

    monkeypatch.setattr(dept_gui, "_mcp_registry_mutate", mutate)
    monkeypatch.setattr(dept_gui, "update_sync_department_map", lambda **kw: events.append("sync-map") or "ee")
    monkeypatch.setattr(dept_gui, "_provision_access_token", lambda: "fake-token")
    for helper in ("_delete_rag_corpus", "_delete_bucket_resource", "_delete_department_firestore_state", "_delete_gcs_prefix"):
        monkeypatch.setattr(dept_gui, helper, delete)
    monkeypatch.setattr(dept_gui, "_delete_cloud_run_service", lambda *args: pytest.fail("Shared MCP deletion attempted"))
    run = dept_gui.start_teardown_run(dept_gui.department_teardown_plan("cs"), "cs")
    dept_gui._execute_teardown_run(run["runId"])
    assert dept_gui._TEARDOWN_RUNS[run["runId"]]["status"] == "COMPLETED"
    assert events[:4] == ["disable_audience", "disable_audience", "update_department", "sync-map"]
    assert "delete-data" in events[4:-1]
    assert events[-1] == "remove_department"
    assert set(unified.configs) == {"ee"}


@pytest.mark.parametrize("new_department,disabled", [(False, False), (True, False), (False, True)])
def test_deployment_registers_cloud_routes_without_building_or_deploying_department_services(
    unified, monkeypatch, new_department, disabled
):
    config = copy.deepcopy(unified.configs["cs"])
    if new_department:
        unified.configs.pop("cs")
        (unified.dept_dir / "cs.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    elif disabled:
        unified.configs["cs"]["mcpDisabledAudiences"] = ["staff", "student"]
        unified.configs["cs"]["syncDisabled"] = True
    health_calls, maps = [], []
    monkeypatch.setattr(dept_gui, "_http_json", lambda url, **kw: health_calls.append(url) or (200, {"status": "ok"}, 1))
    monkeypatch.setattr(dept_gui, "update_sync_department_map", lambda **kw: maps.append(json.loads(dept_gui.cloud_departments_json())) or "ee")
    run = dept_gui.start_mcp_deployment("cs")
    assert run["serviceNames"] == ["rag-mcp"] and run["unifiedMcp"]
    dept_gui._execute_mcp_deployment(run["runId"])
    result = dept_gui._MCP_DEPLOY_RUNS[run["runId"]]
    assert result["status"] == "COMPLETED", result
    assert [method for method, *_ in unified.mutations] == ["update_department"]
    assert health_calls == ["https://shared-mcp.example/health"]
    assert set(maps[0]) == ({"ee"} if disabled else {"cs", "ee"})
    assert config["keys"]["staff"] not in json.dumps(result)
