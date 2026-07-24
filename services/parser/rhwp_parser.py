"""HWP/HWPX → Markdown (rhwp-python 전용)."""

from __future__ import annotations

import logging
import tempfile
from dataclasses import dataclass
from pathlib import Path

from services.parser.quality_gate import ParseMetrics

logger = logging.getLogger(__name__)


@dataclass
class ParseOutput:
    markdown: str
    metrics: ParseMetrics
    engine: str = "rhwp"


def parse_hwp_bytes(data: bytes, *, filename: str = "doc.hwp") -> ParseOutput:
    """바이트 → GFM Markdown (rhwp-python)."""
    import rhwp

    suffix = ".hwpx" if filename.lower().endswith(".hwpx") else ".hwp"
    warnings: list[str] = ["RHWP"]

    # from_bytes 우선, 없으면 임시 파일
    doc = None
    if hasattr(rhwp, "Document") and hasattr(rhwp.Document, "from_bytes"):
        try:
            doc = rhwp.Document.from_bytes(data)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Document.from_bytes failed: %s — try parse(path)", exc)
            warnings.append(f"FROM_BYTES_FAIL:{exc}")

    if doc is None:
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp.write(data)
            tmp_path = tmp.name
        try:
            doc = rhwp.parse(tmp_path)
        finally:
            Path(tmp_path).unlink(missing_ok=True)

    ir = doc.to_ir()
    markdown = ir.to_markdown()
    table_count = _count_tables(ir, markdown)

    metrics = ParseMetrics(
        text_length=len(markdown),
        source_bytes=len(data),
        table_count=table_count,
        warnings=warnings,
    )
    return ParseOutput(markdown=markdown, metrics=metrics, engine="rhwp")


def rhwp_available() -> bool:
    try:
        import rhwp  # noqa: F401

        return True
    except ImportError:
        return False


def _count_tables(ir: object, markdown: str) -> int:
    count = 0
    try:
        blocks = getattr(ir, "blocks", None) or []
        for block in blocks:
            name = type(block).__name__.lower()
            kind = str(getattr(block, "kind", "") or getattr(block, "type", "")).lower()
            if "table" in name or "table" in kind:
                count += 1
    except Exception:  # noqa: BLE001
        pass
    if count == 0:
        count = markdown.lower().count("<table")
    if count == 0:
        lines = markdown.splitlines()
        count = sum(
            1
            for i, line in enumerate(lines[:-1])
            if line.strip().startswith("|") and "---" in lines[i + 1]
        )
    return count
