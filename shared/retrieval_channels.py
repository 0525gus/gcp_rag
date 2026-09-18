"""Observable rankings for each retrieval signal over one vector candidate pool.

Only ``vector`` creates candidates in the current architecture.  The other
channels are deterministic rankings of exactly that pool.  This module keeps the
distinction visible so an evaluation cannot accidentally call title/metadata
reranking "independent recall".
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Literal

from shared.lexical_rerank import bm25_scores, title_scores
from shared.metadata_rank import metadata_feature_scores
from shared.temporal_rank import temporal_scores

RetrievalChannel = Literal["vector", "body", "title", "bundle", "metadata"]
RETRIEVAL_CHANNELS: tuple[RetrievalChannel, ...] = (
    "vector",
    "body",
    "title",
    "bundle",
    "metadata",
)


@dataclass(frozen=True)
class ChannelSignals:
    period: float = 0.0
    sequence: float = 0.0
    document_kind: float = 0.0
    revision: float = 0.0

    @property
    def total(self) -> float:
        return self.period + self.sequence + self.document_kind + self.revision

    def to_dict(self) -> dict[str, float]:
        return {**asdict(self), "total": self.total}


@dataclass(frozen=True)
class ChannelRanking:
    order: list[int]
    scores: list[float]
    score_type: str
    signals: list[ChannelSignals] | None = None


def rank_channel(
    query: str,
    channel: RetrievalChannel,
    *,
    bodies: list[str],
    titles: list[str],
    bundles: list[str],
    metadata: list[str],
) -> ChannelRanking:
    """Rank one fixed candidate pool by a single retrieval signal.

    Ties preserve the incoming vector order.  Metadata uses the raw deterministic
    rule sum; its components are returned so conflict penalties remain auditable.
    """
    if channel not in RETRIEVAL_CHANNELS:
        raise ValueError(f"unknown retrieval channel: {channel}")
    n = len(bodies)
    if any(len(values) != n for values in (titles, bundles, metadata)):
        raise ValueError("all channel inputs must have the same length")

    signals: list[ChannelSignals] | None = None
    if channel == "vector":
        # Vertex score semantics are backend-defined, so expose rank-derived values
        # and retain the unmodified Vertex score separately in the HTTP response.
        scores = [1.0 / (60.0 + index) for index in range(n)]
        score_type = "vector_rank_reciprocal"
    elif channel == "body":
        scores = bm25_scores(query, bodies)
        score_type = "candidate_local_body_bm25"
    elif channel == "title":
        scores = title_scores(query, titles)
        score_type = "candidate_local_normalized_title_bm25_boost"
    elif channel == "bundle":
        scores = title_scores(query, bundles)
        score_type = "candidate_local_normalized_bundle_bm25_boost"
    else:
        periods = temporal_scores(query, metadata)
        features = metadata_feature_scores(query, metadata)
        signals = [
            ChannelSignals(
                period=period,
                sequence=feature.sequence,
                document_kind=feature.document_kind,
                revision=feature.revision,
            )
            for period, feature in zip(periods, features, strict=True)
        ]
        scores = [signal.total for signal in signals]
        score_type = "period_sequence_kind_revision_rule_sum"

    order = sorted(range(n), key=lambda index: (-scores[index], index))
    return ChannelRanking(order=order, scores=scores, score_type=score_type, signals=signals)
