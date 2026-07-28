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


# 크기 한도 초과로 쪼갠 조각: {fileId}.part2.pdf → {fileId}
_PART_SUFFIX = re.compile(r"\.part\d+$", re.IGNORECASE)


def extract_file_id(display: str, source_uri: str | None = None) -> str:
    """GCS displayName/URI → Drive fileId (확장자·meta·분할 접미사 제거)."""
    name = display or (source_uri or "")
    base = name.rsplit("/", 1)[-1].strip()
    if not base:
        return "unknown"
    lower = base.lower()
    for suf in _FILE_SUFFIXES:
        if lower.endswith(suf):
            base = base[: -len(suf)]
            break
    return _PART_SUFFIX.sub("", base)


def unescape_chunk_text(text: str) -> str:
    """PDF 추출 HTML 엔티티·과도한 \\r 정리."""
    if not text:
        return ""
    out = html.unescape(text)
    out = out.replace("\r\n", "\n").replace("\r", "\n")
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip()


def distance_to_relevance(raw_score: float) -> float:
    """(사용 안 함) 거리 → relevance 변환.

    score 가 거리인지 유사도인지 추측해 뒤집는 방식이라, 유사도였을 경우
    순위를 정확히 거꾸로 만든다. 순위는 Vertex 가 준 순서를 그대로 쓰므로
    더 이상 정렬에 쓰지 않는다. 점수 의미가 확정되면 그때 다시 검토할 것.
    """
    s = float(raw_score)
    if s < 0:
        return 0.0
    if s > 1.5:
        return min(s / 10.0, 1.0) if s > 10 else min(s, 1.0)
    return 1.0 / (1.0 + s)


def _text_fingerprint(text: str, n: int = 180) -> str:
    t = re.sub(r"\s+", " ", (text or "").lower()).strip()
    return t[:n]


CHUNK_JOINER = "\n\n[...]\n\n"


def postprocess_hits(
    hits: list[SearchHit],
    *,
    top_k: int,
    max_chunks_per_file: int = 3,
) -> list[SearchHit]:
    """unescape + 같은 문서의 청크 병합 후 상위 top_k 문서.

    **들어온 순서를 그대로 존중한다.** 정렬은 호출측 책임이다(벡터 순서 그대로
    이거나 lexical_rerank 를 거친 순서). score 를 거리로 볼지 유사도로 볼지
    추측해 여기서 다시 정렬하면 추측이 틀렸을 때 순위가 통째로 뒤집힌다.
    점수는 표시용으로 원값을 통과시킨다 — 실측상 Vertex score 는 **거리**라
    작을수록 관련이 높고, 재정렬을 거치면 순서와 점수가 단조롭지 않게 된다.

    파일당 1청크만 남기면, 조문이 많은 규정에 넓은 질의가 들어올 때 가장 잘
    맞는 청크 하나만 나가고 나머지가 잘린다. 실제로 FactChat 이 조문 번호를
    바꿔가며 같은 규정을 4~5회 재질의했다. 질의 하나가 25개 조문을 다 덮을 수
    없는데 문서당 1청크로는 조문 하나치씩만 전달되기 때문이다.
    그래서 문서 단위 다양성은 유지하되, 한 문서 안에서는 상위 몇 청크를
    이어 붙여 함께 돌려준다.
    """
    if not hits:
        return []

    prepared: list[SearchHit] = []
    for hit in hits:
        fid = extract_file_id(
            hit.source.file_id or hit.source.name,
            hit.source.source_uri,
        )
        text = unescape_chunk_text(hit.text)
        src = replace(hit.source, file_id=fid)
        prepared.append(SearchHit(text=text, score=hit.score, source=src))

    # 문서별로 묶되 처음 나온 순서(=관련도 순)를 보존한다
    order: list[str] = []
    grouped: dict[str, list[SearchHit]] = {}
    seen_text: set[str] = set()
    for hit in prepared:
        fid = hit.source.file_id
        fp = _text_fingerprint(hit.text)
        if fp and fp in seen_text:
            continue  # 같은 본문이 다른 파일로 중복 색인된 경우
        if fp:
            seen_text.add(fp)
        if fid not in grouped:
            grouped[fid] = []
            order.append(fid)
        if len(grouped[fid]) < max_chunks_per_file:
            grouped[fid].append(hit)

    out: list[SearchHit] = []
    for fid in order[:top_k]:
        chunks = grouped[fid]
        head = chunks[0]
        if len(chunks) == 1:
            out.append(head)
            continue
        merged = CHUNK_JOINER.join(c.text for c in chunks)
        out.append(SearchHit(text=merged, score=head.score, source=head.source))
    return out
