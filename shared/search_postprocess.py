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
    ".doc",
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


def postprocess_hits(
    hits: list[SearchHit],
    *,
    top_k: int,
) -> list[SearchHit]:
    """unescape + fileId/본문 중복 제거 후 top_k.

    **Vertex 가 돌려준 순서를 그대로 유지한다.** retrieval 응답은 이미 관련도
    순으로 정렬돼 있는데, score 가 거리인지 유사도인지 추측해 변환한 값으로
    재정렬하면 추측이 틀렸을 때 순위가 통째로 뒤집힌다. 중복 제거가 '먼저 나온
    것'을 남기므로 파일당 엉뚱한 청크가 선택되는 결과까지 이어진다.
    점수는 표시용으로만 원값을 통과시킨다.
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

    seen_files: set[str] = set()
    seen_text: set[str] = set()
    out: list[SearchHit] = []
    for hit, is_sidecar in zip(prepared, sidecar_flags, strict=True):
        fid = hit.source.file_id
        if is_sidecar and fid in files_with_content:
            continue
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
