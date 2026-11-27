from __future__ import annotations

import copy
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import yaml
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from scripts import dept_config, docai_setup, gcp_cli
from scripts.console import cloud_registry, core, status_checks
from scripts.run_store import RunStore

router = APIRouter()


_MCP_DEPLOYS = RunStore(60 * 60, "steps")


_COMMON_RUNTIME_DEPLOYS = RunStore(60 * 60, "steps")


def _expected_runtime_env() -> tuple[str, dict[str, str], str]:
    configs = cloud_registry._cloud_sync_department_configs()
    if not configs:
        raise ValueError("활성 동기화 학과가 없습니다.")
    first = min(configs)
    return (
        first,
        dept_config.build_env_from_config(first, "staff", configs[first]),
        dept_config.departments_json_from_configs(configs),
    )


def _cloud_run_service_state(service: dict[str, Any]) -> dict[str, Any]:
    status = service.get("status") or {}
    metadata = service.get("metadata") or {}
    ready_condition = next(
        (item for item in status.get("conditions") or [] if item.get("type") == "Ready"),
        {},
    )
    ready = str(ready_condition.get("status") or "").lower() == "true"
    latest_ready = str(status.get("latestReadyRevisionName") or "")
    latest_created = str(status.get("latestCreatedRevisionName") or "")
    return {
        "exists": True,
        "state": "READY" if ready and latest_ready == latest_created else "DEGRADED",
        "ready": ready,
        "revision": latest_ready or latest_created,
        "pendingRevision": latest_created if latest_created != latest_ready else "",
        "url": str(status.get("url") or ""),
        "updatedAt": str(metadata.get("creationTimestamp") or ""),
        "detail": latest_ready or str(ready_condition.get("message") or "Ready 아님"),
    }


def common_runtime_state() -> dict[str, Any]:
    common = core._common()
    project = str(common.get("GCP_PROJECT_ID") or "")
    region = str(common["GCP_REGION"])
    if not project:
        return {
            "status": "UNKNOWN",
            "reason": "공통 설정이 없습니다.",
            "projectId": "",
            "region": region,
            "services": [],
        }

    definitions = [
        ("parser", "cloudRun", "rag-parser", "HWP·HWPX 파싱 런타임"),
        ("sync", "cloudRun", "rag-sync", "Drive 수집·색인 런타임"),
        ("workflow", "workflow", "rag-daily-sync", "동기화 오케스트레이션"),
        ("scheduler", "scheduler", "rag-daily-sync", "정기 실행 트리거"),
    ]
    scope = f"--project={project}"
    commands = {
        "cloudRun": lambda name: ["run", "services", "describe", name, f"--region={region}", scope],
        "workflow": lambda name: ["workflows", "describe", name, f"--location={region}", scope],
        "scheduler": lambda name: [
            "scheduler", "jobs", "describe", name, f"--location={region}", scope,
        ],
    }

    def _probe(item: tuple[str, str, str, str]) -> dict[str, Any]:
        key, kind, name, purpose = item
        ok, data = gcp_cli.gcloud_json(commands[kind](name), timeout=25)
        display_name = {
            "workflow": f"{name} · Workflow",
            "scheduler": f"{name} · Scheduler",
        }.get(kind, name)
        base = {
            "key": key,
            "kind": kind,
            "name": name,
            "displayName": display_name,
            "purpose": purpose,
        }
        if not ok or not isinstance(data, dict):
            missing = core._missing_resource_output(str(data or ""))
            return {
                **base,
                "exists": False if missing else None,
                "state": "MISSING" if missing else "UNKNOWN",
                "detail": "배포되지 않았습니다" if missing else str(data or "조회 실패")[:200],
            }
        if kind == "cloudRun":
            return {**base, **_cloud_run_service_state(data)}
        if kind == "workflow":
            state = str(data.get("state") or "UNKNOWN")
            return {
                **base,
                "exists": True,
                "state": "READY" if state == "ACTIVE" else "DEGRADED",
                "revision": str(data.get("revisionId") or ""),
                "updatedAt": str(data.get("updateTime") or ""),
                "detail": state,
            }
        state = str(data.get("state") or "UNKNOWN")
        schedule = f"{data.get('schedule') or ''} {data.get('timeZone') or ''}".strip()
        return {
            **base,
            "exists": True,
            "state": "READY" if state == "ENABLED" else "DEGRADED",
            "schedule": schedule,
            "updatedAt": str(data.get("lastAttemptTime") or ""),
            "detail": f"{state}{' · ' + schedule if schedule else ''}",
        }

    with ThreadPoolExecutor(max_workers=4) as pool:
        services = list(pool.map(_probe, definitions))

    states = {item["state"] for item in services}
    status = (
        "MISSING"
        if states == {"MISSING"}
        else "DEGRADED"
        if states & {"DEGRADED", "MISSING", "UNKNOWN"}
        else "READY"
    )
    return {
        "status": status,
        "projectId": project,
        "region": region,
        "services": services,
    }


def runtime_env_drift(cache: status_checks._StatusRunCache | None = None) -> dict[str, Any]:
    common = core._common()
    project = str(common.get("GCP_PROJECT_ID") or "")
    region = str(common["GCP_REGION"])
    if not project:
        return {"status": "UNKNOWN", "reason": "공통 설정이 없습니다.", "services": []}

    try:
        base_code, base_env, expected_departments = _expected_runtime_env()
    except (OSError, RuntimeError, SystemExit, TypeError, ValueError, yaml.YAMLError) as exc:
        return {"status": "UNKNOWN", "reason": str(exc)[:200], "services": []}

    expected_by_service = {
        "rag-parser": {
            **{key: str(common[key]) for keys in docai_setup.KINDS.values() for key in keys[1:]},
            "GCS_HWP_ORIGINAL_BUCKET": base_env.get("GCS_HWP_ORIGINAL_BUCKET", ""),
            "GCS_SOURCE_BUCKET": base_env.get("GCS_SOURCE_BUCKET", ""),
            "RAG_CORPUS_NAME": base_env.get("RAG_CORPUS_NAME", ""),
        },
        "rag-sync": {
            "GCS_HWP_ORIGINAL_BUCKET": base_env.get("GCS_HWP_ORIGINAL_BUCKET", ""),
            "GCS_SOURCE_BUCKET": base_env.get("GCS_SOURCE_BUCKET", ""),
            "RAG_CORPUS_NAME": base_env.get("RAG_CORPUS_NAME", ""),
            "DEPARTMENTS_JSON": expected_departments,
        },
    }

    gcloud_json = cache.gcloud_json if cache else gcp_cli.gcloud_json
    services: list[dict[str, Any]] = []
    drifted = False
    for service, expected in expected_by_service.items():
        ok, data = gcloud_json(
            [
                "run",
                "services",
                "describe",
                service,
                f"--region={region}",
                f"--project={project}",
            ]
        )
        if not ok or not isinstance(data, dict):
            services.append({"serviceName": service, "status": "UNKNOWN", "staleKeys": []})
            continue
        deployed = cloud_registry._cloud_run_env(data)
        stale = [
            {
                "key": key,
                "deployed": str(deployed.get(key) or "")[:200],
                "expected": str(value or "")[:200],
            }
            for key, value in expected.items()
            if (value or key.startswith("DOCAI_")) and str(deployed.get(key) or "") != str(value)
        ]
        if stale:
            drifted = True
        services.append(
            {
                "serviceName": service,
                "status": "DRIFT" if stale else "OK",
                "staleKeys": stale,
            }
        )
    return {
        "status": "DRIFT" if drifted else "OK",
        "projectId": project,
        "region": region,
        "baseDepartment": base_code,
        "services": services,
    }


def _advance_common_runtime_deploy_from_log(run_id: str, line: str) -> None:
    markers = (
        ("== Refresh Cloud Run env ==", "config", "cloudRun", "Parser / Sync env 갱신 중"),
        ("== Build & push images ==", "config", "images", "Parser / Sync 이미지 조회 중 · 없으면 빌드"),
        ("== Deploy Cloud Run ==", "images", "cloudRun", "Parser / Sync 조회 중 · 없으면 배포"),
        ("== Deploy Workflow ==", "cloudRun", "workflow", "Workflow 배포 시작"),
        ("== Ensure Scheduler SA / App Engine ==", "workflow", "scheduler", "Scheduler 준비 시작"),
    )
    for marker, completed_key, running_key, detail in markers:
        if marker in line:
            _COMMON_RUNTIME_DEPLOYS.update_item(
                run_id, completed_key, status="COMPLETE", detail="배포 명령 완료"
            )
            _COMMON_RUNTIME_DEPLOYS.update_item(
                run_id, running_key, status="RUNNING", detail=detail
            )
            return
    if "== Cloud Scheduler" in line:
        _COMMON_RUNTIME_DEPLOYS.update_item(
            run_id, "scheduler", status="RUNNING", detail="Scheduler 작업 등록 중"
        )


def _append_common_runtime_deploy_log(run_id: str, line: str) -> None:
    clean = str(line or "").strip()
    if not clean:
        return
    clean = re.sub(r"(?i)(authorization:\s*bearer\s+)[^\s]+", r"\1***", clean)
    clean = re.sub(r"(?i)(access[_ -]?token[=:]\s*)[^\s]+", r"\1***", clean)
    with _COMMON_RUNTIME_DEPLOYS.lock:
        run = _COMMON_RUNTIME_DEPLOYS.runs.get(run_id)
        if not run:
            return
        run["logs"] = [*run.get("logs", []), clean[:500]][-220:]
        for step in run["steps"]:
            if step["status"] == "RUNNING":
                step["detail"] = clean[:300]
                break
    _advance_common_runtime_deploy_from_log(run_id, clean)


def _run_common_runtime_deploy_script(*, on_line: Any, env_only: bool = False) -> int:
    executable = shutil.which("pwsh.exe") or shutil.which("pwsh")
    if not executable:
        raise RuntimeError("PowerShell 7(pwsh)을 찾을 수 없습니다.")
    args = [
        executable,
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(core.ROOT / "scripts" / "deploy.ps1"),
    ]
    if env_only:
        args.append("-EnvOnly")
    else:
        args.extend(["-SkipMcp", "-ReuseExisting"])
    env = os.environ.copy()
    env.update(
        {
            "PYTHONUTF8": "1",
            "PYTHONIOENCODING": "utf-8",
            "CLOUDSDK_CORE_DISABLE_PROMPTS": "1",
            "PREFLIGHT_NO_FIX": "1",
        }
    )
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    process = subprocess.Popen(
        args,
        cwd=core.ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        shell=False,
        creationflags=flags,
    )
    if process.stdout:
        for line in process.stdout:
            on_line(line)
    return process.wait()


def _verify_common_runtime_deployment() -> list[dict[str, str]]:
    common = core._common()
    project = str(common.get("GCP_PROJECT_ID") or "")
    region = str(common["GCP_REGION"])
    services: list[dict[str, str]] = []
    failures: list[str] = []
    for service in ("rag-parser", "rag-sync"):
        ok, data = gcp_cli.gcloud_json(
            [
                "run",
                "services",
                "describe",
                service,
                f"--region={region}",
                f"--project={project}",
            ],
            timeout=30,
        )
        status_data = data.get("status") if ok and isinstance(data, dict) else {}
        status_data = status_data if isinstance(status_data, dict) else {}
        ready_condition = next(
            (
                item
                for item in status_data.get("conditions") or []
                if item.get("type") == "Ready"
            ),
            {},
        )
        ready = str(ready_condition.get("status") or "").lower() == "true"
        url = str(status_data.get("url") or "")
        if not ok or not ready or not url:
            detail = str(data if not ok else ready_condition.get("message") or "Ready 아님")
            failures.append(f"{service}: {detail[:180]}")
            continue
        services.append(
            {
                "serviceName": service,
                "url": url,
                "healthUrl": url.rstrip("/") + "/health",
            }
        )

    for kind, args in (
        (
            "Workflow",
            ["workflows", "describe", "rag-daily-sync", f"--location={region}", f"--project={project}"],
        ),
        (
            "Scheduler",
            ["scheduler", "jobs", "describe", "rag-daily-sync", f"--location={region}", f"--project={project}"],
        ),
    ):
        ok, detail = gcp_cli.gcloud_json(args, timeout=30)
        if not ok:
            failures.append(f"{kind}: {str(detail)[:180]}")
    if failures:
        raise RuntimeError(" · ".join(failures))
    return services


def _execute_common_runtime_deployment(run_id: str) -> None:
    with _COMMON_RUNTIME_DEPLOYS.lock:
        env_only = bool((_COMMON_RUNTIME_DEPLOYS.runs.get(run_id) or {}).get("envOnly"))
    try:
        if env_only:
            codes = cloud_registry.update_sync_department_map(
                on_line=lambda line: _append_common_runtime_deploy_log(run_id, line)
            )
            for key in ("config", "cloudRun"):
                _COMMON_RUNTIME_DEPLOYS.update_item(
                    run_id, key, status="COMPLETE", detail=f"동기화 대상 학과: {codes}"
                )
        else:
            exit_code = _run_common_runtime_deploy_script(
                on_line=lambda line: _append_common_runtime_deploy_log(run_id, line),
                env_only=env_only,
            )
            if exit_code != 0:
                raise RuntimeError(f"공통 런타임 배포 스크립트가 종료 코드 {exit_code}로 실패했습니다.")
            for key in ("config", "images", "cloudRun", "workflow", "scheduler"):
                _COMMON_RUNTIME_DEPLOYS.update_item(
                    run_id, key, status="COMPLETE", detail="배포 명령 완료"
                )
        _COMMON_RUNTIME_DEPLOYS.update_item(
            run_id, "ready", status="RUNNING", detail="배포 리소스 Ready 확인 중"
        )
        services = _verify_common_runtime_deployment()
        _COMMON_RUNTIME_DEPLOYS.update_item(
            run_id,
            "ready",
            status="COMPLETE",
            detail="Parser / Sync / Workflow / Scheduler 확인 완료",
        )
        with _COMMON_RUNTIME_DEPLOYS.lock:
            current = _COMMON_RUNTIME_DEPLOYS.runs[run_id]
            current.update(status="COMPLETED", services=services, finishedEpoch=time.time())
    except (OSError, RuntimeError, SystemExit, TypeError, ValueError, yaml.YAMLError) as exc:
        _COMMON_RUNTIME_DEPLOYS.fail_running(run_id, str(exc)[:500])


def start_common_runtime_deployment(
    follow_up_department_code: str = "", env_only: bool = False
) -> dict[str, Any]:
    follow_up = str(follow_up_department_code or "").strip().lower()
    if follow_up and (
        not core.DEPT_CODE_RE.fullmatch(follow_up)
        or follow_up not in cloud_registry._cloud_department_configs()
    ):
        raise FileNotFoundError(follow_up)
    common = core._common()
    project = str(common.get("GCP_PROJECT_ID") or "")
    region = str(common["GCP_REGION"])
    if not project:
        raise ValueError("공통 환경의 GCP 프로젝트가 설정되어 있지 않습니다.")
    with _COMMON_RUNTIME_DEPLOYS.lock:
        _COMMON_RUNTIME_DEPLOYS.cleanup()
        active = next(
            (
                copy.deepcopy(run)
                for run in _COMMON_RUNTIME_DEPLOYS.runs.values()
                if run["status"] == "RUNNING"
            ),
            None,
        )
        if active:
            if follow_up:
                _COMMON_RUNTIME_DEPLOYS.runs[active["runId"]]["followUpDepartmentCode"] = follow_up
            raise FileExistsError(active["runId"])
        run_id = uuid.uuid4().hex
        run = {
            "runId": run_id,
            "status": "RUNNING",
            "project": project,
            "region": region,
            "followUpDepartmentCode": follow_up,
            "serviceNames": ["rag-parser", "rag-sync", "rag-daily-sync"],
            "services": [],
            "envOnly": bool(env_only),
            "steps": [
                {"key": "config", "label": "설정 확인", "status": "RUNNING", "detail": "학과 라우팅 및 공통 설정 확인 중"},
                *(
                    []
                    if env_only
                    else [
                        {"key": "images", "label": "공통 이미지", "status": "PENDING", "detail": "Parser / Sync 이미지 조회 대기"},
                    ]
                ),
                {
                    "key": "cloudRun",
                    "label": "Parser / Sync",
                    "status": "PENDING",
                    "detail": "Cloud Run env 갱신 대기" if env_only else "Cloud Run 조회 대기",
                },
                *(
                    []
                    if env_only
                    else [
                        {"key": "workflow", "label": "Workflow", "status": "PENDING", "detail": "rag-daily-sync 배포 대기"},
                        {"key": "scheduler", "label": "Scheduler", "status": "PENDING", "detail": "정기 동기화 작업 등록 대기"},
                    ]
                ),
                {"key": "ready", "label": "Ready 확인", "status": "PENDING", "detail": "배포 리소스 확인 대기"},
            ],
            "logs": [],
            "createdEpoch": time.time(),
        }
        _COMMON_RUNTIME_DEPLOYS.runs[run_id] = run
    threading.Thread(
        target=_execute_common_runtime_deployment,
        args=(run_id,),
        name=f"common-runtime-deploy-{run_id[:8]}",
        daemon=True,
    ).start()
    return copy.deepcopy(run)


def _append_mcp_deploy_log(run_id: str, line: str, secrets_to_redact: list[str]) -> None:
    clean = str(line or "").strip()
    if not clean:
        return
    for secret in secrets_to_redact:
        if secret:
            clean = clean.replace(secret, "***")
    clean = re.sub(r"(?i)(authorization:\s*bearer\s+)[^\s]+", r"\1***", clean)
    with _MCP_DEPLOYS.lock:
        run = _MCP_DEPLOYS.runs.get(run_id)
        if not run:
            return
        run["logs"] = [*run.get("logs", []), clean[:500]][-160:]
        for step in run["steps"]:
            if step["key"] == "deploy" and step["status"] == "RUNNING":
                step["detail"] = clean[:300]
                break


def _execute_mcp_deployment(run_id: str) -> None:
    with _MCP_DEPLOYS.lock:
        run = copy.deepcopy(_MCP_DEPLOYS.runs[run_id])
        cloud_config = copy.deepcopy(_MCP_DEPLOYS.private.get(run_id))
    code = run["code"]
    try:
        config = cloud_config or cloud_registry.cloud_department_config(code)[0]
        audiences = cloud_registry._cloud_config_audiences(config)
        secrets_to_redact = [
            str((config.get("keys") or {}).get(audience) or "") for audience in audiences
        ]
        _MCP_DEPLOYS.update_item(
            run_id,
            "config",
            status="COMPLETE",
            detail=f"{len(audiences)}개 MCP 설정 검증 완료",
        )

        _MCP_DEPLOYS.update_item(run_id, "deploy", status="RUNNING", detail="Cloud Run 배포 시작")
        _MCP_DEPLOYS.update_item(
            run_id, "image", status="COMPLETE", detail="현재 공통 MCP 이미지 사용"
        )
        cloud_registry._update_cloud_department_config(
            code,
            config,
            on_line=lambda line: _append_mcp_deploy_log(
                run_id, line, secrets_to_redact
            ),
        )
        _MCP_DEPLOYS.update_item(run_id, "deploy", status="COMPLETE", detail="Cloud Run 배포 명령 완료")

        _MCP_DEPLOYS.update_item(run_id, "ready", status="RUNNING", detail="Cloud Run Ready 확인 중")
        inventory = cloud_registry.department_mcp_servers(code)
        servers = inventory.get("servers") or []
        expected = {"rag-mcp"}
        ready = {
            str(item.get("serviceName") or "")
            for item in servers
            if item.get("status") == "READY" or item.get("serviceReady") is True
        }
        missing = sorted(expected - ready)
        if missing:
            raise RuntimeError("Ready 상태가 아닌 서비스: " + ", ".join(missing))
        _MCP_DEPLOYS.update_item(
            run_id,
            "ready",
            status="COMPLETE",
            detail=f"{len(expected)}개 서비스 Ready",
        )

        _MCP_DEPLOYS.update_item(run_id, "health", status="RUNNING", detail="서비스 health 확인 중")
        health_failures: list[str] = []
        health_urls = set()
        for server in servers:
            if server.get("healthUrl") in health_urls:
                continue
            health_urls.add(server.get("healthUrl"))
            status_code, _body, _latency = gcp_cli.http_json(str(server.get("healthUrl") or ""), timeout=30)
            if status_code != 200:
                health_failures.append(
                    f"{server.get('serviceName')}: HTTP {status_code or 'timeout'}"
                )
        if health_failures:
            raise RuntimeError(" · ".join(health_failures))
        _MCP_DEPLOYS.update_item(
            run_id,
            "health",
            status="COMPLETE",
            detail=f"{len(servers)}개 서비스 정상 응답",
        )

        _MCP_DEPLOYS.update_item(
            run_id, "syncMap", status="RUNNING", detail="rag-sync 학과 라우팅 맵 갱신 중"
        )
        codes = cloud_registry.update_sync_department_map(
            on_line=lambda line: _append_mcp_deploy_log(run_id, line, secrets_to_redact)
        )
        _MCP_DEPLOYS.update_item(
            run_id, "syncMap", status="COMPLETE", detail=f"동기화 대상 학과: {codes}"
        )

        with _MCP_DEPLOYS.lock:
            current = _MCP_DEPLOYS.runs[run_id]
            current.update(
                status="COMPLETED",
                servers=servers,
                finishedEpoch=time.time(),
            )
            _MCP_DEPLOYS.private.pop(run_id, None)
    except (OSError, RuntimeError, SystemExit, TypeError, ValueError, yaml.YAMLError) as exc:
        _MCP_DEPLOYS.fail_running(run_id, str(exc)[:500])


def start_mcp_deployment(
    code: str, *, cloud_config: dict[str, Any] | None = None
) -> dict[str, Any]:
    normalised = str(code or "").strip().lower()
    if not core.DEPT_CODE_RE.fullmatch(normalised):
        raise FileNotFoundError(normalised)
    full_config = (
        copy.deepcopy(cloud_config)
        if cloud_config is not None
        else cloud_registry.cloud_department_config(normalised)[0]
    )
    audiences = cloud_registry._cloud_config_audiences(full_config)
    config = {
        "name": str(full_config.get("name") or normalised),
        "corpusMode": "split" if len(audiences) == 2 else "single",
    }
    services = ["rag-mcp"]
    with _MCP_DEPLOYS.lock:
        _MCP_DEPLOYS.cleanup()
        active = next(
            (
                copy.deepcopy(run)
                for run in _MCP_DEPLOYS.runs.values()
                if run["code"] == normalised and run["status"] == "RUNNING"
            ),
            None,
        )
        if active:
            raise FileExistsError(active["runId"])
        run_id = uuid.uuid4().hex
        run = {
            "runId": run_id,
            "code": normalised,
            "name": config["name"],
            "corpusMode": config["corpusMode"],
            "status": "RUNNING",
            "serviceNames": services,
            "unifiedMcp": True,
            "services": [],
            "steps": [
                {"key": "config", "label": "설정 확인", "status": "RUNNING", "detail": "YAML 및 MCP 키 검증 중"},
                {"key": "image", "label": "MCP 이미지", "status": "PENDING", "detail": "대기 중"},
                {"key": "deploy", "label": "Cloud Run 배포", "status": "PENDING", "detail": "대기 중"},
                {"key": "ready", "label": "Ready 확인", "status": "PENDING", "detail": "대기 중"},
                {"key": "health", "label": "Health 확인", "status": "PENDING", "detail": "대기 중"},
                {"key": "syncMap", "label": "학과 라우팅 반영", "status": "PENDING", "detail": "대기 중"},
            ],
            "logs": [],
            "createdEpoch": time.time(),
        }
        _MCP_DEPLOYS.runs[run_id] = run
        _MCP_DEPLOYS.private[run_id] = full_config
    threading.Thread(
        target=_execute_mcp_deployment,
        args=(run_id,),
        name=f"mcp-deploy-{normalised}-{run_id[:8]}",
        daemon=True,
    ).start()
    return copy.deepcopy(run)


@router.get("/api/v1/mcp-deployments")
def mcp_deployments(code: str = "", status: str = "") -> JSONResponse:
    with _MCP_DEPLOYS.lock:
        _MCP_DEPLOYS.cleanup()
        rows = [
            copy.deepcopy(run)
            for run in _MCP_DEPLOYS.runs.values()
            if (not code or run["code"] == code)
            and (not status or run["status"] == status.upper())
        ]
    rows.sort(key=lambda item: item.get("createdEpoch", 0), reverse=True)
    return JSONResponse({"runs": rows})


@router.get("/api/v1/common-runtime-deployments")
def common_runtime_deployments(status: str = "") -> JSONResponse:
    with _COMMON_RUNTIME_DEPLOYS.lock:
        _COMMON_RUNTIME_DEPLOYS.cleanup()
        rows = [
            copy.deepcopy(run)
            for run in _COMMON_RUNTIME_DEPLOYS.runs.values()
            if not status or run["status"] == status.upper()
        ]
    rows.sort(key=lambda item: item.get("createdEpoch", 0), reverse=True)
    return JSONResponse({"runs": rows})


@router.get("/api/v1/common-runtime-deployments/{run_id}")
def common_runtime_deployment(run_id: str) -> JSONResponse:
    with _COMMON_RUNTIME_DEPLOYS.lock:
        _COMMON_RUNTIME_DEPLOYS.cleanup()
        run = _COMMON_RUNTIME_DEPLOYS.runs.get(run_id)
        if not run:
            return JSONResponse(
                {
                    "error": {
                        "code": "COMMON_RUNTIME_DEPLOYMENT_NOT_FOUND",
                        "message": "공통 런타임 배포 작업을 찾을 수 없습니다.",
                    }
                },
                status_code=404,
            )
        return JSONResponse(copy.deepcopy(run))


@router.get("/api/v1/runtime-env")
def runtime_env() -> JSONResponse:
    try:
        return JSONResponse(runtime_env_drift())
    except (OSError, RuntimeError, TypeError, ValueError, yaml.YAMLError) as exc:
        return JSONResponse(
            {"status": "UNKNOWN", "reason": str(exc)[:300], "services": []},
        )


@router.post("/api/v1/common-runtime-deployments")
def create_common_runtime_deployment(
    request: Request, followUpDepartmentCode: str = "", envOnly: bool = False
) -> JSONResponse:
    core._require_local_session(request)
    try:
        run = start_common_runtime_deployment(followUpDepartmentCode, env_only=envOnly)
    except FileNotFoundError:
        return JSONResponse(
            {"error": {"code": "NOT_FOUND", "message": "후속 MCP 배포 대상 학과를 찾을 수 없습니다."}},
            status_code=404,
        )
    except FileExistsError as exc:
        return JSONResponse(
            {
                "error": {
                    "code": "COMMON_RUNTIME_DEPLOYMENT_RUNNING",
                    "message": "공통 런타임 배포가 이미 진행 중입니다.",
                    "runId": str(exc),
                }
            },
            status_code=409,
        )
    except (OSError, RuntimeError, SystemExit, TypeError, ValueError, yaml.YAMLError) as exc:
        return JSONResponse(
            {"error": {"code": "COMMON_RUNTIME_DEPLOYMENT_INVALID", "message": str(exc)[:400]}},
            status_code=422,
        )
    return JSONResponse(run, status_code=202)


@router.get("/api/v1/common-runtime/status")
def common_runtime_status_endpoint() -> JSONResponse:
    try:
        return JSONResponse(common_runtime_state())
    except (OSError, RuntimeError, TypeError, ValueError, yaml.YAMLError) as exc:
        return JSONResponse(
            {
                "status": "UNKNOWN",
                "reason": str(exc)[:300],
                "projectId": "",
                "region": "",
                "services": [],
            }
        )


@router.get("/api/v1/mcp-deployments/{run_id}")
def mcp_deployment(run_id: str) -> JSONResponse:
    with _MCP_DEPLOYS.lock:
        _MCP_DEPLOYS.cleanup()
        run = _MCP_DEPLOYS.runs.get(run_id)
        if not run:
            return JSONResponse(
                {"error": {"code": "MCP_DEPLOYMENT_NOT_FOUND", "message": "MCP 배포 작업을 찾을 수 없습니다."}},
                status_code=404,
            )
        return JSONResponse(copy.deepcopy(run))


@router.post("/api/v1/departments/{code}/mcp-deployments")
def create_mcp_deployment(code: str, request: Request) -> JSONResponse:
    core._require_local_session(request)
    try:
        run = start_mcp_deployment(code)
    except FileNotFoundError:
        return JSONResponse(
            {"error": {"code": "NOT_FOUND", "message": "학과 설정을 찾을 수 없습니다."}},
            status_code=404,
        )
    except FileExistsError as exc:
        return JSONResponse(
            {
                "error": {
                    "code": "MCP_DEPLOYMENT_RUNNING",
                    "message": "이 학과의 MCP 배포가 이미 진행 중입니다.",
                    "runId": str(exc),
                }
            },
            status_code=409,
        )
    except (OSError, RuntimeError, SystemExit, TypeError, ValueError, yaml.YAMLError) as exc:
        return JSONResponse(
            {"error": {"code": "MCP_DEPLOYMENT_INVALID", "message": str(exc)[:400]}},
            status_code=422,
        )
    return JSONResponse(run, status_code=202)
