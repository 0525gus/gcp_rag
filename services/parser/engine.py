"""파싱 엔진 디스패처 — 포맷별로 엔진을 고른다.

  .hwpx → python-hwpx (순수 파이썬)
  .hwp  → rhwp        (PyO3 네이티브)

HWP 와 HWPX 는 이름만 비슷할 뿐 다른 포맷이다. HWPX 는 ZIP+XML 이라 네이티브
확장 없이 읽히므로, rhwp 의 네이티브 ABI 리스크에서 분리한다.
"""

from __future__ import annotations

import logging

from services.parser.hwpx_parser import hwpx_available, parse_hwpx_bytes
from services.parser.rhwp_parser import ParseOutput, parse_hwp_bytes, rhwp_available

logger = logging.getLogger(__name__)


def is_hwpx_filename(filename: str) -> bool:
    return filename.lower().endswith(".hwpx")


def parse_document_bytes(data: bytes, *, filename: str = "doc.hwp") -> ParseOutput:
    """확장자로 엔진을 고른다. HWPX 엔진이 없으면 rhwp 로 내려간다."""
    if is_hwpx_filename(filename):
        if hwpx_available():
            return parse_hwpx_bytes(data, filename=filename)
        logger.warning("python-hwpx unavailable — falling back to rhwp for %s", filename)
    return parse_hwp_bytes(data, filename=filename)


def engine_status() -> dict[str, str]:
    """/health 용 엔진 가용성."""
    return {
        "hwpx": "ok" if hwpx_available() else "missing",
        "hwp": "ok" if rhwp_available() else "missing",
    }


def can_parse(filename: str) -> bool:
    """해당 파일을 처리할 엔진이 하나라도 있는가."""
    if is_hwpx_filename(filename):
        return hwpx_available() or rhwp_available()
    return rhwp_available()
