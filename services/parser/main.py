"""
HWP/HWPX 파서 서비스 — rhwp-python → Markdown → GCS.

POST /parse
  Request:  { gcsUri, mimeType, fileId }
  Response: { gcsMarkdownUri, route, contentHash, tableCount, warnings, engine, qualityGate }

품질 게이트(QG_MODE):
  log      — 기본. 미달해도 색인 진행, 로그만
  reject   — 422 (Sync가 DLQ로 이관)
  fallback — Document AI (ENABLE_DOCAI_FALLBACK=true 필요)
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from shared.config import get_settings  # noqa: E402
from shared.gcs import GcsClient  # noqa: E402
from shared.hashing import sha256_text  # noqa: E402
from shared.logging_config import setup_logging  # noqa: E402
from shared.mime_types import is_hwpx  # noqa: E402
from shared.models import ParseResult, ParseRoute  # noqa: E402

from services.parser.cleanup import cleanup_markdown  # noqa: E402
from services.parser.quality_gate import evaluate_quality  # noqa: E402
from services.parser.rhwp_parser import parse_hwp_bytes, rhwp_available  # noqa: E402

setup_logging()
logger = logging.getLogger("parser_service")

app = FastAPI(title="HWP/HWPX Parser (rhwp)", version="4.0.0")


class ParseRequestBody(BaseModel):
    gcs_uri: str = Field(..., alias="gcsUri")
    mime_type: str = Field(..., alias="mimeType")
    file_id: str = Field(..., alias="fileId")

    model_config = {"populate_by_name": True}


@app.get("/health")
def health() -> dict[str, str]:
    settings = get_settings()
    return {
        "status": "ok" if rhwp_available() else "degraded",
        "engine": "rhwp",
        "rhwp": "ok" if rhwp_available() else "missing",
        "qgMode": settings.qg_mode,
    }


@app.post("/parse")
def parse_document(req: ParseRequestBody) -> JSONResponse:
    settings = get_settings()
    gcs = GcsClient(settings)

    if not rhwp_available():
        raise HTTPException(status_code=503, detail="rhwp-python is not installed")

    try:
        raw = gcs.download_bytes(req.gcs_uri)
    except Exception as exc:  # noqa: BLE001
        logger.exception("GCS download failed: %s", req.gcs_uri)
        raise HTTPException(status_code=400, detail=f"GCS download failed: {exc}") from exc

    filename = (
        f"{req.file_id}.hwpx"
        if is_hwpx(req.mime_type, req.gcs_uri)
        else f"{req.file_id}.hwp"
    )

    try:
        parsed = parse_hwp_bytes(raw, filename=filename)
        markdown = cleanup_markdown(parsed.markdown)
        parsed.metrics.text_length = len(markdown)
    except Exception as exc:  # noqa: BLE001
        logger.exception("rhwp parse failed for %s", req.file_id)
        raise HTTPException(
            status_code=422,
            detail={"error": "PARSE_FAILED", "message": str(exc), "fileId": req.file_id},
        ) from exc

    warnings = list(parsed.metrics.warnings)
    route = ParseRoute.RHWP
    table_count = parsed.metrics.table_count
    gate_status = "pass"

    gate = evaluate_quality(parsed.metrics, settings)
    if gate.triggered:
        warnings.extend(gate.reasons)
        gate_status = "triggered"
        mode = settings.qg_mode

        if gate.empty_text:
            logger.error(
                "quality_gate empty_text fileId=%s reasons=%s",
                req.file_id,
                gate.reasons,
            )
            raise HTTPException(
                status_code=422,
                detail={
                    "error": "EMPTY_TEXT",
                    "reasons": gate.reasons,
                    "engine": "rhwp",
                    "fileId": req.file_id,
                },
            )

        if mode == "log":
            logger.warning(
                "quality_gate soft_fail mode=log fileId=%s reasons=%s "
                "textLength=%s sourceBytes=%s — indexing continues",
                req.file_id,
                gate.reasons,
                parsed.metrics.text_length,
                parsed.metrics.source_bytes,
            )
            gate_status = "logged"

        elif mode == "reject":
            logger.warning(
                "quality_gate reject fileId=%s reasons=%s",
                req.file_id,
                gate.reasons,
            )
            raise HTTPException(
                status_code=422,
                detail={
                    "error": "QUALITY_GATE",
                    "reasons": gate.reasons,
                    "engine": "rhwp",
                    "fileId": req.file_id,
                    "qgMode": mode,
                },
            )

        elif mode == "fallback":
            if not settings.enable_docai_fallback:
                logger.warning(
                    "quality_gate fallback requested but ENABLE_DOCAI_FALLBACK=false "
                    "fileId=%s — treating as log",
                    req.file_id,
                )
                gate_status = "logged"
            else:
                from services.parser.fallback_docai import (  # noqa: PLC0415
                    FallbackParseError,
                    fallback_parse,
                )

                try:
                    fb_md, fb_metrics = fallback_parse(
                        raw, filename=filename, settings=settings
                    )
                    markdown = cleanup_markdown(fb_md)
                    route = ParseRoute.PDF_DOCAI
                    table_count = fb_metrics.table_count
                    warnings.extend(fb_metrics.warnings)
                    gate_status = "fallback_ok"
                except FallbackParseError as exc:
                    raise HTTPException(
                        status_code=422,
                        detail={"error": "FALLBACK_FAILED", "message": str(exc)},
                    ) from exc

    content_hash = sha256_text(markdown)
    md_uri = gcs.upload_normalized_md(markdown, req.file_id)

    result = ParseResult(
        gcs_markdown_uri=md_uri,
        route=route,
        content_hash=content_hash,
        table_count=table_count,
        warnings=warnings,
        text_length=len(markdown),
    )
    payload = result.to_dict()
    payload["engine"] = "rhwp"
    payload["qualityGate"] = {
        "mode": settings.qg_mode,
        "status": gate_status,
        "reasons": gate.reasons if gate.triggered else [],
    }
    logger.info(
        "Parsed %s engine=rhwp route=%s gate=%s hash=%s len=%s",
        req.file_id,
        route.value,
        gate_status,
        content_hash,
        len(markdown),
    )
    return JSONResponse(content=payload)


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", "8080"))
    uvicorn.run("services.parser.main:app", host="0.0.0.0", port=port, reload=False)
