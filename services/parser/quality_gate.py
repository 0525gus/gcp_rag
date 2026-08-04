"""품질 게이트 — 관측용(기본) / 거부·폴백은 설정으로 전환 가능.

**주의: 세 게이트 중 G2·G3 는 현재 구조적으로 발동하지 않는다.**

  G1 밀도    살아 있음
  G2 표      `table_cell_failures` 를 채우는 코드가 없다(항상 0). 따라서
             fail_ratio 는 늘 0 이고 `QG_TABLE_FAIL_RATIO` 는 아무것도 안 한다.
             rhwp 는 `table_cells_total` 조차 안 채운다.
  G3 이미지  `image_area_ratio` 를 채우는 코드가 없다(항상 0.0). 0.0 >= 0.5 는
             거짓이라 `QG_IMAGE_RATIO` 도 아무것도 안 한다.
  EMPTY_TEXT 살아 있음

빈 값을 채우는 파서 쪽 계측이 없어서지, 판정 로직이 틀린 게 아니다. 그래서
발동 조건은 그대로 두고 사실만 적어 둔다 — 임계값을 만지러 온 사람이 시간을
버리지 않도록.

이게 무해하지 않은 이유: G3 은 "밀도가 낮은 게 이미지가 많아서인지, 파싱이
실패해서인지"를 가르는 게이트다. G3 이 죽어 있어 그 구분을 G1 혼자 져야 했고,
결국 이미지 많은 공문의 오탐을 막느라 G1 임계값을 0.0005 까지 낮췄다
(shared/config.py 참고). 즉 **진짜 파싱 실패를 잡을 여력도 같이 잃었다.**
"""

from __future__ import annotations

from dataclasses import dataclass, field

from shared.config import Settings, get_settings

# GFM 구분행에 나올 수 있는 문자 전부 (정렬 콜론 포함)
_SEPARATOR_CHARS = frozenset("|:- \t")


def _is_separator_row(line: str) -> bool:
    """``| --- |`` 류의 GFM 표 구분행인가. 1열 표(``|---|``)도 포함."""
    text = line.strip()
    if "|" not in text or "---" not in text:
        return False
    return all(ch in _SEPARATOR_CHARS for ch in text)


def count_markdown_tables(markdown: str) -> int:
    """마크다운 본문에 실제로 렌더된 표 개수 (GFM 구분행 + HTML <table>)."""
    if not markdown:
        return 0
    html = markdown.lower().count("<table")
    lines = markdown.splitlines()
    gfm = sum(
        1
        for i, line in enumerate(lines)
        # 구분행 앞에는 헤더행이 있어야 표다 (수평선 ----- 오탐 방지)
        if i > 0 and _is_separator_row(line) and lines[i - 1].lstrip().startswith("|")
    )
    return html + gfm


@dataclass
class ParseMetrics:
    text_length: int
    source_bytes: int
    # 문서 구조(IR/OWPML)가 말하는 표 개수 — 판정의 기준값
    table_count: int = 0
    # 마크다운에 실제로 남은 표 개수. 파서가 아니라 cleanup 이후 본문에서 잰다.
    tables_rendered: int = 0
    warnings: list[str] = field(default_factory=list)


@dataclass
class GateResult:
    """triggered=True 이면 임계 미달. 조치(로그/거부/폴백)는 QG_MODE가 결정."""

    triggered: bool
    reasons: list[str]
    empty_text: bool = False

    @property
    def should_fallback(self) -> bool:
        """하위 호환 별칭."""
        return self.triggered


def evaluate_quality(
    metrics: ParseMetrics, settings: Settings | None = None
) -> GateResult:
    """
    G1 추출 밀도, G2 표 손실률.
    판정만 수행 — 파이프라인 중단 여부는 호출측 + QG_MODE.

    구 G2(표 '셀' 추출 실패율)와 G3(이미지 면적비)는 제거했다. 두 판정이 읽던
    table_cell_failures / image_area_ratio 를 채우는 파서가 없어 구조적으로 발동할
    수 없었다 — 셀 5000개가 전부 비어도, 이미지가 지면의 100%여도 통과했다.
    셀 단위 '실패'는 빈 셀이 정상이라 애초에 판별이 불가능하고, 이미지 면적비는
    페이지 기하 정보가 없어 계산 경로가 없다. 대신 모호하지 않은 신호인
    '문서 구조상 표 N개 중 마크다운에 M개만 남음'으로 G2 를 다시 세웠다.
    """
    cfg = settings or get_settings()
    reasons: list[str] = []
    empty_text = False

    density = (
        metrics.text_length / metrics.source_bytes if metrics.source_bytes > 0 else 0.0
    )
    if density < cfg.qg_density_threshold:
        reasons.append(f"G1_DENSITY:{density:.6f}<{cfg.qg_density_threshold}")

    if metrics.table_count > 0:
        lost = max(0, metrics.table_count - metrics.tables_rendered)
        loss_ratio = lost / metrics.table_count
        if loss_ratio > cfg.qg_table_loss_ratio:
            reasons.append(
                f"G2_TABLE_LOST:{lost}/{metrics.table_count}"
                f"={loss_ratio:.3f}>{cfg.qg_table_loss_ratio}"
            )

    if metrics.text_length < cfg.qg_min_text_length:
        empty_text = True
        reasons.append(f"EMPTY_TEXT:{metrics.text_length}<{cfg.qg_min_text_length}")

    return GateResult(
        triggered=bool(reasons),
        reasons=reasons,
        empty_text=empty_text,
    )
