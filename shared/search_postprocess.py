"""검색 결과 후처리 — fileId 정규화, unescape, score→relevance, 중복 제거."""

from __future__ import annotations

import html
import re
from dataclasses import replace
from typing import Any

from shared.lexical_rerank import query_terms
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


CONTEXT_SEPARATOR = "\n\n---\n\n"


def build_answer_payload(chunks: list[dict[str, Any]], query: str) -> dict[str, Any]:
    """search 결과 → answer 툴 payload (출처 라벨 + 커버리지 신호).

    context 블록마다 `[n] 파일명` 라벨을 붙인다. 본문만 이어붙이던 이전 형식은
    문서 경계와 출처의 연결을 끊어, 호출 LLM 이 어느 문장이 어느 문서에서 왔는지
    복원할 수 없게 만들었다 — 서로 다른 문서를 한 서술로 합치는 답의 원인이다.

    coverage 는 '되물어야 하는 질의'를 호출측이 판별할 근거다. MCP 툴은 자기
    턴이 없어 사용자에게 되물을 수 없으므로, 판단 재료만 넘기고 행동은 맡긴다.
      - full: 어떤 문서 **하나**가 질의어를 전부 담고 있다
      - partial: 질의어가 여러 문서에 흩어져 있거나 일부는 어디에도 없다
      - none: 결과 없음
    """
    terms = query_terms(query)

    blocks: list[str] = []
    citations: list[dict[str, Any]] = []
    covered: set[str] = set()
    single_doc_covers_all = False

    for n, chunk in enumerate(chunks, start=1):
        src = chunk.get("source") or {}
        label = src.get("name") or src.get("fileId") or "(이름 없음)"
        blocks.append(f"[{n}] {label}\n{chunk.get('text', '')}")
        citations.append(
            {
                "n": n,
                "fileId": src.get("fileId"),
                "name": src.get("name"),
                "sourceUri": src.get("sourceUri"),
            }
        )
        covered.update(t.lower() for t in chunk.get("matchedTerms", []))
        if not chunk.get("missingTerms"):
            single_doc_covers_all = True

    if not chunks:
        coverage = "none"
    elif single_doc_covers_all:
        coverage = "full"
    else:
        coverage = "partial"

    return {
        "context": CONTEXT_SEPARATOR.join(blocks),
        "citations": citations,
        "chunk_count": len(chunks),
        "queryTerms": terms,
        "uncoveredTerms": [t for t in terms if t.lower() not in covered],
        "coverage": coverage,
    }


def postprocess_hits(
    hits: list[SearchHit],
    *,
    top_k: int,
    max_chunks_per_file: int = 3,
    max_total_chunks: int | None = None,
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

    다만 top_k × max_chunks_per_file 은 곱셈이라 큰 k 에서 응답이 폭발한다
    (top_k=20 → 최대 60청크 ≈ 6만 토큰). max_total_chunks 로 총량을 묶되,
    **문서마다 첫 청크는 먼저 보장하고** 남은 예산만 2번째·3번째 청크에
    나눠 준다. 순서를 반대로 하면 앞 문서가 예산을 다 먹고 뒤 문서가 통째로
    사라져, 이 함수의 존재 이유인 문서 다양성이 깨진다.
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

    selected = order[:top_k]
    # 문서당 1청크는 보장 — 예산이 문서 수보다 작아도 문서를 통째로 버리지 않는다
    budget = max(max_total_chunks or len(selected) * max_chunks_per_file, len(selected))
    take = {fid: 1 for fid in selected}
    spare = budget - len(selected)
    # 남은 예산은 관련도 순으로 한 개씩 돌려 나눈다(라운드로빈).
    # 앞 문서에 몰아주면 뒤 문서는 조문 하나치도 못 받는다.
    for _ in range(1, max_chunks_per_file):
        if spare <= 0:
            break
        for fid in selected:
            if spare <= 0:
                break
            if take[fid] < len(grouped[fid]):
                take[fid] += 1
                spare -= 1

    out: list[SearchHit] = []
    for fid in selected:
        chunks = grouped[fid][: take[fid]]
        head = chunks[0]
        if len(chunks) == 1:
            out.append(head)
            continue
        merged = CHUNK_JOINER.join(c.text for c in chunks)
        out.append(SearchHit(text=merged, score=head.score, source=head.source))
    return out
