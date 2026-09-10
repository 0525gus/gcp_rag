"""Credential-bound MCP routing. Registry contains hashes, never API keys."""

from __future__ import annotations

import hashlib
import json
import math
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from google.cloud import firestore


@dataclass(frozen=True)
class SearchScope:
    department: str
    audience: str
    corpus: str
    drive_ids: tuple[str, ...]
    version: str

    @property
    def cache_partition(self) -> tuple:
        return (self.department, self.audience, self.corpus, self.drive_ids, self.version)


def key_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate MCP registry field")
        result[key] = value
    return result


def parse_routes(snapshot: dict, project: str, region: str) -> dict[str, SearchScope]:
    if (
        not isinstance(snapshot, dict)
        or type(snapshot.get("schemaVersion")) is not int
        or snapshot.get("schemaVersion") != 1
        or snapshot.get("enabled") is not True
    ):
        raise ValueError("MCP registry is unavailable")
    version = snapshot.get("revision")
    encoded_routes = snapshot.get("routesJson")
    if not isinstance(version, str) or not version.strip() or not isinstance(encoded_routes, str):
        raise ValueError("Invalid MCP registry")
    routes = json.loads(encoded_routes, object_pairs_hook=_unique_object)
    if not isinstance(routes, dict):
        raise ValueError("Invalid MCP registry")  # noqa: TRY004 - invalid serialized data
    result = {}
    for digest, item in routes.items():
        if not re.fullmatch(r"[a-f0-9]{64}", digest) or not isinstance(item, dict):
            raise ValueError("Invalid MCP credential record")
        if "enabled" in item and type(item["enabled"]) is not bool:
            raise ValueError("Invalid MCP credential status")
        if item.get("enabled") is False:
            continue
        department = item.get("department", "")
        audience = item.get("audience")
        corpus = item.get("corpus", "")
        drives = item.get("driveIds")
        if (
            not isinstance(department, str)
            or not re.fullmatch(r"[a-z][a-z0-9-]{1,19}", department)
            or not isinstance(audience, str) or audience not in {"staff", "student"}
            or not isinstance(corpus, str)
            or not re.fullmatch(
                rf"projects/{re.escape(project)}/locations/{re.escape(region)}/ragCorpora/[A-Za-z0-9_-]+", corpus
            )
            or not isinstance(drives, list) or not drives
            or any(not isinstance(drive, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", drive)
                   for drive in drives)
        ):
            raise ValueError("Invalid MCP routing scope")
        result[digest] = SearchScope(department, audience, corpus, tuple(drives), version)
    return result


class RouteRegistry:
    """Bounded credential cache. Expired snapshots never survive a read failure."""

    def __init__(self, settings, *, loader: Callable | None = None, ttl: float = 5):
        if not math.isfinite(ttl) or ttl < 0:
            raise ValueError("MCP registry TTL must be finite and nonnegative")
        self.project = settings.gcp_project_id
        self.region = settings.gcp_region
        self.database = settings.firestore_database
        self.loader = loader or self._read
        self.ttl = ttl
        self._expires = 0.0
        self._routes: dict[str, SearchScope] = {}
        self._lock = threading.Lock()
        self._db = None

    def _read(self):
        if self._db is None:
            self._db = firestore.Client(project=self.project, database=self.database)
        snap = self._db.document("mcp_registry/current").get(timeout=10)
        return snap.to_dict() if snap.exists else {}

    def resolve(self, token: str) -> SearchScope | None:
        if not isinstance(token, str) or not token or len(token) > 512:
            return None
        with self._lock:
            if time.monotonic() >= self._expires:
                self._routes = {}
                self._expires = 0
                started = time.monotonic()
                self._routes = parse_routes(self.loader(), self.project, self.region)
                # A slow read must not extend the lifetime of credentials read before revocation.
                self._expires = started + self.ttl
            return self._routes.get(key_digest(token))
