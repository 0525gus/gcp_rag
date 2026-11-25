from __future__ import annotations

from dataclasses import dataclass, field

from shared.config import Settings, get_settings

_SEPARATOR_CHARS = frozenset("|:- \t")


def _is_separator_row(line: str) -> bool:
    text = line.strip()
    if "|" not in text or "---" not in text:
        return False
    return all(ch in _SEPARATOR_CHARS for ch in text)


def count_markdown_tables(markdown: str) -> int:
    if not markdown:
        return 0
    html = markdown.lower().count("<table")
    lines = markdown.splitlines()
    gfm = sum(
        1
        for i, line in enumerate(lines)
        if i > 0 and _is_separator_row(line) and lines[i - 1].lstrip().startswith("|")
    )
    return html + gfm


@dataclass
class ParseMetrics:
    text_length: int
    source_bytes: int
    table_count: int = 0
    tables_rendered: int = 0
    warnings: list[str] = field(default_factory=list)


@dataclass
class GateResult:
    triggered: bool
    reasons: list[str]
    empty_text: bool = False

    @property
    def should_fallback(self) -> bool:
        return self.triggered


def evaluate_quality(
    metrics: ParseMetrics, settings: Settings | None = None
) -> GateResult:
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
