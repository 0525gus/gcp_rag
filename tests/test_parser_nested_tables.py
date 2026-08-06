"""표 정규화(_normalize_tables) + 표 개수 기준값 계약.

rhwp 렌더러의 두 성질이 마크다운 품질을 깎는다.

  1. 중첩 표를 렌더하지 않아 텍스트가 통째로 사라진다 (실측 코퍼스 145건에서
     추출 가능 텍스트의 13.2%, 공문 문서번호·수신자 목록·본문 표 포함).
  2. 병합 셀이 하나라도 있으면 표 전체를 인라인 HTML 한 줄로 뱉는다
     (마크다운의 52.5%, 그중 65.6%가 순수 태그, 325/339개가 개행 없음).

여기에 G2 게이트 기준값(table_count)이 중첩 표까지 세면 실제 3배로 부풀어
멀쩡한 문서를 '표 손실'로 오탐한다 (146건 중 85건).

세 계약을 고정한다. rhwp 미설치 환경에서는 건너뛴다 —
파서 이미지(requirements-parser.txt)에만 있는 의존성이다.
"""

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


# --- 중첩 표 회수 -------------------------------------------------------


def test_nested_table_text_survives_into_markdown() -> None:
    inner = table(1, 2, cell(0, 0, para("담당")), cell(0, 1, para("이영원")))
    outer = table(1, 1, cell(0, 0, inner))

    before = cleanup_markdown(doc(outer).to_markdown())
    assert "이영원" not in before  # 정규화 전에는 사라진다 (회귀 감시용 기준)

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
    assert folded == 1  # 최상위 표의 직속 중첩 표 1개
    assert "3단" in cleanup_markdown(normalized.to_markdown())


def test_normalization_is_noop_on_plain_tables() -> None:
    plain = table(1, 2, cell(0, 0, para("가")), cell(0, 1, para("나")))
    ir = doc(plain, para("본문"))
    normalized, folded, expanded = _normalize_tables(ir)
    assert (folded, expanded) == (0, 0)
    assert normalized is ir  # 새 IR 을 만들지 않는다
    assert normalized.to_markdown() == ir.to_markdown()


def test_normalization_never_breaks_parsing() -> None:
    """정규화는 부가 기능이다 — 실패해도 원본 IR 을 그대로 돌려준다."""

    class Exploding:
        @property
        def body(self):
            raise RuntimeError("boom")

    broken = Exploding()
    out, folded, expanded = _normalize_tables(broken)
    assert out is broken
    assert (folded, expanded) == (0, 0)


# --- 병합 셀 격자 펼침 --------------------------------------------------


def test_merged_table_renders_as_gfm_not_inline_html() -> None:
    """병합 셀이 있으면 렌더러가 표 전체를 인라인 HTML 한 줄로 뱉는다.

    실측 코퍼스에서 그 blob 이 마크다운의 52.5%를 먹고 65.6%가 순수 태그였다.
    격자로 펼쳐 GFM 경로로 돌린다.
    """
    merged = table(
        2,
        2,
        cell(0, 0, para("제목"), col_span=2),
        cell(1, 0, para("가")),
        cell(1, 1, para("나")),
        html='<table><tr><td colspan="2">제목</td></tr></table>',
    )

    before = cleanup_markdown(doc(merged).to_markdown())
    assert "<table" in before  # 펼치기 전에는 HTML (회귀 감시용 기준)

    normalized, _, expanded = _normalize_tables(doc(merged))
    assert expanded == 1
    md = cleanup_markdown(normalized.to_markdown())
    assert "<table" not in md
    assert count_markdown_tables(md) == 1
    assert "제목" in md and "가" in md and "나" in md


def test_rowspan_value_repeats_down_so_each_row_stands_alone() -> None:
    """세로 병합은 값을 복제한다 — 청크가 행 단위로 잘려도 분류가 붙어 있어야 한다."""
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
    """가로 병합은 같은 줄이라 복제해도 정보가 없다 — 첫 칸에만 둔다."""
    merged = table(1, 3, cell(0, 0, para("공고"), col_span=3))
    normalized, _, _ = _normalize_tables(doc(merged))
    md = cleanup_markdown(normalized.to_markdown())
    assert md.count("공고") == 1


def test_expansion_recomputes_rows_cols_so_no_cell_is_dropped() -> None:
    """렌더러는 rows×cols 밖의 셀을 조용히 버린다 (rhwp/ir/_view.py `_md_table`).

    병합을 펼치면 좌표가 원래 rows/cols 를 넘길 수 있으므로 다시 계산해야 한다.
    여기서는 rows/cols 가 실제보다 작게 신고된 표를 준다.
    """
    understated = table(
        1,  # 실제로는 2행
        1,  # 실제로는 3열
        cell(0, 0, para("가"), row_span=2, col_span=2),
        cell(0, 2, para("나")),
    )
    normalized, _, _ = _normalize_tables(doc(understated))
    block = normalized.body[0]
    # 3열로 펼친 뒤 값이 없는 가운데 열(colspan 연장분)이 트리밍돼 2열이 된다.
    # 신고값 1x1 을 그대로 믿었다면 '나' 가 통째로 사라졌을 자리다.
    assert (block.rows, block.cols) == (2, 2)
    assert len(block.cells) == 4
    md = cleanup_markdown(normalized.to_markdown())
    assert "가" in md and "나" in md


def test_normalization_is_idempotent() -> None:
    """이미 펼친 IR 을 다시 넣어도 출력이 같아야 한다."""
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
    """GFM 은 행마다 열 수가 같아야 한다 — 구멍이 있으면 빈 셀로 메운다."""
    merged = table(
        2,
        3,
        cell(0, 0, para("머리"), col_span=3),
        cell(1, 0, para("값")),  # (1,1) (1,2) 가 비어 있다
    )
    normalized, _, _ = _normalize_tables(doc(merged))
    body_rows = [
        line
        for line in cleanup_markdown(normalized.to_markdown()).splitlines()
        if line.startswith("|")
    ]
    widths = {line.count("|") for line in body_rows}
    assert len(widths) == 1  # 헤더·구분행·본문 모두 같은 열 수


# --- 빈 행/열 트리밍 -----------------------------------------------------


def test_fully_empty_columns_are_dropped() -> None:
    """공문 서식은 빈 칸이 많고 colspan 펼침이 더 늘린다 — 정보 0인 열은 지운다."""
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
    """빈 표는 마크다운이 아니라 IR 에서 지운다 — 기준값이 같이 줄어야 G2 가 산다.

    마크다운 레벨에서 지우면 table_count 만 남아 표 손실로 오탐한다.
    """
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


# --- 표 개수 기준값 -----------------------------------------------------


def test_table_count_excludes_nested_tables() -> None:
    """기준값은 렌더러가 표로 찍는 것과 같은 집합이어야 한다.

    중첩 표를 세면 G2 가 근거 없이 손실을 선언한다.
    """
    inner_a = table(1, 1, cell(0, 0, para("a")))
    inner_b = table(1, 1, cell(0, 0, para("b")))
    outer = table(1, 2, cell(0, 0, inner_a), cell(0, 1, inner_b))
    ir = doc(outer, table(1, 1, cell(0, 0, para("독립"))))

    assert _count_tables_in_ir(ir) == 2  # 중첩 2개는 제외


def test_table_count_matches_rendered_after_normalization() -> None:
    """정규화 후에도 기준값 == 렌더된 표 개수 (G2 가 통과해야 한다)."""
    inner = table(1, 1, cell(0, 0, para("중첩 내용")))
    outer = table(1, 1, cell(0, 0, inner))
    merged = table(1, 2, cell(0, 0, para("병합"), col_span=2))
    ir = doc(outer, merged, table(1, 1, cell(0, 0, para("독립"))))

    normalized, _, _ = _normalize_tables(ir)
    md = cleanup_markdown(normalized.to_markdown())
    assert _count_tables_in_ir(normalized) == count_markdown_tables(md) == 3


def test_table_count_ignores_furniture() -> None:
    """머리말/꼬리말 표는 to_markdown() 출력에 안 들어간다 — 기준값에서도 뺀다."""
    from rhwp.ir.nodes import Furniture  # noqa: PLC0415

    header_table = table(1, 1, cell(0, 0, para("머리말 표")))
    ir = HwpDocument(
        body=[table(1, 1, cell(0, 0, para("본문 표")))],
        furniture=Furniture(page_headers=[header_table]),
    )
    assert _count_tables_in_ir(ir) == 1


def test_table_count_returns_none_when_ir_cannot_be_walked() -> None:
    assert _count_tables_in_ir(object()) is None
