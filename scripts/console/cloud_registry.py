from __future__ import annotations

import copy
import json
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from scripts import dept_config, gcp_cli
from scripts.console import core, sync_history

_MCP_REGISTRY_READ_CACHE_LOCK = threading.Lock()


_MCP_REGISTRY_READ_CACHE: dict[tuple[str, str, str], dict[str, Any]] = {}


_MCP_REGISTRY_READ_CACHE_TTL_SECONDS = 15


_UNIFIED_MCP_SERVICE_CACHE_LOCK = threading.Lock()


_UNIFIED_MCP_SERVICE_CACHE: dict[tuple[str, str], dict[str, Any]] = {}


_UNIFIED_MCP_SERVICE_CACHE_TTL_SECONDS = 15


def _mcp_registry_admin() -> Any:
    from scripts.mcp_registry import RegistryAdmin

    common = core._common()
    return RegistryAdmin(
        str(common.get("GCP_PROJECT_ID") or ""),
        str(common["GCP_REGION"]),
        str(common["FIRESTORE_DATABASE"]),
        gcp_cli.access_token(),
    )


def _mcp_registry_read(
    *, allow_empty: bool = False
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    common = core._common()
    cache_key = (
        str(common.get("GCP_PROJECT_ID") or ""),
        str(common["GCP_REGION"]),
        str(common["FIRESTORE_DATABASE"]),
    )
    now = time.monotonic()
    with _MCP_REGISTRY_READ_CACHE_LOCK:
        cached = _MCP_REGISTRY_READ_CACHE.get(cache_key)
        if cached and now - float(cached["created"]) < _MCP_REGISTRY_READ_CACHE_TTL_SECONDS:
            record = copy.deepcopy(cached["record"])
            configs = copy.deepcopy(cached["configs"])
        else:
            try:
                _snap, record, configs = _mcp_registry_admin().read()
            except Exception as exc:
                raise RuntimeError("통합 MCP Cloud 등록부 조회에 실패했습니다.") from exc
            _MCP_REGISTRY_READ_CACHE[cache_key] = {
                "created": now,
                "record": copy.deepcopy(record),
                "configs": copy.deepcopy(configs),
            }
    if not record and allow_empty:
        return {}, {}
    if not record or not isinstance(configs, dict):
        raise RuntimeError("통합 MCP Cloud 등록부가 초기화되지 않았습니다.")
    return record, copy.deepcopy(configs)


def _mcp_registry_mutate(method: str, *args: Any) -> Any:
    try:
        result = getattr(_mcp_registry_admin(), method)(*args)
    except Exception as exc:
        raise RuntimeError("통합 MCP Cloud 등록부 변경에 실패했습니다. 설정을 새로 조회한 후 다시 시도해 주세요.") from exc
    with _MCP_REGISTRY_READ_CACHE_LOCK:
        _MCP_REGISTRY_READ_CACHE.clear()
    return result


def _unified_mcp_service() -> dict[str, Any]:
    common = core._common()
    project = str(common.get("GCP_PROJECT_ID") or "")
    region = str(common["GCP_REGION"])
    cache_key = (project, region)
    now = time.monotonic()
    with _UNIFIED_MCP_SERVICE_CACHE_LOCK:
        cached = _UNIFIED_MCP_SERVICE_CACHE.get(cache_key)
        if cached and now - float(cached["created"]) < _UNIFIED_MCP_SERVICE_CACHE_TTL_SECONDS:
            return copy.deepcopy(cached["service"])
        ok, service = gcp_cli.gcloud_json([
            "run", "services", "describe", "rag-mcp",
            f"--region={region}",
            f"--project={project}",
        ], timeout=30)
        if ok and isinstance(service, dict):
            _UNIFIED_MCP_SERVICE_CACHE[cache_key] = {
                "created": now,
                "service": copy.deepcopy(service),
            }
    if not ok or not isinstance(service, dict):
        raise RuntimeError("공통 rag-mcp 서비스를 조회하지 못했습니다.")
    return service


def _unified_mcp_servers(
    config: dict[str, Any], record: dict[str, Any], service: dict[str, Any]
) -> list[dict[str, Any]]:
    status = service.get("status") or {}
    ready = any(item.get("type") == "Ready" and str(item.get("status")).lower() == "true"
                for item in status.get("conditions") or [])
    url = str(record.get("serviceUrl") or "").rstrip("/")
    if not url.startswith("https://"):
        url = ""
    disabled = config.get("mcpDisabledAudiences") or []
    return [{
        "audience": audience, "label": "교직원" if audience == "staff" else "학생",
        "serviceName": "rag-mcp", "unifiedMcp": True,
        "serviceReady": bool(ready and url),
        "url": url, "mcpUrl": f"{url}/mcp" if url else "",
        "healthUrl": f"{url}/health" if url else "",
        "status": "DISABLED" if audience in disabled else ("READY" if ready and url else "NOT_READY"),
        "latestReadyRevision": str(status.get("latestReadyRevisionName") or ""),
    } for audience in ("staff", "student") if (config.get("corpora") or {}).get(audience)]


def department_public_config_any(code: str) -> dict[str, Any]:
    return cloud_department_public_config(code)


def list_department_records() -> list[dict[str, Any]]:
    return cloud_mcp_department_records()


def cloud_department_config(code: str) -> tuple[dict[str, Any], str]:
    normalised = str(code or "").strip().lower()
    if not core.DEPT_CODE_RE.fullmatch(normalised):
        raise FileNotFoundError(normalised)
    _record, configs = _mcp_registry_read()
    if normalised not in configs:
        raise FileNotFoundError(normalised)
    config = configs[normalised]
    return config, core._mapping_revision(config)


def cloud_department_public_config(code: str) -> dict[str, Any]:
    config, revision = cloud_department_config(code)
    return {
        "code": code,
        "name": str(config.get("name") or code),
        "corpora": copy.deepcopy(config.get("corpora") or {}),
        "buckets": copy.deepcopy(config.get("buckets") or {}),
        "drive": copy.deepcopy(config.get("drive") or {}),
        "minInstances": copy.deepcopy(config.get("minInstances") or {}),
        "enableDocaiFallback": config.get("enableDocaiFallback", False),
        "enableDocaiFallbackStaff": config.get("enableDocaiFallbackStaff", config.get("enableDocaiFallback", False)),
        "enableDocaiFallbackStudent": config.get("enableDocaiFallbackStudent", config.get("enableDocaiFallback", False)),
        "enableImageOcrStaff": config.get("enableImageOcrStaff", config.get("enableImageOcr", False)),
        "enableImageOcrStudent": config.get("enableImageOcrStudent", config.get("enableImageOcr", False)),
        "enableImageOcr": config.get("enableImageOcr", False),
        "corpusMode": "split" if (config.get("corpora") or {}).get("student") else "single",
        "configRevision": revision,
        "source": "cloud",
        "unifiedMcp": True,
    }


def _cloud_run_env(service: dict[str, Any]) -> dict[str, str]:
    spec = service.get("spec") if isinstance(service.get("spec"), dict) else {}
    template = spec.get("template") if isinstance(spec.get("template"), dict) else {}
    template_spec = template.get("spec") if isinstance(template.get("spec"), dict) else {}
    containers = template_spec.get("containers") if isinstance(template_spec.get("containers"), list) else []
    env_rows = containers[0].get("env") if containers and isinstance(containers[0], dict) else []
    return {
        str(item.get("name") or ""): str(item.get("value") or "")
        for item in env_rows or []
        if isinstance(item, dict) and item.get("name") and item.get("name") != "MCP_API_KEY"
    }


def cloud_mcp_department_records() -> list[dict[str, Any]]:
    with ThreadPoolExecutor(max_workers=2) as pool:
        registry_future = pool.submit(_mcp_registry_read, allow_empty=True)
        service_future = pool.submit(_unified_mcp_service)
        registry, configs = registry_future.result()
        if not registry:
            service_future.cancel()
            return []
        service = service_future.result()
    records = []
    for code, config in sorted(configs.items()):
        servers = _unified_mcp_servers(config, registry, service)
        corpora, buckets = config.get("corpora") or {}, config.get("buckets") or {}
        checks = [core._check("LOCAL", "cloud-metadata", "OK", "통합 MCP Cloud 등록부", hidden=True)]
        for name, value in (("bucket-hwp", buckets.get("hwpOriginal")),
                            ("bucket-source", buckets.get("source")),
                            ("rag-corpus-staff", corpora.get("staff"))):
            checks.append(core._check("RESOURCE", name, "OK" if value else "WARN", str(value or "설정 없음")))
        if corpora.get("student"):
            checks.append(core._check("RESOURCE", "rag-corpus-student", "OK", str(corpora["student"])))
        for server in servers:
            label = f"mcp-{code}-{server['audience']}"
            enabled = server["status"] == "READY"
            detail = "접근 범위 중지됨" if server["status"] == "DISABLED" else server["latestReadyRevision"] or "Ready 확인 필요"
            checks.append(core._check("DEPLOY", label, "OK" if enabled else "WARN", detail,
                                 serviceName="rag-mcp", url=server["url"]))
            checks.append(core._check("RUNTIME", label + "-health", "OK" if enabled else "WARN", "공통 MCP · 인증 키별 범위 분리"))
        revision = core._mapping_revision(config)
        latest = core._LATEST.get(code)
        result = latest if latest and latest.get("configRevision") == revision else {
            "code": code, "overall": core._overall(checks), "checkedAt": datetime.now(UTC).isoformat(), "checks": checks,
        }
        records.append({
            "code": code, "name": str(config.get("name") or code),
            "path": "Cloud MCP registry", "configRevision": revision,
            "lastStatus": result["overall"], "lastResult": result, "parseError": None,
            "corpusMode": "split" if corpora.get("student") else "single",
            "cloudOnly": True, "cloudEditable": True, "metadataComplete": True,
            "unifiedMcp": True, "metadata": core._public_cloud_yaml(config), "cloudServices": servers,
        })
    return records


def department_mcp_servers(code: str) -> dict[str, Any]:
    normalised = str(code or "").strip().lower()
    if not core.DEPT_CODE_RE.fullmatch(normalised):
        raise FileNotFoundError(normalised)
    record, configs = _mcp_registry_read()
    if normalised not in configs:
        raise FileNotFoundError(normalised)
    common = core._common()
    return {
        "code": normalised,
        "projectId": common.get("GCP_PROJECT_ID"),
        "region": common.get("GCP_REGION"),
        "unifiedMcp": True,
        "servers": _unified_mcp_servers(
            configs[normalised], record, _unified_mcp_service()
        ),
    }


def department_mcp_key(code: str, audience: str) -> str:
    normalised = str(code or "").strip().lower()
    if not core.DEPT_CODE_RE.fullmatch(normalised):
        raise FileNotFoundError(normalised)
    if audience not in dept_config.AUDIENCES:
        raise ValueError("교직원 또는 학생 MCP 키를 선택해 주세요.")
    data = cloud_department_config(normalised)[0]
    if audience in data.get("mcpDisabledAudiences", []):
        raise ValueError("이 접근 범위는 중지되어 있습니다.")
    key = str((data.get("keys") or {}).get(audience) or "").strip()
    if not key or key in dept_config.PLACEHOLDER_KEYS:
        raise ValueError("복사할 MCP 키가 설정되어 있지 않습니다.")
    return key


def _cloud_department_configs() -> dict[str, dict[str, Any]]:
    return _mcp_registry_read(allow_empty=True)[1]


def _cloud_sync_department_configs() -> dict[str, dict[str, Any]]:
    return {code: config for code, config in _cloud_department_configs().items()
            if not config.get("syncDisabled")}


def cloud_departments_json() -> str:
    return dept_config.departments_json_from_configs(_cloud_sync_department_configs())


def update_sync_department_map(
    *, on_line: Any = lambda _line: None, expected: str | None = None
) -> str:
    gcloud = gcp_cli.gcloud_executable()
    if not gcloud:
        raise RuntimeError("gcloud를 찾을 수 없습니다.")
    common = core._common()
    project = str(common.get("GCP_PROJECT_ID") or "")
    region = str(common["GCP_REGION"])
    expected = expected if expected is not None else cloud_departments_json()

    ok, service = gcp_cli.gcloud_json(
        [
            "run",
            "services",
            "describe",
            core.SYNC_SERVICE,
            f"--region={region}",
            f"--project={project}",
        ],
        timeout=30,
    )
    if not ok or not isinstance(service, dict):
        raise RuntimeError(f"{core.SYNC_SERVICE} 조회 실패: {str(service)[:200]}")
    if _cloud_run_env(service).get("DEPARTMENTS_JSON", "") == expected:
        codes = ", ".join(sorted(json.loads(expected)))
        on_line(f"{core.SYNC_SERVICE}: 학과 라우팅 맵이 이미 최신입니다 ({codes})")
        return codes

    on_line(f"{core.SYNC_SERVICE}: 학과 라우팅 맵 갱신 중")
    # 값을 명령줄에 싣지 않고 flags-file 로 넘긴다. 학과 맵에는 콤마가 있어
    # --update-env-vars 는 `^|^` 커스텀 구분자를 요구하는데, Windows 에서
    # gcloud 는 .cmd 배치 shim 이라 그 `^` 를 cmd.exe 가 먼저 먹는다. 그러면
    # gcloud 는 콤마마다 값을 끊어 "Bad syntax for dict arg" 로 죽는다(실측).
    # flags-file 은 YAML 이라 셸 인용 규칙을 전혀 타지 않는다.
    with tempfile.NamedTemporaryFile(
        "w", suffix=".yaml", delete=False, encoding="utf-8"
    ) as handle:
        yaml.safe_dump(
            [{"--update-env-vars": {"DEPARTMENTS_JSON": expected}}],
            handle,
            allow_unicode=False,
            default_flow_style=False,
        )
        flags_path = handle.name
    try:
        args = [
            gcloud,
            "run",
            "services",
            "update",
            core.SYNC_SERVICE,
            f"--region={region}",
            f"--project={project}",
            "--platform=managed",
            f"--flags-file={flags_path}",
            "--quiet",
        ]
        update_ok, output = gcp_cli.run_command(args, timeout=600)
    finally:
        Path(flags_path).unlink(missing_ok=True)
    if not update_ok:
        raise RuntimeError(f"{core.SYNC_SERVICE}: {str(output or '갱신 실패')[:400]}")
    with sync_history._SYNC_TARGET_CACHE_LOCK:
        sync_history._SYNC_TARGET_CACHE.clear()
    codes = ", ".join(sorted(json.loads(expected)))
    on_line(f"{core.SYNC_SERVICE}: 학과 라우팅 맵 갱신 완료 ({codes})")
    return codes


def _cloud_config_audiences(config: dict[str, Any]) -> tuple[str, ...]:
    corpora = config.get("corpora") if isinstance(config.get("corpora"), dict) else {}
    drive = config.get("drive") if isinstance(config.get("drive"), dict) else {}
    keys = config.get("keys") if isinstance(config.get("keys"), dict) else {}
    staff_key = str(keys.get("staff") or "").strip()
    if not staff_key or staff_key in dept_config.PLACEHOLDER_KEYS:
        raise ValueError("keys.staff가 없어 Cloud 설정을 배포할 수 없습니다.")
    student_parts = (
        bool(str(corpora.get("student") or "").strip()),
        bool(core._normalise_ids(drive.get("studentFolderIds"))),
        bool(str(keys.get("student") or "").strip()),
    )
    if any(student_parts) and not all(student_parts):
        raise ValueError("학생 분리는 corpus, 폴더, MCP 키가 모두 필요합니다.")
    if all(student_parts) and str(keys.get("student") or "") == staff_key:
        raise ValueError("교직원과 학생 MCP 키가 같습니다.")
    return dept_config.AUDIENCES if all(student_parts) else ("staff",)


def _update_cloud_department_config(
    code: str, config: dict[str, Any], *, on_line: Any = lambda _line: None
) -> list[str]:
    _cloud_config_audiences(config)
    deployment_config = copy.deepcopy(config)
    deployment_config.pop("mcpDisabledAudiences", None)
    deployment_config.pop("syncDisabled", None)
    _mcp_registry_mutate("update_department", code, deployment_config)
    on_line(f"{code}: 공통 MCP에 학과·권한별 라우팅 등록 완료")
    core._LATEST.pop(code, None)
    return ["rag-mcp"]
