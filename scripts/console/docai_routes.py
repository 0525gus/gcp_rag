from __future__ import annotations

import hashlib
import os
import threading
import uuid

import yaml
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from scripts import docai_setup, gcp_cli
from scripts.console import core, provisioning

router = APIRouter()


_DOCAI_SETUP = docai_setup.ProcessorSetup(
    lambda *a, **kw: gcp_cli.http_json(*a, **kw),
    lambda *a, **kw: gcp_cli.http_post_json(*a, **kw),
    lambda: gcp_cli.access_token(),
)


_DOCAI_CONFIG_LOCK = threading.Lock()


def _docai_common_view():
    path = core.CONFIG_DIR / "common.yaml"
    raw = path.read_bytes()
    common = yaml.safe_load(raw)
    return {
        "projectId": common["GCP_PROJECT_ID"],
        "configRevision": hashlib.sha256(raw).hexdigest(),
        "docai": {kind: {"processorId": str(common.get(keys[1]) or ""),
                         "location": str(common.get(keys[2]) or "") if common.get(keys[1]) else ""}
                  for kind, keys in docai_setup.KINDS.items()},
        "locations": docai_setup.LOCATIONS,
    }


@router.get("/api/v1/common-config/docai")
def docai_common_config() -> JSONResponse:
    try:
        return JSONResponse(_docai_common_view())
    except (OSError, KeyError, TypeError, yaml.YAMLError):
        return JSONResponse({"error": {"message": "공통 설정을 먼저 준비해 주세요."}}, status_code=422)


@router.post("/api/v1/common-config/docai/lookup")
async def lookup_docai_processors(request: Request) -> JSONResponse:
    core._require_local_session(request)
    payload = await request.json()
    try:
        if not isinstance(payload, dict):
            raise ValueError("조회 요청을 확인해 주세요.")
        result = _DOCAI_SETUP.lookup(payload.get("projectId"), payload.get("kind"), payload.get("location"))
        return JSONResponse(result)
    except (ValueError, RuntimeError, TypeError) as exc:
        return JSONResponse({"error": {"message": str(exc)[:400]}}, status_code=422)


@router.post("/api/v1/common-config/docai/create")
async def create_docai_processor(request: Request) -> JSONResponse:
    core._require_local_session(request)
    payload = await request.json()
    try:
        if not isinstance(payload, dict) or set(payload) != {"planId"} or not isinstance(payload["planId"], str):
            raise ValueError("먼저 프로세서를 조회하고 생성 계획을 확인해 주세요.")
        return JSONResponse(_DOCAI_SETUP.create(payload["planId"]), status_code=201)
    except (ValueError, RuntimeError) as exc:
        return JSONResponse({"error": {"message": str(exc)[:400]}}, status_code=422)


@router.post("/api/v1/common-config/docai/check")
async def check_docai_processor(request: Request) -> JSONResponse:
    core._require_local_session(request)
    payload = await request.json()
    try:
        if not isinstance(payload, dict) or not isinstance(payload.get("selection"), dict):
            raise ValueError("프로세서를 선택해 주세요.")
        _DOCAI_SETUP.base(payload.get("projectId"), payload.get("kind"), payload["selection"].get("location"))
        _DOCAI_SETUP.verify(payload["projectId"], payload["kind"], payload["selection"])
        return JSONResponse({"ok": True, "message": "현재 로그인 계정으로 존재·종류·활성 상태를 확인했습니다. 실제 문서 처리와 Parser 실행 계정 권한은 별도 검증이 필요합니다."})
    except (ValueError, RuntimeError, TypeError) as exc:
        return JSONResponse({"error": {"message": str(exc)[:400]}}, status_code=422)


@router.post("/api/v1/common-config/docai/enable-api")
async def enable_docai_api(request: Request) -> JSONResponse:
    core._require_local_session(request)
    payload = await request.json()
    try:
        project = payload.get("projectId") if isinstance(payload, dict) else ""
        _DOCAI_SETUP.base(project, "ocr", "us")
        provisioning._enable_gcloud_service("documentai.googleapis.com", project)
        return JSONResponse({"enabled": True})
    except (ValueError, RuntimeError) as exc:
        return JSONResponse({"error": {"message": str(exc)[:400]}}, status_code=422)


@router.put("/api/v1/common-config/docai")
async def save_docai_common_config(request: Request) -> JSONResponse:
    core._require_local_session(request)
    payload = await request.json()
    try:
        if not isinstance(payload, dict) or set(payload) != {"docai", "configRevision"}:
            raise ValueError("Document AI 설정 요청을 확인해 주세요.")
        fields = docai_setup.selection_fields(payload["docai"])
        with _DOCAI_CONFIG_LOCK:
            view = _docai_common_view()
            if payload["configRevision"] != view["configRevision"]:
                return JSONResponse({"error": {"message": "공통 설정이 변경됐습니다. 화면을 다시 열어 주세요."}}, status_code=409)
            for kind, selection in payload["docai"].items():
                _DOCAI_SETUP.verify(view["projectId"], kind, selection)
            target = core.CONFIG_DIR / "common.yaml"
            raw = target.read_bytes()
            if hashlib.sha256(raw).hexdigest() != view["configRevision"]:
                return JSONResponse({"error": {"message": "공통 설정이 변경됐습니다. 다시 열어 주세요."}}, status_code=409)
            common = yaml.safe_load(raw)
            common.update(fields)
            temp = core.CONFIG_DIR / f".common.docai.{uuid.uuid4().hex}.tmp"
            try:
                temp.write_text(yaml.safe_dump(common, allow_unicode=True, sort_keys=False), encoding="utf-8")
                os.replace(temp, target)
            finally:
                temp.unlink(missing_ok=True)
            return JSONResponse({**_docai_common_view(), "deploymentRequired": True})
    except (ValueError, RuntimeError, OSError, TypeError, KeyError, yaml.YAMLError) as exc:
        return JSONResponse({"error": {"message": str(exc)[:400]}}, status_code=422)
