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
    import rhwp

    suffix = ".hwpx" if filename.lower().endswith(".hwpx") else ".hwp"
    warnings: list[str] = []

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
    ir, folded, expanded = _normalize_tables(ir)
    if folded:
        warnings.append(f"NESTED_TABLES_FLATTENED:{folded}")
    if expanded:
        warnings.append(f"MERGED_CELLS_EXPANDED:{expanded}")
    markdown = ir.to_markdown()
    table_count = _count_tables_in_ir(ir)
    if table_count is None:
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


_NESTED_CELL_SEP = " / "


def _normalize_tables(ir: object) -> tuple[object, int, int]:
    """표를 렌더 전에 손본다. (새 IR, 접은 중첩 표 수, 펼친 병합 셀 수)

    rhwp 의 렌더러에는 마크다운 품질을 크게 깎는 두 가지 성질이 있다.

    1. **중첩 표를 렌더하지 않는다.** 부모 셀이 빈 <td></td> 로 나가고 내용이
       통째로 사라진다. 실측 코퍼스 145건에서 추출 가능 텍스트의 13.2%(30,816자)가
       이 경로로만 없어졌고, 공문 시행 문서번호·수신자 목록·본문 표가 섞여 있었다.
    2. **병합 셀이 하나라도 있으면 표 전체를 TableBlock.html 인라인으로 뱉는다.**
       실측에서 표 339개가 이 경로를 탔고, 그 blob 이 마크다운의 52.5%(226,616자),
       그중 65.6%(148,572자)가 순수 태그였다. 게다가 339개 중 325개가 개행 없는
       한 줄(최장 4,337자)이라 청커가 자를 지점을 못 찾는다.

    그래서 셀을 **1x1 격자로 펼쳐** 렌더러를 GFM 경로로 돌린다. rowspan 은 값을
    세로로 복제하고(행이 스스로를 설명하게 — 청크가 잘려도 부문/항목이 붙어 있다),
    colspan 은 첫 칸에만 두고 나머지는 빈 칸으로 둔다(같은 줄이라 정보 중복일 뿐).
    실측 표 2,181개 전부 좌표 충돌 0·빈칸 0 이었으나, 다른 문서를 대비해 충돌은
    먼저 온 셀을 남기고 빈 좌표는 빈 셀로 메운다.

    중첩 표는 부모 셀 안 문단으로 접는다 — 행/열 구조는 잃지만 통째로 사라지는
    것보다 낫고, 표 개수 기준값(_count_tables_in_ir)과도 정합한다.

    근거는 렌더러 소스에서 확인했다 (rhwp/ir/_view.py).

      - _md_table: any(c.row_span > 1 or c.col_span > 1) 이면 block.html
        을 그대로 반환한다 — 병합이 없어야 GFM 을 만든다.
      - _md_table: 격자를 block.rows × block.cols 로 잡고 그 밖의 셀은
        **조용히 버린다**. 그래서 rows/cols 를 펼친 좌표의 최댓값으로 다시 계산한다.
      - _md_cell_text: Paragraph/ListItem/Formula/Field 만 읽는다 — 셀 안
        TableBlock 은 아무것도 내지 않는다(중첩 표 소실의 출처).
      - TableBlock.text 는 렌더러가 쓰지 않는다. html 은 마크다운 경로에서
        병합 표일 때만 쓰인다. 둘 다 펼친 뒤 갱신하지 않으므로 cells 와 어긋난 채로
        남는다 — 이 파이프라인은 to_markdown() 만 쓰므로 무해하지만,
        to_html() 을 쓰게 되면 병합 표가 옛 HTML 로 나온다.

    실패해도 파싱은 살린다 — 원본 IR 을 그대로 돌려준다.
    """
    try:
        from rhwp.ir.nodes import ParagraphBlock, TableBlock  # noqa: PLC0415

        body = getattr(ir, "body", None)
        if body is None:
            return ir, 0, 0

        state = {
            "folded": 0,
            "expanded": 0,
            "trimmed_rows": 0,
            "trimmed_cols": 0,
            "emptied": 0,
        }

        def cell_texts(table: object, out: list[str]) -> list[str]:
            for cell in table.cells:
                for block in cell.blocks:
                    if isinstance(block, TableBlock):
                        cell_texts(block, out)
                    else:
                        text = (getattr(block, "text", "") or "").strip()
                        if text:
                            out.append(text)
            return out

        def fold_blocks(cell: object) -> list[object]:
            blocks: list[object] = []
            for block in cell.blocks:
                if isinstance(block, TableBlock):
                    state["folded"] += 1
                    text = _NESTED_CELL_SEP.join(cell_texts(block, []))
                    if text:
                        blocks.append(ParagraphBlock(text=text, prov=block.prov))
                else:
                    blocks.append(block)
            return blocks

        def expand(table: object) -> object:
            grid: dict[tuple[int, int], object] = {}
            for cell in table.cells:
                blocks = fold_blocks(cell)
                row_span = max(1, cell.row_span)
                col_span = max(1, cell.col_span)
                if row_span > 1 or col_span > 1:
                    state["expanded"] += 1
                for dr in range(row_span):
                    for dc in range(col_span):
                        key = (cell.row + dr, cell.col + dc)
                        if key in grid:
                            continue
                        grid[key] = cell.model_copy(
                            update={
                                "row": key[0],
                                "col": key[1],
                                "row_span": 1,
                                "col_span": 1,
                                "blocks": list(blocks) if dc == 0 else [],
                            }
                        )
            if not grid:
                return table

            rows = max(r for r, _ in grid) + 1
            cols = max(c for _, c in grid) + 1
            empty = next(iter(grid.values()))
            for r in range(rows):
                for c in range(cols):
                    if (r, c) not in grid:
                        grid[(r, c)] = empty.model_copy(
                            update={
                                "row": r,
                                "col": c,
                                "row_span": 1,
                                "col_span": 1,
                                "blocks": [],
                            }
                        )
            return trim(table, grid, rows, cols)

        def has_text(cell: object) -> bool:
            return any((getattr(b, "text", "") or "").strip() for b in cell.blocks)

        def trim(table: object, grid: dict, rows: int, cols: int) -> object | None:
            keep_rows = [r for r in range(rows) if any(has_text(grid[(r, c)]) for c in range(cols))]
            keep_cols = [c for c in range(cols) if any(has_text(grid[(r, c)]) for r in range(rows))]
            if not keep_rows or not keep_cols:
                state["emptied"] += 1
                return None

            state["trimmed_rows"] += rows - len(keep_rows)
            state["trimmed_cols"] += cols - len(keep_cols)
            new_rows, new_cols = len(keep_rows), len(keep_cols)
            cells = []
            for ri, r in enumerate(keep_rows):
                for ci, c in enumerate(keep_cols):
                    cells.append(
                        grid[(r, c)].model_copy(
                            update={
                                "row": ri,
                                "col": ci,
                                "grid_index": ri * new_cols + ci,
                            }
                        )
                    )
            return table.model_copy(
                update={"cells": cells, "rows": new_rows, "cols": new_cols}
            )

        new_body: list[object] = []
        for block in body:
            if not isinstance(block, TableBlock):
                new_body.append(block)
                continue
            table = expand(block)
            if table is not None:
                new_body.append(table)

        if not any(state.values()):
            return ir, 0, 0
        return ir.model_copy(update={"body": new_body}), state["folded"], state["expanded"]
    except Exception as exc:  # noqa: BLE001
        logger.warning("table normalization failed: %s", exc)
        return ir, 0, 0


def _count_tables_in_ir(ir: object) -> int | None:
    try:
        from rhwp.ir.nodes import TableBlock  # noqa: PLC0415

        iter_blocks = getattr(ir, "iter_blocks", None)
        if not callable(iter_blocks):
            return None
        return sum(
            1
            for b in iter_blocks(scope="body", recurse=False)
            if isinstance(b, TableBlock)
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("table count from IR failed: %s", exc)
        return None
