"""HWPX → Markdown (python-hwpx).

HWPX 는 ZIP+XML(OWPML) 이라 네이티브 확장 없이 읽힘. 
PyO3 네이티브 휠이라 libfreetype ABI 에 묶여 있어 HWPX 는 순수 파이썬 경로로 뺌.
"""

from __future__ import annotations

import io
import logging
import re
import unicodedata
from html.parser import HTMLParser

from services.parser.quality_gate import ParseMetrics, count_markdown_tables
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
    markdown, lost, repaired = _repair_tables(doc, markdown)
    if lost:
        warnings.append(f"HWPX_CELLS_RECOVERED:{lost}")
    if repaired:
        warnings.append(f"HWPX_TABLES_REPAIRED:{repaired}")

    table_count = _table_count(doc, warnings)
    if table_count is None:
        # 기준값이 없으면 손실을 판정할 수 없다 — 마크다운 개수를 그대로 써서
        # G2 를 무력화한다(근거 없이 실패로 몰지 않는다).
        table_count = count_markdown_tables(markdown)
        warnings.append("TABLE_COUNT_UNAVAILABLE")

    metrics = ParseMetrics(
        text_length=len(markdown),
        source_bytes=len(data),
        table_count=table_count,
        warnings=warnings,
    )
    return ParseOutput(markdown=markdown, metrics=metrics, engine=ENGINE)


def hwpx_available() -> bool:
    try:
        import hwpx  # noqa: F401

        return True
    except ImportError:
        return False


_HTML_TABLE = re.compile(r"(?is)<table[^>]*>.*?</table>")
# 사제 글꼴 글머리표(PUA)와 마크다운 강조 기호 — 두 exporter 가 서로 다르게
# 처리하므로 '내용이 살아 있나'를 볼 때는 지우고 본다.
_PUA = re.compile("[\ue000-\uf8ff]")
_MARKS = frozenset("*~`#" + chr(92))


def _canon(text: str) -> str:
    text = unicodedata.normalize("NFC", text or "")
    text = _PUA.sub("", text)
    text = "".join(ch for ch in text if ch not in _MARKS)
    return re.sub(r"\s+", "", text)


class _HtmlTableGrid(HTMLParser):
    """``<table>`` 를 1x1 격자로 편다 (rhwp 쪽 _normalize_tables 와 같은 규칙)."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.cells: dict[tuple[int, int], str] = {}
        self._row = -1
        self._col = 0
        self._buf: list[str] | None = None
        self._span = (1, 1)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "tr":
            self._row += 1
            self._col = 0
        elif tag in ("td", "th"):
            self._buf = []
            attr = dict(attrs)
            self._span = (_span(attr.get("rowspan")), _span(attr.get("colspan")))

    def handle_endtag(self, tag: str) -> None:
        if tag in ("td", "th") and self._buf is not None:
            text = " ".join("".join(self._buf).split())
            while (self._row, self._col) in self.cells:
                self._col += 1
            row_span, col_span = self._span
            for dr in range(row_span):
                for dc in range(col_span):
                    # rowspan 은 값을 세로로 복제, colspan 은 첫 칸에만
                    self.cells.setdefault(
                        (self._row + dr, self._col + dc), text if dc == 0 else ""
                    )
            self._col += col_span
            self._buf = None

    def handle_data(self, data: str) -> None:
        if self._buf is not None:
            self._buf.append(data)


def _span(value: str | None) -> int:
    try:
        return max(1, int(value or 1))
    except (TypeError, ValueError):
        return 1


def _html_table_to_gfm(html: str) -> str:
    parser = _HtmlTableGrid()
    parser.feed(html)
    if not parser.cells:
        return html
    rows = max(r for r, _ in parser.cells) + 1
    cols = max(c for _, c in parser.cells) + 1
    grid = [
        [parser.cells.get((r, c), "").replace("|", "\\|") for c in range(cols)]
        for r in range(rows)
    ]
    lines = [
        "| " + " | ".join(grid[0]) + " |",
        "| " + " | ".join("---" for _ in range(cols)) + " |",
    ]
    lines += ["| " + " | ".join(row) + " |" for row in grid[1:]]
    return "\n".join(lines)


def _gfm_blocks(markdown: str) -> list[list[str]]:
    """연속한 ``|`` 줄 뭉치 = GFM 표 하나."""
    lines = markdown.splitlines()
    blocks: list[list[str]] = []
    i = 0
    while i < len(lines):
        if lines[i].lstrip().startswith("|"):
            j = i
            while j < len(lines) and lines[j].lstrip().startswith("|"):
                j += 1
            blocks.append(lines[i:j])
            i = j
        else:
            i += 1
    return blocks


def _broken_tables(markdown: str) -> int:
    """구조가 깨진 GFM 표 수 — 열 수가 들쭉날쭉하거나 구분행이 없는 것."""
    return sum(1 for block in _gfm_blocks(markdown) if _is_broken(block))


def _is_broken(block: list[str]) -> bool:
    widths = {line.count("|") for line in block}
    return len(widths) != 1 or not any("---" in line for line in block)


def _rejoin_split_rows(markdown: str) -> str:
    """셀 안 개행 때문에 여러 줄로 찢어진 표 행을 도로 붙인다.

    python-hwpx 는 셀 텍스트의 개행을 이스케이프하지 않아
    ``| 줄1`` / ``줄2 | 값 |`` 처럼 한 행이 두 줄로 나간다. 구분행의 ``|`` 개수를
    기대 열 수로 삼아, 모자란 줄을 다음 줄과 공백으로 이어 붙인다.

    구분행이 없거나 멀쩡한 표는 건드리지 않는다.
    """
    lines = markdown.splitlines()
    out: list[str] = []
    i = 0
    changed = False

    while i < len(lines):
        if not lines[i].lstrip().startswith("|"):
            out.append(lines[i])
            i += 1
            continue

        # 기대 열 수는 구분행이 정한다. 빈 줄 전까지만 찾는다.
        width = 0
        for line in lines[i:]:
            if not line.strip():
                break
            if "|" in line and "---" in line:
                width = line.count("|")
                break
        if width < 2:  # 구분행이 없으면 추측하지 않는다
            out.append(lines[i])
            i += 1
            continue

        merged: list[str] = []
        buf = ""
        while i < len(lines):
            line = lines[i]
            # 행이 완결된 상태에서 '|' 로 시작하지 않으면 표가 끝난 것
            if not buf and not line.lstrip().startswith("|"):
                break
            # 빈 줄은 어떤 경우에도 표를 끝낸다 (덜 채워졌어도 삼키지 않는다)
            if not line.strip():
                break
            buf = f"{buf} {line.strip()}".strip() if buf else line
            i += 1
            if buf.count("|") >= width:
                merged.append(buf)
                buf = ""
        if buf:
            merged.append(buf)
        if any("\n" not in m and m not in lines for m in merged):
            changed = True
        out.extend(merged)

    return "\n".join(out) if changed else markdown


def _repair_tables(doc: object, markdown: str) -> tuple[str, int, int]:
    """HWPX 표의 셀 소실·구조 파손을 고친다. (마크다운, 되살린 셀, 고친 표)

    python-hwpx 의 ``export_markdown`` 에는 표를 망가뜨리는 두 결함이 있다
    (hwpx/tools/exporter.py).

    1. **셀 소실** — 표를 GFM 으로 내면서 첫 행 길이로 뒤 행을 자르고
       (``padded[: len(header)]``), ``_table_cells_text`` 가 rowspan/colspan 을
       무시한다. 첫 행이 가로 병합된 표는 헤더가 1칸으로 잡혀 **나머지 열이
       통째로 사라진다** (3x3 표 A1:C1 병합 → 4셀 소실).
    2. **구조 파손** — 셀 안 개행을 이스케이프하지 않는다
       (``"\\n".join(cell_parts)``). 여러 줄 셀이 있으면 표 한 행이 여러 줄로
       찢어져 GFM 표가 조각난다. 실측한 실제 문서에서 **표 2개가 6조각**이 났고
       6조각 전부 열 수가 어긋나 있었다.

    둘 다 표 개수는 맞아서 품질 게이트가 통과시킨다(= 조용한 손상).

    판정 기준은 두 가지다.

      - 셀 소실: ``export_text()``(헤더 절단을 안 한다)에 있는데 마크다운에 없는 셀
      - 구조 파손: 한 표 안에서 열 수가 불균일하거나 구분행이 없음

    **둘 다 없으면 원본 마크다운을 그대로 돌려준다** — 멀쩡한 문서의 출력은
    한 글자도 바뀌지 않는다. 하나라도 있으면 ``export_rich_markdown``(병합을
    rowspan/colspan 속성으로 보존)으로 다시 뽑아 HTML 표를 격자 GFM 으로 편다.
    이 경로는 셀 텍스트를 한 줄로 접으므로 2번도 함께 낫는다.

    대체 결과가 어느 지표로도 나아지지 않거나 하나라도 나빠지면 원본을
    유지한다 — 개악을 만들지 않는다.

    실패해도 파싱은 살린다.
    """
    try:
        tokens = [t for t in re.split(r"[\t\n]+", doc.export_text()) if t.strip()]

        def score(md: str) -> tuple[int, int]:
            canon = _canon(md)
            lost = sum(1 for t in tokens if _canon(t) and _canon(t) not in canon)
            return lost, _broken_tables(md)

        base_lost, base_broken = score(markdown)

        # 1단계: 찢어진 행 재조립. rich 를 부르지 않고 끝나는 경우가 많다.
        joined = _rejoin_split_rows(markdown)
        best, (best_lost, best_broken) = joined, score(joined)
        if (best_lost, best_broken) > (base_lost, base_broken):
            best, best_lost, best_broken = markdown, base_lost, base_broken
        if not best_lost and not best_broken:
            return best, base_lost - best_lost, base_broken - best_broken

        # 2단계: 병합 셀 소실은 rich exporter 로만 되살릴 수 있다.
        rich = doc.export_rich_markdown()
        fixed = _rejoin_split_rows(
            _HTML_TABLE.sub(lambda m: _html_table_to_gfm(m.group(0)), rich)
        )
        fixed_lost, fixed_broken = score(fixed)
        if (fixed_lost, fixed_broken) < (best_lost, best_broken):
            best, best_lost, best_broken = fixed, fixed_lost, fixed_broken

        if (best_lost, best_broken) >= (base_lost, base_broken):
            logger.warning(
                "hwpx table repair rejected (missing %s->%s, broken %s->%s)",
                base_lost, best_lost, base_broken, best_broken,
            )
            return markdown, 0, 0
        return best, base_lost - best_lost, base_broken - best_broken
    except Exception as exc:  # noqa: BLE001
        logger.warning("hwpx table repair failed: %s", exc)
        return markdown, 0, 0


def _table_count(doc: object, warnings: list[str]) -> int | None:
    """OWPML 구조가 말하는 표 개수. 셀 수 없으면 None (파싱 자체는 살린다)."""
    try:
        get_table_map = getattr(doc, "get_table_map", None)
        if not callable(get_table_map):
            return None
        return len((get_table_map() or {}).get("tables") or [])
    except Exception as exc:  # noqa: BLE001
        logger.warning("hwpx table map failed: %s", exc)
        warnings.append(f"TABLE_MAP_FAIL:{exc}")
        return None
