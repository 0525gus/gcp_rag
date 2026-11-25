from __future__ import annotations

import math
import re
from collections import Counter

from shared.metadata_rank import metadata_feature_scores
from shared.temporal_rank import temporal_scores

_TOKEN = re.compile(r"[가-힣]+|[a-zA-Z]+|\d+")
_RRF_K = 60.0

_EXTENSION = re.compile(r"\.[a-z0-9]{1,8}$", re.IGNORECASE)
_TRAILING_TIMESTAMP = re.compile(
    r"(?:^|[\s_\-(])(?:\d{4}[-_.]?\d{2}[-_.]?\d{2})(?:[-_. T]?\d{4,6})?[\s_\-)]*$"
    r"|(?:^|[\s_\-(])\d{10,14}[\s_\-)]*$"
)
_SEMESTER = re.compile(r"(?<!\d)(\d{4})\s*(?:학년도|년)?\s*[-_.]?\s*([12])\s*학기")
_SPECIAL = re.compile(r"[^0-9a-z가-힣]+", re.IGNORECASE)


def _tokens(text: str) -> list[str]:
    out: list[str] = []
    for m in _TOKEN.findall((text or "").lower()):
        out.append(m)
        if len(m) > 1 and "가" <= m[0] <= "힣":
            out.extend(m[i : i + 2] for i in range(len(m) - 1))
    return out


_MIN_TERM_LEN = 2


def query_terms(query: str) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for m in _TOKEN.findall(query or ""):
        if len(m) < _MIN_TERM_LEN:
            continue
        key = m.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(m)
    return out


def normalize_title(value: str) -> str:
    text = _EXTENSION.sub("", (value or "").casefold().strip())
    text = _TRAILING_TIMESTAMP.sub("", text).strip()
    text = _SEMESTER.sub(r"\1 \2학기", text)
    return _SPECIAL.sub(" ", text).strip()


def _is_hangul(term: str) -> bool:
    return bool(term) and "가" <= term[0] <= "힣"


def term_coverage(terms: list[str], text: str) -> tuple[list[str], list[str]]:
    lowered = (text or "").lower()
    non_hangul_tokens = {t for t in _TOKEN.findall(lowered) if not _is_hangul(t)}

    matched: list[str] = []
    missing: list[str] = []
    for term in terms:
        key = term.lower()
        found = key in lowered if _is_hangul(term) else key in non_hangul_tokens
        (matched if found else missing).append(term)
    return matched, missing


def _bm25_scores(query: str, docs: list[str]) -> list[float]:
    k1, b = 1.5, 0.75
    doc_tokens = [_tokens(d) for d in docs]
    lengths = [len(t) or 1 for t in doc_tokens]
    avg_len = sum(lengths) / len(lengths)
    n = len(docs)

    df: Counter[str] = Counter()
    for toks in doc_tokens:
        df.update(set(toks))

    q_terms = set(_tokens(query))
    scores = []
    for toks, length in zip(doc_tokens, lengths):
        tf = Counter(toks)
        s = 0.0
        for term in q_terms:
            f = tf.get(term, 0)
            if not f:
                continue
            idf = math.log(1 + (n - df[term] + 0.5) / (df[term] + 0.5))
            s += idf * (f * (k1 + 1)) / (f + k1 * (1 - b + b * length / avg_len))
        scores.append(s)
    return scores


class Bm25Index:
    def __init__(self, documents: list[str]) -> None:
        self._doc_tokens = [_tokens(document) for document in documents]
        self._term_frequencies = [Counter(tokens) for tokens in self._doc_tokens]
        self._lengths = [len(tokens) or 1 for tokens in self._doc_tokens]
        self._count = len(documents)
        self._avg_length = (
            sum(self._lengths) / self._count if self._count else 0.0
        )
        self._document_frequency: Counter[str] = Counter()
        for tokens in self._doc_tokens:
            self._document_frequency.update(set(tokens))

    def scores(self, query: str) -> list[float]:
        if not self._count:
            return []
        k1, b = 1.5, 0.75
        query_terms = set(_tokens(query))
        scores: list[float] = []
        for term_frequencies, length in zip(
            self._term_frequencies, self._lengths, strict=True
        ):
            score = 0.0
            for term in query_terms:
                frequency = term_frequencies.get(term, 0)
                if not frequency:
                    continue
                df = self._document_frequency[term]
                idf = math.log(1 + (self._count - df + 0.5) / (df + 0.5))
                score += idf * (frequency * (k1 + 1)) / (
                    frequency + k1 * (1 - b + b * length / self._avg_length)
                )
            scores.append(score)
        return scores


def bm25_scores(query: str, documents: list[str]) -> list[float]:
    if not documents:
        return []
    return _bm25_scores(query, documents)


class TitleBm25Index:
    def __init__(self, titles: list[str]) -> None:
        self._normalized_titles = [normalize_title(title) for title in titles]
        self._bm25 = Bm25Index(self._normalized_titles)

    def scores(self, query: str) -> list[float]:
        normalized_query = normalize_title(query)
        scores = self._bm25.scores(normalized_query)
        query_tokens = set(_tokens(normalized_query))
        for i, title in enumerate(self._normalized_titles):
            if not normalized_query or not title:
                continue
            if normalized_query == title:
                scores[i] += 100.0
            elif normalized_query in title or title in normalized_query:
                scores[i] += 25.0
            elif query_tokens:
                coverage = len(query_tokens & set(_tokens(title))) / len(query_tokens)
                if coverage >= 0.6:
                    scores[i] += 10.0 * coverage
        return scores


def _title_scores(query: str, titles: list[str]) -> list[float]:
    return TitleBm25Index(titles).scores(query)


def title_scores(query: str, titles: list[str]) -> list[float]:
    if not titles:
        return []
    return _title_scores(query, titles)


def rrf_rerank(
    query: str,
    texts: list[str],
    *,
    titles: list[str] | None = None,
    title_weight: float = 0.0,
    temporal_metadata: list[str] | None = None,
    temporal_weight: float = 0.0,
    sequence_weight: float = 0.0,
    document_kind_weight: float = 0.0,
    revision_weight: float = 0.0,
) -> list[int]:
    n = len(texts)
    if n <= 1:
        return list(range(n))
    if title_weight < 0:
        raise ValueError("title_weight must be nonnegative")
    if titles is not None and len(titles) != n:
        raise ValueError("titles and texts must have the same length")
    if any(weight < 0 for weight in (
        temporal_weight, sequence_weight, document_kind_weight, revision_weight
    )):
        raise ValueError("metadata weights must be nonnegative")
    if temporal_metadata is not None and len(temporal_metadata) != n:
        raise ValueError("temporal_metadata and texts must have the same length")

    # 입력 순서가 곧 벡터 순위 (Vertex 가 관련도 순으로 준다)
    vec_rank = {i: i for i in range(n)}

    lex = _bm25_scores(query, texts)
    lex_order = sorted(range(n), key=lambda i: (-lex[i], i))
    lex_rank = {idx: pos for pos, idx in enumerate(lex_order)}

    title_rank: dict[int, int] | None = None
    if title_weight and titles is not None:
        title_scores = _title_scores(query, titles)
        title_order = sorted(range(n), key=lambda i: (-title_scores[i], i))
        title_rank = {idx: pos for pos, idx in enumerate(title_order)}
    period_scores = (
        temporal_scores(query, temporal_metadata)
        if temporal_weight and temporal_metadata is not None
        else [0.0] * n
    )
    metadata_scores = (
        metadata_feature_scores(query, temporal_metadata)
        if temporal_metadata is not None
        and any((sequence_weight, document_kind_weight, revision_weight))
        else None
    )

    def fused(i: int) -> float:
        score = 1.0 / (_RRF_K + vec_rank[i]) + 1.0 / (_RRF_K + lex_rank[i])
        if title_rank is not None:
            score += title_weight / (_RRF_K + title_rank[i])
        score += temporal_weight * period_scores[i] / _RRF_K
        if metadata_scores is not None:
            features = metadata_scores[i]
            score += sequence_weight * features.sequence / _RRF_K
            score += document_kind_weight * features.document_kind / _RRF_K
            score += revision_weight * features.revision / _RRF_K
        return score

    return sorted(range(n), key=lambda i: (-fused(i), i))


def metadata_overlay_order(
    query: str,
    titles: list[str],
    metadata: list[str],
    *,
    title_weight: float,
    temporal_weight: float,
    sequence_weight: float,
    document_kind_weight: float,
    revision_weight: float,
) -> list[int]:
    if len(titles) != len(metadata):
        raise ValueError("titles and metadata must have the same length")
    n = len(titles)
    title_values = _title_scores(query, titles)
    title_order = sorted(range(n), key=lambda i: (-title_values[i], i))
    title_rank = {index: rank for rank, index in enumerate(title_order)}
    periods = temporal_scores(query, metadata)
    features = metadata_feature_scores(query, metadata)

    def score(i: int) -> float:
        value = 2.0 / (_RRF_K + i) + title_weight / (_RRF_K + title_rank[i])
        value += temporal_weight * periods[i] / _RRF_K
        value += sequence_weight * features[i].sequence / _RRF_K
        value += document_kind_weight * features[i].document_kind / _RRF_K
        value += revision_weight * features[i].revision / _RRF_K
        return value

    return sorted(range(n), key=lambda i: (-score(i), i))
