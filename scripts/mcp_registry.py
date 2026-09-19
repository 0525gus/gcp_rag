"""Cloud control plane for unified MCP: Secret Manager configs + hashed Firestore routes."""

from __future__ import annotations

import base64
import copy
import json
import re
import uuid

from google.auth.transport.requests import AuthorizedSession
from google.cloud import firestore
from google.oauth2.credentials import Credentials

from shared.mcp_routing import key_digest, parse_routes

SECRET_ID = "rag-mcp-departments"


def routing_snapshot(configs: dict, project: str, region: str) -> dict:
    if not isinstance(configs, dict):
        raise TypeError("MCP department configurations must be an object")
    routes = {}
    for code, config in configs.items():
        if not isinstance(config, dict):
            raise TypeError("Invalid MCP department configuration")
        disabled = config.get("mcpDisabledAudiences", [])
        if not isinstance(disabled, list) or any(
            not isinstance(audience, str) or audience not in {"staff", "student"}
            for audience in disabled
        ):
            raise ValueError("Invalid disabled MCP audiences")
        for audience in ("staff", "student"):
            corpus = (config.get("corpora") or {}).get(audience)
            if not corpus or audience in disabled:
                continue
            key = (config.get("keys") or {}).get(audience, "")
            if not isinstance(key, str) or not 24 <= len(key) <= 512 or key != key.strip():
                raise ValueError(f"{code}/{audience}: a strong existing API key is required")
            digest = key_digest(key)
            if digest in routes:
                raise ValueError("One API key cannot identify multiple scopes")
            routes[digest] = {"department": code, "audience": audience, "corpus": corpus,
                              "driveIds": (config.get("drive") or {}).get("driveIds", [])}
    result = {"schemaVersion": 1, "enabled": True, "revision": uuid.uuid4().hex,
              "routesJson": json.dumps(routes, separators=(",", ":"))}
    parse_routes(result, project, region)
    return result


class RegistryAdmin:
    def __init__(self, project: str, region: str, database: str, token: str):
        self.project, self.region = project, region
        credentials = Credentials(token)
        self.db = firestore.Client(project=project, database=database, credentials=credentials)
        self.doc = self.db.document("mcp_registry/current")
        self.http = AuthorizedSession(credentials)

    def read(self):
        snap = self.doc.get(timeout=15)
        if not snap.exists:
            return None, None, {}
        record = snap.to_dict()
        version = record.get("configSecretVersion", "")
        prefix = f"projects/{self.project}/secrets/{SECRET_ID}/versions/"
        match = re.fullmatch(
            rf"projects/({re.escape(self.project)}|[0-9]+)/secrets/{SECRET_ID}/versions/([1-9][0-9]*)",
            version if isinstance(version, str) else "",
        )
        if not match:
            raise ValueError("Invalid configuration secret version")
        # Secret Manager returns project numbers even when called with a project ID.
        # Always access the configured project; a numeric pointer must match the
        # response's canonical name before its configuration can be used.
        requested_version = prefix + match.group(2)
        response = self.http.get(
            f"https://secretmanager.googleapis.com/v1/{requested_version}:access", timeout=20
        )
        if response.status_code != 200:
            raise RuntimeError(f"Cannot read MCP configuration secret (HTTP {response.status_code})")
        body = response.json()
        if match.group(1) != self.project and body.get("name") != version:
            raise ValueError("Configuration secret version belongs to another project")
        configs = json.loads(base64.b64decode(body["payload"]["data"]))
        if not isinstance(configs, dict):
            raise ValueError("Invalid MCP department configurations")  # noqa: TRY004
        return snap, record, configs

    def write(self, configs: dict, *, service_url: str = "", expected_revision: str | None = None):
        # Freeze the caller's input so routes and their secret cannot diverge during HTTP calls.
        configs = copy.deepcopy(configs)
        snap, previous, _ = self.read()
        if expected_revision is not None and (previous or {}).get("revision") != expected_revision:
            raise RuntimeError("MCP registry changed; reload before updating")
        record = routing_snapshot(configs, self.project, self.region)
        base = f"https://secretmanager.googleapis.com/v1/projects/{self.project}"
        response = self.http.get(f"{base}/secrets/{SECRET_ID}", timeout=20)
        if response.status_code == 404:
            response = self.http.post(f"{base}/secrets", params={"secretId": SECRET_ID},
                                      json={"replication": {"automatic": {}}}, timeout=20)
        if response.status_code not in (200, 201):
            raise RuntimeError(f"Cannot prepare MCP config secret (HTTP {response.status_code})")
        payload = base64.b64encode(json.dumps(configs, ensure_ascii=False).encode()).decode()
        response = self.http.post(f"{base}/secrets/{SECRET_ID}:addVersion",
                                  json={"payload": {"data": payload}}, timeout=20)
        if response.status_code != 200:
            raise RuntimeError(f"Cannot store MCP config secret (HTTP {response.status_code})")
        record.update(configSecretVersion=response.json()["name"], serviceName="rag-mcp",
                      serviceUrl=service_url or (previous or {}).get("serviceUrl", ""))
        if snap is None:
            self.doc.create(record, timeout=15)
        else:
            self.doc.update(record, option=firestore.LastUpdateOption(snap.update_time), timeout=15)
        return record

    def update_department(self, code: str, config: dict):
        _, current, configs = self.read()
        replacement = copy.deepcopy(config)
        if "mcpDisabledAudiences" not in replacement:
            replacement["mcpDisabledAudiences"] = copy.deepcopy(
                configs.get(code, {}).get("mcpDisabledAudiences", [])
            )
        if "syncDisabled" not in replacement and "syncDisabled" in configs.get(code, {}):
            replacement["syncDisabled"] = configs[code]["syncDisabled"]
        configs[code] = replacement
        return self.write(
            configs,
            expected_revision=current["revision"] if current is not None else None,
        )

    def disable_audience(self, code: str, audience: str):
        if audience not in {"staff", "student"}:
            raise ValueError("Invalid MCP audience")
        _, current, configs = self.read()
        if code not in configs:
            return
        disabled = set(configs[code].get("mcpDisabledAudiences", []))
        disabled.add(audience)
        configs[code]["mcpDisabledAudiences"] = sorted(disabled)
        self.write(configs, expected_revision=current["revision"])

    def remove_department(self, code: str):
        _, current, configs = self.read()
        if code not in configs:
            return
        configs.pop(code)
        self.write(configs, expected_revision=current["revision"])
