"""HWPX → Markdown (python-hwpx).

HWPX 는 ZIP+XML(OWPML) 이라 네이티브 확장 없이 읽힌다. rhwp 도 HWPX 를 지원하지만
PyO3 네이티브 휠이라 libfreetype ABI 에 묶이므로, HWPX 는 이 순수 파이썬 경로로 뺀다.
"""

from __future__ import annotations

import io
import logging

from services.parser.quality_gate import ParseMetrics
from services.parser.rhwp_parser import ParseOutput

logger = logging.getLogger(__name__)

ENGINE = "python-hwpx"

class _ManifestFallbackFilter(logging.Filter):
    """manifest 탐색 fallback 안내만 버린다.

    python-hwpx 는 파일마다 이 안내를 WARNING 으로 3건씩 찍어 배치 로그를 덮는다.
    라이브러리가 스스로 복구한 상황이라 파일 단위로 볼 가치가 없다. 같은 로거가 내는
    'container.xml 파싱 실패'·'파트 누락' 같은 실제 경고는 통과시킨다(무상태 = 스레드 안전).
    """

    _BENIGN = "fallback을 사용합니다"

    def filter(self, record: logging.LogRecord) -> bool:
        return self._BENIGN not in str(record.msg)


logging.getLogger("hwpx.opc.package").addFilter(_ManifestFallbackFilter())


def parse_hwpx_bytes(data: bytes, *, filename: str = "doc.hwpx") -> ParseOutput:
    """HWPX 바이트 → GFM Markdown."""
    import hwpx

    warnings: list[str] = []
    doc = hwpx.HwpxDocument.open(io.BytesIO(data))
    markdown = doc.export_markdown()

    table_count, cells_total = _table_metrics(doc, warnings)

    metrics = ParseMetrics(
        text_length=len(markdown),
        source_bytes=len(data),
        table_count=table_count,
        # python-hwpx 는 셀 단위 추출 실패 신호를 주지 않는다 — 총계만 채우고
        # failures 는 0 으로 둔다(= G2 는 이 엔진에서 발동하지 않음).
        table_cells_total=cells_total,
        warnings=warnings,
    )
    return ParseOutput(markdown=markdown, metrics=metrics, engine=ENGINE)


def hwpx_available() -> bool:
    try:
        import hwpx  # noqa: F401

        return True
    except ImportError:
        return False


def _table_metrics(doc: object, warnings: list[str]) -> tuple[int, int]:
    """(표 개수, 셀 총수). 실패해도 파싱 자체는 살린다."""
    try:
        get_table_map = getattr(doc, "get_table_map", None)
        if not callable(get_table_map):
            return 0, 0
        tables = (get_table_map() or {}).get("tables") or []
        cells = 0
        for t in tables:
            explicit = t.get("cells")
            if explicit:
                cells += len(explicit)
            else:
                cells += int(t.get("rows") or 0) * int(t.get("cols") or 0)
        return len(tables), cells
    except Exception as exc:  # noqa: BLE001
        logger.warning("hwpx table map failed: %s", exc)
        warnings.append(f"TABLE_MAP_FAIL:{exc}")
        return 0, 0
