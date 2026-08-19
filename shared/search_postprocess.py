"""검색 결과 후처리 — fileId 정규화, unescape, score→relevance, 중복 제거."""

from __future__ import annotations

import html
import re
from dataclasses import replace
from typing import Any

from shared.lexical_rerank import query_terms
from shared.models import SearchHit

# 긴 suffix 먼저.
#
# 파이프라인이 실제로 만들어내는 확장자를 모두 담아야 한다. 빠진 확장자가 있으면
# extract_file_id 가 `abc.hwp` 를 fileId 로 되돌리지 못해 `abc.hwp` 를 그대로
# 반환하고, 그 값은 두 곳에서 조용히 잘못 쓰인다.
#   rag_engine._file_index   fileId 로 안 접혀 delete_files_by_ids 가 못 찾는다
#   sync._clean_file_ids     점이 들어가 _FILE_ID_RE 에 걸려 버려진다
# `.hwp`/`.hwpx` 는 hwp-original 버킷에, `.doc` 은 _ext_for_mime 이 source 버킷에 만든다.
_FILE_SUFFIXES = (
    ".meta.md",
    ".markdown",
    ".md",
    ".pdf",
    ".txt",
    ".html",
    ".htm",
    ".hwpx",
    ".hwp",
    ".docx",
    ".doc",
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


_SIDECAR_SUFFIX = ".meta.md"


def is_path_sidecar(display: str, source_uri: str | None = None) -> bool:
    """경로·자료묶음만 담은 합성 문서(``{fileId}.meta.md``)인지.

    바이너리(PDF/PPTX 등)는 본문과 sidecar 가 같은 fileId 로 함께 색인된다.
    sidecar 본문은 "이 파일은 자료묶음 X 소속입니다" 안내문뿐이라 질의에 답하지
    못한다 — 같은 파일의 본문 청크가 있으면 그쪽이 이겨야 한다.
    """
    for candidate in (display or "", source_uri or ""):
        if candidate.rsplit("/", 1)[-1].strip().lower().endswith(_SIDECAR_SUFFIX):
            return True
    return False


def unescape_chunk_text(text: str) -> str:
    """PDF 추출 HTML 엔티티·과도한 \\r 정리."""
    if not text:
        return ""
    out = html.unescape(text)
    out = out.replace("\r\n", "\n").replace("\r", "\n")
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip()


def _text_fingerprint(text: str, n: int = 180) -> str:
    t = re.sub(r"\s+", " ", (text or "").lower()).strip()
    return t[:n]


CHUNK_JOINER = "\n\n[...]\n\n"


CONTEXT_SEPARATOR = "\n\n---\n\n"

# 게시판 수집물처럼 제목이 폴더에 있고 파일명은 기계가 붙인 경우
_GENERIC_FILENAMES = frozenset({"content", "index", "body", "untitled"})


def citation_label(source: dict[str, Any]) -> str:
    """인용에 쓸, 사람이 읽고 문서를 특정할 수 있는 이름.

    파일명만으로는 문서를 못 가리는 경우가 많다.

    - 게시판 수집물은 제목이 폴더에 있고 파일명이 전부 `content.txt` 다.
      실측 1,155건 중 27건이며 **코퍼스에서 가장 흔한 파일명**(2위의 7배)이다.
      공지 본문이라 자주 걸리는데, 라벨이 `content.txt` 로만 나가면 여러 건이
      동시에 걸렸을 때 어느 공지인지 구분되지 않는다.
    - 첨부가 여러 개인 게시글이 표준이다(bundle 당 파일 중앙값 2개, 80.2% 가
      2개 이상, 최대 28개). `매뉴얼_pc.pdf` / `매뉴얼_mobile.pdf` 처럼
      같은 게시글의 다른 첨부인지 다른 게시글인지 파일명으로는 알 수 없다.

    그래서 자료묶음을 앞에 붙여 `자료묶음 / 파일명` 으로 만든다. 파일명이 이미
    자료묶음을 담고 있으면 중복이라 그대로 둔다.
    """
    name = (source.get("name") or "").strip()
    bundle = (source.get("bundle") or "").strip()
    if not name:
        return bundle or str(source.get("fileId") or "") or "(이름 없음)"
    if bundle and bundle != name and bundle not in name:
        return f"{bundle} / {name}"
    if bundle:
        return name
    # 자료묶음이 없는데 파일명도 기계가 붙인 것이면 경로가 유일한 단서다
    if name.rsplit(".", 1)[0].lower() in _GENERIC_FILENAMES:
        path = (source.get("path") or "").strip()
        if path:
            return path
    return name


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
        label = citation_label(src)
        blocks.append(f"[{n}] {label}\n{chunk.get('text', '')}")
        citations.append(
            {
                "n": n,
                "fileId": src.get("fileId"),
                "name": src.get("name"),
                "label": label,
                "bundle": src.get("bundle"),
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
        # `chunk_count` 는 이름과 달리 **문서(=context 블록) 수**다. search 결과
        # 한 항목이 청크 하나가 아니라 한 문서(청크 여러 개를 이어 붙인 것)이기
        # 때문이다. 이름을 바꾸면 FactChat 쪽이 깨지므로 남겨 두고, 뜻이 맞는
        # 이름을 함께 싣는다.
        "chunk_count": len(chunks),
        "documentCount": len(chunks),
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
    sidecar_flags: list[bool] = []
    for hit in hits:
        fid = extract_file_id(
            hit.source.file_id or hit.source.name,
            hit.source.source_uri,
        )
        text = unescape_chunk_text(hit.text)
        src = replace(hit.source, file_id=fid)
        prepared.append(SearchHit(text=text, score=hit.score, source=src))
        sidecar_flags.append(
            is_path_sidecar(hit.source.name, hit.source.source_uri)
        )

    # 본문 청크가 함께 걸린 파일의 sidecar 는 버린다. 파일당 '먼저 나온 것'을
    # 남기는 중복 제거 특성상, 제목·폴더명 질의에서 sidecar 가 상위에 오면 정작
    # 답이 든 본문이 통째로 사라진다. sidecar 만 걸린 파일은 그대로 둔다.
    files_with_content = {
        hit.source.file_id
        for hit, is_sidecar in zip(prepared, sidecar_flags, strict=True)
        if not is_sidecar
    }

    # 문서별로 묶되 처음 나온 순서(=관련도 순)를 보존한다
    order: list[str] = []
    grouped: dict[str, list[SearchHit]] = {}
    seen_text: set[str] = set()
    for hit, is_sidecar in zip(prepared, sidecar_flags, strict=True):
        fid = hit.source.file_id
        if is_sidecar and fid in files_with_content:
            continue
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
