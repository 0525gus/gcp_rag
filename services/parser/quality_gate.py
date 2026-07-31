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


@dataclass
class ParseMetrics:
    text_length: int
    source_bytes: int
    table_count: int = 0
    table_cell_failures: int = 0
    table_cells_total: int = 0
    image_area_ratio: float = 0.0
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
    G1 추출 밀도, G2 표 정합성, G3 이미지 비율.
    판정만 수행 — 파이프라인 중단 여부는 호출측 + QG_MODE.
    """
    cfg = settings or get_settings()
    reasons: list[str] = []
    empty_text = False

    density = (
        metrics.text_length / metrics.source_bytes if metrics.source_bytes > 0 else 0.0
    )
    if density < cfg.qg_density_threshold:
        reasons.append(f"G1_DENSITY:{density:.6f}<{cfg.qg_density_threshold}")

    if metrics.table_cells_total > 0:
        fail_ratio = metrics.table_cell_failures / metrics.table_cells_total
        if fail_ratio > cfg.qg_table_fail_ratio:
            reasons.append(f"G2_TABLE:{fail_ratio:.3f}>{cfg.qg_table_fail_ratio}")

    if (
        metrics.image_area_ratio >= cfg.qg_image_ratio
        and density < cfg.qg_density_threshold * 2
    ):
        reasons.append(f"G3_IMAGE:{metrics.image_area_ratio:.3f}>={cfg.qg_image_ratio}")

    if metrics.text_length < cfg.qg_min_text_length:
        empty_text = True
        reasons.append(f"EMPTY_TEXT:{metrics.text_length}<{cfg.qg_min_text_length}")

    return GateResult(
        triggered=bool(reasons),
        reasons=reasons,
        empty_text=empty_text,
    )
