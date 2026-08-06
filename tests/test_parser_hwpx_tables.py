"""HWPX 표 복구 계약 (셀 소실 + 구조 파손).

python-hwpx 의 ``export_markdown`` 에는 표를 망가뜨리는 결함이 둘 있다
(hwpx/tools/exporter.py).

  1. **셀 소실** — 첫 행 길이로 뒤 행을 자르고(``padded[: len(header)]``),
     ``_table_cells_text`` 가 rowspan/colspan 을 무시한다. 첫 행이 가로 병합된
     표는 헤더가 1칸으로 잡혀 나머지 열이 통째로 사라진다.
  2. **구조 파손** — 셀 안 개행을 이스케이프하지 않아 한 행이 여러 줄로 찢어진다.
     실측한 실제 문서에서 표 2개가 GFM 블록 6조각으로 났고 전부 열 수가 어긋났다.

둘 다 표 개수는 맞아서 품질 게이트가 통과시킨다(= 조용한 손상).

여기서 고정하는 계약.

  1. 소실도 파손도 없는 문서의 출력은 **한 글자도 바뀌지 않는다** (회귀 방지).
  2. 찢어진 행은 구분행의 열 수를 기준으로 도로 붙인다 (rich 호출 없이).
  3. 병합 셀 소실은 rich exporter 로 되살리고, HTML 표는 격자 GFM 으로 편다.
  4. 어느 지표로도 나아지지 않으면 원본을 유지한다 (개악 금지).

격자 펼침 규칙은 rhwp 쪽 `_normalize_tables` 와 같다 —
rowspan 은 세로 복제, colspan 은 첫 칸만.
"""

from __future__ import annotations

import io
import re

import pytest

from services.parser.hwpx_parser import (
    _broken_tables,
    _canon,
    _gfm_blocks,
    _html_table_to_gfm,
    _rejoin_split_rows,
    _repair_tables,
)

HTML_TABLE = re.compile(r"(?is)<table[^>]*>.*?</table>")


# --- 격자 펼침 (hwpx 불필요) ---------------------------------------------


def test_colspan_fills_row_width_without_duplicating_value() -> None:
    gfm = _html_table_to_gfm(
        '<table><tr><td colspan="3">제목</td></tr>'
        "<tr><td>A</td><td>B</td><td>C</td></tr></table>"
    )
    lines = gfm.splitlines()
    assert lines[0] == "| 제목 |  |  |"
    assert lines[1] == "| --- | --- | --- |"
    assert lines[2] == "| A | B | C |"
    assert gfm.count("제목") == 1


def test_rowspan_repeats_value_down_each_row() -> None:
    gfm = _html_table_to_gfm(
        '<table><tr><td rowspan="2">분류</td><td>가</td></tr>'
        "<tr><td>나</td></tr></table>"
    )
    rows = [line for line in gfm.splitlines() if "---" not in line]
    assert rows == ["| 분류 | 가 |", "| 분류 | 나 |"]


def test_pipe_in_cell_is_escaped() -> None:
    gfm = _html_table_to_gfm("<table><tr><td>a|b</td></tr></table>")
    assert r"a\|b" in gfm


def test_malformed_table_returns_input_untouched() -> None:
    assert _html_table_to_gfm("<table></table>") == "<table></table>"


# --- 복구 판정 (hwpx 불필요, 더미 문서) ----------------------------------


class _Doc:
    def __init__(self, text: str, markdown: str, rich: str) -> None:
        self._text, self._md, self._rich = text, markdown, rich

    def export_text(self) -> str:
        return self._text

    def export_markdown(self) -> str:
        return self._md

    def export_rich_markdown(self) -> str:
        return self._rich


def test_healthy_document_is_returned_unchanged() -> None:
    md = "| A | B |\n| --- | --- |\n"
    doc = _Doc("A\tB", md, "<table><tr><td>A</td><td>B</td></tr></table>")
    out, lost, repaired = _repair_tables(doc, md)
    assert out is md  # 같은 객체 — 멀쩡한 문서는 손대지 않는다
    assert (lost, repaired) == (0, 0)


def test_lost_cells_are_recovered_from_rich_export() -> None:
    truncated = "| 제목 |\n| --- |\n| A2 |\n"
    doc = _Doc(
        "제목\nA2\tB2",
        truncated,
        '<table><tr><td colspan="2">제목</td></tr>'
        "<tr><td>A2</td><td>B2</td></tr></table>",
    )
    out, lost, _ = _repair_tables(doc, truncated)
    assert lost == 1
    assert "B2" in out
    assert not HTML_TABLE.search(out)  # HTML 표는 남기지 않는다


def test_split_rows_are_rejoined_without_calling_rich() -> None:
    """찢어진 행은 1단계에서 붙는다 — rich 를 부르면 안 된다."""

    class _NoRich(_Doc):
        def export_rich_markdown(self):
            raise AssertionError("rich exporter should not be needed")

    broken = "| 머리1 | 머리2 |\n| --- | --- |\n| 줄1\n줄2 | 값 |\n"
    doc = _NoRich("머리1\t머리2\n줄1\n줄2\t값", broken, "")
    out, _lost, repaired = _repair_tables(doc, broken)
    assert repaired == 1
    assert _broken_tables(out) == 0
    assert len(_gfm_blocks(out)) == 1
    assert "| 줄1 줄2 | 값 |" in out


def test_repair_that_does_not_help_keeps_the_original() -> None:
    """어느 지표로도 나아지지 않으면 원본 유지 — 개악을 만들지 않는다."""
    md = "| A |\n| --- |\n"
    doc = _Doc("A\t사라진값", md, "<table><tr><td>A</td></tr></table>")
    out, *counts = _repair_tables(doc, md)
    assert out is md
    assert counts == [0, 0]


def test_repair_never_breaks_parsing() -> None:
    class Exploding:
        def export_text(self):
            raise RuntimeError("boom")

    md = "본문"
    assert _repair_tables(Exploding(), md) == (md, 0, 0)


# --- 찢어진 행 재조립 (순수 함수) ----------------------------------------


def test_rejoin_leaves_healthy_tables_untouched() -> None:
    md = "| A | B |\n| --- | --- |\n| 1 | 2 |\n"
    assert _rejoin_split_rows(md) is md


def test_rejoin_needs_a_separator_row_to_know_the_width() -> None:
    """구분행이 없으면 기대 열 수를 모른다 — 추측하지 않는다."""
    md = "| A | B |\n| 1 |\n"
    assert _rejoin_split_rows(md) is md


def test_rejoin_handles_three_way_split() -> None:
    md = "| A | B |\n| --- | --- |\n| 가\n나\n다 | 값 |\n"
    out = _rejoin_split_rows(md)
    assert "| 가 나 다 | 값 |" in out
    assert _broken_tables(out) == 0


def test_broken_table_detection() -> None:
    assert _broken_tables("| A | B |\n| --- | --- |\n| 1 | 2 |\n") == 0
    assert _broken_tables("| A | B |\n| --- | --- |\n| 1 |\n") == 1
    assert _broken_tables("| A | B |\n| 1 | 2 |\n") == 1  # 구분행 없음
    assert _broken_tables("본문만 있고 표는 없다") == 0


def test_canon_ignores_pua_bullets_and_emphasis() -> None:
    """두 exporter 가 글머리표·강조를 다르게 내므로 비교 시 지운다."""
    assert _canon(" (일시) 2026") == _canon("**(일시)** 2026")


# --- 실제 라이브러리 왕복 (hwpx 필요) ------------------------------------


hwpx = pytest.importorskip("hwpx", reason="python-hwpx not installed")


def _doc_with_table(merge: str | None, rows: int = 3, cols: int = 3):
    doc = hwpx.HwpxDocument.new()
    doc.add_paragraph("머리 문단")
    table = doc.add_table(rows=rows, cols=cols)
    for r in range(rows):
        for c in range(cols):
            table.cell(r, c).text = f"{chr(65 + c)}{r + 1}"
    if merge:
        doc.merge_table_cells(table, merge)
    return doc


def _cells(doc) -> list[str]:
    return [t for t in re.split(r"[\t\n]+", doc.export_text()) if t.strip()]


def test_merged_header_row_loses_cells_without_recovery() -> None:
    """회귀 감시용 기준 — 라이브러리가 고쳐지면 이 테스트가 먼저 깨진다."""
    doc = _doc_with_table("A1:C1")
    md = _canon(doc.export_markdown())
    lost = [t for t in _cells(doc) if _canon(t) and _canon(t) not in md]
    assert lost, "python-hwpx 가 더 이상 셀을 버리지 않는다면 복구 경로를 재검토할 것"


@pytest.mark.parametrize("merge", ["A1:C1", "A1:D1"])
def test_parse_recovers_every_cell_for_merged_header(merge: str) -> None:
    from services.parser.hwpx_parser import parse_hwpx_bytes  # noqa: PLC0415

    cols = 3 if merge == "A1:C1" else 4
    doc = _doc_with_table(merge, cols=cols)
    expected = _cells(doc)
    out = parse_hwpx_bytes(doc.to_bytes(), filename="t.hwpx")

    body = _canon(out.markdown)
    assert [t for t in expected if _canon(t) and _canon(t) not in body] == []
    assert any(w.startswith("HWPX_CELLS_RECOVERED:") for w in out.metrics.warnings)
    assert not HTML_TABLE.search(out.markdown)
    assert _broken_tables(out.markdown) == 0


def test_parse_repairs_rows_split_by_cell_newlines() -> None:
    from services.parser.hwpx_parser import parse_hwpx_bytes  # noqa: PLC0415

    doc = hwpx.HwpxDocument.new()
    doc.add_paragraph("머리")
    table = doc.add_table(rows=2, cols=2)
    table.cell(0, 0).text = "머리1"
    table.cell(0, 1).text = "머리2"
    table.cell(1, 0).text = "줄1\n줄2"
    table.cell(1, 1).text = "값"
    data = doc.to_bytes()

    baseline = hwpx.HwpxDocument.open(io.BytesIO(data)).export_markdown()
    assert _broken_tables(baseline) > 0  # 회귀 감시용 기준

    out = parse_hwpx_bytes(data, filename="t.hwpx")
    assert _broken_tables(out.markdown) == 0
    assert len(_gfm_blocks(out.markdown)) == 1
    assert any(w.startswith("HWPX_TABLES_REPAIRED:") for w in out.metrics.warnings)


@pytest.mark.parametrize("merge", [None, "A2:B2", "A1:A2", "B2:C3"])
def test_healthy_documents_are_byte_identical(merge: str | None) -> None:
    """소실도 파손도 없는 문서는 출력이 그대로여야 한다 — 회귀 위험 0 조건."""
    from services.parser.hwpx_parser import parse_hwpx_bytes  # noqa: PLC0415

    doc = _doc_with_table(merge)
    data = doc.to_bytes()
    baseline = hwpx.HwpxDocument.open(io.BytesIO(data)).export_markdown()
    assert _broken_tables(baseline) == 0  # 전제 확인
    out = parse_hwpx_bytes(data, filename="t.hwpx")

    assert out.markdown == baseline
    assert out.metrics.warnings == []
