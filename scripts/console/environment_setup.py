from __future__ import annotations

import copy
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode

import yaml
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from scripts import common_config, docai_setup, gcp_cli
from scripts.console import cloud_registry, core, dept_forms, docai_routes, sync_history

router = APIRouter()


_AUTH_PROCESS: subprocess.Popen[Any] | None = None


_GCLOUD_INSTALLER_URL = "https://dl.google.com/dl/cloudsdk/channels/rapid/GoogleCloudSDKInstaller.exe"


_GCLOUD_INSTALL_LOCK = threading.Lock()


_GCLOUD_INSTALL: dict[str, Any] = {"status": "IDLE"}


def validate_common_candidate(payload: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    errors: dict[str, list[str]] = {}
    defaults = common_config.defaults()
    project = str(payload.get("projectId") or "").strip()
    region = str(payload.get("region") or defaults["GCP_REGION"]).strip()
    artifact_repo = str(payload.get("artifactRepo") or defaults["ARTIFACT_REPO"]).strip()
    firestore = str(payload.get("firestoreDatabase") or defaults["FIRESTORE_DATABASE"]).strip()

    if not core.PROJECT_RE.fullmatch(project):
        core._field_error(errors, "projectId", "유효한 GCP 프로젝트 ID를 입력해 주세요.")
    if not core.REGION_RE.fullmatch(region):
        core._field_error(errors, "region", "예: asia-northeast3 형식이어야 합니다.")
    if not re.fullmatch(r"[a-z][a-z0-9._-]{1,62}", artifact_repo):
        core._field_error(errors, "artifactRepo", "유효한 Artifact Registry 저장소 이름이 필요합니다.")
    if firestore != "(default)" and not re.fullmatch(r"[a-z][a-z0-9-]{1,61}[a-z0-9]", firestore):
        core._field_error(errors, "firestoreDatabase", "유효한 Firestore 데이터베이스 ID가 필요합니다.")

    candidate = {
        "GCP_PROJECT_ID": project,
        "GCP_REGION": region,
        "ARTIFACT_REPO": artifact_repo,
        "FIRESTORE_DATABASE": firestore,
    }
    try:
        candidate.update(docai_setup.selection_fields(payload.get("docai", {})))
    except ValueError as exc:
        core._field_error(errors, "docai", str(exc))
    return candidate, {"valid": not errors, "fieldErrors": errors}


def create_common_config(candidate: dict[str, Any]) -> Path:
    target = (core.CONFIG_DIR / "common.yaml").resolve()
    if target.parent != core.CONFIG_DIR.resolve():
        raise ValueError("설정 디렉터리 밖의 경로는 사용할 수 없습니다.")
    if target.exists():
        raise FileExistsError(target)
    core.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    temp = core.CONFIG_DIR / f".common.{uuid.uuid4().hex}.tmp"
    try:
        rendered = yaml.safe_dump(candidate, allow_unicode=True, sort_keys=False, width=1000)
        temp.write_text(rendered, encoding="utf-8", newline="\n")
        parsed = core._read_yaml(temp)
        if parsed.get("GCP_PROJECT_ID") != candidate["GCP_PROJECT_ID"]:
            raise RuntimeError("작성한 common.yaml의 재검증에 실패했습니다.")
        os.replace(temp, target)
        return target
    finally:
        temp.unlink(missing_ok=True)


def _default_compute_service_account(project: str) -> str:
    if not project:
        return ""
    ok, project_info = gcp_cli.gcloud_json(["projects", "describe", project])
    project_number = str(project_info.get("projectNumber") or "") if ok else ""
    return f"{project_number}-compute@developer.gserviceaccount.com" if project_number else ""


def _gcloud_bootstrap_state(*, include_projects: bool = True) -> dict[str, Any]:
    gcloud = gcp_cli.gcloud_executable()
    state: dict[str, Any] = {
        "installed": bool(gcloud),
        "authenticated": False,
        "account": "",
        "currentProject": "",
        "projects": [],
        "regions": core.SETUP_REGIONS,
    }
    if not gcloud:
        return state

    with ThreadPoolExecutor(max_workers=4) as pool:
        auth_future = pool.submit(gcp_cli.gcloud_json, ["auth", "list", "--filter=status:ACTIVE"])
        token_future = pool.submit(
            gcp_cli.run_command, [gcloud, "auth", "print-access-token", "--quiet"]
        )
        current_future = pool.submit(
            gcp_cli.run_command, [gcloud, "config", "get-value", "project", "--quiet"]
        )
        auth_ok, accounts = auth_future.result()
        token_ok, _ = token_future.result()
        current_ok, current = current_future.result()

    if not auth_ok or not isinstance(accounts, list) or not accounts:
        return state
    account = str(accounts[0].get("account") or "")
    state["authenticated"] = bool(account and token_ok)
    if not state["authenticated"]:
        return state
    if "@" in account:
        local, domain = account.split("@", 1)
        state["account"] = (local[:2] + "***@" + domain) if local else "***@" + domain
    if current_ok:
        state["currentProject"] = current.strip()

    if include_projects and state["authenticated"] and state["currentProject"]:
        state["projects"] = [
            {"id": state["currentProject"], "name": state["currentProject"]}
        ]
    return state


def _gcloud_project_resources(project: str, region: str) -> dict[str, Any]:
    artifacts_ok, artifacts_raw = gcp_cli.gcloud_json(
        [
            "artifacts",
            "repositories",
            "list",
            f"--project={project}",
            f"--location={region}",
        ],
        timeout=20,
    )
    artifacts: list[dict[str, str]] = []
    if artifacts_ok and isinstance(artifacts_raw, list):
        for item in artifacts_raw:
            name = str(item.get("name") or "")
            repo_id = name.rsplit("/repositories/", 1)[-1] if "/repositories/" in name else name
            repo_format = str(item.get("format") or "")
            if repo_id and repo_format in {"", "DOCKER"}:
                artifacts.append({"id": repo_id, "format": repo_format or "DOCKER"})

    firestore_ok, firestore_raw = gcp_cli.gcloud_json(
        ["firestore", "databases", "list", f"--project={project}"], timeout=20
    )
    databases: list[dict[str, str]] = []
    if firestore_ok and isinstance(firestore_raw, list):
        for item in firestore_raw:
            name = str(item.get("name") or "")
            database_id = name.rsplit("/databases/", 1)[-1] if "/databases/" in name else name
            database_type = str(item.get("type") or "")
            if database_id and database_type in {"", "FIRESTORE_NATIVE"}:
                databases.append(
                    {
                        "id": database_id,
                        "location": str(item.get("locationId") or ""),
                        "type": database_type or "FIRESTORE_NATIVE",
                    }
                )
    return {
        "artifactRepositories": sorted(artifacts, key=lambda item: item["id"]),
        "firestoreDatabases": sorted(databases, key=lambda item: item["id"]),
        "artifactError": "" if artifacts_ok else str(artifacts_raw)[:200],
        "firestoreError": "" if firestore_ok else str(firestore_raw)[:200],
    }


def _stop_gcloud_login_process() -> None:
    global _AUTH_PROCESS
    if _AUTH_PROCESS is not None and _AUTH_PROCESS.poll() is None:
        if os.name == "nt":
            subprocess.run(
                ["taskkill.exe", "/PID", str(_AUTH_PROCESS.pid), "/T", "/F"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        else:
            _AUTH_PROCESS.terminate()
            try:
                _AUTH_PROCESS.wait(timeout=3)
            except subprocess.TimeoutExpired:
                _AUTH_PROCESS.kill()
    _AUTH_PROCESS = None


def _clear_cloud_caches() -> None:
    gcp_cli.forget_access_token()
    for lock, cache in (
        (sync_history._SYNC_TARGET_CACHE_LOCK, sync_history._SYNC_TARGET_CACHE),
        (cloud_registry._MCP_REGISTRY_READ_CACHE_LOCK, cloud_registry._MCP_REGISTRY_READ_CACHE),
        (cloud_registry._UNIFIED_MCP_SERVICE_CACHE_LOCK, cloud_registry._UNIFIED_MCP_SERVICE_CACHE),
    ):
        with lock:
            cache.clear()


def _start_gcloud_login() -> bool:
    global _AUTH_PROCESS
    _stop_gcloud_login_process()
    gcloud = gcp_cli.gcloud_executable()
    if not gcloud:
        raise FileNotFoundError("gcloud를 찾을 수 없습니다.")
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    _AUTH_PROCESS = subprocess.Popen(
        [gcloud, "auth", "login", "--quiet"],
        cwd=core.ROOT,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        shell=False,
        creationflags=flags,
        start_new_session=os.name != "nt",
    )
    return True


def _set_gcloud_install(**fields: Any) -> None:
    with _GCLOUD_INSTALL_LOCK:
        _GCLOUD_INSTALL.update(fields)


def _gcloud_install_view() -> dict[str, Any]:
    with _GCLOUD_INSTALL_LOCK:
        view = copy.deepcopy(_GCLOUD_INSTALL)
    view["installed"] = bool(gcp_cli.gcloud_executable())
    if view["installed"] and view["status"] in {"RUNNING", "INSTALLER_OPEN"}:
        view["status"] = "DONE"
    return view


def _installer_signed_by_google(path: Path) -> bool:
    script = (
        "$s = Get-AuthenticodeSignature -LiteralPath $env:RAG_INSTALLER;"
        "if ($s.Status -eq 'Valid' -and $s.SignerCertificate.Subject -match 'O=Google LLC') { 'OK' }"
    )
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, timeout=60, check=False, shell=False,
            env={**os.environ, "RAG_INSTALLER": str(path)},
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.stdout.strip() == "OK"


def _run_gcloud_install() -> None:
    flags = subprocess.CREATE_NO_WINDOW
    winget = shutil.which("winget.exe") or shutil.which("winget")
    if winget:
        _set_gcloud_install(method="winget", message="winget으로 Google Cloud CLI를 설치하고 있습니다.")
        try:
            subprocess.run(
                [winget, "install", "--id", "Google.CloudSDK", "--exact", "--silent",
                 "--accept-package-agreements", "--accept-source-agreements", "--disable-interactivity"],
                cwd=core.ROOT, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=20 * 60, check=False, shell=False, creationflags=flags,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
        if gcp_cli.gcloud_executable():
            _set_gcloud_install(status="DONE", message="설치가 끝났습니다.")
            return

    _set_gcloud_install(method="installer", message="Google 공식 설치 프로그램을 내려받고 있습니다.")
    target = Path(tempfile.mkdtemp(prefix="gcloud-installer-")) / "GoogleCloudSDKInstaller.exe"
    try:
        with urllib.request.urlopen(_GCLOUD_INSTALLER_URL, timeout=60) as response, target.open("wb") as out:
            shutil.copyfileobj(response, out)
    except (OSError, urllib.error.URLError) as exc:
        _set_gcloud_install(status="FAILED", message=f"설치 파일을 내려받지 못했습니다: {str(exc)[:200]}")
        return
    if not _installer_signed_by_google(target):
        target.unlink(missing_ok=True)
        _set_gcloud_install(status="FAILED", message="설치 파일 서명을 확인하지 못해 실행하지 않았습니다.")
        return
    try:
        subprocess.Popen([str(target)], cwd=target.parent, shell=False)
    except OSError as exc:
        _set_gcloud_install(status="FAILED", message=f"설치 프로그램을 열지 못했습니다: {str(exc)[:200]}")
        return
    _set_gcloud_install(status="INSTALLER_OPEN", message="설치 프로그램 창에서 설치를 마쳐 주세요. 끝나면 자동으로 이어집니다.")


# Resource Manager REST. gcloud CLI 는 기동만 1.4초라 대화형 검색에 못 쓴다
# (실측: CLI 6.3s / REST 0.35s, 접근 가능 프로젝트 2,676개 기준).
_CRM_PROJECTS = "https://cloudresourcemanager.googleapis.com/v1/projects"


PROJECT_SEARCH_LIMIT = 20


def _crm_projects(filter_expr: str, limit: int, token: str) -> list[dict[str, str]]:
    params = {"pageSize": max(1, min(limit, 50))}
    if filter_expr:
        params["filter"] = filter_expr
    status, body, _ = gcp_cli.http_json(f"{_CRM_PROJECTS}?{urlencode(params)}", token, timeout=20)
    if status != 200 or not isinstance(body, dict):
        return []
    found: list[dict[str, str]] = []
    for item in body.get("projects") or []:
        project_id = str(item.get("projectId") or "")
        if project_id and str(item.get("lifecycleState") or "ACTIVE") == "ACTIVE":
            found.append({"id": project_id, "name": str(item.get("name") or project_id)})
    return found


def search_projects(term: str, limit: int = PROJECT_SEARCH_LIMIT) -> dict[str, Any]:
    term = str(term or "").strip().lower()[:64]
    try:
        token = gcp_cli.access_token()
    except RuntimeError as exc:
        return {"projects": [], "term": term, "error": str(exc)[:200]}

    if not term:
        return {"projects": [], "term": "", "error": ""}

    escaped = term.replace("*", "")
    with ThreadPoolExecutor(max_workers=2) as pool:
        by_id = pool.submit(_crm_projects, f"id:*{escaped}*", limit, token)
        by_name = pool.submit(_crm_projects, f"name:*{escaped}*", limit, token)
        merged = by_id.result() + by_name.result()

    seen: set[str] = set()
    projects: list[dict[str, str]] = []
    for item in merged:
        if item["id"] in seen:
            continue
        seen.add(item["id"])
        projects.append(item)
    projects.sort(key=lambda item: (not item["id"].startswith(escaped), item["id"]))
    return {"projects": projects[:limit], "term": term, "error": ""}


def _project_accessible(project_id: str) -> bool:
    if not core.PROJECT_RE.fullmatch(project_id or ""):
        return False
    try:
        token = gcp_cli.access_token()
    except RuntimeError:
        return False
    status, body, _ = gcp_cli.http_json(f"{_CRM_PROJECTS}/{quote(project_id, safe='')}", token, timeout=20)
    if status != 200 or not isinstance(body, dict):
        return False
    return str(body.get("lifecycleState") or "ACTIVE") == "ACTIVE"


@router.get("/api/v1/environment")
def environment() -> dict[str, Any]:
    common_path = core.CONFIG_DIR / "common.yaml"
    common_exists = common_path.exists()
    common_valid = False
    common_error = ""
    common = common_config.resolve({})
    if common_exists:
        try:
            common = core._common()
            common_valid = bool(common.get("GCP_PROJECT_ID"))
            if not common_valid:
                common_error = "GCP_PROJECT_ID가 없습니다."
        except (OSError, TypeError, UnicodeError, yaml.YAMLError) as exc:
            common_error = str(exc)[:200]
    configured_project = str(common.get("GCP_PROJECT_ID") or "")
    department_count = 0
    if common_valid:
        try:
            department_count = len(cloud_registry._cloud_department_configs())
        except (OSError, RuntimeError, TypeError, ValueError):
            department_count = 0
    with ThreadPoolExecutor(max_workers=2) as pool:
        bootstrap_future = pool.submit(
            _gcloud_bootstrap_state, include_projects=not common_exists
        )
        sa_future = (
            pool.submit(_default_compute_service_account, configured_project)
            if configured_project
            else None
        )
        bootstrap = bootstrap_future.result()
        service_account = (
            sa_future.result() if sa_future and bootstrap["authenticated"] else ""
        )
    return {
        "repository": str(core.ROOT),
        "pythonVersion": sys.version.split()[0],
        "gcloudInstalled": bootstrap["installed"],
        "gcloudAuthenticated": bootstrap["authenticated"],
        "gcloudAccount": bootstrap["account"],
        "configuredProject": configured_project,
        "serviceAccount": service_account,
        "gcloudProject": bootstrap["currentProject"],
        "gcloudProjects": bootstrap["projects"],
        "availableRegions": bootstrap["regions"],
        "region": str(common["GCP_REGION"]),
        "departmentCount": department_count,
        "commonExists": common_exists,
        "commonValid": common_valid,
        "commonError": common_error,
        "docaiConfigured": {
            kind: bool(common.get(keys[1]) and common.get(keys[2]))
            for kind, keys in docai_setup.KINDS.items()
        },
        "unifiedMcp": True,
    }


@router.get("/api/v1/projects/search")
def project_search(q: str = "", limit: int = PROJECT_SEARCH_LIMIT) -> JSONResponse:
    return JSONResponse(search_projects(q, limit))


@router.post("/api/v1/gcloud-auth/login")
def start_gcloud_login(request: Request) -> JSONResponse:
    core._require_local_session(request)
    bootstrap = _gcloud_bootstrap_state(include_projects=False)
    if not bootstrap["installed"]:
        return JSONResponse(
            {"error": {"code": "GCLOUD_NOT_FOUND", "message": "gcloud를 찾을 수 없습니다."}},
            status_code=404,
        )
    if bootstrap["authenticated"]:
        return JSONResponse({"started": False, "authenticated": True})
    try:
        started = _start_gcloud_login()
    except (OSError, ValueError) as exc:
        return JSONResponse(
            {"error": {"code": "LOGIN_START_FAILED", "message": str(exc)[:300]}},
            status_code=500,
        )
    return JSONResponse(
        {"started": started, "authenticated": False, "message": "브라우저 로그인을 기다리는 중"},
        status_code=202,
    )


@router.post("/api/v1/gcloud-auth/logout")
def gcloud_logout(request: Request) -> JSONResponse:
    core._require_local_session(request)
    gcloud = gcp_cli.gcloud_executable()
    if not gcloud:
        return JSONResponse(
            {"error": {"code": "GCLOUD_NOT_FOUND", "message": "gcloud를 찾을 수 없습니다."}},
            status_code=404,
        )
    _stop_gcloud_login_process()
    ok, output = gcp_cli.run_command([gcloud, "auth", "revoke", "--all", "--quiet"], timeout=30)
    _clear_cloud_caches()
    if not ok and _gcloud_bootstrap_state(include_projects=False)["authenticated"]:
        return JSONResponse(
            {"error": {"code": "LOGOUT_FAILED", "message": output.strip()[:300] or "로그아웃하지 못했습니다."}},
            status_code=500,
        )
    return JSONResponse({"authenticated": False})


@router.get("/api/v1/gcloud-install")
def gcloud_install_status() -> dict[str, Any]:
    return _gcloud_install_view()


@router.post("/api/v1/gcloud-install")
def start_gcloud_install(request: Request) -> JSONResponse:
    core._require_local_session(request)
    if gcp_cli.gcloud_executable():
        return JSONResponse(_gcloud_install_view())
    if os.name != "nt":
        return JSONResponse(
            {"error": {"code": "GCLOUD_INSTALL_UNSUPPORTED",
                       "message": "자동 설치는 Windows에서만 지원합니다. https://cloud.google.com/sdk/docs/install 을 따라 설치해 주세요."}},
            status_code=501,
        )
    with _GCLOUD_INSTALL_LOCK:
        if _GCLOUD_INSTALL.get("status") in {"RUNNING", "INSTALLER_OPEN"}:
            return JSONResponse(copy.deepcopy({**_GCLOUD_INSTALL, "installed": False}), status_code=202)
        _GCLOUD_INSTALL.clear()
        _GCLOUD_INSTALL.update(status="RUNNING", method="", message="설치를 준비하고 있습니다.",
                               startedAt=datetime.now(UTC).isoformat())
    threading.Thread(target=_run_gcloud_install, name="gcloud-install", daemon=True).start()
    return JSONResponse(_gcloud_install_view(), status_code=202)


@router.post("/api/v1/common-config")
async def create_common(request: Request) -> JSONResponse:
    core._require_local_session(request)
    payload = await request.json()
    if not isinstance(payload, dict) or dept_forms._has_secret_input(payload):
        return JSONResponse(
            {"error": {"code": "INVALID_COMMON_CONFIG", "message": "요청이 올바르지 않습니다."}},
            status_code=400,
        )
    bootstrap = _gcloud_bootstrap_state()
    if not bootstrap["installed"] or not bootstrap["authenticated"]:
        return JSONResponse(
            {
                "error": {
                    "code": "GCLOUD_AUTH_REQUIRED",
                    "message": "gcloud auth login 후 로그인 상태를 다시 확인해 주세요.",
                }
            },
            status_code=412,
        )
    candidate, result = validate_common_candidate(payload)
    if not _project_accessible(candidate["GCP_PROJECT_ID"]):
        core._field_error(
            result["fieldErrors"],
            "projectId",
            "현재 gcloud 계정으로 접근 가능한 프로젝트를 선택해 주세요.",
        )
        result["valid"] = False
    if result["valid"]:
        resources = _gcloud_project_resources(
            candidate["GCP_PROJECT_ID"], candidate["GCP_REGION"]
        )
        artifact_ids = {item["id"] for item in resources["artifactRepositories"]}
        database_ids = {item["id"] for item in resources["firestoreDatabases"]}
        if candidate["ARTIFACT_REPO"] not in artifact_ids:
            core._field_error(
                result["fieldErrors"],
                "artifactRepo",
                "선택한 프로젝트·리전에 존재하는 Docker 저장소를 선택해 주세요.",
            )
            result["valid"] = False
        if candidate["FIRESTORE_DATABASE"] not in database_ids:
            core._field_error(
                result["fieldErrors"],
                "firestoreDatabase",
                "선택한 프로젝트에 존재하는 Native Firestore DB를 선택해 주세요.",
            )
            result["valid"] = False
    if not result["valid"]:
        return JSONResponse(
            {
                "error": {
                    "code": "VALIDATION_FAILED",
                    "message": "공통 설정 입력값을 확인해 주세요.",
                    "fieldErrors": result["fieldErrors"],
                }
            },
            status_code=422,
        )
    try:
        for kind, selection in payload.get("docai", {}).items():
            docai_routes._DOCAI_SETUP.verify(candidate["GCP_PROJECT_ID"], kind, selection)
    except (ValueError, RuntimeError) as exc:
        return JSONResponse({"error": {"message": str(exc)[:400]}}, status_code=422)
    try:
        target = create_common_config(candidate)
    except FileExistsError:
        return JSONResponse(
            {
                "error": {
                    "code": "FILE_EXISTS",
                    "message": "common.yaml이 이미 있어 덮어쓰지 않았습니다.",
                }
            },
            status_code=409,
        )
    except (OSError, RuntimeError, TypeError, ValueError, yaml.YAMLError) as exc:
        return JSONResponse(
            {"error": {"code": "WRITE_FAILED", "message": str(exc)[:300]}}, status_code=500
        )
    return JSONResponse(
        {"created": True, "path": f"config/{target.name}", "projectId": candidate["GCP_PROJECT_ID"]},
        status_code=201,
    )
