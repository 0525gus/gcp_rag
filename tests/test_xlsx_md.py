"""shared/xlsx_md.py 계약 테스트 — 실제 xlsx 바이트를 만들어 검증한다."""

from __future__ import annotations

import datetime as dt
import io
import re
import zipfile

import pytest

openpyxl = pytest.importorskip("openpyxl")

from shared.xlsx_md import MAX_BYTES, XlsxParseError, xlsx_to_markdown  # noqa: E402


def _book(sheets: dict[str, list[list[object]]]) -> bytes:
    """{시트명: 행목록} 으로 xlsx 바이트를 만든다."""
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    for title, rows in sheets.items():
        ws = wb.create_sheet(title=title)
        for row in rows:
            ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ---------------------------------------------------------------- 기본 변환
def test_single_sheet_becomes_markdown_table() -> None:
    data = _book({"명단": [["부서", "담당자"], ["교무처", "홍길동"]]})
    md = xlsx_to_markdown(data)

    assert "| 부서 | 담당자 |" in md
    assert "| --- | --- |" in md
    assert "| 교무처 | 홍길동 |" in md


def test_single_sheet_omits_sheet_heading() -> None:
    # 시트가 하나뿐이면 파일명이 이미 제목으로 붙으므로 `Sheet1` 류를 싣지 않는다
    data = _book({"Sheet1": [["a", "b"], [1, 2]]})
    assert "## Sheet1" not in xlsx_to_markdown(data)


def test_multi_sheet_labels_each_sheet() -> None:
    data = _book(
        {
            "1학기": [["과목"], ["운영체제"]],
            "2학기": [["과목"], ["컴파일러"]],
        }
    )
    md = xlsx_to_markdown(data)
    assert "## 1학기" in md
    assert "## 2학기" in md
    assert "운영체제" in md and "컴파일러" in md


# ---------------------------------------------------------------- 값 서식
def test_integral_float_does_not_leak_decimal_point() -> None:
    # 엑셀은 정수도 float 로 돌려준다. '1.0' 이 본문에 박히면 검색어와 어긋난다
    data = _book({"s": [["수량"], [3.0], [2.5]]})
    md = xlsx_to_markdown(data)
    assert "| 3 |" in md
    assert "| 2.5 |" in md


def test_datetime_is_rendered_iso() -> None:
    data = _book({"s": [["일자"], [dt.datetime(2026, 7, 29, 9, 21)]]})
    md = xlsx_to_markdown(data)
    assert "2026-07-29 09:21:00" in md


def test_pipe_in_cell_is_escaped() -> None:
    # 셀에 | 가 들어가면 표 구조가 깨진다
    data = _book({"s": [["구분"], ["가|나"]]})
    md = xlsx_to_markdown(data)
    assert r"가\|나" in md


def test_newline_in_cell_is_flattened() -> None:
    data = _book({"s": [["비고"], ["첫줄\n둘째줄"]]})
    md = xlsx_to_markdown(data)
    # 줄바꿈이 그대로 나가면 표 한 행이 두 행으로 찢어진다
    assert "첫줄 둘째줄" in md
    assert len([ln for ln in md.splitlines() if ln.startswith("|")]) == 3


# ---------------------------------------------------------------- 빈 값 처리
def test_blank_rows_are_dropped() -> None:
    data = _book({"s": [["헤더"], [None], ["값"]]})
    md = xlsx_to_markdown(data)
    assert len([ln for ln in md.splitlines() if ln.startswith("|")]) == 3


def test_ragged_rows_are_padded_to_same_width() -> None:
    data = _book({"s": [["a", "b", "c"], ["1"]]})
    md = xlsx_to_markdown(data)
    for line in md.splitlines():
        if line.startswith("|"):
            assert line.count("|") == 4


def test_empty_workbook_raises() -> None:
    with pytest.raises(XlsxParseError):
        xlsx_to_markdown(_book({"s": []}))


# ---------------------------------------------------------------- 열 수 없는 파일
def test_ole2_container_raises_with_reason() -> None:
    # 암호 걸린 xlsx 는 OLE2 로 감싸여 구형 .xls 와 매직바이트가 같다
    data = b"\xd0\xcf\x11\xe0" + b"\x00" * 512
    with pytest.raises(XlsxParseError, match="OLE2"):
        xlsx_to_markdown(data)


def test_garbage_bytes_raise_parse_error() -> None:
    with pytest.raises(XlsxParseError):
        xlsx_to_markdown(b"not a spreadsheet at all")


def test_namespaced_cellstyle_without_name_is_recovered() -> None:
    """`<x:cellStyle>` 에 name 이 없으면 openpyxl 이 죽는다 — 복구 경로 검증.

    운영 코퍼스에서 실제로 2건 나왔다. 네임스페이스 접두사를 붙여 쓰는
    생성기가 만든 파일이라, 접두사 없는 형태만 찾으면 못 잡는다.
    """
    ns = b"http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    data = _book({"s": [["헤더"], ["값"]]})
    src = io.BytesIO(data)
    out = io.BytesIO()
    with zipfile.ZipFile(src) as zin, zipfile.ZipFile(out, "w") as zout:
        for item in zin.infolist():
            payload = zin.read(item.filename)
            if item.filename == "xl/styles.xml":
                # 실제 파일처럼 루트에 접두사를 선언한 뒤 그 접두사로 쓴다
                payload = payload.replace(
                    b"<styleSheet ", b'<styleSheet xmlns:x="' + ns + b'" ', 1
                )
                payload = re.sub(
                    rb"<cellStyles\b.*?</cellStyles>",
                    b'<x:cellStyles count="1">'
                    b'<x:cellStyle xfId="0" builtinId="0"/>'
                    b"</x:cellStyles>",
                    payload,
                    flags=re.S,
                )
            zout.writestr(item, payload)

    md = xlsx_to_markdown(out.getvalue())
    assert "| 값 |" in md


# ---------------------------------------------------------------- 상한
def test_cell_budget_truncates_and_says_so() -> None:
    data = _book({"s": [[f"r{i}c{j}" for j in range(4)] for i in range(50)]})
    md = xlsx_to_markdown(data, max_cells=20)
    assert "잘림" in md
    assert len(md.splitlines()) < 50


def test_byte_budget_caps_output() -> None:
    """셀 수만 막으면 출력 바이트가 새어 RAG 한도를 넘는다.

    실제로 셀 30만개가 29MB 를 만들어 색인이 통째로 실패한 적이 있다.
    """
    data = _book({"s": [[f"긴셀값{i}-{j}" * 5 for j in range(8)] for i in range(400)]})
    md = xlsx_to_markdown(data, max_bytes=4_000)

    assert len(md.encode("utf-8")) <= 4_200  # 상한 + 잘림 표기 여유
    assert "잘림" in md


def test_byte_budget_untouched_when_small() -> None:
    data = _book({"s": [["부서", "담당자"], ["교무처", "홍길동"]]})
    md = xlsx_to_markdown(data, max_bytes=MAX_BYTES)
    assert "잘림" not in md
    assert "홍길동" in md


# ------------------------------------------------------- 세로 병합 값 전파
def _book_merged(rows: list[list[object]], merges: list[str]) -> bytes:
    """행 목록 + 병합 범위(예: "A2:A4")로 xlsx 바이트를 만든다."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "s"
    for row in rows:
        ws.append(row)
    for ref in merges:
        ws.merge_cells(ref)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _body_rows(md: str) -> list[list[str]]:
    out = []
    for line in md.splitlines():
        s = line.strip()
        if not s.startswith("|"):
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        if all(c and set(c) <= {"-", ":"} for c in cells):
            continue
        out.append(cells)
    return out


def test_vertical_merge_value_repeats_down_each_row() -> None:
    """세로 병합된 분류 값은 아래 행으로 복제한다.

    엑셀은 병합 시 좌상단에만 값을 남긴다. 그대로 두면 청크가 행 단위로
    잘렸을 때 나머지 행이 자기 분류를 잃는다 — HWP 쪽 rowspan 처리와 같은 이유.
    실측 코퍼스 13건 중 11건에 병합이 있었고 그중 80%가 세로 병합이었다.
    """
    data = _book_merged(
        [
            ["분류", "항목"],
            ["교육", "강의"],
            [None, "실습"],
            [None, "평가"],
        ],
        ["A2:A4"],
    )
    rows = _body_rows(xlsx_to_markdown(data))
    assert [r[0] for r in rows] == ["분류", "교육", "교육", "교육"]
    assert [r[1] for r in rows] == ["항목", "강의", "실습", "평가"]


def test_horizontal_merge_is_left_alone() -> None:
    """가로 병합은 같은 줄 안 중복이라 좌상단에만 둔다."""
    data = _book_merged([["제목", None, None], ["a", "b", "c"]], ["A1:C1"])
    md = xlsx_to_markdown(data)
    assert md.count("제목") == 1
    assert _body_rows(md)[1] == ["a", "b", "c"]


def test_workbook_without_merges_is_unaffected() -> None:
    plain = [["부서", "담당자"], ["교무처", "홍길동"], ["기획처", "김철수"]]
    assert xlsx_to_markdown(_book({"s": plain})) == xlsx_to_markdown(_book_merged(plain, []))


def test_merge_propagation_never_overwrites_a_real_value() -> None:
    """병합 범위 안에 값이 있으면 그 값을 지킨다.

    정상 엑셀은 병합 시 아래 셀을 비우므로 파일로는 이 상황을 만들 수 없다.
    다른 도구가 만든 파일이나 손상 파일을 대비한 방어라서 단위로 확인한다.
    """
    from shared.xlsx_md import _sheet_rows  # noqa: PLC0415

    class _Ws:
        min_row = 1

        def iter_rows(self, values_only=True):  # noqa: ARG002
            yield ("교육", "강의")
            yield ("연구", "실습")  # 병합 범위인데 값이 남아 있다

    rows = _sheet_rows(_Ws(), [100], {2: [1]})
    assert [r[0] for r in rows] == ["교육", "연구"]


def test_merge_read_failure_falls_back_to_plain_conversion(monkeypatch) -> None:
    """병합 정보를 못 읽어도 변환 자체는 살아야 한다."""
    import shared.xlsx_md as mod

    monkeypatch.setattr(mod, "_sheet_xml_paths", lambda _zf: (_ for _ in ()).throw(RuntimeError("boom")))
    data = _book_merged([["분류", "항목"], ["교육", "강의"], [None, "실습"]], ["A2:A3"])
    md = xlsx_to_markdown(data)
    assert "실습" in md and "교육" in md


def test_multi_sheet_merges_are_matched_to_the_right_sheet() -> None:
    """시트 XML 이름(sheetN.xml)은 표시 순서와 무관 — rels 를 거쳐야 맞는다."""
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    first = wb.create_sheet(title="첫째")
    for row in [["A", "B"], ["병합", "x"], [None, "y"]]:
        first.append(row)
    first.merge_cells("A2:A3")
    second = wb.create_sheet(title="둘째")
    for row in [["C", "D"], ["단독", "z"]]:
        second.append(row)
    wb.move_sheet("둘째", offset=-1)  # 표시 순서를 뒤집는다
    buf = io.BytesIO()
    wb.save(buf)

    md = xlsx_to_markdown(buf.getvalue())
    assert md.index("## 둘째") < md.index("## 첫째")
    assert md.count("병합") == 2  # 첫째 시트에서만 전파
