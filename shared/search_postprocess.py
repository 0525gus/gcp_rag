from __future__ import annotations

import html
import re
from dataclasses import replace
from typing import Any

from shared.models import SearchChunk, SearchHit

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


_PART_SUFFIX = re.compile(r"\.part\d+$", re.IGNORECASE)


def extract_file_id(display: str, source_uri: str | None = None) -> str:
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
    for candidate in (display or "", source_uri or ""):
        if candidate.rsplit("/", 1)[-1].strip().lower().endswith(_SIDECAR_SUFFIX):
            return True
    return False


def unescape_chunk_text(text: str) -> str:
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


_GENERIC_FILENAMES = frozenset({"content", "index", "body", "untitled"})


def citation_label(source: dict[str, Any]) -> str:
    post_title = (source.get("title") or "").strip()
    name = (source.get("name") or "").strip()
    if post_title:
        return f"{post_title} / {name}" if name and name != post_title else post_title
    bundle = (source.get("bundle") or "").strip()
    if not name:
        return bundle or str(source.get("fileId") or "") or "(이름 없음)"
    if bundle and bundle != name and bundle not in name:
        return f"{bundle} / {name}"
    if bundle:
        return name
    if name.rsplit(".", 1)[0].lower() in _GENERIC_FILENAMES:
        path = (source.get("path") or "").strip()
        if path:
            return path
    return name


def postprocess_hits(
    hits: list[SearchHit],
    *,
    top_k: int,
    max_chunks_per_file: int = 3,
    max_total_chunks: int | None = None,
) -> list[SearchHit]:
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

    files_with_content = {
        hit.source.file_id
        for hit, is_sidecar in zip(prepared, sidecar_flags, strict=True)
        if not is_sidecar
    }

    order: list[str] = []
    grouped: dict[str, list[SearchHit]] = {}
    seen_text: set[str] = set()
    for hit, is_sidecar in zip(prepared, sidecar_flags, strict=True):
        fid = hit.source.file_id
        if is_sidecar and fid in files_with_content:
            continue
        fp = _text_fingerprint(hit.text)
        if fp and fp in seen_text:
            continue
        if fp:
            seen_text.add(fp)
        if fid not in grouped:
            grouped[fid] = []
            order.append(fid)
        if len(grouped[fid]) < max_chunks_per_file:
            grouped[fid].append(hit)

    limit = min(top_k, max_total_chunks) if max_total_chunks is not None else top_k
    selected = order[:max(0, limit)]
    budget = max_total_chunks if max_total_chunks is not None else len(selected) * max_chunks_per_file
    take = {fid: 1 for fid in selected}
    spare = budget - len(selected)
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
        merged = CHUNK_JOINER.join(c.text for c in chunks)
        out.append(SearchHit(
            text=merged, score=head.score, source=head.source,
            chunks=tuple(SearchChunk(text=c.text, score=c.score) for c in chunks),
        ))
    return out
