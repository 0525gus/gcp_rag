"""HWP(바이너리) → Markdown (rhwp-python).

한글 문서 중 .hwp 만 이 모듈이 기본 담당한다. .hwpx 는
hwpx_parser 가 처리하고, python-hwpx 가 없을 때만 여기로 내려온다.

rhwp-python 은 PyO3 네이티브 휠이다. Cloud Run 이미지는 libfreetype
ABI 에 맞춰 맞춰 두어야 한다.

흐름:

1. Document.from_bytes 로 연다. 실패하면 임시 파일에 쓰고 rhwp.parse.
2. doc.to_ir() 로 중간 표현을 만든다.
3. _normalize_tables 가 중첩 표를 문단으로 접고, 병합 셀을 1x1 격자로
   펼친 뒤 빈 행·열을 걷어낸다. 그래야 렌더러가 HTML blob 대신 GFM 표를 낸다.
   손댈 게 없으면 IR 을 그대로 둔다. 정규화 실패해도 파싱은 살린다.
4. ir.to_markdown() 으로 GFM 을 뽑고, IR 표 개수·텍스트 길이를
   ParseMetrics 에 실어 품질 게이트가 표 소실을 잡게 한다.

진입점은 parse_hwp_bytes. ParseOutput 은 HWPX 경로와 같은 반환형이다.
"""

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
    ir, folded, expanded = _normalize_tables(ir)
    if folded:
        warnings.append(f"NESTED_TABLES_FLATTENED:{folded}")
    if expanded:
        warnings.append(f"MERGED_CELLS_EXPANDED:{expanded}")
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
            """셀 안 중첩 표를 문단으로 접는다."""
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
                            continue  # 겹치면 먼저 온 셀을 남긴다
                        grid[key] = cell.model_copy(
                            update={
                                "row": key[0],
                                "col": key[1],
                                "row_span": 1,
                                "col_span": 1,
                                # colspan 은 첫 칸에만 값을 둔다 (같은 줄 = 중복).
                                # list() 로 떠서 복제된 셀끼리 같은 리스트를 공유하지
                                # 않게 한다 — 현재 렌더러는 읽기만 하지만
                                # (rhwp/ir/_view.py `_md_table`), 공유는 남겨 둘 이유가 없다.
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
                    if (r, c) not in grid:  # 원본에 구멍이 있으면 빈 셀로 메운다
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
            """내용이 하나도 없는 행·열을 걷어낸다. 표 전체가 비면 None.

            공문 서식은 빈 칸이 많고, colspan 을 첫 칸에만 두는 규칙 때문에 펼친 뒤
            더 늘어난다 — 실측에서 셀의 60.3%가 빈 칸, 표 문자의 45.1%가 | 와
            공백이었다. 전부 빈 행·열은 정보가 0인데 자리만 차지하므로 지운다
            (실측: 빈 열 1,025개·빈 행 164개, 마크다운 8.0% 감소, 텍스트 손실 0).

            빈 표를 통째로 없애는 것이 기준값과도 정합한다 — 여기서 None 을
            돌려주면 body 에서 빠지고, _count_tables_in_ir 도 세지 않는다.
            마크다운 레벨에서 지웠다면 기준값만 남아 G2 가 오탐했을 자리다.
            """
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
    """문서 구조가 말하는 표 개수. 셀 수 없으면 None.

    마크다운을 세지 않는다 — 이 값은 '마크다운에 몇 개가 살아남았나'를 재는
    기준값이므로, 마크다운에서 유도하면 손실이 항상 0 이 되어 판정이 무의미해진다.

    scope="body", recurse=False 여야 한다. 렌더러가 표로 찍는 것과 같은 집합이다.

      - furniture(머리말/꼬리말/각주)는 to_markdown() 출력에 안 들어간다.
      - 중첩 표는 GFM 이 표 안 표를 표현하지 못해 별도 표로 안 나온다
        (내용은 _recover_nested_tables 가 부모 셀 텍스트로 접어 살린다).

    recurse=True(중첩 포함)로 세면 기준값이 실제 3배로 부풀어 G2 가 오탐한다 —
    실측 코퍼스 146건에서 2183 vs 714, 85건이 근거 없이 손실 판정을 받았다.
    """
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
