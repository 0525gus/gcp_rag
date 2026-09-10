import base64
import copy
import json
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

from shared.mcp_routing import RouteRegistry, key_digest, parse_routes
from shared.models import Audience, DocState, DocStatus, SearchHit, SearchSource


def snapshot():
    return {"schemaVersion": 1, "enabled": True, "revision": "v1", "routesJson": json.dumps({
        key_digest(f"{dept}-{aud}"): {
            "department": dept, "audience": aud,
            "corpus": f"projects/test-project/locations/asia-northeast3/ragCorpora/{dept}-{aud}",
            "driveIds": [f"drive-{dept}"],
        } for dept in ("cs", "ee") for aud in ("staff", "student")
    })}


@pytest.fixture
def routed(monkeypatch):
    for key, value in {"GCP_PROJECT_ID": "test-project", "GCS_HWP_ORIGINAL_BUCKET": "raw",
                       "GCS_SOURCE_BUCKET": "norm", "RAG_CORPUS_NAME": "unused"}.items():
        monkeypatch.setenv(key, value)
    import services.mcp_server.main as app
    config = replace(app.settings, gcp_project_id="test-project", gcp_region="asia-northeast3",
                     search_lexical_rerank=False)
    current = snapshot()
    registry = RouteRegistry(config, loader=lambda: current, ttl=0)
    calls = []

    class Rag:
        def __init__(self, settings):
            self.scope = settings.rag_corpus_name.rsplit("/", 1)[-1]

        def retrieve(self, query, **kwargs):
            calls.append(self.scope)
            time.sleep(0.03)
            # Return adversarial candidates too: another dept, other audience, missing metadata.
            return [SearchHit(fid, 0.1, SearchSource(fid))
                    for fid in [self.scope, self.scope.split("-")[0] + "-student",
                                "cs-staff", "ee-student", "unknown"]]

    class Store:
        def get(self, fid):
            if fid == "unknown":
                return None
            dept, aud = fid.split("-")
            return DocState(file_id=fid, drive_id=f"drive-{dept}", audience=Audience(aud.upper()),
                            status=DocStatus.INDEXED, name=fid)

    monkeypatch.setattr(app, "settings", config)
    monkeypatch.setattr(app, "ROUTING_MODE", "registry")
    monkeypatch.setattr(app, "route_registry", registry)
    monkeypatch.setattr(app, "RagEngineClient", Rag)
    monkeypatch.setattr(app, "DocStateStore", lambda *_: Store())
    app._cache.clear()
    app.mcp._session_manager = None
    with TestClient(app.build_app()) as client:
        yield app, client, current, calls
    app._cache.clear()
    app.mcp._session_manager = None


def rpc(client, key, method="tools/call", arguments=None):
    response = client.post("/mcp", headers={"Authorization": f"Bearer {key}",
                           "Accept": "application/json, text/event-stream"}, json={
        "jsonrpc": "2.0", "id": 1, "method": method,
        "params": {"name": "search", "arguments": arguments or {"query": "same"}}
                  if method == "tools/call" else {},
    })
    if response.status_code != 200:
        return response.status_code, response.json()
    raw = response.text
    if raw.startswith(("event:", "data:")):
        raw = next(line[5:].strip() for line in raw.splitlines() if line.startswith("data:"))
    return response.status_code, json.loads(raw)


def test_single_uri_scopes_and_cache_are_isolated_under_concurrent_requests(routed):
    _, client, _, calls = routed
    keys = ["cs-staff", "cs-student", "ee-staff", "ee-student"] * 3
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda key: rpc(client, key), keys))
    for key, (status, response) in zip(keys, results):
        assert status == 200
        assert "structuredContent" in response["result"], response
        docs = response["result"]["structuredContent"]["documents"]
        department, audience = key.split("-")
        expected = {key, f"{department}-student"} if audience == "staff" else {key}
        assert docs and {d["source"]["fileId"] for d in docs} == expected
    assert set(calls) == set(keys)
    before = len(calls)
    rpc(client, "ee-student")
    assert len(calls) == before


def test_unknown_key_revocation_and_registry_failure_fail_closed(routed):
    _, client, current, calls = routed
    assert rpc(client, "bad")[0] == 401
    rpc(client, "cs-staff")
    before = len(calls)
    current["routesJson"] = "{}"
    assert rpc(client, "cs-staff")[0] == 401
    current["routesJson"] = "broken"
    assert rpc(client, "cs-staff")[0] == 503
    assert len(calls) == before


def test_tool_inputs_cannot_override_authenticated_scope(routed):
    _, client, _, calls = routed
    tools = rpc(client, "cs-student", "tools/list")[1]["result"]["tools"]
    assert set(tools[0]["inputSchema"]["properties"]) == {"query", "top_k", "drive_id"}
    _, response = rpc(client, "cs-student", arguments={"query": "same", "drive_id": "drive-ee"})
    assert response["result"]["isError"]
    assert not calls
    _, response = rpc(client, "cs-student", arguments={"query": "same", "department": "ee", "audience": "staff"})
    if not response.get("result", {}).get("isError"):
        assert {d["source"]["fileId"] for d in response["result"]["structuredContent"]["documents"]} == {"cs-student"}


def test_no_ambient_scope_or_fallback_corpus(routed):
    app, _, _, _ = routed
    with pytest.raises(PermissionError):
        app.search("question")


def test_expired_registry_does_not_reuse_stale_credentials():
    current = snapshot()
    def loader():
        if current is None:
            raise RuntimeError("offline")
        return current
    registry = RouteRegistry(SimpleNamespace(gcp_project_id="test-project", gcp_region="asia-northeast3",
                                            firestore_database="test"), loader=loader, ttl=0)
    assert registry.resolve("cs-staff")
    current = None
    with pytest.raises(RuntimeError):
        registry.resolve("cs-staff")
    assert registry._routes == {}


def test_registry_rejects_foreign_project_or_unknown_audience():
    current = snapshot()
    with pytest.raises(ValueError):
        parse_routes(current, "different-project", "asia-northeast3")
    routes = json.loads(current["routesJson"])
    routes[key_digest("cs-staff")]["audience"] = "admin"
    current["routesJson"] = json.dumps(routes)
    with pytest.raises(ValueError):
        parse_routes(current, "test-project", "asia-northeast3")


@pytest.mark.parametrize("field,value", [
    ("department", None), ("department", "../ee"), ("audience", []),
    ("audience", "STAFF"), ("corpus", None), ("driveIds", []),
    ("driveIds", [" "]), ("driveIds", ["drive/cs"]), ("enabled", "false"),
])
def test_malformed_registry_scope_is_rejected(field, value):
    current = snapshot()
    routes = json.loads(current["routesJson"])
    routes[key_digest("cs-staff")][field] = value
    current["routesJson"] = json.dumps(routes)
    with pytest.raises(ValueError):
        parse_routes(current, "test-project", "asia-northeast3")


def test_duplicate_credential_records_are_rejected_instead_of_last_wins():
    current = snapshot()
    routes = json.loads(current["routesJson"])
    digest = key_digest("cs-staff")
    current["routesJson"] = (
        "{" + json.dumps(digest) + ":" + json.dumps(routes[digest]) + ","
        + json.dumps(digest) + ":" + json.dumps(routes[key_digest("ee-student")]) + "}"
    )
    with pytest.raises(ValueError, match="Duplicate"):
        parse_routes(current, "test-project", "asia-northeast3")


def test_registry_revision_invalidates_search_cache(routed):
    _, client, current, calls = routed
    rpc(client, "cs-student")
    before = len(calls)
    rpc(client, "cs-student")
    assert len(calls) == before
    current["revision"] = "v2"
    rpc(client, "cs-student")
    assert len(calls) == before + 1


def test_key_rotation_revokes_old_key_and_repartitions_cached_search(routed):
    _, client, current, calls = routed
    rpc(client, "cs-student")
    before = len(calls)
    routes = json.loads(current["routesJson"])
    routes[key_digest("rotated-cs-student")] = routes.pop(key_digest("cs-student"))
    current.update(revision="rotated-v2", routesJson=json.dumps(routes))
    assert rpc(client, "cs-student")[0] == 401
    assert len(calls) == before
    status, response = rpc(client, "rotated-cs-student")
    assert status == 200
    assert {doc["source"]["fileId"] for doc in response["result"]["structuredContent"]["documents"]} == {"cs-student"}
    assert len(calls) == before + 1
    rpc(client, "rotated-cs-student")
    assert len(calls) == before + 1


def test_registry_revocation_is_observed_at_ttl_without_cached_fallback(monkeypatch):
    import shared.mcp_routing as routing
    current = snapshot()
    now = [100.0]
    reads = []
    monkeypatch.setattr(routing.time, "monotonic", lambda: now[0])

    def loader():
        reads.append(now[0])
        return current

    registry = RouteRegistry(SimpleNamespace(gcp_project_id="test-project", gcp_region="asia-northeast3",
                                            firestore_database="test"), loader=loader, ttl=5)
    assert registry.resolve("cs-staff")
    current["routesJson"] = "{}"
    now[0] = 104.9
    assert registry.resolve("cs-staff")  # Explicit, bounded cache lifetime.
    now[0] = 105
    assert registry.resolve("cs-staff") is None
    assert reads == [100, 105]


def configs():
    return {dept: {
        "corpora": {aud: f"projects/test-project/locations/asia-northeast3/ragCorpora/{dept}-{aud}"
                    for aud in ("staff", "student")},
        "keys": {aud: f"secret-{dept}-{aud}-" + "x" * 24 for aud in ("staff", "student")},
        "drive": {"driveIds": [f"drive-{dept}"]},
    } for dept in ("cs", "ee")}


def test_registry_admin_rejects_key_shared_by_multiple_scopes():
    from scripts.mcp_registry import routing_snapshot
    departments = configs()
    departments["ee"]["keys"]["student"] = departments["cs"]["keys"]["staff"]
    with pytest.raises(ValueError, match="multiple scopes"):
        routing_snapshot(departments, "test-project", "asia-northeast3")


def test_registry_contains_only_key_hashes_and_excludes_disabled_scopes():
    from scripts.mcp_registry import routing_snapshot
    departments = configs()
    departments["cs"]["mcpDisabledAudiences"] = ["student"]
    departments["cs"]["syncDisabled"] = True
    record = routing_snapshot(departments, "test-project", "asia-northeast3")
    routes = parse_routes(record, "test-project", "asia-northeast3")
    assert len(routes) == 3
    assert key_digest(departments["cs"]["keys"]["student"]) not in routes
    assert all(key not in json.dumps(record) for cfg in departments.values() for key in cfg["keys"].values())


def test_department_save_preserves_disabled_scopes_until_explicitly_enabled():
    from scripts.mcp_registry import RegistryAdmin
    admin = RegistryAdmin.__new__(RegistryAdmin)
    departments = configs()
    departments["cs"]["mcpDisabledAudiences"] = ["student"]
    departments["cs"]["syncDisabled"] = True
    admin.read = lambda: (None, {"revision": "v1"}, copy.deepcopy(departments))
    writes = []
    admin.write = lambda updated, **kwargs: writes.append((updated, kwargs))
    replacement = configs()["cs"]
    admin.update_department("cs", replacement)
    assert writes[-1][0]["cs"]["mcpDisabledAudiences"] == ["student"]
    assert writes[-1][0]["cs"]["syncDisabled"] is True
    assert writes[-1][1]["expected_revision"] == "v1"
    replacement["mcpDisabledAudiences"] = []
    replacement["syncDisabled"] = False
    admin.update_department("cs", replacement)
    assert writes[-1][0]["cs"]["mcpDisabledAudiences"] == []
    assert writes[-1][0]["cs"]["syncDisabled"] is False


@pytest.mark.parametrize("secret_failure,conflict", [(False, False), (True, False), (False, True)])
def test_registry_activation_is_pinned_atomic_and_preserves_live_state_on_failure(secret_failure, conflict):
    from google.api_core.exceptions import FailedPrecondition

    from scripts.mcp_registry import SECRET_ID, RegistryAdmin
    admin = RegistryAdmin.__new__(RegistryAdmin)
    admin.project, admin.region = "test-project", "asia-northeast3"
    previous = {"revision": "v1", "configSecretVersion": "old-version", "serviceUrl": "https://mcp.example"}
    stamp = object()
    admin.read = lambda: (SimpleNamespace(update_time=stamp), copy.deepcopy(previous), {})
    state = copy.deepcopy(previous)
    order = []
    versions = []
    departments = configs()

    def response(status, body):
        return SimpleNamespace(status_code=status, json=lambda: body)

    def post(url, **kwargs):
        order.append("secret")
        versions.append(json.loads(base64.b64decode(kwargs["json"]["payload"]["data"])))
        return response(500 if secret_failure else 200,
                        {"name": f"projects/test-project/secrets/{SECRET_ID}/versions/42"})

    def get(url, **kwargs):
        # A concurrent caller edit cannot change the configs captured for this publication.
        departments["cs"]["keys"]["staff"] = "modified-by-caller"
        return response(200, {})

    def update(record, *, option, **kwargs):
        order.append("registry")
        assert option._last_update_time is stamp
        if conflict:
            raise FailedPrecondition("Another writer won")
        state.update(record)

    admin.http = SimpleNamespace(get=get, post=post)
    admin.doc = SimpleNamespace(update=update)
    if secret_failure or conflict:
        with pytest.raises((RuntimeError, FailedPrecondition)):
            admin.write(departments, expected_revision="v1")
        assert state == previous
        assert order == (["secret"] if secret_failure else ["secret", "registry"])
    else:
        record = admin.write(departments, expected_revision="v1")
        assert order == ["secret", "registry"]
        assert record["configSecretVersion"].endswith("/versions/42")
        assert record["serviceUrl"] == previous["serviceUrl"]
        assert key_digest(versions[0]["cs"]["keys"]["staff"]) in json.loads(record["routesJson"])
        assert versions[0]["cs"]["keys"]["staff"] != departments["cs"]["keys"]["staff"]


def test_stale_registry_editor_cannot_publish():
    from scripts.mcp_registry import RegistryAdmin
    admin = RegistryAdmin.__new__(RegistryAdmin)
    admin.read = lambda: (object(), {"revision": "v2"}, {})
    with pytest.raises(RuntimeError, match="reload"):
        admin.write(configs(), expected_revision="v1")


@pytest.mark.parametrize("version", ["latest", "0", "-1", "1/../../other", "\u0661"])
def test_registry_read_rejects_nonimmutable_secret_pointer(version):
    from scripts.mcp_registry import SECRET_ID, RegistryAdmin
    admin = RegistryAdmin.__new__(RegistryAdmin)
    admin.project = "test-project"
    record = {"configSecretVersion": f"projects/test-project/secrets/{SECRET_ID}/versions/{version}"}
    admin.doc = SimpleNamespace(get=lambda **kwargs: SimpleNamespace(exists=True, to_dict=lambda: record))
    with pytest.raises(ValueError, match="secret version"):
        admin.read()


@pytest.mark.parametrize("stored_project", ["test-project", "929655581748", "999999999999"])
def test_registry_read_accepts_only_verified_numeric_project_alias(stored_project):
    from scripts.mcp_registry import SECRET_ID, RegistryAdmin
    admin = RegistryAdmin.__new__(RegistryAdmin)
    admin.project = "test-project"
    record = {"configSecretVersion": f"projects/{stored_project}/secrets/{SECRET_ID}/versions/42"}
    admin.doc = SimpleNamespace(get=lambda **kwargs: SimpleNamespace(exists=True, to_dict=lambda: record))
    accesses = []
    expected = configs()

    def get(url, **kwargs):
        accesses.append(url)
        body = {
            "name": f"projects/929655581748/secrets/{SECRET_ID}/versions/42",
            "payload": {"data": base64.b64encode(json.dumps(expected).encode()).decode()},
        }
        return SimpleNamespace(status_code=200, json=lambda: body)

    admin.http = SimpleNamespace(get=get)
    if stored_project == "999999999999":
        with pytest.raises(ValueError, match="another project"):
            admin.read()
    else:
        assert admin.read()[2] == expected
    assert accesses == [
        f"https://secretmanager.googleapis.com/v1/projects/test-project/secrets/{SECRET_ID}/versions/42:access"
    ]
