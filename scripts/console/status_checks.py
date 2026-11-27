from __future__ import annotations

import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from typing import Any

import yaml
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from scripts import dept_config, gcp_cli
from scripts.console import cloud_registry, core, drive_lookup
from scripts.run_store import RunStore

router = APIRouter()


_STATUS_RUNS = RunStore(15 * 60)


def _cloud_config_status(data: dict[str, Any]) -> list[dict[str, Any]]:
    started = time.perf_counter()
    checks = [core._check("LOCAL", "cloud-metadata", "OK", "Cloud 등록부", hidden=True)]
    split_enabled = bool((data.get("corpora") or {}).get("student"))
    try:
        audiences = cloud_registry._cloud_config_audiences(data)
        mode_label = "Cloud 설정 · 교직원/학생" if len(audiences) == 2 else "Cloud 설정 · 단일 코퍼스"
        checks.append(core._check("LOCAL", "derived-env", "OK", mode_label))
    except ValueError as exc:
        checks.append(core._check("LOCAL", "derived-env", "FAIL", str(exc)))

    drive = data.get("drive") or {}
    sync_ids = set(core._normalise_ids(drive.get("syncFolderIds")))
    student_ids = set(core._normalise_ids(drive.get("studentFolderIds")))
    if not split_enabled:
        checks.append(core._check("LOCAL", "folder-scope", "SKIP", "단일 코퍼스 운영"))
    elif student_ids and not student_ids - sync_ids:
        checks.append(core._check("LOCAL", "folder-scope", "OK", "student ⊆ sync"))
    else:
        missing = sorted(student_ids - sync_ids)
        checks.append(
            core._check(
                "LOCAL",
                "folder-scope",
                "FAIL",
                "학생 폴더 없음" if not student_ids else "부분집합 위반: " + ", ".join(missing),
            )
        )

    keys = data.get("keys") or {}
    audiences = dept_config.AUDIENCES if split_enabled else ("staff",)
    weak = [audience for audience in audiences if len(str(keys.get(audience) or "")) < 24]
    checks.append(
        core._check(
            "LOCAL",
            "mcp-keys",
            "WARN" if weak else "OK",
            "24자 미만: " + ", ".join(weak) if weak else "키 길이 기준 충족",
        )
    )
    checks[0]["latencyMs"] = round((time.perf_counter() - started) * 1000)
    return checks


class _StatusRunCache:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._gcloud_calls: dict[tuple[tuple[str, ...], int], Future[Any]] = {}
        self._commands: dict[tuple[tuple[str, ...], int], Future[Any]] = {}

    def _once(
        self,
        calls: dict[tuple[tuple[str, ...], int], Future[Any]],
        key: tuple[tuple[str, ...], int],
        producer: Any,
    ) -> Any:
        with self._lock:
            future = calls.get(key)
            owner = future is None
            if future is None:
                future = Future()
                calls[key] = future
        if owner:
            try:
                future.set_result(producer())
            except Exception as exc:  # noqa: BLE001 - 동일 호출 대기자에게 오류 전달
                future.set_exception(exc)
        return future.result()

    def gcloud_json(self, args: list[str], timeout: int = 12) -> tuple[bool, Any]:
        key = (tuple(args), timeout)
        return self._once(
            self._gcloud_calls,
            key,
            lambda: gcp_cli.gcloud_json(args, timeout),
        )

    def run_command(self, args: list[str], timeout: int = 12) -> tuple[bool, str]:
        key = (tuple(args), timeout)
        return self._once(
            self._commands,
            key,
            lambda: gcp_cli.run_command(args, timeout),
        )


def _resource_status(
    code: str,
    cfg: dict[str, Any],
    common: dict[str, Any],
    cache: _StatusRunCache | None = None,
) -> list[dict]:
    checks: list[dict[str, Any]] = []
    project = str(common.get("GCP_PROJECT_ID") or "")
    region = str(common["GCP_REGION"])
    gcloud = gcp_cli.gcloud_executable()
    if not gcloud:
        return [core._check("RESOURCE", "gcloud", "FAIL", "gcloud를 찾을 수 없습니다.")]

    gcloud_json = cache.gcloud_json if cache else gcp_cli.gcloud_json
    run_command = cache.run_command if cache else gcp_cli.run_command

    ok, active = gcloud_json(["auth", "list", "--filter=status:ACTIVE"])
    if not ok or not active:
        return [core._check("RESOURCE", "gcloud-auth", "FAIL", "활성 gcloud 계정이 없습니다.")]
    checks.append(core._check("RESOURCE", "gcloud-auth", "OK", "활성 계정 확인"))

    bucket_inventory: dict[str, dict[str, Any]] | None = None
    if cache:
        list_ok, bucket_rows = gcloud_json(
            ["storage", "buckets", "list", f"--project={project}"]
        )
        if list_ok and isinstance(bucket_rows, list):
            bucket_inventory = {
                str(item.get("name") or ""): item
                for item in bucket_rows
                if isinstance(item, dict)
            }

    for key, label in (("hwpOriginal", "bucket-hwp"), ("source", "bucket-source")):
        bucket = str((cfg.get("buckets") or {}).get(key) or "")
        if bucket_inventory is not None:
            info = bucket_inventory.get(bucket) or {}
            ok = bool(info)
        else:
            ok, info = gcloud_json(["storage", "buckets", "describe", f"gs://{bucket}"])
        if ok:
            location = str(info.get("location") or "").lower()
            status = "OK" if not location or location == region.lower() else "WARN"
            checks.append(core._check("RESOURCE", label, status, location or "존재"))
        else:
            checks.append(core._check("RESOURCE", label, "FAIL", f"버킷 조회 실패: {bucket}"))

    database = str(common["FIRESTORE_DATABASE"])
    ok, db = gcloud_json(
        ["firestore", "databases", "describe", f"--database={database}", f"--project={project}"]
    )
    db_type = str(db.get("type") or "") if ok else ""
    checks.append(
        core._check(
            "RESOURCE",
            "firestore",
            "OK" if db_type == "FIRESTORE_NATIVE" else "FAIL",
            db_type or "조회 실패",
        )
    )

    token_ok, token = run_command([gcloud, "auth", "print-access-token", "--quiet"])
    split_enabled = bool((cfg.get("corpora") or {}).get("student"))
    audiences = dept_config.AUDIENCES if split_enabled else ("staff",)
    for audience in audiences:
        corpus = str((cfg.get("corpora") or {}).get(audience) or "")
        name = f"rag-corpus-{audience}"
        if not token_ok:
            checks.append(core._check("RESOURCE", name, "WARN", "access token 발급 실패"))
            continue
        url = f"https://{region}-aiplatform.googleapis.com/v1/{corpus}"
        status_code, body, latency = gcp_cli.http_json(url, token)
        state = str(((body.get("corpusStatus") or {}).get("state")) or "UNKNOWN")
        if status_code == 200 and state in {"ACTIVE", "UNKNOWN"}:
            status = "OK" if state == "ACTIVE" else "WARN"
            checks.append(core._check("RESOURCE", name, status, state, latencyMs=latency))
        else:
            checks.append(
                core._check("RESOURCE", name, "FAIL", f"HTTP {status_code or 'timeout'} · {state}")
            )

    if token_ok:
        checks.append(drive_lookup._drive_service_account_status(cfg, project, token, cache))
    else:
        checks.append(
            core._check(
                "RESOURCE",
                "drive-service-account",
                "WARN",
                "gcloud access token이 없어 SA Drive 접근을 확인하지 못했습니다.",
                action="gcloud auth login",
            )
        )
    return checks


def _deploy_and_runtime_status(
    code: str,
    common: dict[str, Any],
    cache: _StatusRunCache | None = None,
) -> list[dict]:
    checks: list[dict[str, Any]] = []
    project = str(common.get("GCP_PROJECT_ID") or "")
    region = str(common["GCP_REGION"])
    gcloud = gcp_cli.gcloud_executable()
    if not gcloud:
        return [core._check("DEPLOY", "gcloud", "FAIL", "gcloud를 찾을 수 없습니다.")]
    gcloud_json = cache.gcloud_json if cache else gcp_cli.gcloud_json
    run_command = cache.run_command if cache else gcp_cli.run_command
    services = ["rag-parser", "rag-sync", "rag-mcp"]
    discovered: dict[str, str] = {}
    service_inventory: dict[str, dict[str, Any]] | None = None
    if cache:
        list_ok, service_rows = gcloud_json(
            [
                "run",
                "services",
                "list",
                "--platform=managed",
                f"--region={region}",
                f"--project={project}",
            ]
        )
        if list_ok and isinstance(service_rows, list):
            service_inventory = {
                str((item.get("metadata") or {}).get("name") or ""): item
                for item in service_rows
                if isinstance(item, dict)
            }
    for service in services:
        if service_inventory is not None:
            data = service_inventory.get(service) or {}
            ok = bool(data)
        else:
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
        label = service.removeprefix("rag-")
        if not ok:
            missing_detail = str(data or "").lower()
            confirmed_missing = service_inventory is not None or any(
                marker in missing_detail
                for marker in ("not found", "not_found", "does not exist")
            )
            if confirmed_missing and service in {"rag-parser", "rag-sync"}:
                checks.append(
                    core._check(
                        "DEPLOY",
                        label,
                        "WARN",
                        "공통 Cloud Run 서비스가 아직 배포되지 않았습니다.",
                        action="공통 런타임 배포",
                        actionType="COMMON_RUNTIME_DEPLOY",
                        departmentCode=code,
                    )
                )
            else:
                checks.append(core._check("DEPLOY", label, "FAIL", "Cloud Run 서비스 없음 또는 조회 실패"))
            continue
        status_data = data.get("status") or {}
        ready_condition = next(
            (item for item in status_data.get("conditions") or [] if item.get("type") == "Ready"),
            {},
        )
        ready = str(ready_condition.get("status") or "").lower() == "true"
        latest_ready = str(status_data.get("latestReadyRevisionName") or "")
        latest_created = str(status_data.get("latestCreatedRevisionName") or "")
        revision_ok = bool(latest_ready and latest_ready == latest_created)
        status = "OK" if ready and revision_ok else "FAIL"
        detail = latest_ready or str(ready_condition.get("message") or "Ready 아님")
        url = str(status_data.get("url") or "")
        action_extra = (
            {
                "action": "공통 런타임 다시 배포",
                "actionType": "COMMON_RUNTIME_DEPLOY",
                "departmentCode": code,
            }
            if status != "OK" and service in {"rag-parser", "rag-sync"}
            else {}
        )
        checks.append(
            core._check(
                "DEPLOY",
                label,
                status,
                detail,
                serviceName=service,
                url=url,
                **action_extra,
            )
        )
        if ready and url:
            discovered[service] = url

    for service in services:
        label = service.removeprefix("rag-") + "-health"
        url = discovered.get(service)
        if not url:
            checks.append(core._check("RUNTIME", label, "SKIP", "배포 상태가 준비되지 않음"))
            continue
        token = ""
        if service in {"rag-parser", "rag-sync"}:
            # 사용자 gcloud 계정은 --audiences를 지원하지 않는다. Cloud Run 개발자
            # 호출은 gcloud의 기본 사용자 ID 토큰으로 인증할 수 있다.
            ok, token = run_command([gcloud, "auth", "print-identity-token", "--quiet"])
            if not ok:
                checks.append(core._check("RUNTIME", label, "WARN", "ID token 발급 실패"))
                continue
        health_url = url.rstrip("/") + "/health"
        status_code, body, latency = gcp_cli.http_json(health_url, token)
        retried = False
        if status_code == 0:
            retried = True
            status_code, body, retry_latency = gcp_cli.http_json(health_url, token, timeout=20)
            latency += retry_latency
        health = str(body.get("status") or "")
        status = "OK" if status_code == 200 and health == "ok" else (
            "WARN" if status_code == 200 and health == "degraded" else "FAIL"
        )
        checks.append(
            core._check(
                "RUNTIME",
                label,
                status,
                (
                    f"HTTP {status_code or 'timeout'} · {health or '응답 없음'}"
                    + (" · cold-start retry" if retried else "")
                ),
                latencyMs=latency,
            )
        )
    return checks


def _sync_status(
    common: dict[str, Any],
    cache: _StatusRunCache | None = None,
    deploy_checks: list[dict[str, Any]] | None = None,
    code: str = "",
) -> list[dict]:
    project = str(common.get("GCP_PROJECT_ID") or "")
    region = str(common["GCP_REGION"])
    gcloud_json = cache.gcloud_json if cache else gcp_cli.gcloud_json

    blockers = [
        str(item.get("name") or "")
        for item in (deploy_checks or [])
        if item.get("layer") == "DEPLOY"
        and str(item.get("name") or "") in {"parser", "sync"}
        and item.get("status") != "OK"
    ]
    if blockers:
        return [
            core._check(
                "SYNC",
                "latest-workflow",
                "SKIP",
                f"배포 단계가 끝나지 않아 확인하지 못했습니다: {', '.join(blockers)}",
                action="공통 런타임 배포",
                actionType="COMMON_RUNTIME_DEPLOY",
                blockedBy=blockers,
                departmentCode=code,
            )
        ]

    workflow_ok, workflow = gcloud_json(
        [
            "workflows",
            "describe",
            "rag-daily-sync",
            f"--location={region}",
            f"--project={project}",
        ]
    )
    if not workflow_ok:
        detail = str(workflow or "").lower()
        if any(marker in detail for marker in ("not found", "not_found", "does not exist")):
            return [
                core._check(
                    "SYNC",
                    "latest-workflow",
                    "WARN",
                    "정기 동기화 Workflow(rag-daily-sync)가 아직 배포되지 않았습니다.",
                    action="공통 런타임 배포",
                    actionType="COMMON_RUNTIME_DEPLOY",
                    departmentCode=code,
                )
            ]
        return [
            core._check(
                "SYNC",
                "latest-workflow",
                "FAIL",
                f"Workflow 조회 실패 · {str(workflow or '')[:200]}",
            )
        ]

    checks: list[dict[str, Any]] = []
    scheduler_ready = False
    job_ok, job = gcloud_json(
        [
            "scheduler",
            "jobs",
            "describe",
            "rag-daily-sync",
            f"--location={region}",
            f"--project={project}",
        ]
    )
    if not job_ok:
        job_detail = str(job or "").lower()
        if any(marker in job_detail for marker in ("not found", "not_found", "does not exist")):
            checks.append(
                core._check(
                    "SYNC",
                    "scheduler-job",
                    "WARN",
                    "정기 동기화 작업(Cloud Scheduler)이 등록되지 않아 자동 실행되지 않습니다.",
                    action="공통 런타임 배포",
                    actionType="COMMON_RUNTIME_DEPLOY",
                    departmentCode=code,
                )
            )
        else:
            checks.append(
                core._check(
                    "SYNC",
                    "scheduler-job",
                    "FAIL",
                    f"Scheduler 작업 조회 실패 · {str(job or '')[:200]}",
                )
            )
    else:
        state = str((job or {}).get("state") or "UNKNOWN")
        schedule = str((job or {}).get("schedule") or "")
        zone = str((job or {}).get("timeZone") or "")
        if state == "ENABLED":
            scheduler_ready = True
            checks.append(
                core._check("SYNC", "scheduler-job", "OK", f"ENABLED · {schedule} {zone}".strip())
            )
        else:
            checks.append(
                core._check(
                    "SYNC",
                    "scheduler-job",
                    "WARN",
                    f"{state} · 자동 실행이 멈춰 있습니다",
                    action=f"gcloud scheduler jobs resume rag-daily-sync --location={region}",
                )
            )

    ok, rows = gcloud_json(
        [
            "workflows",
            "executions",
            "list",
            "rag-daily-sync",
            f"--location={region}",
            f"--project={project}",
            "--limit=10",
        ]
    )
    if not ok:
        checks.append(
            core._check(
                "SYNC",
                "latest-workflow",
                "FAIL",
                f"실행 이력 조회 실패 · {str(rows or '')[:200]}",
            )
        )
        return checks
    if not rows:
        if not scheduler_ready:
            checks.append(
                core._check(
                    "SYNC",
                    "latest-workflow",
                    "SKIP",
                    "정기 실행이 걸려 있지 않아 실행 이력이 없습니다: scheduler-job",
                    action="공통 런타임 배포",
                    actionType="COMMON_RUNTIME_DEPLOY",
                    blockedBy=["scheduler-job"],
                    departmentCode=code,
                )
            )
            return checks
        checks.append(
            core._check(
                "SYNC",
                "latest-workflow",
                "WARN",
                "정기 실행은 등록됐지만 아직 실행된 적이 없습니다. 첫 동기화를 실행해 확인하세요.",
                action="동기화 실행",
                actionType="MANUAL_SYNC",
                departmentCode=code,
            )
        )
        return checks
    row = next(
        (item for item in rows if str(item.get("state") or "") != "CANCELLED"),
        rows[0],
    )
    state = str(row.get("state") or "UNKNOWN")
    started_raw = str(row.get("startTime") or "")
    finished_raw = str(row.get("endTime") or "")
    status = "FAIL"
    detail = state
    if state == "ACTIVE":
        status = "WARN"
        detail = f"ACTIVE · {started_raw}"
    elif state == "CANCELLED":
        status = "WARN"
        detail = "CANCELLED · 취소된 실행이며 서비스 장애는 아닙니다"
    elif state == "SUCCEEDED":
        status = "OK"
        try:
            finished = datetime.fromisoformat(finished_raw)
            hours = (datetime.now(UTC) - finished).total_seconds() / 3600
            if hours > 26:
                status = "WARN"
            detail = f"SUCCEEDED · {hours:.1f}시간 전"
        except ValueError:
            detail = "SUCCEEDED"
    checks.append(core._check("SYNC", "latest-workflow", status, detail))
    return checks


def _warm_status_cache(cache: _StatusRunCache, common: dict[str, Any]) -> None:
    project = str(common.get("GCP_PROJECT_ID") or "")
    region = str(common["GCP_REGION"])
    database = str(common["FIRESTORE_DATABASE"])
    gcloud = gcp_cli.gcloud_executable()
    if not gcloud or not project:
        return

    jobs: list[tuple[Any, list[str]]] = [
        (cache.gcloud_json, ["auth", "list", "--filter=status:ACTIVE"]),
        (cache.gcloud_json, ["storage", "buckets", "list", f"--project={project}"]),
        (
            cache.gcloud_json,
            [
                "firestore",
                "databases",
                "describe",
                f"--database={database}",
                f"--project={project}",
            ],
        ),
        (cache.gcloud_json, ["projects", "describe", project]),
        (
            cache.gcloud_json,
            [
                "run",
                "services",
                "list",
                "--platform=managed",
                f"--region={region}",
                f"--project={project}",
            ],
        ),
        (
            cache.gcloud_json,
            [
                "workflows",
                "executions",
                "list",
                "rag-daily-sync",
                f"--location={region}",
                f"--project={project}",
                "--limit=1",
            ],
        ),
        (cache.run_command, [gcloud, "auth", "print-access-token", "--quiet"]),
        (cache.run_command, [gcloud, "auth", "print-identity-token", "--quiet"]),
    ]
    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = [executor.submit(operation, args) for operation, args in jobs]
        for future in futures:
            future.result()


def _run_department_status(
    code: str, offline: bool, cache: _StatusRunCache | None = None
) -> dict[str, Any]:
    cfg: dict[str, Any] | None = None
    config_revision: str | None = None
    try:
        cfg, config_revision = cloud_registry.cloud_department_config(code)
        checks = _cloud_config_status(cfg)
    except (FileNotFoundError, OSError, RuntimeError, TypeError, ValueError, yaml.YAMLError) as exc:
        checks = [core._check("LOCAL", "cloud-metadata", "FAIL", str(exc))]

    if cfg is None or any(item["status"] == "FAIL" for item in checks):
        for layer in ("RESOURCE", "DEPLOY", "RUNTIME", "SYNC"):
            checks.append(core._check(layer, "prerequisite", "SKIP", "설정 검사 실패"))
    elif offline:
        for layer in ("RESOURCE", "DEPLOY", "RUNTIME", "SYNC"):
            checks.append(core._check(layer, "offline", "SKIP", "오프라인 검사"))
    else:
        common = core._common()
        checks.extend(_resource_status(code, cfg, common, cache))
        deploy_checks = _deploy_and_runtime_status(code, common, cache)
        checks.extend(deploy_checks)
        checks.extend(_sync_status(common, cache, deploy_checks=deploy_checks, code=code))
    result = {
        "code": code,
        "overall": core._overall(checks),
        "checkedAt": datetime.now().astimezone().isoformat(timespec="seconds"),
        "configRevision": config_revision,
        "checks": checks,
    }
    core._LATEST[code] = result
    return result


def _execute_run(run_id: str, codes: list[str], offline: bool) -> None:
    results_by_code: dict[str, dict[str, Any]] = {}
    cache = _StatusRunCache()

    def run_one(code: str) -> dict[str, Any] | None:
        with _STATUS_RUNS.lock:
            if _STATUS_RUNS.runs[run_id].get("cancelRequested"):
                return None
            _STATUS_RUNS.runs[run_id]["currentDepartment"] = code
        return _run_department_status(code, offline, cache)

    try:
        if not offline:
            _warm_status_cache(cache, core._common())
        worker_count = min(4, len(codes))
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            futures = {executor.submit(run_one, code): code for code in codes}
            for future in as_completed(futures):
                code = futures[future]
                result = future.result()
                if result is not None:
                    results_by_code[code] = result
                with _STATUS_RUNS.lock:
                    ordered = [results_by_code[item] for item in codes if item in results_by_code]
                    _STATUS_RUNS.runs[run_id]["departments"] = ordered
        with _STATUS_RUNS.lock:
            run = _STATUS_RUNS.runs[run_id]
            if run.get("cancelRequested"):
                run["status"] = "CANCELLED"
            else:
                run["status"] = "COMPLETED"
            run["departments"] = [results_by_code[item] for item in codes if item in results_by_code]
            run["currentDepartment"] = None
            run["finishedEpoch"] = time.time()
            run["finishedAt"] = datetime.now().astimezone().isoformat(timespec="seconds")
    except Exception as exc:  # noqa: BLE001 - background job boundary
        with _STATUS_RUNS.lock:
            run = _STATUS_RUNS.runs[run_id]
            run["status"] = "FAILED"
            run["error"] = str(exc)[:500]
            run["finishedEpoch"] = time.time()


@router.post("/api/v1/status-runs")
async def start_status(request: Request) -> JSONResponse:
    core._require_local_session(request)
    payload = await request.json()
    requested = payload.get("departments") if isinstance(payload, dict) else []
    cloud_codes = {str(item.get("code") or "") for item in cloud_registry.cloud_mcp_department_records()}
    if requested:
        codes = sorted({str(item).strip().lower() for item in requested})
    else:
        codes = sorted(cloud_codes)
    if not codes or any(not core.DEPT_CODE_RE.fullmatch(code) for code in codes):
        raise HTTPException(status_code=404, detail="department not found")
    for code in codes:
        try:
            cloud_registry.cloud_department_config(code)
        except (FileNotFoundError, OSError, RuntimeError, TypeError, ValueError, yaml.YAMLError):
            raise HTTPException(status_code=404, detail="department not found") from None
    offline = bool(payload.get("offline", False))
    with _STATUS_RUNS.lock:
        _STATUS_RUNS.cleanup()
        for run in _STATUS_RUNS.runs.values():
            if run["status"] == "RUNNING" and run["scope"] == codes and run["offline"] == offline:
                return JSONResponse({"runId": run["runId"], "status": "RUNNING"})
        active = sum(run["status"] == "RUNNING" for run in _STATUS_RUNS.runs.values())
        if active >= 2:
            return JSONResponse(
                {"error": {"code": "STATUS_CAPACITY", "message": "상태 검사 2개가 실행 중입니다."}},
                status_code=429,
            )
        run_id = uuid.uuid4().hex
        run = {
            "runId": run_id,
            "status": "RUNNING",
            "scope": codes,
            "offline": offline,
            "startedAt": datetime.now().astimezone().isoformat(timespec="seconds"),
            "startedEpoch": time.time(),
            "departments": [],
            "currentDepartment": None,
            "cancelRequested": False,
        }
        _STATUS_RUNS.runs[run_id] = run
    threading.Thread(target=_execute_run, args=(run_id, codes, offline), daemon=True).start()
    return JSONResponse({"runId": run_id, "status": "RUNNING"}, status_code=202)


@router.get("/api/v1/status-runs")
def list_runs(status: str | None = None) -> dict[str, Any]:
    with _STATUS_RUNS.lock:
        _STATUS_RUNS.cleanup()
        rows = [dict(run) for run in _STATUS_RUNS.runs.values() if not status or run["status"] == status]
    for row in rows:
        row.pop("startedEpoch", None)
        row.pop("finishedEpoch", None)
        row.pop("cancelRequested", None)
    return {"runs": rows}


@router.get("/api/v1/status-runs/{run_id}")
def get_run(run_id: str) -> dict[str, Any]:
    with _STATUS_RUNS.lock:
        run = _STATUS_RUNS.runs.get(run_id)
        if not run:
            raise HTTPException(status_code=404, detail="status run not found")
        result = dict(run)
    result.pop("startedEpoch", None)
    result.pop("finishedEpoch", None)
    result.pop("cancelRequested", None)
    return result


@router.delete("/api/v1/status-runs/{run_id}")
def cancel_run(run_id: str, request: Request) -> dict[str, str]:
    core._require_local_session(request)
    with _STATUS_RUNS.lock:
        run = _STATUS_RUNS.runs.get(run_id)
        if not run:
            raise HTTPException(status_code=404, detail="status run not found")
        run["cancelRequested"] = True
    return {"status": "CANCELLING"}
