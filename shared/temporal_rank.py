"""Deterministic Korean period metadata extraction and compatibility scoring."""

from __future__ import annotations

import re
from dataclasses import dataclass


def _numbers(pattern: str, text: str) -> frozenset[int]:
    return frozenset(int(value) for value in re.findall(pattern, text, re.IGNORECASE))


@dataclass(frozen=True)
class PeriodMetadata:
    years: frozenset[int] = frozenset()
    year_kinds: frozenset[str] = frozenset()
    semesters: frozenset[int] = frozenset()
    halves: frozenset[int] = frozenset()
    months: frozenset[int] = frozenset()
    rounds: frozenset[int] = frozenset()
    editions: frozenset[int] = frozenset()


_YEAR = re.compile(r"(?<!\d)((?:19|20)\d{2})\s*(학년도|년도|년)?(?!\d)")


def extract_period_metadata(text: str) -> PeriodMetadata:
    """Extract explicit period markers only; bare small numbers are ignored."""
    value = text or ""
    year_matches = _YEAR.findall(value)
    semesters = set(_numbers(r"(?<!\d)([12])\s*학기", value))
    for first, second in re.findall(r"([12])\s*[,·/~\-]\s*([12])\s*학기", value):
        semesters.update((int(first), int(second)))
    return PeriodMetadata(
        years=frozenset(int(year) for year, _kind in year_matches),
        year_kinds=frozenset(kind for _year, kind in year_matches if kind),
        semesters=frozenset(semesters),
        halves=frozenset(
            half
            for label, half in (("상반기", 1), ("하반기", 2))
            if label in value
        ),
        months=_numbers(r"(?<!\d)(1[0-2]|0?[1-9])\s*월", value),
        rounds=_numbers(r"(?<!\d)(\d{1,3})\s*차(?!\s*(?:원|선|산업))", value),
        editions=_numbers(r"제\s*(\d{1,3})\s*회", value),
    )


_FIELDS = ("years", "semesters", "halves", "months")


def temporal_compatibility(query: PeriodMetadata, document: PeriodMetadata) -> float:
    """Return match boosts minus explicit-conflict penalties.

    Missing document metadata is neutral. A disjoint value for a field explicitly
    present in both query and document is a conflict. Year/semester/half conflicts
    are stronger because those values define mutually exclusive periods.
    """
    score = 0.0
    strong = {"years", "semesters", "halves"}
    for field in _FIELDS:
        wanted = getattr(query, field)
        actual = getattr(document, field)
        if not wanted or not actual:
            continue
        if wanted & actual:
            score += 1.0
        else:
            score -= 3.0 if field in strong else 2.0
    return score


def temporal_scores(query: str, documents: list[str]) -> list[float]:
    query_metadata = extract_period_metadata(query)
    return [
        temporal_compatibility(query_metadata, extract_period_metadata(document))
        for document in documents
    ]
