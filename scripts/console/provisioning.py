from __future__ import annotations

import copy
import re
import secrets
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import yaml
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from scripts import common_config, gcp_cli
from scripts.console import cloud_registry, core, dept_forms, environment_setup, sa_repair
from scripts.run_store import RunStore

router = APIRouter()


_PROVISIONS = RunStore(30 * 60, "resources")


_COMMON_PROVISIONS = RunStore(30 * 60, "resources")


PROVISION_RESOURCE_DEFINITIONS: dict[str, dict[str, str]] = {
    "bucketHwp": {"kind": "bucket", "label": "HWP 원본 버킷"},
    "bucketSource": {"kind": "bucket", "label": "Source 버킷"},
    "corpusStaff": {"kind": "corpus", "label": "교직원 코퍼스"},
    "corpusStudent": {"kind": "corpus", "label": "학생 코퍼스"},
}


COMMON_PROVISION_RESOURCE_DEFINITIONS: dict[str, dict[str, Any]] = {
    "artifactRepo": {
        "kind": "artifactRepo",
        "label": "Artifact Registry (Docker)",
        "service": "artifactregistry.googleapis.com",
        "defaultName": common_config.defaults()["ARTIFACT_REPO"],
    },
    "firestoreDatabase": {
        "kind": "firestoreDatabase",
        "label": "Firestore (Native)",
        "service": "firestore.googleapis.com",
        "defaultName": common_config.defaults()["FIRESTORE_DATABASE"],
    },
}


# Firestore 는 위치를 나중에 못 바꾼다. 만들기 전에 화면에서 반드시 알린다.
COMMON_PROVISION_WARNINGS: dict[str, str] = {
    "firestoreDatabase": "생성 후 위치를 변경할 수 없습니다. 리전을 확인해 주세요.",
}


COMMON_REQUIRED_SERVICES: tuple[dict[str, str], ...] = (
    {"name": "compute.googleapis.com", "label": "Compute Engine API"},
    {"name": "iamcredentials.googleapis.com", "label": "IAM Service Account Credentials API"},
    {"name": "run.googleapis.com", "label": "Cloud Run Admin API"},
    {"name": "artifactregistry.googleapis.com", "label": "Artifact Registry API"},
    {"name": "cloudbuild.googleapis.com", "label": "Cloud Build API"},
    {"name": "aiplatform.googleapis.com", "label": "Vertex AI API"},
    {"name": "documentai.googleapis.com", "label": "Document AI API"},
    {"name": "storage.googleapis.com", "label": "Cloud Storage API"},
    {"name": "firestore.googleapis.com", "label": "Cloud Firestore API"},
    {"name": "workflows.googleapis.com", "label": "Workflows API"},
    {"name": "workflowexecutions.googleapis.com", "label": "Workflow Executions API"},
    {"name": "cloudscheduler.googleapis.com", "label": "Cloud Scheduler API"},
    {"name": "appengine.googleapis.com", "label": "App Engine Admin API"},
    {"name": "drive.googleapis.com", "label": "Google Drive API"},
    {"name": "secretmanager.googleapis.com", "label": "Secret Manager API"},
)


ARTIFACT_REPO_RE = re.compile(r"[a-z][a-z0-9._-]{1,62}")


FIRESTORE_DB_RE = re.compile(r"[a-z][a-z0-9-]{1,61}[a-z0-9]")


RAG_EMBEDDING_MODEL = "text-multilingual-embedding-002"


def _department_resource_options(common: dict[str, Any]) -> dict[str, Any]:
    project = str(common.get("GCP_PROJECT_ID") or "")
    region = str(common["GCP_REGION"])
    gcloud = gcp_cli.gcloud_executable()
    if not gcloud:
        return {"corpora": [], "buckets": [], "error": "gcloud를 찾을 수 없습니다."}
    token_ok, token = gcp_cli.run_command([gcloud, "auth", "print-access-token", "--quiet"])
    if not token_ok:
        return {"corpora": [], "buckets": [], "error": "gcloud 로그인이 필요합니다."}

    corpus_url = (
        f"https://{region}-aiplatform.googleapis.com/v1/projects/{project}/locations/"
        f"{region}/ragCorpora?pageSize=100"
    )
    corpus_status, corpus_body, _ = gcp_cli.http_json(corpus_url, token, timeout=20)
    corpora = []
    if corpus_status == 200:
        corpora = sorted(
            [
                {
                    "name": str(item.get("name") or ""),
                    "displayName": str(item.get("displayName") or "이름 없음"),
                    "description": str(item.get("description") or ""),
                }
                for item in corpus_body.get("ragCorpora") or []
                if item.get("name")
            ],
            key=lambda item: (item["displayName"].lower(), item["name"]),
        )

    buckets_ok, buckets_raw = gcp_cli.gcloud_json(
        ["storage", "buckets", "list", f"--project={project}"], timeout=20
    )
    buckets = []
    if buckets_ok and isinstance(buckets_raw, list):
        for item in buckets_raw:
            name = str(item.get("name") or "")
            location = str(item.get("location") or "").lower()
            uniform = bool(item.get("uniform_bucket_level_access"))
            pap = str(item.get("public_access_prevention") or "").lower()
            if name and location == region.lower() and uniform and pap == "enforced":
                buckets.append({"name": name, "location": location})
        buckets.sort(key=lambda item: item["name"])

    errors: list[str] = []
    if corpus_status != 200:
        errors.append(f"RAG 코퍼스 조회 HTTP {corpus_status or 'timeout'}")
    if not buckets_ok:
        errors.append("GCS 버킷 조회 실패")
    return {"corpora": corpora, "buckets": buckets, "error": " · ".join(errors)}


def _department_bucket_usage() -> dict[str, list[str]]:
    usage: dict[str, set[str]] = {}
    for code, config in cloud_registry._cloud_department_configs().items():
        for bucket in (config.get("buckets") or {}).values():
            name = str(bucket or "").removeprefix("gs://").strip()
            if name:
                usage.setdefault(name, set()).add(code)
    return {name: sorted(codes) for name, codes in usage.items()}


def _provision_editing_code(payload: dict[str, Any], code: str) -> str:
    editing_code = str(payload.get("editingCode") or "").strip().lower()
    if not editing_code:
        return ""
    if editing_code != code or editing_code not in cloud_registry._cloud_department_configs():
        raise ValueError("수정 중인 학과 설정을 다시 열어 주세요.")
    return editing_code


def create_resource_plan(payload: dict[str, Any]) -> dict[str, Any]:
    code = str(payload.get("code") or "").strip().lower()
    name = str(payload.get("name") or "").strip()
    editing_code = _provision_editing_code(payload, code)
    availability = dept_forms.department_code_availability(code, current_code=editing_code)
    if not availability["available"]:
        raise FileExistsError(availability["reason"])
    if not name:
        raise ValueError("학과명을 먼저 입력해 주세요.")

    requested = payload.get("resources")
    if not isinstance(requested, list):
        requested = list(PROVISION_RESOURCE_DEFINITIONS)
    resource_keys = list(dict.fromkeys(str(item) for item in requested))
    if not resource_keys or any(item not in PROVISION_RESOURCE_DEFINITIONS for item in resource_keys):
        raise ValueError("생성할 리소스를 올바르게 선택해 주세요.")
    corpus_mode = str(payload.get("corpusMode") or "split").strip().lower()
    if corpus_mode not in {"single", "split"}:
        raise ValueError("단일 코퍼스 또는 학생 분리를 선택해 주세요.")
    if corpus_mode == "single" and "corpusStudent" in resource_keys:
        raise ValueError("단일 코퍼스 모드에서는 학생 코퍼스를 생성하지 않습니다.")

    common = core._common()
    project = str(common.get("GCP_PROJECT_ID") or "").strip()
    region = str(common["GCP_REGION"]).strip()
    if not core.PROJECT_RE.fullmatch(project) or not core.REGION_RE.fullmatch(region):
        raise ValueError("공통 프로젝트·리전 설정을 먼저 확인해 주세요.")

    suffix = secrets.token_hex(4)
    bucket_names = {
        "bucketHwp": f"{code}-rag-hwp-{suffix}",
        "bucketSource": f"{code}-rag-source-{suffix}",
    }
    corpus_display_names = {
        "corpusStaff": f"{code}-rag-corpus" if corpus_mode == "single" else f"{code}-rag-corpus-staff",
        "corpusStudent": f"{code}-rag-corpus-student",
    }
    resources: list[dict[str, Any]] = []
    for key in resource_keys:
        definition = PROVISION_RESOURCE_DEFINITIONS[key]
        label = "기본 코퍼스" if corpus_mode == "single" and key == "corpusStaff" else definition["label"]
        resources.append(
            {
                "key": key,
                "kind": definition["kind"],
                "label": label,
                "displayName": bucket_names.get(key) or corpus_display_names.get(key) or "",
                "value": bucket_names.get(key, ""),
            }
        )

    plan_id = uuid.uuid4().hex
    plan = {
        "planId": plan_id,
        "code": code,
        "name": name,
        "editingCode": editing_code,
        "corpusMode": corpus_mode,
        "projectId": project,
        "region": region,
        "resources": resources,
        "bucketProtection": {
            "uniformBucketLevelAccess": True,
            "publicAccessPrevention": "enforced",
            "softDeleteDays": 7,
        },
        "corpusConfig": {
            "embeddingModel": RAG_EMBEDDING_MODEL,
            "vectorDatabase": "RAG Managed DB",
        },
        "createdEpoch": time.time(),
        "started": False,
    }
    with _PROVISIONS.lock:
        _PROVISIONS.cleanup()
        _PROVISIONS.plans[plan_id] = plan
    return copy.deepcopy(plan)


def _apply_resource_plan_overrides(
    plan: dict[str, Any], overrides: dict[str, Any] | None
) -> dict[str, Any]:
    if overrides is None:
        return plan
    if not isinstance(overrides, dict):
        raise TypeError("수정한 리소스 이름이 올바르지 않습니다.")
    resources = {item["key"]: item for item in plan["resources"]}
    unknown = sorted(set(overrides) - set(resources))
    if unknown:
        raise ValueError("생성 계획에 없는 리소스는 수정할 수 없습니다.")
    for key, raw_value in overrides.items():
        value = str(raw_value or "").strip()
        resource = resources[key]
        if resource["kind"] == "bucket":
            value = value.removeprefix("gs://").strip()
            if not core.BUCKET_RE.fullmatch(value):
                raise ValueError(f"{resource['label']} 이름이 GCS 버킷 규칙에 맞지 않습니다.")
            resource["value"] = value
            resource["displayName"] = value
        else:
            if not value or len(value) > 128:
                raise ValueError(f"{resource['label']} 이름은 1~128자여야 합니다.")
            resource["displayName"] = value

    bucket_values = [item["value"] for item in resources.values() if item["kind"] == "bucket"]
    if len(bucket_values) != len(set(bucket_values)):
        raise ValueError("HWP 원본 버킷과 Source 버킷 이름은 서로 달라야 합니다.")
    corpus_values = [
        item["displayName"] for item in resources.values() if item["kind"] == "corpus"
    ]
    if len(corpus_values) != len(set(corpus_values)):
        raise ValueError("교직원 코퍼스와 학생 코퍼스 이름은 서로 달라야 합니다.")
    return plan


def _create_bucket_resource(name: str, project: str, region: str) -> str:
    gcloud = gcp_cli.gcloud_executable()
    if not gcloud:
        raise RuntimeError("gcloud를 찾을 수 없습니다.")
    ok, output = gcp_cli.run_command(
        [
            gcloud,
            "storage",
            "buckets",
            "create",
            f"gs://{name}",
            f"--project={project}",
            f"--location={region}",
            "--default-storage-class=STANDARD",
            "--uniform-bucket-level-access",
            "--public-access-prevention",
            "--soft-delete-duration=7d",
            "--quiet",
        ],
        timeout=120,
    )
    if not ok:
        raise RuntimeError((output or "GCS 버킷 생성에 실패했습니다.")[-400:])
    described, metadata = gcp_cli.gcloud_json(
        ["storage", "buckets", "describe", f"gs://{name}"], timeout=30
    )
    if not described or not isinstance(metadata, dict):
        raise RuntimeError("버킷은 생성됐지만 보호 설정을 다시 확인하지 못했습니다.")
    location = str(metadata.get("location") or "").lower()
    uniform = bool(metadata.get("uniform_bucket_level_access"))
    pap = str(metadata.get("public_access_prevention") or "").lower()
    if location != region.lower() or not uniform or pap != "enforced":
        raise RuntimeError("버킷은 생성됐지만 필수 리전·접근 보호 설정이 일치하지 않습니다.")
    return name


def _create_corpus_resource(display_name: str, project: str, region: str, token: str) -> str:
    url = (
        f"https://{region}-aiplatform.googleapis.com/v1/projects/{project}/locations/"
        f"{region}/ragCorpora"
    )
    status, body, _ = gcp_cli.http_post_json(
        url,
        {
            "displayName": display_name,
            "description": "학과 관리 콘솔에서 생성한 RAG 코퍼스",
            "vectorDbConfig": {
                "ragEmbeddingModelConfig": {
                    "vertexPredictionEndpoint": {
                        "endpoint": (
                            f"projects/{project}/locations/{region}/publishers/google/models/"
                            f"{RAG_EMBEDDING_MODEL}"
                        )
                    }
                },
                "ragManagedDb": {},
            },
        },
        token,
        timeout=30,
    )
    if status not in {200, 201, 202} or not isinstance(body, dict):
        message = str((body.get("error") or {}).get("message") or "") if isinstance(body, dict) else ""
        detail = f": {message[:300]}" if message else ""
        raise RuntimeError(f"RAG 코퍼스 생성 요청 실패 (HTTP {status or 'timeout'}){detail}")

    if body.get("done"):
        response_name = str((body.get("response") or {}).get("name") or "")
        if response_name:
            return response_name
    operation_name = str(body.get("name") or "")
    if operation_name.startswith("projects/") and "/ragCorpora/" in operation_name:
        return operation_name
    if not operation_name:
        raise RuntimeError("RAG 코퍼스 생성 작업 ID를 받지 못했습니다.")

    operation_url = f"https://{region}-aiplatform.googleapis.com/v1/{operation_name}"
    deadline = time.time() + 600
    while time.time() < deadline:
        poll_status, operation, _ = gcp_cli.http_json(operation_url, token, timeout=20)
        if poll_status == 200 and isinstance(operation, dict):
            if operation.get("done"):
                if operation.get("error"):
                    message = str((operation.get("error") or {}).get("message") or "")
                    raise RuntimeError(message[:400] or "RAG 코퍼스 생성 작업이 실패했습니다.")
                response_name = str((operation.get("response") or {}).get("name") or "")
                if response_name:
                    return response_name
                break
        elif poll_status not in {0, 429, 500, 502, 503, 504}:
            raise RuntimeError(f"RAG 코퍼스 생성 상태 조회 실패 (HTTP {poll_status})")
        time.sleep(2)

    options = _department_resource_options({"GCP_PROJECT_ID": project, "GCP_REGION": region})
    matches = [
        item for item in options.get("corpora", []) if item.get("displayName") == display_name
    ]
    if len(matches) == 1:
        return str(matches[0]["name"])
    raise RuntimeError("코퍼스 생성 작업이 오래 걸리고 있습니다. 리소스를 새로 만들기 전에 목록을 확인해 주세요.")


def _gcloud_enabled_services(project: str) -> tuple[bool, set[str]]:
    ok, raw = gcp_cli.gcloud_json(
        ["services", "list", "--enabled", f"--project={project}"], timeout=30
    )
    if not ok or not isinstance(raw, list):
        return False, set()
    names: set[str] = set()
    for item in raw:
        if isinstance(item, dict):
            name = str(item.get("config", {}).get("name") or item.get("name") or "")
            names.add(name.rsplit("/services/", 1)[-1] if "/services/" in name else name)
    return True, {name for name in names if name}


def create_common_resource_plan(payload: dict[str, Any]) -> dict[str, Any]:
    project = str(payload.get("projectId") or "").strip()
    region = str(payload.get("region") or "").strip()
    if not core.PROJECT_RE.fullmatch(project):
        raise ValueError("GCP 프로젝트를 먼저 선택해 주세요.")
    if not core.REGION_RE.fullmatch(region):
        raise ValueError("리전을 먼저 선택해 주세요.")

    names = {
        "artifactRepo": str(payload.get("artifactRepo") or "").strip()
        or COMMON_PROVISION_RESOURCE_DEFINITIONS["artifactRepo"]["defaultName"],
        "firestoreDatabase": str(payload.get("firestoreDatabase") or "").strip()
        or COMMON_PROVISION_RESOURCE_DEFINITIONS["firestoreDatabase"]["defaultName"],
    }
    if not ARTIFACT_REPO_RE.fullmatch(names["artifactRepo"]):
        raise ValueError("유효한 Artifact Registry 저장소 이름이 필요합니다.")
    if not FIRESTORE_DB_RE.fullmatch(names["firestoreDatabase"]):
        raise ValueError("유효한 Firestore 데이터베이스 ID가 필요합니다.")
    if names["firestoreDatabase"] == "(default)":
        raise ValueError("(default) 데이터베이스는 사용하지 않습니다. 다른 ID를 입력해 주세요.")

    # 리소스와 API는 동시에 읽는다. 서비스 계정 조회까지 같은 순간에 여러 gcloud
    # 프로세스로 실행하면 Windows에서 프로젝트 번호 조회가 간헐적으로 실패하므로
    # 정책/연결 확인은 둘이 끝난 뒤 안정적으로 실행한다.
    with ThreadPoolExecutor(max_workers=2) as pool:
        resources_future = pool.submit(environment_setup._gcloud_project_resources, project, region)
        services_future = pool.submit(_gcloud_enabled_services, project)
        existing = resources_future.result()
        services_ok, enabled = services_future.result()
    service_account_status = sa_repair.drive_service_account_status(project)
    existing_ids = {
        "artifactRepo": {item["id"] for item in existing["artifactRepositories"]},
        "firestoreDatabase": {item["id"] for item in existing["firestoreDatabases"]},
    }

    resources: list[dict[str, Any]] = []
    for key, definition in COMMON_PROVISION_RESOURCE_DEFINITIONS.items():
        already = names[key] in existing_ids[key]
        resources.append(
            {
                "key": key,
                "kind": definition["kind"],
                "label": definition["label"],
                "displayName": names[key],
                "value": names[key] if already else "",
                "exists": already,
                "warning": COMMON_PROVISION_WARNINGS.get(key, ""),
            }
        )

    services = [
        {
            "name": definition["name"],
            "label": definition["label"],
            "enabled": bool(services_ok and definition["name"] in enabled),
            "known": services_ok,
        }
        for definition in COMMON_REQUIRED_SERVICES
    ]

    plan_id = uuid.uuid4().hex
    plan = {
        "planId": plan_id,
        "projectId": project,
        "region": region,
        "resources": resources,
        "services": services,
        "serviceAccount": {
            "key": "driveImpersonation",
            "label": "Drive 서비스 계정 사용 권한",
            "role": "roles/iam.serviceAccountTokenCreator",
            "serviceAccount": str(service_account_status.get("serviceAccount") or ""),
            "account": str(service_account_status.get("account") or ""),
            "ready": service_account_status.get("status") == "OK",
            "issues": list(service_account_status.get("issues") or []),
            "detail": str(service_account_status.get("detail") or "상태를 확인하지 못했습니다."),
        },
        "createdEpoch": time.time(),
        "started": False,
    }
    with _COMMON_PROVISIONS.lock:
        _COMMON_PROVISIONS.cleanup()
        _COMMON_PROVISIONS.plans[plan_id] = plan
    return copy.deepcopy(plan)


def _enable_gcloud_service(service: str, project: str) -> None:
    gcloud = gcp_cli.gcloud_executable()
    if not gcloud:
        raise RuntimeError("gcloud를 찾을 수 없습니다.")
    ok, output = gcp_cli.run_command(
        [gcloud, "services", "enable", service, f"--project={project}", "--quiet"],
        timeout=180,
    )
    if not ok:
        raise RuntimeError((output or f"{service} 활성화에 실패했습니다.")[-400:])


def _create_artifact_repo_resource(name: str, project: str, region: str) -> str:
    gcloud = gcp_cli.gcloud_executable()
    if not gcloud:
        raise RuntimeError("gcloud를 찾을 수 없습니다.")
    ok, output = gcp_cli.run_command(
        [
            gcloud,
            "artifacts",
            "repositories",
            "create",
            name,
            "--repository-format=docker",
            f"--location={region}",
            f"--project={project}",
            "--quiet",
        ],
        timeout=180,
    )
    if not ok:
        raise RuntimeError((output or "Artifact Registry 저장소 생성에 실패했습니다.")[-400:])
    described, metadata = gcp_cli.gcloud_json(
        [
            "artifacts",
            "repositories",
            "describe",
            name,
            f"--location={region}",
            f"--project={project}",
        ],
        timeout=30,
    )
    if not described or not isinstance(metadata, dict):
        raise RuntimeError("저장소는 생성됐지만 형식을 다시 확인하지 못했습니다.")
    if str(metadata.get("format") or "").upper() != "DOCKER":
        raise RuntimeError("저장소가 Docker 형식으로 생성되지 않았습니다.")
    return name


def _create_firestore_database_resource(name: str, project: str, region: str) -> str:
    """Native 모드로만 만든다. Datastore 모드로 굳으면 되돌릴 수 없다."""
    gcloud = gcp_cli.gcloud_executable()
    if not gcloud:
        raise RuntimeError("gcloud를 찾을 수 없습니다.")
    ok, output = gcp_cli.run_command(
        [
            gcloud,
            "firestore",
            "databases",
            "create",
            f"--database={name}",
            f"--location={region}",
            "--type=firestore-native",
            f"--project={project}",
            "--quiet",
        ],
        timeout=300,
    )
    if not ok:
        raise RuntimeError((output or "Firestore 데이터베이스 생성에 실패했습니다.")[-400:])
    described, metadata = gcp_cli.gcloud_json(
        [
            "firestore",
            "databases",
            "describe",
            f"--database={name}",
            f"--project={project}",
        ],
        timeout=30,
    )
    if not described or not isinstance(metadata, dict):
        raise RuntimeError("데이터베이스는 생성됐지만 모드를 다시 확인하지 못했습니다.")
    if str(metadata.get("type") or "") != "FIRESTORE_NATIVE":
        raise RuntimeError("데이터베이스가 Native 모드로 생성되지 않았습니다.")
    return name


def _execute_provision_run(run_id: str) -> None:
    with _PROVISIONS.lock:
        run = copy.deepcopy(_PROVISIONS.runs[run_id])
    token = ""
    try:
        token = gcp_cli.access_token()
    except RuntimeError as exc:
        with _PROVISIONS.lock:
            current = _PROVISIONS.runs[run_id]
            for item in current["resources"]:
                item.update(status="FAILED", detail=str(exc))
            current.update(status="FAILED", finishedEpoch=time.time())
        return

    for resource in run["resources"]:
        key = resource["key"]
        _PROVISIONS.update_item(run_id, key, status="RUNNING", detail="생성 요청 중")
        try:
            if resource["kind"] == "bucket":
                value = _create_bucket_resource(
                    resource["value"], run["projectId"], run["region"]
                )
            else:
                value = _create_corpus_resource(
                    resource["displayName"], run["projectId"], run["region"], token
                )
            _PROVISIONS.update_item(
                run_id,
                key,
                status="COMPLETE",
                value=value,
                detail="생성 및 연결 확인 완료",
            )
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            _PROVISIONS.update_item(run_id, key, status="FAILED", detail=str(exc)[:400])

    with _PROVISIONS.lock:
        current = _PROVISIONS.runs[run_id]
        statuses = [item["status"] for item in current["resources"]]
        if all(status == "COMPLETE" for status in statuses):
            current["status"] = "COMPLETED"
        elif any(status == "COMPLETE" for status in statuses):
            current["status"] = "PARTIAL"
        else:
            current["status"] = "FAILED"
        current["finishedEpoch"] = time.time()


def start_provision_run(
    plan_id: str, overrides: dict[str, Any] | None = None
) -> dict[str, Any]:
    with _PROVISIONS.lock:
        _PROVISIONS.cleanup()
        plan = _PROVISIONS.plans.get(plan_id)
        if not plan:
            raise FileNotFoundError(plan_id)
        if plan["started"]:
            raise FileExistsError("이미 실행한 생성 계획입니다.")
        plan_for_run = _apply_resource_plan_overrides(copy.deepcopy(plan), overrides)
        availability = dept_forms.department_code_availability(
            plan["code"], current_code=plan.get("editingCode", "")
        )
        if not availability["available"]:
            raise FileExistsError(availability["reason"])
        for active in _PROVISIONS.runs.values():
            if active["code"] == plan["code"] and active["status"] == "RUNNING":
                raise FileExistsError("이 학과의 리소스 생성이 이미 진행 중입니다.")

        run_id = uuid.uuid4().hex
        resources = copy.deepcopy(plan_for_run["resources"])
        for item in resources:
            item.update(status="PENDING", detail="대기 중")
        run = {
            "runId": run_id,
            "planId": plan_id,
            "code": plan_for_run["code"],
            "projectId": plan_for_run["projectId"],
            "region": plan_for_run["region"],
            "bucketProtection": copy.deepcopy(plan_for_run["bucketProtection"]),
            "corpusConfig": copy.deepcopy(plan_for_run["corpusConfig"]),
            "status": "RUNNING",
            "resources": resources,
            "createdEpoch": time.time(),
        }
        plan["started"] = True
        _PROVISIONS.runs[run_id] = run

    threading.Thread(
        target=_execute_provision_run,
        args=(run_id,),
        name=f"resource-provision-{run_id[:8]}",
        daemon=True,
    ).start()
    return copy.deepcopy(run)


def _set_common_provision_service(run_id: str, name: str, **changes: Any) -> None:
    with _COMMON_PROVISIONS.lock:
        run = _COMMON_PROVISIONS.runs.get(run_id)
        if not run:
            return
        for item in run["services"]:
            if item["name"] == name:
                item.update(changes)
                return


def _set_common_provision_service_account(run_id: str, **changes: Any) -> None:
    with _COMMON_PROVISIONS.lock:
        run = _COMMON_PROVISIONS.runs.get(run_id)
        if run:
            run["serviceAccount"].update(changes)


def _prepare_common_service_account(
    run_id: str, project: str, failed_services: set[str]
) -> None:
    dependencies = {sa_repair.COMPUTE_SERVICE, sa_repair.IAM_CREDENTIALS_SERVICE}
    failed_dependencies = sorted(dependencies & failed_services)
    if failed_dependencies:
        _set_common_provision_service_account(
            run_id,
            status="FAILED",
            detail=f"{', '.join(failed_dependencies)} 활성화 실패로 확인하지 못했습니다.",
        )
        return

    _set_common_provision_service_account(
        run_id, status="RUNNING", detail="서비스 계정 연결 상태를 확인하는 중"
    )
    status = sa_repair.drive_service_account_status(project)
    service_account = str(status.get("serviceAccount") or "")
    account = str(status.get("account") or "")
    _set_common_provision_service_account(
        run_id, serviceAccount=service_account, account=account
    )
    if status.get("status") == "OK":
        _set_common_provision_service_account(
            run_id, status="SKIPPED", detail="서비스 계정 연결 확인됨 · 이미 정상"
        )
        return
    if not service_account or not account:
        _set_common_provision_service_account(
            run_id,
            status="FAILED",
            detail=str(status.get("detail") or "서비스 계정을 확인하지 못했습니다.")[:400],
        )
        return

    member_type = "serviceAccount" if account.endswith(".gserviceaccount.com") else "user"
    _set_common_provision_service_account(
        run_id,
        status="RUNNING",
        detail=f"{account}에 Token Creator 권한을 부여하는 중",
    )
    try:
        sa_repair._grant_service_account_role(
            service_account,
            f"{member_type}:{account}",
            sa_repair.SA_TOKEN_CREATOR_ROLE,
            project,
        )
    except (OSError, RuntimeError) as exc:
        _set_common_provision_service_account(
            run_id, status="FAILED", detail=str(exc)[:400]
        )
        return

    # IAM 정책 반영은 실제로 10초 이상 걸릴 수 있다. 명령 성공 직후의 403을
    # 최종 실패로 오판하지 않도록 약 30초 동안 실제 토큰 발급을 재확인한다.
    verified = status
    verification_attempts = 11
    for attempt in range(1, verification_attempts + 1):
        _set_common_provision_service_account(
            run_id,
            status="RUNNING",
            detail=f"권한 적용 후 서비스 계정 연결 재확인 중 ({attempt}/{verification_attempts})",
        )
        verified = sa_repair.drive_service_account_status(project)
        if verified.get("status") == "OK":
            break
        if attempt < verification_attempts:
            time.sleep(3)
    if verified.get("status") == "OK":
        _set_common_provision_service_account(
            run_id, status="COMPLETE", detail="Token Creator 권한 부여 및 서비스 계정 연결 확인 완료"
        )
    else:
        _set_common_provision_service_account(
            run_id,
            status="FAILED",
            detail=str(verified.get("detail") or "서비스 계정 연결 재확인에 실패했습니다.")[:400],
        )


def _execute_common_provision_run(run_id: str) -> None:
    with _COMMON_PROVISIONS.lock:
        run = copy.deepcopy(_COMMON_PROVISIONS.runs[run_id])
    project = run["projectId"]
    region = run["region"]

    failed_services: set[str] = set()
    for service in run["services"]:
        name = service["name"]
        if service.get("enabled") and service.get("known"):
            _set_common_provision_service(run_id, name, status="SKIPPED", detail="이미 활성화됨")
            continue
        _set_common_provision_service(run_id, name, status="RUNNING", detail="활성화 중")
        try:
            _enable_gcloud_service(name, project)
        except (OSError, RuntimeError) as exc:
            failed_services.add(name)
            _set_common_provision_service(run_id, name, status="FAILED", detail=str(exc)[:400])
        else:
            _set_common_provision_service(run_id, name, status="COMPLETE", detail="활성화 완료")

    _prepare_common_service_account(run_id, project, failed_services)

    for resource in run["resources"]:
        key = resource["key"]
        if resource.get("exists"):
            _COMMON_PROVISIONS.update_item(
                run_id, key, status="SKIPPED", detail="이미 존재해 건너뜀"
            )
            continue
        service = str(COMMON_PROVISION_RESOURCE_DEFINITIONS[key]["service"])
        if service in failed_services:
            _COMMON_PROVISIONS.update_item(
                run_id, key, status="FAILED", detail=f"{service} 활성화 실패로 생성하지 않았습니다."
            )
            continue
        _COMMON_PROVISIONS.update_item(run_id, key, status="RUNNING", detail="생성 요청 중")
        try:
            if resource["kind"] == "artifactRepo":
                value = _create_artifact_repo_resource(resource["displayName"], project, region)
            else:
                value = _create_firestore_database_resource(
                    resource["displayName"], project, region
                )
            _COMMON_PROVISIONS.update_item(
                run_id, key, status="COMPLETE", value=value, detail="생성 및 확인 완료"
            )
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            _COMMON_PROVISIONS.update_item(run_id, key, status="FAILED", detail=str(exc)[:400])

    with _COMMON_PROVISIONS.lock:
        current = _COMMON_PROVISIONS.runs[run_id]
        statuses = [item["status"] for item in current["services"] + current["resources"]]
        statuses.append(current["serviceAccount"]["status"])
        settled = [item for item in statuses if item in {"COMPLETE", "SKIPPED"}]
        if len(settled) == len(statuses):
            current["status"] = "COMPLETED"
        elif settled:
            current["status"] = "PARTIAL"
        else:
            current["status"] = "FAILED"
        current["finishedEpoch"] = time.time()


def start_common_provision_run(plan_id: str) -> dict[str, Any]:
    with _COMMON_PROVISIONS.lock:
        _COMMON_PROVISIONS.cleanup()
        plan = _COMMON_PROVISIONS.plans.get(plan_id)
        if not plan:
            raise FileNotFoundError(plan_id)
        if plan["started"]:
            raise FileExistsError("이미 실행한 생성 계획입니다.")
        for active in _COMMON_PROVISIONS.runs.values():
            if active["status"] == "RUNNING":
                raise FileExistsError("공통 리소스 생성이 이미 진행 중입니다.")

        run_id = uuid.uuid4().hex
        resources = copy.deepcopy(plan["resources"])
        for item in resources:
            if item.get("exists"):
                item.update(status="SKIPPED", detail="이미 존재해 건너뜀")
            else:
                item.update(status="PENDING", detail="생성 대기 중")
        services = copy.deepcopy(plan["services"])
        for item in services:
            item.update(status="PENDING", detail="대기 중")
        service_account = copy.deepcopy(plan["serviceAccount"])
        if service_account.get("ready"):
            service_account.update(status="SKIPPED", detail="서비스 계정 연결 확인됨 · 이미 설정됨")
        else:
            service_account.update(status="PENDING", detail="필수 API 준비 후 권한 확인 및 설정")
        run = {
            "runId": run_id,
            "planId": plan_id,
            "projectId": plan["projectId"],
            "region": plan["region"],
            "status": "RUNNING",
            "resources": resources,
            "services": services,
            "serviceAccount": service_account,
            "createdEpoch": time.time(),
        }
        plan["started"] = True
        _COMMON_PROVISIONS.runs[run_id] = run

    threading.Thread(
        target=_execute_common_provision_run,
        args=(run_id,),
        name=f"common-provision-{run_id[:8]}",
        daemon=True,
    ).start()
    return copy.deepcopy(run)


@router.post("/api/v1/departments/resource-plans")
async def resource_plan(request: Request) -> JSONResponse:
    core._require_local_session(request)
    payload = await request.json()
    if not isinstance(payload, dict) or dept_forms._has_secret_input(payload):
        return JSONResponse(
            {"error": {"code": "INVALID_RESOURCE_PLAN", "message": "생성 계획 요청이 올바르지 않습니다."}},
            status_code=400,
        )
    try:
        plan = create_resource_plan(payload)
    except FileExistsError as exc:
        return JSONResponse(
            {
                "error": {
                    "code": "DEPARTMENT_CODE_EXISTS",
                    "message": str(exc),
                    "fieldErrors": {"code": [str(exc)]},
                }
            },
            status_code=409,
        )
    except (ValueError, TypeError) as exc:
        return JSONResponse(
            {"error": {"code": "INVALID_RESOURCE_PLAN", "message": str(exc)}},
            status_code=422,
        )
    except (FileNotFoundError, OSError, UnicodeError, yaml.YAMLError):
        return JSONResponse(
            {"error": {"code": "COMMON_REQUIRED", "message": "공통 설정이 먼저 필요합니다."}},
            status_code=428,
        )
    return JSONResponse(plan, status_code=201)


@router.post("/api/v1/departments/resource-provisioning")
async def provision_resources(request: Request) -> JSONResponse:
    core._require_local_session(request)
    payload = await request.json()
    plan_id = str(payload.get("planId") or "") if isinstance(payload, dict) else ""
    overrides = payload.get("overrides") if isinstance(payload, dict) else None
    if not re.fullmatch(r"[0-9a-f]{32}", plan_id):
        return JSONResponse(
            {"error": {"code": "INVALID_RESOURCE_PLAN", "message": "생성 계획을 다시 열어 주세요."}},
            status_code=422,
        )
    try:
        run = start_provision_run(plan_id, overrides)
    except (TypeError, ValueError) as exc:
        return JSONResponse(
            {"error": {"code": "INVALID_RESOURCE_NAME", "message": str(exc)}},
            status_code=422,
        )
    except FileNotFoundError:
        return JSONResponse(
            {"error": {"code": "RESOURCE_PLAN_EXPIRED", "message": "생성 계획이 만료되었습니다. 다시 확인해 주세요."}},
            status_code=404,
        )
    except FileExistsError as exc:
        return JSONResponse(
            {"error": {"code": "RESOURCE_PROVISION_CONFLICT", "message": str(exc)}},
            status_code=409,
        )
    return JSONResponse(run, status_code=202)


@router.get("/api/v1/departments/resource-provisioning/{run_id}")
def provision_run(run_id: str) -> JSONResponse:
    with _PROVISIONS.lock:
        _PROVISIONS.cleanup()
        run = _PROVISIONS.runs.get(run_id)
        if not run:
            return JSONResponse(
                {"error": {"code": "PROVISION_RUN_NOT_FOUND", "message": "리소스 생성 실행을 찾을 수 없습니다."}},
                status_code=404,
            )
        return JSONResponse(copy.deepcopy(run))


@router.get("/api/v1/common-config/resources")
def common_config_resources(project: str, region: str) -> JSONResponse:
    if not core.PROJECT_RE.fullmatch(project) or not core.REGION_RE.fullmatch(region):
        return JSONResponse(
            {"error": {"code": "INVALID_SCOPE", "message": "프로젝트와 리전을 확인해 주세요."}},
            status_code=400,
        )
    bootstrap = environment_setup._gcloud_bootstrap_state()
    if not bootstrap["installed"] or not bootstrap["authenticated"]:
        return JSONResponse(
            {"error": {"code": "GCLOUD_AUTH_REQUIRED", "message": "gcloud 로그인이 필요합니다."}},
            status_code=412,
        )
    if not environment_setup._project_accessible(project):
        return JSONResponse(
            {"error": {"code": "PROJECT_FORBIDDEN", "message": "접근할 수 없는 프로젝트입니다."}},
            status_code=403,
        )
    return JSONResponse(environment_setup._gcloud_project_resources(project, region))


@router.post("/api/v1/common-config/resource-plans")
async def common_config_resource_plan(request: Request) -> JSONResponse:
    core._require_local_session(request)
    payload = await request.json()
    if not isinstance(payload, dict):
        return JSONResponse(
            {"error": {"code": "INVALID_PAYLOAD", "message": "요청 본문을 확인해 주세요."}},
            status_code=422,
        )
    bootstrap = environment_setup._gcloud_bootstrap_state()
    if not bootstrap["installed"] or not bootstrap["authenticated"]:
        return JSONResponse(
            {"error": {"code": "GCLOUD_AUTH_REQUIRED", "message": "gcloud 로그인이 필요합니다."}},
            status_code=412,
        )
    if not environment_setup._project_accessible(str(payload.get("projectId") or "")):
        return JSONResponse(
            {"error": {"code": "PROJECT_FORBIDDEN", "message": "접근할 수 없는 프로젝트입니다."}},
            status_code=403,
        )
    try:
        plan = create_common_resource_plan(payload)
    except (TypeError, ValueError) as exc:
        return JSONResponse(
            {"error": {"code": "INVALID_RESOURCE_NAME", "message": str(exc)}},
            status_code=422,
        )
    return JSONResponse(plan, status_code=200)


@router.post("/api/v1/common-config/resource-provisioning")
async def common_config_provision(request: Request) -> JSONResponse:
    core._require_local_session(request)
    payload = await request.json()
    plan_id = str(payload.get("planId") or "") if isinstance(payload, dict) else ""
    if not re.fullmatch(r"[0-9a-f]{32}", plan_id):
        return JSONResponse(
            {"error": {"code": "INVALID_RESOURCE_PLAN", "message": "생성 계획을 다시 열어 주세요."}},
            status_code=422,
        )
    try:
        run = start_common_provision_run(plan_id)
    except FileNotFoundError:
        return JSONResponse(
            {
                "error": {
                    "code": "RESOURCE_PLAN_EXPIRED",
                    "message": "생성 계획이 만료되었습니다. 다시 확인해 주세요.",
                }
            },
            status_code=404,
        )
    except FileExistsError as exc:
        return JSONResponse(
            {"error": {"code": "RESOURCE_PROVISION_CONFLICT", "message": str(exc)}},
            status_code=409,
        )
    return JSONResponse(run, status_code=202)


@router.get("/api/v1/common-config/resource-provisioning/{run_id}")
def common_config_provision_run(run_id: str) -> JSONResponse:
    with _COMMON_PROVISIONS.lock:
        _COMMON_PROVISIONS.cleanup()
        run = _COMMON_PROVISIONS.runs.get(run_id)
        if not run:
            return JSONResponse(
                {
                    "error": {
                        "code": "PROVISION_RUN_NOT_FOUND",
                        "message": "리소스 생성 실행을 찾을 수 없습니다.",
                    }
                },
                status_code=404,
            )
        return JSONResponse(copy.deepcopy(run))


@router.get("/api/v1/departments/resource-options")
def department_resource_options() -> JSONResponse:
    try:
        common = core._common()
    except (FileNotFoundError, OSError, TypeError, UnicodeError, yaml.YAMLError):
        return JSONResponse(
            {"error": {"code": "COMMON_REQUIRED", "message": "공통 설정이 먼저 필요합니다."}},
            status_code=428,
        )
    options = _department_resource_options(common)
    usage = _department_bucket_usage()
    for bucket in options["buckets"]:
        bucket["usedBy"] = usage.get(str(bucket.get("name") or ""), [])
    if options.get("error"):
        return JSONResponse(
            {"error": {"code": "RESOURCE_LOOKUP_FAILED", "message": options["error"]}},
            status_code=503,
        )
    return JSONResponse(options)
