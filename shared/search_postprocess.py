"""검색 결과 후처리 — fileId 정규화, unescape, score→relevance, 중복 제거."""

from __future__ import annotations

import html
import re
from dataclasses import replace

from shared.models import SearchHit

# 긴 suffix 먼저
_FILE_SUFFIXES = (
    ".meta.md",
    ".markdown",
    ".md",
    ".pdf",
    ".txt",
    ".html",
    ".htm",
    ".docx",
    ".pptx",
    ".xlsx",
    ".xls",
    ".csv",
    ".rtf",
    ".bin",
)


def extract_file_id(display: str, source_uri: str | None = None) -> str:
    """GCS displayName/URI → Drive fileId (확장자·meta 접미사 제거)."""
    name = display or (source_uri or "")
    base = name.rsplit("/", 1)[-1].strip()
    if not base:
        return "unknown"
    lower = base.lower()
    for suf in _FILE_SUFFIXES:
        if lower.endswith(suf):
            return base[: -len(suf)]
    return base


def unescape_chunk_text(text: str) -> str:
    """PDF 추출 HTML 엔티티·과도한 \\r 정리."""
    if not text:
        return ""
    out = html.unescape(text)
    out = out.replace("\r\n", "\n").replace("\r", "\n")
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip()


def distance_to_relevance(raw_score: float) -> float:
    """Vertex RAG score를 '높을수록 관련' relevance로 변환.

    실측상 코퍼스 retrieve score는 거리형(낮을수록 유사)으로 오는 경우가 많아
    1/(1+d) 로 뒤집는다. 이미 유사도(>1 등)처럼 보이면 그대로 clamp.
    """
    s = float(raw_score)
    if s < 0:
        return 0.0
    # 유사도처럼 보이면 (드묾) 그대로 상한
    if s > 1.5:
        return min(s / 10.0, 1.0) if s > 10 else min(s, 1.0)
    return 1.0 / (1.0 + s)


def _text_fingerprint(text: str, n: int = 180) -> str:
    t = re.sub(r"\s+", " ", (text or "").lower()).strip()
    return t[:n]


def postprocess_hits(
    hits: list[SearchHit],
    *,
    top_k: int,
) -> list[SearchHit]:
    """unescape + relevance 정규화 + fileId/본문 중복 제거 후 top_k."""
    if not hits:
        return []

    prepared: list[SearchHit] = []
    for hit in hits:
        fid = extract_file_id(
            hit.source.file_id or hit.source.name,
            hit.source.source_uri,
        )
        text = unescape_chunk_text(hit.text)
        rel = distance_to_relevance(hit.score)
        src = replace(hit.source, file_id=fid)
        prepared.append(SearchHit(text=text, score=rel, source=src))

    # relevance 높은 순
    prepared.sort(key=lambda h: h.score, reverse=True)

    seen_files: set[str] = set()
    seen_text: set[str] = set()
    out: list[SearchHit] = []
    for hit in prepared:
        fid = hit.source.file_id
        if fid in seen_files:
            continue
        fp = _text_fingerprint(hit.text)
        if fp and fp in seen_text:
            continue
        seen_files.add(fid)
        if fp:
            seen_text.add(fp)
        out.append(hit)
        if len(out) >= top_k:
            break
    return out
