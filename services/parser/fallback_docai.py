"""PDF 전환 → Document AI Layout Parser 폴백."""

from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
from pathlib import Path

from google.api_core.client_options import ClientOptions
from google.cloud import documentai

from shared.config import Settings, get_settings
from services.parser.quality_gate import ParseMetrics

logger = logging.getLogger(__name__)


class FallbackParseError(RuntimeError):
    pass


def hwp_to_pdf(source_path: Path, out_dir: Path) -> Path:
    """LibreOffice 헤드리스로 PDF 변환."""
    soffice = shutil.which("soffice") or shutil.which("libreoffice")
    if not soffice:
        raise FallbackParseError("LibreOffice(soffice) not found in PATH")

    cmd = [
        soffice,
        "--headless",
        "--nologo",
        "--nofirststartwizard",
        "--convert-to",
        "pdf",
        "--outdir",
        str(out_dir),
        str(source_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if result.returncode != 0:
        raise FallbackParseError(
            f"LibreOffice convert failed: {result.stderr or result.stdout}"
        )

    pdf_path = out_dir / f"{source_path.stem}.pdf"
    if not pdf_path.exists():
        # 일부 환경에서 확장자 처리 차이
        candidates = list(out_dir.glob("*.pdf"))
        if not candidates:
            raise FallbackParseError("PDF output not found after conversion")
        pdf_path = candidates[0]
    return pdf_path


def parse_with_document_ai(
    pdf_bytes: bytes, settings: Settings | None = None
) -> tuple[str, ParseMetrics]:
    cfg = settings or get_settings()
    if not cfg.docai_processor_id:
        raise FallbackParseError("DOCAI_PROCESSOR_ID is not configured")

    opts = ClientOptions(
        api_endpoint=f"{cfg.docai_location}-documentai.googleapis.com"
    )
    client = documentai.DocumentProcessorServiceClient(client_options=opts)
    name = client.processor_path(
        cfg.gcp_project_id, cfg.docai_location, cfg.docai_processor_id
    )

    raw_document = documentai.RawDocument(
        content=pdf_bytes, mime_type="application/pdf"
    )
    request = documentai.ProcessRequest(name=name, raw_document=raw_document)
    result = client.process_document(request=request)
    document = result.document

    markdown = _document_to_markdown(document)
    metrics = ParseMetrics(
        text_length=len(markdown),
        source_bytes=len(pdf_bytes),
        table_count=_count_tables(document),
        warnings=["PDF_DOCAI_FALLBACK"],
    )
    return markdown, metrics


def fallback_parse(
    hwp_bytes: bytes, *, filename: str, settings: Settings | None = None
) -> tuple[str, ParseMetrics]:
    """HWP/HWPX → PDF → Document AI Layout Parser."""
    suffix = ".hwpx" if filename.lower().endswith(".hwpx") else ".hwp"
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        src = tmp_dir / f"input{suffix}"
        src.write_bytes(hwp_bytes)
        pdf_path = hwp_to_pdf(src, tmp_dir)
        pdf_bytes = pdf_path.read_bytes()
        return parse_with_document_ai(pdf_bytes, settings=settings)


def _document_to_markdown(document: documentai.Document) -> str:
    """Layout Parser 결과에서 텍스트·표를 마크다운으로 직렬화."""
    parts: list[str] = []

    # document_layout이 있으면 우선
    layout = getattr(document, "document_layout", None)
    blocks = getattr(layout, "blocks", None) if layout else None
    if blocks:
        for block in blocks:
            text_block = getattr(block, "text_block", None)
            if text_block and getattr(text_block, "text", None):
                parts.append(text_block.text)
            table_block = getattr(block, "table_block", None)
            if table_block:
                parts.append(_table_block_to_md(table_block))
        if parts:
            return "\n\n".join(p.strip() for p in parts if p and p.strip())

    # 폴백: 전체 텍스트
    return (document.text or "").strip()


def _table_block_to_md(table_block: object) -> str:
    body_rows = getattr(table_block, "body_rows", None) or []
    header_rows = getattr(table_block, "header_rows", None) or []
    all_rows = list(header_rows) + list(body_rows)
    if not all_rows:
        return ""

    lines: list[str] = []
    for i, row in enumerate(all_rows):
        cells = getattr(row, "cells", None) or []
        cell_texts: list[str] = []
        for cell in cells:
            blocks = getattr(cell, "blocks", None) or []
            texts = []
            for b in blocks:
                tb = getattr(b, "text_block", None)
                if tb and getattr(tb, "text", None):
                    texts.append(tb.text)
            cell_texts.append(" ".join(texts).replace("|", "\\|") or " ")
        lines.append("| " + " | ".join(cell_texts) + " |")
        if i == 0:
            lines.append("| " + " | ".join("---" for _ in cell_texts) + " |")
    return "\n".join(lines)


def _count_tables(document: documentai.Document) -> int:
    layout = getattr(document, "document_layout", None)
    blocks = getattr(layout, "blocks", None) if layout else None
    if not blocks:
        return 0
    return sum(1 for b in blocks if getattr(b, "table_block", None))