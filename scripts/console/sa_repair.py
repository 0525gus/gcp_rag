from __future__ import annotations

import copy
import re
import threading
import time
import uuid
from typing import Any

import yaml
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from scripts import gcp_cli
from scripts.console import core, drive_lookup, environment_setup, provisioning
from scripts.run_store import RunStore

router = APIRouter()


_SA_REPAIRS = RunStore(30 * 60, "steps")


SA_TOKEN_CREATOR_ROLE = "roles/iam.serviceAccountTokenCreator"


COMPUTE_SERVICE = "compute.googleapis.com"


IAM_CREDENTIALS_SERVICE = "iamcredentials.googleapis.com"


def drive_service_account_status(project: str) -> dict[str, Any]:
    result: dict[str, Any] = {
        "projectId": project,
        "serviceAccount": "",
        "account": "",
        "status": "FAIL",
        "detail": "",
        "issues": [],
    }
    if not core.PROJECT_RE.fullmatch(project or ""):
        result["detail"] = "공통 설정의 프로젝트를 먼저 확인해 주세요."
        return result

    bootstrap = environment_setup._gcloud_bootstrap_state(include_projects=False)
    if not bootstrap["installed"] or not bootstrap["authenticated"]:
        result["detail"] = "gcloud 로그인이 필요합니다."
        result["issues"].append("gcloudAuth")
        return result
    account_ok, active_accounts = gcp_cli.gcloud_json(
        ["auth", "list", "--filter=status:ACTIVE"], timeout=20
    )
    if account_ok and isinstance(active_accounts, list) and active_accounts:
        result["account"] = str(active_accounts[0].get("account") or "")
    else:
        result["account"] = bootstrap["account"]

    service_account = environment_setup._default_compute_service_account(project)
    if not service_account:
        result["detail"] = "프로젝트 번호를 확인하지 못했습니다."
        result["issues"].append("projectNumber")
        return result
    result["serviceAccount"] = service_account

    services_ok, enabled = provisioning._gcloud_enabled_services(project)
    if services_ok and COMPUTE_SERVICE not in enabled:
        result["issues"].append("computeApi")
    if services_ok and IAM_CREDENTIALS_SERVICE not in enabled:
        result["issues"].append("iamCredentialsApi")

    exists_ok, _ = gcp_cli.gcloud_json(
        ["iam", "service-accounts", "describe", service_account, f"--project={project}"],
        timeout=20,
    )
    if not exists_ok:
        if "computeApi" not in result["issues"]:
            result["issues"].append("serviceAccountMissing")
        result["status"] = "FAIL"
        result["detail"] = "기본 Compute 서비스 계정이 아직 없습니다."
        return result

    try:
        caller_token = gcp_cli.access_token()
    except RuntimeError as exc:
        result["detail"] = str(exc)[:200]
        result["issues"].append("gcloudAuth")
        return result

    _token, _sa, latency, token_error = drive_lookup._service_account_access_token(
        project,
        caller_token,
        ["https://www.googleapis.com/auth/drive.readonly"],
    )
    result["latencyMs"] = latency
    if token_error:
        result["status"] = "FAIL"
        result["detail"] = token_error
        if "tokenCreator" not in result["issues"]:
            result["issues"].append("tokenCreator")
        return result

    result["status"] = "OK"
    result["detail"] = "서비스 계정 연결 확인됨"
    return result


def create_sa_repair_plan(project: str) -> dict[str, Any]:
    status = drive_service_account_status(project)
    if status["status"] == "OK":
        raise FileExistsError("서비스 계정이 이미 정상입니다.")
    if "gcloudAuth" in status["issues"] or "projectNumber" in status["issues"]:
        raise ValueError(status["detail"] or "gcloud 로그인을 먼저 확인해 주세요.")

    account = status["account"]
    service_account = status["serviceAccount"]
    steps: list[dict[str, Any]] = []
    if "computeApi" in status["issues"] or "serviceAccountMissing" in status["issues"]:
        steps.append(
            {
                "key": "enableCompute",
                "kind": "service",
                "label": "Compute Engine API 활성화",
                "target": COMPUTE_SERVICE,
                "detail": "기본 Compute 서비스 계정은 이 API 를 켤 때 만들어집니다.",
            }
        )
    if "iamCredentialsApi" in status["issues"]:
        steps.append(
            {
                "key": "enableIamCredentials",
                "kind": "service",
                "label": "IAM Credentials API 활성화",
                "target": IAM_CREDENTIALS_SERVICE,
                "detail": "서비스 계정 임시 접근 토큰 발급에 필요합니다.",
            }
        )
    if service_account:
        member_type = (
            "serviceAccount" if account.endswith(".gserviceaccount.com") else "user"
        )
        steps.append(
            {
                "key": "grantTokenCreator",
                "kind": "iamBinding",
                "label": "토큰 생성 권한 부여",
                "target": service_account,
                "role": SA_TOKEN_CREATOR_ROLE,
                "member": f"{member_type}:{account}",
                "detail": f"{account} 계정이 이 서비스 계정으로 연결할 수 있게 합니다.",
            }
        )
    if not steps:
        raise FileExistsError("자동으로 조치할 항목이 없습니다.")

    plan_id = uuid.uuid4().hex
    plan = {
        "planId": plan_id,
        "projectId": project,
        "account": account,
        "serviceAccount": service_account,
        "steps": steps,
        "createdEpoch": time.time(),
        "started": False,
    }
    with _SA_REPAIRS.lock:
        _SA_REPAIRS.cleanup()
        _SA_REPAIRS.plans[plan_id] = plan
    return copy.deepcopy(plan)


def _grant_service_account_role(
    service_account: str, member: str, role: str, project: str
) -> None:
    gcloud = gcp_cli.gcloud_executable()
    if not gcloud:
        raise RuntimeError("gcloud를 찾을 수 없습니다.")
    ok, output = gcp_cli.run_command(
        [
            gcloud,
            "iam",
            "service-accounts",
            "add-iam-policy-binding",
            service_account,
            f"--member={member}",
            f"--role={role}",
            f"--project={project}",
            "--quiet",
        ],
        timeout=120,
    )
    if not ok:
        raise RuntimeError((output or "권한 부여에 실패했습니다.")[-400:])


def _execute_sa_repair_run(run_id: str) -> None:
    with _SA_REPAIRS.lock:
        run = copy.deepcopy(_SA_REPAIRS.runs[run_id])
    project = run["projectId"]

    for step in run["steps"]:
        key = step["key"]
        _SA_REPAIRS.update_item(run_id, key, status="RUNNING", detail="진행 중")
        try:
            if step["kind"] == "service":
                provisioning._enable_gcloud_service(step["target"], project)
            else:
                _grant_service_account_role(
                    step["target"], step["member"], step["role"], project
                )
        except (OSError, RuntimeError) as exc:
            _SA_REPAIRS.update_item(run_id, key, status="FAILED", detail=str(exc)[:400])
        else:
            _SA_REPAIRS.update_item(run_id, key, status="COMPLETE", detail="완료")

    verified: dict[str, Any] = {}
    with _SA_REPAIRS.lock:
        grant_succeeded = any(
            item.get("key") == "grantTokenCreator" and item.get("status") == "COMPLETE"
            for item in _SA_REPAIRS.runs[run_id]["steps"]
        )
    verification_attempts = 11 if grant_succeeded else 1
    for attempt in range(1, verification_attempts + 1):
        verified = drive_service_account_status(project)
        if verified["status"] == "OK":
            break
        if attempt < verification_attempts:
            time.sleep(3)
    with _SA_REPAIRS.lock:
        current = _SA_REPAIRS.runs[run_id]
        current["verification"] = verified
        statuses = [item.get("status") for item in current["steps"]]
        if verified["status"] == "OK":
            current["status"] = "COMPLETED"
        elif any(item == "COMPLETE" for item in statuses):
            current["status"] = "PARTIAL"
        else:
            current["status"] = "FAILED"
        current["finishedEpoch"] = time.time()


def start_sa_repair_run(plan_id: str) -> dict[str, Any]:
    with _SA_REPAIRS.lock:
        _SA_REPAIRS.cleanup()
        plan = _SA_REPAIRS.plans.get(plan_id)
        if not plan:
            raise FileNotFoundError(plan_id)
        if plan["started"]:
            raise FileExistsError("이미 실행한 조치 계획입니다.")
        for active in _SA_REPAIRS.runs.values():
            if active["status"] == "RUNNING":
                raise FileExistsError("서비스 계정 조치가 이미 진행 중입니다.")

        run_id = uuid.uuid4().hex
        steps = copy.deepcopy(plan["steps"])
        for item in steps:
            item.update(status="PENDING", detail="대기 중")
        run = {
            "runId": run_id,
            "planId": plan_id,
            "projectId": plan["projectId"],
            "account": plan["account"],
            "serviceAccount": plan["serviceAccount"],
            "status": "RUNNING",
            "steps": steps,
            "verification": {},
            "createdEpoch": time.time(),
        }
        plan["started"] = True
        _SA_REPAIRS.runs[run_id] = run

    threading.Thread(
        target=_execute_sa_repair_run,
        args=(run_id,),
        name=f"sa-repair-{run_id[:8]}",
        daemon=True,
    ).start()
    return copy.deepcopy(run)


@router.get("/api/v1/drive-service-account/status")
def drive_sa_status() -> JSONResponse:
    try:
        project = str(core._common().get("GCP_PROJECT_ID") or "")
    except (OSError, TypeError, UnicodeError, yaml.YAMLError):
        project = ""
    return JSONResponse(drive_service_account_status(project))


@router.post("/api/v1/drive-service-account/repair-plans")
def drive_sa_repair_plan(request: Request) -> JSONResponse:
    core._require_local_session(request)
    try:
        project = str(core._common().get("GCP_PROJECT_ID") or "")
    except (OSError, TypeError, UnicodeError, yaml.YAMLError):
        project = ""
    try:
        plan = create_sa_repair_plan(project)
    except FileExistsError as exc:
        return JSONResponse(
            {"error": {"code": "SA_ALREADY_OK", "message": str(exc)}}, status_code=409
        )
    except (TypeError, ValueError) as exc:
        return JSONResponse(
            {"error": {"code": "SA_REPAIR_UNAVAILABLE", "message": str(exc)}}, status_code=422
        )
    return JSONResponse(plan, status_code=200)


@router.post("/api/v1/drive-service-account/repairs")
async def drive_sa_repair(request: Request) -> JSONResponse:
    core._require_local_session(request)
    payload = await request.json()
    plan_id = str(payload.get("planId") or "") if isinstance(payload, dict) else ""
    if not re.fullmatch(r"[0-9a-f]{32}", plan_id):
        return JSONResponse(
            {"error": {"code": "INVALID_REPAIR_PLAN", "message": "조치 계획을 다시 열어 주세요."}},
            status_code=422,
        )
    try:
        run = start_sa_repair_run(plan_id)
    except FileNotFoundError:
        return JSONResponse(
            {
                "error": {
                    "code": "REPAIR_PLAN_EXPIRED",
                    "message": "조치 계획이 만료되었습니다. 상태를 다시 확인해 주세요.",
                }
            },
            status_code=404,
        )
    except FileExistsError as exc:
        return JSONResponse(
            {"error": {"code": "SA_REPAIR_CONFLICT", "message": str(exc)}}, status_code=409
        )
    return JSONResponse(run, status_code=202)


@router.get("/api/v1/drive-service-account/repairs/{run_id}")
def drive_sa_repair_run(run_id: str) -> JSONResponse:
    with _SA_REPAIRS.lock:
        _SA_REPAIRS.cleanup()
        run = _SA_REPAIRS.runs.get(run_id)
        if not run:
            return JSONResponse(
                {
                    "error": {
                        "code": "REPAIR_RUN_NOT_FOUND",
                        "message": "조치 실행을 찾을 수 없습니다.",
                    }
                },
                status_code=404,
            )
        return JSONResponse(copy.deepcopy(run))
