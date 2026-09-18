"""Fuse independently ranked retrieval candidates without interpreting score meaning."""

from __future__ import annotations

from collections.abc import Sequence

from shared.models import SearchHit

_RRF_K = 60.0


def rrf_merge_hit_lists(
    rankings: Sequence[Sequence[SearchHit]], *, weights: Sequence[float] | None = None
) -> list[SearchHit]:
    """Merge ranked lists using reciprocal-rank fusion, retaining original hit scores."""
    if weights is None:
        weights = [1.0] * len(rankings)
    if len(weights) != len(rankings) or any(weight < 0 for weight in weights):
        raise ValueError("RRF weights must be nonnegative and match ranked lists")
    scores: dict[tuple[str, str], float] = {}
    first: dict[tuple[str, str], tuple[int, int, SearchHit]] = {}
    for list_index, ranking in enumerate(rankings):
        for rank, hit in enumerate(ranking):
            key = (hit.source.file_id, hit.text)
            scores[key] = scores.get(key, 0.0) + weights[list_index] / (_RRF_K + rank)
            first.setdefault(key, (list_index, rank, hit))
    ordered = sorted(
        first,
        key=lambda key: (-scores[key], first[key][0], first[key][1]),
    )
    return [first[key][2] for key in ordered]
