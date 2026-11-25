from __future__ import annotations

import logging

from services.parser.hwpx_parser import hwpx_available, parse_hwpx_bytes
from services.parser.rhwp_parser import ParseOutput, parse_hwp_bytes, rhwp_available

logger = logging.getLogger(__name__)


def is_hwpx_filename(filename: str) -> bool:
    return filename.lower().endswith(".hwpx")


def parse_document_bytes(data: bytes, *, filename: str = "doc.hwp") -> ParseOutput:
    if is_hwpx_filename(filename):
        if hwpx_available():
            return parse_hwpx_bytes(data, filename=filename)
        logger.warning("python-hwpx unavailable — falling back to rhwp for %s", filename)
    return parse_hwp_bytes(data, filename=filename)


def engine_status() -> dict[str, str]:
    return {
        "hwpx": "ok" if hwpx_available() else "missing",
        "hwp": "ok" if rhwp_available() else "missing",
    }


def can_parse(filename: str) -> bool:
    if is_hwpx_filename(filename):
        return hwpx_available() or rhwp_available()
    return rhwp_available()
