from __future__ import annotations

import pytest

rhwp_nodes = pytest.importorskip("rhwp.ir.nodes", reason="rhwp-python not installed")

from rhwp.ir.nodes import (  # noqa: E402
    HwpDocument,
    ParagraphBlock,
    Provenance,
    TableBlock,
    TableCell,
)

from services.parser.cleanup import cleanup_markdown  # noqa: E402
from services.parser.quality_gate import count_markdown_tables  # noqa: E402
from services.parser.rhwp_parser import (  # noqa: E402
    _count_tables_in_ir,
    _normalize_tables,
)

PROV = Provenance(section_idx=0, para_idx=0)


def para(text: str) -> ParagraphBlock:
    return ParagraphBlock(text=text, prov=PROV)


def cell(row: int, col: int, *blocks, row_span: int = 1, col_span: int = 1) -> TableCell:
    return TableCell(
        row=row,
        col=col,
        row_span=row_span,
        col_span=col_span,
        grid_index=row * 10 + col,
        blocks=list(blocks),
    )


def table(rows: int, cols: int, *cells: TableCell, html: str = "") -> TableBlock:
    return TableBlock(rows=rows, cols=cols, cells=list(cells), html=html, prov=PROV)


def doc(*body) -> HwpDocument:
    return HwpDocument(body=list(body))


def test_nested_table_text_survives_into_markdown() -> None:
    inner = table(1, 2, cell(0, 0, para("담당")), cell(0, 1, para("이영원")))
    outer = table(1, 1, cell(0, 0, inner))

    before = cleanup_markdown(doc(outer).to_markdown())
    assert "이영원" not in before

    normalized, folded, _ = _normalize_tables(doc(outer))
    assert folded == 1
    after = cleanup_markdown(normalized.to_markdown())
    assert "담당" in after
    assert "이영원" in after


def test_deeply_nested_text_is_recovered() -> None:
    deepest = table(1, 1, cell(0, 0, para("3단")))
    middle = table(1, 1, cell(0, 0, deepest))
    outer = table(1, 1, cell(0, 0, middle))

    normalized, folded, _ = _normalize_tables(doc(outer))
    assert folded == 1
    assert "3단" in cleanup_markdown(normalized.to_markdown())


def test_normalization_is_noop_on_plain_tables() -> None:
    plain = table(1, 2, cell(0, 0, para("가")), cell(0, 1, para("나")))
    ir = doc(plain, para("본문"))
    normalized, folded, expanded = _normalize_tables(ir)
    assert (folded, expanded) == (0, 0)
    assert normalized is ir
    assert normalized.to_markdown() == ir.to_markdown()


def test_normalization_never_breaks_parsing() -> None:
    class Exploding:
        @property
        def body(self):
            raise RuntimeError("boom")

    broken = Exploding()
    out, folded, expanded = _normalize_tables(broken)
    assert out is broken
    assert (folded, expanded) == (0, 0)


def test_merged_table_renders_as_gfm_not_inline_html() -> None:
    merged = table(
        2,
        2,
        cell(0, 0, para("제목"), col_span=2),
        cell(1, 0, para("가")),
        cell(1, 1, para("나")),
        html='<table><tr><td colspan="2">제목</td></tr></table>',
    )

    before = cleanup_markdown(doc(merged).to_markdown())
    assert "<table" in before

    normalized, _, expanded = _normalize_tables(doc(merged))
    assert expanded == 1
    md = cleanup_markdown(normalized.to_markdown())
    assert "<table" not in md
    assert count_markdown_tables(md) == 1
    assert "제목" in md and "가" in md and "나" in md


def test_rowspan_value_repeats_down_so_each_row_stands_alone() -> None:
    merged = table(
        2,
        2,
        cell(0, 0, para("교육실적"), row_span=2),
        cell(0, 1, para("강의")),
        cell(1, 1, para("연구")),
    )
    normalized, _, expanded = _normalize_tables(doc(merged))
    assert expanded == 1
    rows = [
        line
        for line in cleanup_markdown(normalized.to_markdown()).splitlines()
        if line.startswith("|") and "---" not in line
    ]
    assert sum(1 for line in rows if "교육실적" in line) == 2


def test_colspan_value_is_not_duplicated_within_a_row() -> None:
    merged = table(1, 3, cell(0, 0, para("공고"), col_span=3))
    normalized, _, _ = _normalize_tables(doc(merged))
    md = cleanup_markdown(normalized.to_markdown())
    assert md.count("공고") == 1


def test_expansion_recomputes_rows_cols_so_no_cell_is_dropped() -> None:
    understated = table(
        1,
        1,
        cell(0, 0, para("가"), row_span=2, col_span=2),
        cell(0, 2, para("나")),
    )
    normalized, _, _ = _normalize_tables(doc(understated))
    block = normalized.body[0]
    assert (block.rows, block.cols) == (2, 2)
    assert len(block.cells) == 4
    md = cleanup_markdown(normalized.to_markdown())
    assert "가" in md and "나" in md


def test_normalization_is_idempotent() -> None:
    merged = table(
        2,
        2,
        cell(0, 0, para("분류"), row_span=2),
        cell(0, 1, para("가")),
        cell(1, 1, para("나")),
    )
    once, _, _ = _normalize_tables(doc(merged))
    twice, folded, expanded = _normalize_tables(once)
    assert (folded, expanded) == (0, 0)
    assert twice.to_markdown() == once.to_markdown()


def test_expanded_grid_has_uniform_column_count() -> None:
    merged = table(
        2,
        3,
        cell(0, 0, para("머리"), col_span=3),
        cell(1, 0, para("값")),
    )
    normalized, _, _ = _normalize_tables(doc(merged))
    body_rows = [
        line
        for line in cleanup_markdown(normalized.to_markdown()).splitlines()
        if line.startswith("|")
    ]
    widths = {line.count("|") for line in body_rows}
    assert len(widths) == 1


def test_fully_empty_columns_are_dropped() -> None:
    t = table(
        2,
        3,
        cell(0, 0, para("값1")),
        cell(0, 1),
        cell(0, 2, para("값2")),
        cell(1, 0, para("값3")),
        cell(1, 1),
        cell(1, 2, para("값4")),
    )
    normalized, _, _ = _normalize_tables(doc(t))
    block = normalized.body[0]
    assert (block.rows, block.cols) == (2, 2)
    md = cleanup_markdown(normalized.to_markdown())
    for value in ("값1", "값2", "값3", "값4"):
        assert value in md


def test_fully_empty_rows_are_dropped() -> None:
    t = table(
        3,
        2,
        cell(0, 0, para("머리")),
        cell(0, 1, para("값")),
        cell(1, 0),
        cell(1, 1),
        cell(2, 0, para("끝")),
        cell(2, 1, para("값2")),
    )
    normalized, _, _ = _normalize_tables(doc(t))
    assert normalized.body[0].rows == 2
    rows = [
        line
        for line in cleanup_markdown(normalized.to_markdown()).splitlines()
        if line.startswith("|") and "---" not in line
    ]
    assert len(rows) == 2


def test_rows_with_content_are_never_dropped() -> None:
    t = table(2, 2, cell(0, 0, para("a")), cell(0, 1), cell(1, 0), cell(1, 1, para("b")))
    normalized, _, _ = _normalize_tables(doc(t))
    block = normalized.body[0]
    assert (block.rows, block.cols) == (2, 2)


def test_empty_table_is_removed_and_stops_counting() -> None:
    blank = table(2, 2, cell(0, 0), cell(0, 1), cell(1, 0), cell(1, 1))
    real = table(1, 1, cell(0, 0, para("내용")))
    ir = doc(blank, real)

    normalized, _, _ = _normalize_tables(ir)
    md = cleanup_markdown(normalized.to_markdown())
    assert _count_tables_in_ir(normalized) == count_markdown_tables(md) == 1


def test_trimmed_table_keeps_uniform_column_count() -> None:
    t = table(
        2,
        3,
        cell(0, 0, para("가"), col_span=3),
        cell(1, 0, para("나")),
        cell(1, 1),
        cell(1, 2),
    )
    normalized, _, _ = _normalize_tables(doc(t))
    lines = [
        line
        for line in cleanup_markdown(normalized.to_markdown()).splitlines()
        if line.startswith("|")
    ]
    assert len({line.count("|") for line in lines}) == 1


def test_table_count_excludes_nested_tables() -> None:
    inner_a = table(1, 1, cell(0, 0, para("a")))
    inner_b = table(1, 1, cell(0, 0, para("b")))
    outer = table(1, 2, cell(0, 0, inner_a), cell(0, 1, inner_b))
    ir = doc(outer, table(1, 1, cell(0, 0, para("독립"))))

    assert _count_tables_in_ir(ir) == 2


def test_table_count_matches_rendered_after_normalization() -> None:
    inner = table(1, 1, cell(0, 0, para("중첩 내용")))
    outer = table(1, 1, cell(0, 0, inner))
    merged = table(1, 2, cell(0, 0, para("병합"), col_span=2))
    ir = doc(outer, merged, table(1, 1, cell(0, 0, para("독립"))))

    normalized, _, _ = _normalize_tables(ir)
    md = cleanup_markdown(normalized.to_markdown())
    assert _count_tables_in_ir(normalized) == count_markdown_tables(md) == 3


def test_table_count_ignores_furniture() -> None:
    from rhwp.ir.nodes import Furniture  # noqa: PLC0415

    header_table = table(1, 1, cell(0, 0, para("머리말 표")))
    ir = HwpDocument(
        body=[table(1, 1, cell(0, 0, para("본문 표")))],
        furniture=Furniture(page_headers=[header_table]),
    )
    assert _count_tables_in_ir(ir) == 1


def test_table_count_returns_none_when_ir_cannot_be_walked() -> None:
    assert _count_tables_in_ir(object()) is None
