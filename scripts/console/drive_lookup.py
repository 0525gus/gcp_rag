from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from urllib.parse import quote, urlencode

import yaml
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from scripts import gcp_cli
from scripts.console import core, dept_forms, status_checks

router = APIRouter()


DRIVE_FOLDER_LOOKUP_LIMIT = 200


DRIVE_FOLDER_MIME_TYPE = "application/vnd.google-apps.folder"


def _service_account_access_token(
    project: str,
    caller_token: str,
    scopes: list[str],
    *,
    gcloud_json: Any = None,
) -> tuple[str, str, int, str]:
    project_ok, project_info = (gcloud_json or gcp_cli.gcloud_json)(["projects", "describe", project])
    project_number = str(project_info.get("projectNumber") or "") if project_ok else ""
    if not project_number:
        return "", "", 0, "프로젝트 번호를 확인하지 못했습니다."

    service_account = f"{project_number}-compute@developer.gserviceaccount.com"
    token_url = (
        "https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/"
        f"{service_account}:generateAccessToken"
    )
    status, body, latency = gcp_cli.http_post_json(
        token_url,
        {"scope": scopes, "lifetime": "900s"},
        caller_token,
    )
    token = str(body.get("accessToken") or "") if isinstance(body, dict) else ""
    if status != 200 or not token:
        return token, service_account, latency, f"서비스 계정 임시 접근 토큰 HTTP {status or 'timeout'}"
    return token, service_account, latency, ""


def _drive_folder_info(folder_id: str, token: str) -> dict[str, Any]:
    params = urlencode(
        {
            "supportsAllDrives": "true",
            "fields": "id,name,driveId,mimeType,parents,trashed",
        }
    )
    url = f"https://www.googleapis.com/drive/v3/files/{quote(folder_id, safe='')}?{params}"
    status, body, latency = gcp_cli.http_json(url, token, timeout=15)
    result: dict[str, Any] = {
        "folderId": folder_id,
        "status": "FAIL",
        "name": "",
        "driveId": "",
        "parentIds": [],
        "latencyMs": latency,
    }
    if status != 200 or not isinstance(body, dict):
        result["reason"] = f"Drive HTTP {status or 'timeout'}"
        return result

    result["name"] = str(body.get("name") or "")
    result["driveId"] = str(body.get("driveId") or "")
    result["parentIds"] = core._normalise_ids(body.get("parents"))
    if body.get("trashed"):
        result["reason"] = "휴지통에 있는 폴더입니다."
    elif str(body.get("mimeType") or "") != DRIVE_FOLDER_MIME_TYPE:
        result["reason"] = "폴더가 아닌 Drive 항목입니다."
    else:
        result["status"] = "OK"
        result["reason"] = ""
    return result


def _lookup_drive_folders(folder_ids: list[str], token: str) -> dict[str, Any]:
    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=min(12, max(1, len(folder_ids)))) as executor:
        folders = list(executor.map(lambda item: _drive_folder_info(item, token), folder_ids))
    resolved = sum(item["status"] == "OK" for item in folders)
    return {
        "status": "COMPLETE" if resolved == len(folders) else "PARTIAL",
        "folders": folders,
        "stats": {
            "requested": len(folder_ids),
            "resolved": resolved,
            "failed": len(folder_ids) - resolved,
        },
        "latencyMs": round((time.perf_counter() - started) * 1000),
    }


def _drive_folder_children(drive_id: str, parent_id: str, token: str) -> dict[str, Any]:
    query = (
        f"'{parent_id}' in parents and "
        f"mimeType = '{DRIVE_FOLDER_MIME_TYPE}' and trashed = false"
    )
    params = urlencode(
        {
            "q": query,
            "corpora": "drive",
            "driveId": drive_id,
            "includeItemsFromAllDrives": "true",
            "supportsAllDrives": "true",
            "pageSize": "200",
            "orderBy": "folder,name_natural",
            "fields": "nextPageToken,files(id,name,driveId,parents,mimeType)",
        }
    )
    status, body, latency = gcp_cli.http_json(
        f"https://www.googleapis.com/drive/v3/files?{params}", token, timeout=20
    )
    if status != 200 or not isinstance(body, dict):
        raise RuntimeError(f"Drive 폴더 목록 HTTP {status or 'timeout'}")
    folders = [
        {
            "folderId": str(item.get("id") or ""),
            "name": str(item.get("name") or "이름 없는 폴더"),
            "driveId": str(item.get("driveId") or drive_id),
            "parentIds": core._normalise_ids(item.get("parents")),
        }
        for item in body.get("files") or []
        if isinstance(item, dict) and item.get("id")
    ]
    return {
        "driveId": drive_id,
        "parentId": parent_id,
        "folders": folders,
        "truncated": bool(body.get("nextPageToken")),
        "latencyMs": latency,
    }


def _drive_service_account_status(
    cfg: dict[str, Any],
    project: str,
    caller_token: str,
    cache: status_checks._StatusRunCache | None = None,
) -> dict[str, Any]:
    drive_ids = core._normalise_ids((cfg.get("drive") or {}).get("driveIds"))
    if not drive_ids:
        return core._check("RESOURCE", "drive-service-account", "FAIL", "공유드라이브 ID가 없습니다.")

    gcloud_json = cache.gcloud_json if cache else gcp_cli.gcloud_json
    impersonated_token, service_account, token_latency, token_error = _service_account_access_token(
        project,
        caller_token,
        ["https://www.googleapis.com/auth/drive.readonly"],
        gcloud_json=gcloud_json,
    )
    if not service_account:
        return core._check(
            "RESOURCE", "drive-service-account", "WARN", "프로젝트 번호를 확인하지 못했습니다."
        )
    if not impersonated_token:
        return core._check(
            "RESOURCE",
            "drive-service-account",
            "WARN",
            f"SA 자동 확인 불가 · {token_error}",
            action=(
                "현재 계정에 roles/iam.serviceAccountTokenCreator 필요: "
                f"{service_account}"
            ),
            latencyMs=token_latency,
        )

    failures: list[str] = []
    names: list[str] = []
    total_latency = token_latency
    for drive_id in drive_ids:
        drive_url = f"https://www.googleapis.com/drive/v3/drives/{drive_id}?fields=id,name"
        status_code, body, latency = gcp_cli.http_json(drive_url, impersonated_token)
        total_latency += latency
        if status_code != 200:
            failures.append(f"{drive_id} (접근 HTTP {status_code or 'timeout'})")
            continue

        name = str(body.get("name") or drive_id)
        delta_url = (
            "https://www.googleapis.com/drive/v3/changes/startPageToken"
            f"?driveId={drive_id}&supportsAllDrives=true"
        )
        delta_status, _, delta_latency = gcp_cli.http_json(delta_url, impersonated_token)
        total_latency += delta_latency
        if delta_status != 200:
            failures.append(f"{drive_id} (변경 토큰 HTTP {delta_status or 'timeout'})")
            continue
        names.append(name)

    if failures:
        return core._check(
            "RESOURCE",
            "drive-service-account",
            "FAIL",
            "SA 연결 실패: " + ", ".join(failures),
            action=f"Drive 멤버 관리에서 {service_account}를 뷰어 이상으로 추가",
            latencyMs=total_latency,
        )
    summary = ", ".join(names[:3])
    if len(names) > 3:
        summary += f" 외 {len(names) - 3}개"
    return core._check(
        "RESOURCE",
        "drive-service-account",
        "OK",
        f"{len(drive_ids)}개 Drive SA 실접근 확인 · {summary}",
        latencyMs=total_latency,
    )


@router.post("/api/v1/departments/drive-preflight")
async def drive_preflight(request: Request) -> JSONResponse:
    core._require_local_session(request)
    payload = await request.json()
    drive_ids = core._normalise_ids(payload.get("driveIds") if isinstance(payload, dict) else None)
    if not drive_ids:
        return JSONResponse(
            {
                "error": {
                    "code": "DRIVE_ID_REQUIRED",
                    "message": "확인할 공유드라이브 ID를 입력해 주세요.",
                }
            },
            status_code=422,
        )
    try:
        common = core._common()
    except (FileNotFoundError, OSError, TypeError, UnicodeError, yaml.YAMLError):
        return JSONResponse(
            {"error": {"code": "COMMON_REQUIRED", "message": "공통 설정이 먼저 필요합니다."}},
            status_code=428,
        )
    gcloud = gcp_cli.gcloud_executable()
    if not gcloud:
        return JSONResponse(
            {"error": {"code": "GCLOUD_REQUIRED", "message": "gcloud를 찾을 수 없습니다."}},
            status_code=503,
        )
    token_ok, token = gcp_cli.run_command([gcloud, "auth", "print-access-token", "--quiet"])
    if not token_ok:
        return JSONResponse(
            {"error": {"code": "GCLOUD_AUTH_REQUIRED", "message": "gcloud 로그인이 필요합니다."}},
            status_code=401,
        )
    project = str(common.get("GCP_PROJECT_ID") or "")
    result = _drive_service_account_status(
        {"drive": {"driveIds": drive_ids}}, project, token
    )
    code = str(payload.get("code") or "").strip().lower() if isinstance(payload, dict) else ""
    return JSONResponse(
        {
            "status": result["status"],
            "detail": result["detail"],
            "action": result.get("action", ""),
            "latencyMs": result.get("latencyMs", 0),
            "driveIds": drive_ids,
            "driveConflicts": dept_forms._department_drive_conflicts(code, drive_ids),
        }
    )


@router.post("/api/v1/departments/folder-lookup")
async def drive_folder_lookup(request: Request) -> JSONResponse:
    core._require_local_session(request)
    payload = await request.json()
    folder_ids = core._normalise_ids(
        payload.get("folderIds") if isinstance(payload, dict) else None
    )
    if not folder_ids:
        return JSONResponse(
            {
                "error": {
                    "code": "FOLDER_ID_REQUIRED",
                    "message": "확인할 동기화 폴더 ID를 입력해 주세요.",
                }
            },
            status_code=422,
        )
    if len(folder_ids) > DRIVE_FOLDER_LOOKUP_LIMIT or any(
        not core.DRIVE_FILE_ID_RE.fullmatch(folder_id) for folder_id in folder_ids
    ):
        return JSONResponse(
            {
                "error": {
                    "code": "INVALID_FOLDER_IDS",
                    "message": (
                        f"Drive 폴더 ID 형식을 확인해 주세요. "
                        f"한 번에 최대 {DRIVE_FOLDER_LOOKUP_LIMIT}개까지 확인할 수 있습니다."
                    ),
                }
            },
            status_code=422,
        )
    try:
        common = core._common()
    except (FileNotFoundError, OSError, TypeError, UnicodeError, yaml.YAMLError):
        return JSONResponse(
            {"error": {"code": "COMMON_REQUIRED", "message": "공통 설정이 먼저 필요합니다."}},
            status_code=428,
        )
    gcloud = gcp_cli.gcloud_executable()
    if not gcloud:
        return JSONResponse(
            {"error": {"code": "GCLOUD_REQUIRED", "message": "gcloud를 찾을 수 없습니다."}},
            status_code=503,
        )
    token_ok, caller_token = gcp_cli.run_command([gcloud, "auth", "print-access-token", "--quiet"])
    if not token_ok:
        return JSONResponse(
            {"error": {"code": "GCLOUD_AUTH_REQUIRED", "message": "gcloud 로그인이 필요합니다."}},
            status_code=401,
        )
    token, service_account, _latency, error = _service_account_access_token(
        str(common.get("GCP_PROJECT_ID") or ""),
        caller_token,
        ["https://www.googleapis.com/auth/drive.readonly"],
    )
    if not token:
        suffix = f" ({service_account})" if service_account else ""
        return JSONResponse(
            {
                "error": {
                    "code": "DRIVE_FOLDER_LOOKUP_FAILED",
                    "message": f"{error}{suffix}",
                }
            },
            status_code=503,
        )
    result = _lookup_drive_folders(folder_ids, token)
    result["serviceAccount"] = service_account
    return JSONResponse(result)


@router.post("/api/v1/departments/drive-folders")
async def drive_folder_children(request: Request) -> JSONResponse:
    core._require_local_session(request)
    payload = await request.json()
    drive_id = str(payload.get("driveId") or "").strip() if isinstance(payload, dict) else ""
    parent_id = str(payload.get("parentId") or drive_id).strip() if isinstance(payload, dict) else ""
    if not core.DRIVE_FILE_ID_RE.fullmatch(drive_id) or not core.DRIVE_FILE_ID_RE.fullmatch(parent_id):
        return JSONResponse(
            {"error": {"code": "INVALID_DRIVE_FOLDER", "message": "공유드라이브 또는 폴더 ID 형식을 확인해 주세요."}},
            status_code=422,
        )
    try:
        common = core._common()
    except (FileNotFoundError, OSError, TypeError, UnicodeError, yaml.YAMLError):
        return JSONResponse(
            {"error": {"code": "COMMON_REQUIRED", "message": "공통 설정이 먼저 필요합니다."}},
            status_code=428,
        )
    gcloud = gcp_cli.gcloud_executable()
    if not gcloud:
        return JSONResponse(
            {"error": {"code": "GCLOUD_REQUIRED", "message": "gcloud를 찾을 수 없습니다."}},
            status_code=503,
        )
    token_ok, caller_token = gcp_cli.run_command([gcloud, "auth", "print-access-token", "--quiet"])
    if not token_ok:
        return JSONResponse(
            {"error": {"code": "GCLOUD_AUTH_REQUIRED", "message": "gcloud 로그인이 필요합니다."}},
            status_code=401,
        )
    token, service_account, _latency, error = _service_account_access_token(
        str(common.get("GCP_PROJECT_ID") or ""),
        caller_token,
        ["https://www.googleapis.com/auth/drive.readonly"],
    )
    if not token:
        suffix = f" ({service_account})" if service_account else ""
        return JSONResponse(
            {"error": {"code": "DRIVE_FOLDER_BROWSE_FAILED", "message": f"{error}{suffix}"}},
            status_code=503,
        )
    try:
        result = _drive_folder_children(drive_id, parent_id, token)
    except RuntimeError as exc:
        return JSONResponse(
            {"error": {"code": "DRIVE_FOLDER_BROWSE_FAILED", "message": str(exc)}},
            status_code=503,
        )
    result["serviceAccount"] = service_account
    return JSONResponse(result)
