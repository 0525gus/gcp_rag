"""포맷별 엔진 디스패치 계약 테스트.

HWP(바이너리)와 HWPX(ZIP+XML)는 다른 포맷이다. HWPX는 순수 파이썬 엔진으로 빼서
rhwp 네이티브 확장의 ABI 리스크에서 분리한다.
"""

from __future__ import annotations

import pytest

import services.parser.engine as engine_mod
from services.parser.engine import can_parse, engine_status, is_hwpx_filename
from services.parser.quality_gate import ParseMetrics
from services.parser.rhwp_parser import ParseOutput


@pytest.fixture
def engines(monkeypatch):
    """두 엔진의 가용성과 호출 여부를 조작/기록한다."""

    def _setup(*, hwpx: bool = True, hwp: bool = True):
        calls: list[str] = []

        def fake_hwpx(data, *, filename="doc.hwpx"):
            calls.append("hwpx")
            return ParseOutput(
                markdown="hwpx md",
                metrics=ParseMetrics(text_length=7, source_bytes=len(data)),
                engine="python-hwpx",
            )

        def fake_rhwp(data, *, filename="doc.hwp"):
            calls.append("rhwp")
            return ParseOutput(
                markdown="rhwp md",
                metrics=ParseMetrics(text_length=7, source_bytes=len(data)),
                engine="rhwp",
            )

        monkeypatch.setattr(engine_mod, "hwpx_available", lambda: hwpx)
        monkeypatch.setattr(engine_mod, "rhwp_available", lambda: hwp)
        monkeypatch.setattr(engine_mod, "parse_hwpx_bytes", fake_hwpx)
        monkeypatch.setattr(engine_mod, "parse_hwp_bytes", fake_rhwp)
        return calls

    return _setup


@pytest.mark.parametrize(
    "name,expected",
    [("a.hwpx", True), ("A.HWPX", True), ("a.hwp", False), ("a.pdf", False), ("hwpx", False)],
)
def test_is_hwpx_filename(name: str, expected: bool) -> None:
    assert is_hwpx_filename(name) is expected


def test_hwpx_goes_to_pure_python_engine(engines) -> None:
    calls = engines()
    out = engine_mod.parse_document_bytes(b"x", filename="doc.hwpx")
    assert calls == ["hwpx"]
    assert out.engine == "python-hwpx"


def test_hwp_goes_to_rhwp(engines) -> None:
    calls = engines()
    out = engine_mod.parse_document_bytes(b"x", filename="doc.hwp")
    assert calls == ["rhwp"]
    assert out.engine == "rhwp"


def test_hwpx_falls_back_to_rhwp_when_engine_missing(engines) -> None:
    # python-hwpx 미설치 배포에서도 HWPX가 파싱 불가가 되면 안 된다
    calls = engines(hwpx=False)
    out = engine_mod.parse_document_bytes(b"x", filename="doc.hwpx")
    assert calls == ["rhwp"]
    assert out.engine == "rhwp"


def test_hwp_never_routes_to_hwpx_engine(engines) -> None:
    # python-hwpx는 .hwp 바이너리를 읽지 못한다 — hwpx 엔진만 있어도 넘기면 안 됨
    calls = engines(hwpx=True, hwp=False)
    engine_mod.parse_document_bytes(b"x", filename="doc.hwp")
    assert calls == ["rhwp"]
    assert "hwpx" not in calls


def test_can_parse_reflects_engine_availability(engines) -> None:
    engines(hwpx=False, hwp=True)
    assert can_parse("a.hwpx") is True  # rhwp가 HWPX도 읽으므로
    assert can_parse("a.hwp") is True

    engines(hwpx=True, hwp=False)
    assert can_parse("a.hwpx") is True
    assert can_parse("a.hwp") is False  # rhwp 없이는 .hwp 불가

    engines(hwpx=False, hwp=False)
    assert can_parse("a.hwpx") is False


def test_engine_status_shape(engines) -> None:
    engines(hwpx=True, hwp=False)
    assert engine_status() == {"hwpx": "ok", "hwp": "missing"}
