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
from services.parser.engine import (  # noqa: E402
    can_parse,
    engine_status,
    is_hwpx_filename,
    parse_document_bytes,
)
from services.parser.hwpx_parser import ENGINE as HWPX_ENGINE  # noqa: E402
from services.parser.quality_gate import (  # noqa: E402
    count_markdown_tables,
    evaluate_quality,
)

setup_logging()
logger = logging.getLogger("parser_service")

app = FastAPI(title="HWP/HWPX Parser", version="4.1.0")


class ParseRequestBody(BaseModel):
    gcs_uri: str = Field(..., alias="gcsUri")
    mime_type: str = Field(..., alias="mimeType")
    file_id: str = Field(..., alias="fileId")

    model_config = {"populate_by_name": True}


@app.get("/health")
def health() -> dict[str, str]:
    settings = get_settings()
    engines = engine_status()
    return {
        "status": "ok" if all(v == "ok" for v in engines.values()) else "degraded",
        **engines,
        "qgMode": settings.qg_mode,
    }


@app.post("/parse")
def parse_document(req: ParseRequestBody) -> JSONResponse:
    settings = get_settings()
    gcs = GcsClient(settings)

    filename = (
        f"{req.file_id}.hwpx"
        if is_hwpx(req.mime_type, req.gcs_uri)
        else f"{req.file_id}.hwp"
    )

    if not can_parse(filename):
        engine_name = "python-hwpx/rhwp" if is_hwpx_filename(filename) else "rhwp-python"
        raise HTTPException(status_code=503, detail=f"{engine_name} is not installed")

    try:
        raw = gcs.download_bytes(req.gcs_uri)
    except Exception as exc:  # noqa: BLE001
        logger.exception("GCS download failed: %s", req.gcs_uri)
        raise HTTPException(status_code=400, detail=f"GCS download failed: {exc}") from exc

    try:
        parsed = parse_document_bytes(raw, filename=filename)
        markdown = cleanup_markdown(parsed.markdown)
        parsed.metrics.text_length = len(markdown)
        # 최종 본문 기준으로 잰다 — cleanup 이 표를 지웠다면 그것도 손실이다.
        parsed.metrics.tables_rendered = count_markdown_tables(markdown)
    except Exception as exc:  # noqa: BLE001
        logger.exception("parse failed for %s", req.file_id)
        raise HTTPException(
            status_code=422,
            detail={"error": "PARSE_FAILED", "message": str(exc), "fileId": req.file_id},
        ) from exc

    warnings = list(parsed.metrics.warnings)
    engine = parsed.engine
    route = ParseRoute.HWPX if engine == HWPX_ENGINE else ParseRoute.RHWP
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
                    "engine": engine,
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
                    "engine": engine,
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
                    engine = "docai"
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
    payload["engine"] = engine
    payload["qualityGate"] = {
        "mode": settings.qg_mode,
        "status": gate_status,
        "reasons": gate.reasons if gate.triggered else [],
    }
    logger.info(
        "Parsed %s engine=%s route=%s gate=%s hash=%s len=%s",
        req.file_id,
        engine,
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
