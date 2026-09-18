"""Rule-based metadata features used only to rerank an existing candidate set."""

from __future__ import annotations

import re
from dataclasses import dataclass

_SEQUENCE_PATTERNS = {
    "round": re.compile(r"(?<!\d)(\d{1,3})\s*차(?!\s*(?:원|선|산업))"),
    "edition": re.compile(r"제\s*(\d{1,3})\s*회"),
    "attachment": re.compile(r"(?:붙임|첨부|별첨)\s*[-_.]?\s*(\d{1,3})"),
    "form": re.compile(r"(?:서식|양식)\s*[-_.]?\s*(\d{1,3})"),
    "agenda": re.compile(r"심의\s*[-_.]?\s*(\d{1,3})\s*호"),
}

_KINDS = {
    "comparison": ("신구대비표", "신구조문대비표", "대비표"),
    "regulation": ("규정", "시행세칙", "지침"),
    "application": ("신청서", "신청양식", "지원서", "동의서"),
    "form": ("서식", "양식"),
    "manual": ("매뉴얼", "가이드"),
    "plan": ("계획", "계획서"),
    "report": ("결과보고", "결과 보고", "보고서", "리포트"),
    "notice": ("안내", "공지"),
    "announcement": ("공고", "모집요강", "요강"),
    "roster": ("명단", "현황"),
}
_REVISION_QUERY = ("최신", "최종", "확정", "개정", "바뀐", "변경", "신구")
_REVISION_DOCUMENT = ("최종", "확정", "개정", "일부개정", "신구대비")


@dataclass(frozen=True)
class MetadataFeatures:
    sequence: float = 0.0
    document_kind: float = 0.0
    revision: float = 0.0


def _sequences(text: str) -> dict[str, frozenset[int]]:
    return {
        label: frozenset(int(value) for value in pattern.findall(text or ""))
        for label, pattern in _SEQUENCE_PATTERNS.items()
    }


def _kinds(text: str) -> frozenset[str]:
    value = text or ""
    return frozenset(
        kind for kind, markers in _KINDS.items() if any(marker in value for marker in markers)
    )


def metadata_features(query: str, document: str) -> MetadataFeatures:
    query_sequences = _sequences(query)
    document_sequences = _sequences(document)
    sequence = 0.0
    for label, wanted in query_sequences.items():
        actual = document_sequences[label]
        if wanted and actual:
            sequence += 1.0 if wanted & actual else -2.0

    query_kinds = _kinds(query)
    document_kinds = _kinds(document)
    document_kind = 0.0
    if query_kinds and document_kinds:
        document_kind = 1.0 if query_kinds & document_kinds else -1.0

    asks_revision = any(marker in query for marker in _REVISION_QUERY)
    is_revision = any(marker in document for marker in _REVISION_DOCUMENT)
    revision = 1.0 if asks_revision and is_revision else 0.0
    return MetadataFeatures(
        sequence=sequence,
        document_kind=document_kind,
        revision=revision,
    )


def metadata_feature_scores(query: str, documents: list[str]) -> list[MetadataFeatures]:
    return [metadata_features(query, document) for document in documents]
