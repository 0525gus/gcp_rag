"""품질 게이트 — 관측용(기본) / 거부·폴백은 설정으로 전환 가능."""

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
