"""shared/xlsx_md.py 계약 테스트 — 실제 xlsx 바이트를 만들어 검증한다."""

from __future__ import annotations

import datetime as dt
import io
import re
import zipfile

import pytest

openpyxl = pytest.importorskip("openpyxl")

from shared.xlsx_md import XlsxParseError, xlsx_to_markdown  # noqa: E402


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
