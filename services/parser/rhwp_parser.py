"""HWP/HWPX → Markdown (rhwp-python 전용)."""

from __future__ import annotations

import logging
import tempfile
from dataclasses import dataclass
from pathlib import Path

from services.parser.quality_gate import ParseMetrics, count_markdown_tables

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
    warnings: list[str] = []

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
    table_count = _count_tables_in_ir(ir)
    if table_count is None:
        # IR 에서 못 세면 표 손실을 판정할 기준이 없다. 마크다운에 남은 수를 그대로
        # 기준으로 삼아 G2 를 무력화한다 — 근거 없이 실패로 몰지 않는다.
        table_count = count_markdown_tables(markdown)
        warnings.append("TABLE_COUNT_UNAVAILABLE")

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


def _count_tables_in_ir(ir: object) -> int | None:
    """문서 구조가 말하는 표 개수. 셀 수 없으면 None.

    마크다운을 세지 않는다 — 이 값은 '마크다운에 몇 개가 살아남았나'를 재는
    기준값이므로, 마크다운에서 유도하면 손실이 항상 0 이 되어 판정이 무의미해진다.
    """
    try:
        # HwpDocument 는 .blocks 가 아니라 .body/.furniture 구조 —
        # iter_blocks(scope="all") 가 표를 세는 정식 경로 (중첩 표도 포함).
        from rhwp.ir.nodes import TableBlock  # noqa: PLC0415

        iter_blocks = getattr(ir, "iter_blocks", None)
        if not callable(iter_blocks):
            return None
        return sum(1 for b in iter_blocks(scope="all") if isinstance(b, TableBlock))
    except Exception as exc:  # noqa: BLE001
        logger.warning("table count from IR failed: %s", exc)
        return None
