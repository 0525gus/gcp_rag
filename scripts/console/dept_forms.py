from __future__ import annotations

import copy
import secrets
from typing import Any

import yaml
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from scripts import dept_config
from scripts.console import cloud_registry, core, provisioning, runtime_deploy

router = APIRouter()


def _department_drive_conflicts(code: str, drive_ids: list[str]) -> list[dict[str, Any]]:
    wanted = set(drive_ids)
    if not wanted:
        return []
    conflicts: list[dict[str, Any]] = []
    for other, other_cfg in cloud_registry._cloud_department_configs().items():
        duplicate = sorted(
            wanted & set(core._normalise_ids((other_cfg.get("drive") or {}).get("driveIds")))
        )
        if other != code and duplicate:
            conflicts.append(
                {
                    "code": other,
                    "name": str(other_cfg.get("name") or other),
                    "driveIds": duplicate,
                }
            )
    return conflicts


def _drive_conflict_response(conflicts: list[dict[str, Any]]) -> JSONResponse:
    owners = ", ".join(f"{item['name']}({item['code']})" for item in conflicts)
    return JSONResponse(
        {
            "error": {
                "code": "DRIVE_ID_CONFLICT",
                "message": f"다른 학과와 공유드라이브 ID가 겹칩니다: {owners}",
                "driveConflicts": conflicts,
            }
        },
        status_code=409,
    )


def _unacked_drive_conflicts(
    payload: dict[str, Any], result: dict[str, Any]
) -> JSONResponse | None:
    conflicts = result.get("driveConflicts") or []
    if not conflicts or payload.get("allowDuplicateDriveIds"):
        return None
    return _drive_conflict_response(conflicts)


def _has_secret_input(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    for key, child in value.items():
        if key in core.SECRET_FIELDS or "token" in key.lower() or "secret" in key.lower():
            return True
        if isinstance(child, dict) and _has_secret_input(child):
            return True
    return False


def department_code_availability(code: str, *, current_code: str = "") -> dict[str, Any]:
    normalised = str(code or "").strip().lower()
    current = str(current_code or "").strip().lower()
    if not core.DEPT_CODE_RE.fullmatch(normalised):
        return {
            "code": normalised,
            "available": False,
            "reason": "영문 소문자로 시작하는 2~20자 코드여야 합니다.",
        }
    if current and current != normalised:
        return {
            "code": normalised,
            "available": False,
            "reason": "수정 중에는 학과 코드를 변경할 수 없습니다.",
        }
    registered = normalised in cloud_registry._cloud_department_configs()
    if registered and current != normalised:
        return {
            "code": normalised,
            "available": False,
            "reason": f"{normalised} 코드는 이미 다른 학과에서 사용 중입니다.",
        }
    return {"code": normalised, "available": True, "reason": "사용 가능한 학과 코드입니다."}


def validate_candidate(payload: dict[str, Any], *, check_existing: bool = True) -> tuple[dict, dict]:
    errors: dict[str, list[str]] = {}
    warnings: list[str] = []
    common = core._common()

    code = str(payload.get("code") or "").strip().lower()
    name = str(payload.get("name") or "").strip()
    if not core.DEPT_CODE_RE.fullmatch(code):
        core._field_error(errors, "code", "영문 소문자로 시작하는 2~20자 코드여야 합니다.")
    if not name:
        core._field_error(errors, "name", "학과명을 입력해 주세요.")
    if check_existing and code:
        availability = department_code_availability(code)
        if not availability["available"] and core.DEPT_CODE_RE.fullmatch(code):
            core._field_error(errors, "code", availability["reason"])

    corpora = payload.get("corpora") if isinstance(payload.get("corpora"), dict) else {}
    requested_mode = str(payload.get("corpusMode") or "").strip().lower()
    staff_corpus = str(corpora.get("staff") or "").strip()
    student_corpus = str(corpora.get("student") or "").strip()
    drive = payload.get("drive") if isinstance(payload.get("drive"), dict) else {}
    student_ids = core._normalise_ids(drive.get("studentFolderIds"))
    split_enabled = requested_mode == "split" if requested_mode else bool(student_corpus or student_ids)
    if requested_mode not in {"", "single", "split"}:
        core._field_error(errors, "corpusMode", "단일 코퍼스 또는 학생 분리를 선택해 주세요.")
    if not split_enabled:
        student_corpus = ""
        student_ids = []
    project = str(common.get("GCP_PROJECT_ID") or "").strip()
    region = str(common["GCP_REGION"]).strip()
    corpus_values = [("staff", staff_corpus)]
    if split_enabled:
        corpus_values.append(("student", student_corpus))
    for audience, value in corpus_values:
        match = core.CORPUS_RE.fullmatch(value)
        field = f"corpora.{audience}"
        if not match:
            core._field_error(errors, field, "projects/.../locations/.../ragCorpora/... 형식이어야 합니다.")
        elif match.group(1) != project or match.group(2) != region:
            core._field_error(errors, field, f"공통 설정의 {project}/{region}과 일치해야 합니다.")
    if staff_corpus and staff_corpus == student_corpus:
        core._field_error(errors, "corpora.student", "교직원 코퍼스와 달라야 합니다.")

    buckets = payload.get("buckets") if isinstance(payload.get("buckets"), dict) else {}
    hwp_bucket = str(buckets.get("hwpOriginal") or "").removeprefix("gs://").strip()
    source_bucket = str(buckets.get("source") or "").removeprefix("gs://").strip()
    for field, value in (("buckets.hwpOriginal", hwp_bucket), ("buckets.source", source_bucket)):
        if not core.BUCKET_RE.fullmatch(value):
            core._field_error(errors, field, "유효한 GCS 버킷 이름을 입력해 주세요.")
    if hwp_bucket and hwp_bucket == source_bucket:
        core._field_error(errors, "buckets.source", "HWP 원본 버킷과 달라야 합니다.")

    drive_ids = core._normalise_ids(drive.get("driveIds"))
    sync_ids = core._normalise_ids(drive.get("syncFolderIds"))
    if not drive_ids:
        core._field_error(errors, "drive.driveIds", "공유드라이브 ID가 하나 이상 필요합니다.")
    if not sync_ids:
        core._field_error(errors, "drive.syncFolderIds", "동기화 폴더가 하나 이상 필요합니다.")
    if split_enabled and not student_ids:
        core._field_error(errors, "drive.studentFolderIds", "학생 폴더가 하나 이상 필요합니다.")
    outside = [item for item in student_ids if item not in sync_ids]
    if outside:
        core._field_error(
            errors,
            "drive.studentFolderIds",
            "동기화 폴더에 포함되지 않은 ID입니다: " + ", ".join(outside),
        )

    drive_conflicts = _department_drive_conflicts(code, drive_ids)
    for item in drive_conflicts:
        warnings.append(
            f"{item['code']} 학과와 중복된 공유드라이브 ID입니다: {', '.join(item['driveIds'])}"
        )

    mins = payload.get("minInstances") if isinstance(payload.get("minInstances"), dict) else {}
    min_instances: dict[str, int] = {}
    active_audiences = dept_config.AUDIENCES if split_enabled else ("staff",)
    for audience in active_audiences:
        raw = mins.get(audience, 0)
        try:
            parsed = int(raw)
            if parsed < 0:
                raise ValueError
            min_instances[audience] = parsed
        except (TypeError, ValueError):
            core._field_error(errors, f"minInstances.{audience}", "0 이상의 정수여야 합니다.")

    enable_docai_fallback = payload.get("enableDocaiFallback", False)
    if not isinstance(enable_docai_fallback, bool):
        core._field_error(errors, "enableDocaiFallback", "켜기 또는 끄기를 선택해 주세요.")
    elif enable_docai_fallback and not all(common.get(k) for k in ("DOCAI_PROCESSOR_ID", "DOCAI_LOCATION")):
        core._field_error(errors, "enableDocaiFallback", "공통 설정의 Layout Parser ID와 리전을 먼저 설정해 주세요.")
    enable_image_ocr = payload.get("enableImageOcr", False)
    if not isinstance(enable_image_ocr, bool):
        core._field_error(errors, "enableImageOcr", "켜기 또는 끄기를 선택해 주세요.")
    elif enable_image_ocr and not all(common.get(k) for k in ("DOCAI_OCR_PROCESSOR_ID", "DOCAI_OCR_LOCATION")):
        core._field_error(errors, "enableImageOcr", "공통 설정의 OCR 프로세서 ID와 리전을 먼저 설정해 주세요.")

    audience_options = {}
    for base, required in (("enableDocaiFallback", ("DOCAI_PROCESSOR_ID", "DOCAI_LOCATION")),
                           ("enableImageOcr", ("DOCAI_OCR_PROCESSOR_ID", "DOCAI_OCR_LOCATION"))):
        for audience in ("Staff", "Student"):
            key = base + audience
            if key not in payload:
                continue
            value = payload[key]
            if not isinstance(value, bool):
                core._field_error(errors, key, "켜기 또는 끄기를 선택해 주세요.")
            elif value and not all(common.get(k) for k in required):
                core._field_error(errors, key, "공통 설정의 프로세서 ID와 리전을 먼저 설정해 주세요.")
            audience_options[key] = value

    candidate: dict[str, Any] = {
        "name": name,
        "corpora": {"staff": staff_corpus},
        "buckets": {"hwpOriginal": hwp_bucket, "source": source_bucket},
        "drive": {
            "driveIds": drive_ids,
            "syncFolderIds": sync_ids,
        },
        "minInstances": min_instances,
        **audience_options,
        "enableDocaiFallback": enable_docai_fallback,
        "enableImageOcr": enable_image_ocr,
    }
    if split_enabled:
        candidate["corpora"]["student"] = student_corpus
        candidate["drive"]["studentFolderIds"] = student_ids
    return candidate, {
        "valid": not errors,
        "fieldErrors": errors,
        "warnings": warnings,
        "driveConflicts": drive_conflicts,
    }


def _render_yaml(
    candidate: dict[str, Any], *, preview: bool, keys: dict[str, str] | None = None
) -> str:
    body = dict(candidate)
    split_enabled = bool((body.get("corpora") or {}).get("student"))
    active_audiences = dept_config.AUDIENCES if split_enabled else ("staff",)
    available_keys = keys or {
        audience: "<자동 생성>" if preview else secrets.token_urlsafe(32)
        for audience in active_audiences
    }
    body["keys"] = {
        audience: available_keys[audience]
        for audience in active_audiences
        if audience in available_keys
    }
    ordered = {
        "name": body["name"],
        "corpora": body["corpora"],
        "keys": body["keys"],
        "buckets": body["buckets"],
        "drive": body["drive"],
        "minInstances": body["minInstances"],
        "enableDocaiFallback": body.get("enableDocaiFallback", False),
        "enableImageOcr": body.get("enableImageOcr", False),
    }
    for key in ('enableDocaiFallbackStaff', 'enableDocaiFallbackStudent', 'enableImageOcrStaff', 'enableImageOcrStudent'):
        if key in body:
            ordered[key] = body[key]
    return yaml.safe_dump(ordered, allow_unicode=True, sort_keys=False, width=1000)


def create_department(
    code: str, candidate: dict[str, Any], *, allow_duplicate_drives: bool = False
) -> dict[str, Any]:
    configs = cloud_registry._cloud_department_configs()
    if code in configs:
        raise FileExistsError(code)
    config = copy.deepcopy(candidate)
    split_enabled = bool((config.get("corpora") or {}).get("student"))
    audiences = dept_config.AUDIENCES if split_enabled else ("staff",)
    config["keys"] = {audience: secrets.token_urlsafe(32) for audience in audiences}
    cloud_registry._cloud_config_audiences(config)
    if not allow_duplicate_drives:
        try:
            dept_config.departments_map_from_configs({**configs, code: config})
        except SystemExit as exc:
            raise RuntimeError(str(exc)) from exc
    cloud_registry._mcp_registry_mutate("update_department", code, config)
    return config


def _merge_live_resource_validation(candidate: dict[str, Any], result: dict[str, Any]) -> None:
    options = provisioning._department_resource_options(core._common())
    corpus_names = {item["name"] for item in options["corpora"]}
    bucket_names = {item["name"] for item in options["buckets"]}
    error = str(options.get("error") or "")
    split_enabled = bool((candidate.get("corpora") or {}).get("student"))
    audiences = dept_config.AUDIENCES if split_enabled else ("staff",)
    for audience in audiences:
        field = f"corpora.{audience}"
        value = str((candidate.get("corpora") or {}).get(audience) or "")
        if value and value not in corpus_names:
            core._field_error(
                result["fieldErrors"],
                field,
                error or "현재 프로젝트·리전에 존재하는 RAG 코퍼스를 선택해 주세요.",
            )
    for key, field in (("hwpOriginal", "buckets.hwpOriginal"), ("source", "buckets.source")):
        value = str((candidate.get("buckets") or {}).get(key) or "")
        if value and value not in bucket_names:
            core._field_error(
                result["fieldErrors"],
                field,
                error or "현재 프로젝트·리전의 보호된 GCS 버킷을 선택해 주세요.",
            )
    result["valid"] = not result["fieldErrors"]


@router.get("/api/v1/departments")
def departments() -> dict[str, Any]:
    return {"departments": cloud_registry.list_department_records()}


@router.get("/api/v1/cloud-mcp-services")
def cloud_mcp_services() -> JSONResponse:
    try:
        return JSONResponse({"departments": cloud_registry.cloud_mcp_department_records()})
    except (FileNotFoundError, OSError, RuntimeError, TypeError, yaml.YAMLError) as exc:
        return JSONResponse(
            {"error": {"code": "CLOUD_MCP_LOOKUP_FAILED", "message": str(exc)[:400]}},
            status_code=503,
        )


@router.get("/api/v1/departments/code-availability")
def code_availability(code: str, current: str = "") -> JSONResponse:
    return JSONResponse(department_code_availability(code, current_code=current))


@router.get("/api/v1/departments/{code}/mcp-servers")
def get_department_mcp_servers(code: str) -> JSONResponse:
    try:
        return JSONResponse(cloud_registry.department_mcp_servers(code))
    except FileNotFoundError:
        return JSONResponse(
            {"error": {"code": "NOT_FOUND", "message": "학과 설정을 찾을 수 없습니다."}},
            status_code=404,
        )
    except (OSError, RuntimeError, TypeError, UnicodeError, yaml.YAMLError) as exc:
        return JSONResponse(
            {"error": {"code": "MCP_LOOKUP_FAILED", "message": str(exc)[:300]}},
            status_code=503,
        )


@router.post("/api/v1/departments/{code}/mcp-keys/{audience}")
def copy_department_mcp_key(code: str, audience: str, request: Request) -> JSONResponse:
    core._require_local_session(request)
    try:
        key = cloud_registry.department_mcp_key(code, audience)
    except FileNotFoundError:
        return JSONResponse(
            {"error": {"code": "NOT_FOUND", "message": "학과 설정을 찾을 수 없습니다."}},
            status_code=404,
        )
    except (TypeError, ValueError, yaml.YAMLError) as exc:
        return JSONResponse(
            {"error": {"code": "MCP_KEY_UNAVAILABLE", "message": str(exc)}},
            status_code=422,
        )
    except (OSError, UnicodeError) as exc:
        return JSONResponse(
            {"error": {"code": "MCP_KEY_READ_FAILED", "message": str(exc)[:300]}},
            status_code=500,
        )
    return JSONResponse({"audience": audience, "key": key})


@router.post("/api/v1/departments/preview")
async def preview(request: Request) -> JSONResponse:
    core._require_local_session(request)
    payload = await request.json()
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="invalid request")
    if _has_secret_input(payload):
        return JSONResponse(
            {"error": {"code": "UNSUPPORTED_SECRET_INPUT", "message": "키는 서버에서 생성합니다."}},
            status_code=400,
        )
    candidate, result = validate_candidate(payload)
    if result["valid"]:
        _merge_live_resource_validation(candidate, result)
    result["yamlPreview"] = _render_yaml(candidate, preview=True)
    return JSONResponse(result)


@router.post("/api/v1/departments")
async def create(request: Request) -> JSONResponse:
    core._require_local_session(request)
    payload = await request.json()
    if not isinstance(payload, dict) or _has_secret_input(payload):
        return JSONResponse(
            {"error": {"code": "UNSUPPORTED_SECRET_INPUT", "message": "키는 서버에서 생성합니다."}},
            status_code=400,
        )
    candidate, result = validate_candidate(payload, check_existing=False)
    if result["valid"]:
        _merge_live_resource_validation(candidate, result)
    if not result["valid"]:
        return JSONResponse(
            {
                "error": {
                    "code": "VALIDATION_FAILED",
                    "message": "입력값을 확인해 주세요.",
                    "fieldErrors": result["fieldErrors"],
                }
            },
            status_code=422,
        )
    conflict = _unacked_drive_conflicts(payload, result)
    if conflict:
        return conflict
    code = str(payload["code"]).strip().lower()
    try:
        config = create_department(
            code, candidate, allow_duplicate_drives=bool(payload.get("allowDuplicateDriveIds"))
        )
    except FileExistsError:
        return JSONResponse(
            {"error": {"code": "FILE_EXISTS", "message": "이미 등록된 학과입니다."}},
            status_code=409,
        )
    except (OSError, RuntimeError, TypeError, ValueError, yaml.YAMLError) as exc:
        return JSONResponse(
            {"error": {"code": "WRITE_FAILED", "message": str(exc)[:300]}}, status_code=500
        )
    return JSONResponse(
        {
            "code": code,
            "path": "Cloud MCP registry",
            "created": True,
            "configRevision": core._mapping_revision(config),
            "nextActions": ["status", "deploy"],
        },
        status_code=201,
    )


@router.get("/api/v1/departments/{code}/config")
def get_department_config(code: str) -> JSONResponse:
    try:
        return JSONResponse(cloud_registry.department_public_config_any(code))
    except (FileNotFoundError, OSError, TypeError, UnicodeError, ValueError, yaml.YAMLError) as exc:
        return JSONResponse(
            {"error": {"code": "NOT_FOUND", "message": str(exc) or "학과 설정을 찾을 수 없습니다."}},
            status_code=404,
        )


@router.post("/api/v1/departments/{code}/preview")
async def preview_update(code: str, request: Request) -> JSONResponse:
    core._require_local_session(request)
    payload = await request.json()
    if (
        not isinstance(payload, dict)
        or _has_secret_input(payload)
        or str(payload.get("code") or "").strip().lower() != code
    ):
        return JSONResponse(
            {"error": {"code": "INVALID_UPDATE", "message": "수정 요청이 올바르지 않습니다."}},
            status_code=400,
        )
    try:
        current = cloud_registry.department_public_config_any(code)
    except (FileNotFoundError, OSError, TypeError, UnicodeError, ValueError, yaml.YAMLError):
        return JSONResponse(
            {"error": {"code": "NOT_FOUND", "message": "학과 설정을 찾을 수 없습니다."}},
            status_code=404,
        )
    candidate, result = validate_candidate(payload, check_existing=False)
    candidate_mode = "split" if candidate.get("corpora", {}).get("student") else "single"
    if candidate_mode != current["corpusMode"]:
        core._field_error(
            result["fieldErrors"],
            "corpusMode",
            "운영 중 코퍼스 구성 변경은 재색인·기존 서비스 정리가 필요해 별도 마이그레이션으로 진행해야 합니다.",
        )
        result["valid"] = False
    if result["valid"]:
        _merge_live_resource_validation(candidate, result)
    result["yamlPreview"] = _render_yaml(
        candidate,
        preview=True,
        keys={"staff": "<기존 키 유지>", "student": "<기존 키 유지>"},
    )
    return JSONResponse(result)


@router.put("/api/v1/departments/{code}")
async def update(code: str, request: Request) -> JSONResponse:
    core._require_local_session(request)
    payload = await request.json()
    if (
        not isinstance(payload, dict)
        or _has_secret_input(payload)
        or str(payload.get("code") or "").strip().lower() != code
    ):
        return JSONResponse(
            {"error": {"code": "INVALID_UPDATE", "message": "수정 요청이 올바르지 않습니다."}},
            status_code=400,
        )
    try:
        current = cloud_registry.department_public_config_any(code)
    except (FileNotFoundError, OSError, TypeError, UnicodeError, ValueError, yaml.YAMLError):
        return JSONResponse(
            {"error": {"code": "NOT_FOUND", "message": "학과 설정을 찾을 수 없습니다."}},
            status_code=404,
        )
    if payload.get("configRevision") != current["configRevision"]:
        return JSONResponse(
            {
                "error": {
                    "code": "REVISION_CONFLICT",
                    "message": "설정이 다른 곳에서 변경되었습니다. 다시 열어 주세요.",
                }
            },
            status_code=409,
        )
    candidate, result = validate_candidate(payload, check_existing=False)
    candidate_mode = "split" if candidate.get("corpora", {}).get("student") else "single"
    if candidate_mode != current["corpusMode"]:
        core._field_error(
            result["fieldErrors"],
            "corpusMode",
            "운영 중 코퍼스 구성 변경은 별도 마이그레이션이 필요합니다.",
        )
        result["valid"] = False
    if result["valid"]:
        _merge_live_resource_validation(candidate, result)
    if not result["valid"]:
        return JSONResponse(
            {
                "error": {
                    "code": "VALIDATION_FAILED",
                    "message": "입력값을 확인해 주세요.",
                    "fieldErrors": result["fieldErrors"],
                }
            },
            status_code=422,
        )
    conflict = _unacked_drive_conflicts(payload, result)
    if conflict:
        return conflict
    try:
        existing, cloud_revision = cloud_registry.cloud_department_config(code)
        if cloud_revision != current["configRevision"]:
            raise RuntimeError("Cloud 설정이 변경되었습니다. 다시 열어 주세요.")
        merged = copy.deepcopy(existing)
        for key in ("name", "corpora", "buckets", "drive", "minInstances", "enableDocaiFallback", "enableImageOcr", "enableDocaiFallbackStaff", "enableDocaiFallbackStudent", "enableImageOcrStaff", "enableImageOcrStudent"):
            merged[key] = copy.deepcopy(candidate[key])
        run = runtime_deploy.start_mcp_deployment(code, cloud_config=merged)
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
    except (OSError, RuntimeError, TypeError, ValueError, yaml.YAMLError) as exc:
        return JSONResponse(
            {"error": {"code": "CLOUD_UPDATE_FAILED", "message": str(exc)[:400]}},
            status_code=422,
        )
    return JSONResponse(
        {
            "code": code,
            "path": "Cloud MCP registry",
            "updated": True,
            "configRevision": core._mapping_revision(merged),
            "deployment": run,
        },
        status_code=202,
    )


@router.put("/api/v1/departments/{code}/docai-fallback")
async def update_docai_fallback(code: str, request: Request) -> JSONResponse:
    return await _update_parser_options(code, request, option="enableDocaiFallback")


@router.put("/api/v1/departments/{code}/image-ocr")
async def update_image_ocr(code: str, request: Request) -> JSONResponse:
    return await _update_parser_options(code, request, option="enableImageOcr")


@router.put("/api/v1/departments/{code}/parser-options")
async def update_parser_options(code: str, request: Request) -> JSONResponse:
    return await _update_parser_options(code, request)


async def _update_parser_options(
    code: str, request: Request, *, option: str | None = None,
) -> JSONResponse:
    core._require_local_session(request)
    payload = await request.json()
    audience_keys = {"enableDocaiFallbackStaff", "enableDocaiFallbackStudent", "enableImageOcrStaff", "enableImageOcrStudent"}
    scoped_keys = {key for key in audience_keys if not option or key.startswith(option)}
    scoped_request = isinstance(payload, dict) and bool(set(payload) & scoped_keys) and set(payload) <= scoped_keys | {"configRevision"}
    allowed_keys = ({option, "configRevision"},) if option else (
        {"enableDocaiFallback", "configRevision"},
        {"enableDocaiFallback", "enableImageOcr", "configRevision"},
    )
    if (
        not isinstance(payload, dict)
        or (set(payload) not in allowed_keys and not scoped_request)
        or any(not isinstance(payload[key], bool) for key in payload if key != "configRevision")
        or not isinstance(payload.get("configRevision"), str)
        or not payload["configRevision"]
    ):
        return JSONResponse(
            {"error": {"code": "INVALID_UPDATE", "message": "설정 변경 요청이 올바르지 않습니다."}},
            status_code=400,
        )
    try:
        existing, revision = cloud_registry.cloud_department_config(code)
        if payload["configRevision"] != revision:
            return JSONResponse(
                {"error": {"code": "REVISION_CONFLICT", "message": "설정이 다른 곳에서 변경되었습니다. 학과 상세를 다시 열어 주세요."}},
                status_code=409,
            )
        merged = copy.deepcopy(existing)
        for key in scoped_keys & set(payload):
            required = ("DOCAI_PROCESSOR_ID", "DOCAI_LOCATION") if "DocaiFallback" in key else ("DOCAI_OCR_PROCESSOR_ID", "DOCAI_OCR_LOCATION")
            if payload[key] and not all(core._common().get(k) for k in required):
                raise ValueError("공통 설정의 프로세서 ID와 리전을 먼저 설정해 주세요.")
            merged[key] = payload[key]
        if "enableDocaiFallback" in payload:
            if payload["enableDocaiFallback"] and not all(
                core._common().get(k) for k in ("DOCAI_PROCESSOR_ID", "DOCAI_LOCATION")
            ):
                raise ValueError("공통 설정의 Layout Parser ID와 리전을 먼저 설정해 주세요.")
            merged["enableDocaiFallback"] = payload["enableDocaiFallback"]
        if "enableImageOcr" in payload:
            if payload["enableImageOcr"] and not all(
                core._common().get(k) for k in ("DOCAI_OCR_PROCESSOR_ID", "DOCAI_OCR_LOCATION")
            ):
                raise ValueError("공통 설정의 OCR 프로세서 ID와 리전을 먼저 설정해 주세요.")
            merged["enableImageOcr"] = payload["enableImageOcr"]
        run = runtime_deploy.start_mcp_deployment(code, cloud_config=merged)
    except FileNotFoundError:
        return JSONResponse(
            {"error": {"code": "NOT_FOUND", "message": "학과 설정을 찾을 수 없습니다."}},
            status_code=404,
        )
    except FileExistsError as exc:
        return JSONResponse(
            {"error": {"code": "MCP_DEPLOYMENT_RUNNING", "message": "이 학과의 설정 반영이 이미 진행 중입니다.", "runId": str(exc)}},
            status_code=409,
        )
    except (OSError, RuntimeError, TypeError, ValueError, yaml.YAMLError) as exc:
        return JSONResponse(
            {"error": {"code": "CLOUD_UPDATE_FAILED", "message": str(exc)[:400]}},
            status_code=422,
        )
    return JSONResponse({"code": code, "deployment": run}, status_code=202)
