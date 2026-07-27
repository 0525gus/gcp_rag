"""RAG Engine 크기 한도를 넘는 PDF를 페이지 단위로 분할.

Vertex RAG Engine 은 PDF 를 50MB 까지만 받는다. 한도를 넘는 문서를 통째로
버리면 검색에서 아예 사라지므로, 페이지 경계로 잘라 여러 조각으로 올린다.
청킹은 조각마다 독립적으로 일어나지만, 조각 경계가 페이지 경계라
문장이 잘리지는 않는다.
"""

from __future__ import annotations

import io
import logging

logger = logging.getLogger(__name__)

# 실제 조각이 한도에 아슬아슬하게 걸리지 않도록 여유를 둔다.
# (pypdf 로 다시 쓰면 원본보다 커지는 경우가 있다)
_SAFETY = 0.85


class PdfSplitError(RuntimeError):
    """분할 불가 — 단일 페이지가 한도를 넘는 등."""


def split_pdf(data: bytes, limit_bytes: int) -> list[bytes]:
    """PDF 를 limit_bytes 미만 조각들로 나눈다.

    한도 이하면 [data] 를 그대로 돌려준다(호출측 분기 단순화).
    페이지 하나가 한도를 넘으면 PdfSplitError.
    """
    if len(data) <= limit_bytes:
        return [data]

    from pypdf import PdfReader, PdfWriter

    target = int(limit_bytes * _SAFETY)
    reader = PdfReader(io.BytesIO(data))
    total = len(reader.pages)
    if total <= 1:
        raise PdfSplitError(
            f"단일 페이지가 한도 초과라 분할 불가: {len(data)}B > {limit_bytes}B"
        )

    parts: list[bytes] = []
    writer = PdfWriter()
    pages_in_writer = 0

    def flush() -> None:
        nonlocal writer, pages_in_writer
        if pages_in_writer == 0:
            return
        buf = io.BytesIO()
        writer.write(buf)
        parts.append(buf.getvalue())
        writer = PdfWriter()
        pages_in_writer = 0

    for idx in range(total):
        writer.add_page(reader.pages[idx])
        pages_in_writer += 1

        buf = io.BytesIO()
        writer.write(buf)
        size = buf.tell()
        if size <= target:
            continue

        if pages_in_writer == 1:
            # 이 페이지 하나만으로 한도를 넘는다 — 더 쪼갤 수 없다.
            raise PdfSplitError(
                f"페이지 {idx + 1} 단독으로 한도 초과: {size}B > {target}B"
            )

        # 방금 넣은 페이지를 빼고 확정한 뒤, 그 페이지로 새 조각을 시작한다.
        pages = list(range(idx - pages_in_writer + 1, idx))
        writer = PdfWriter()
        for p in pages:
            writer.add_page(reader.pages[p])
        pages_in_writer = len(pages)
        flush()

        writer.add_page(reader.pages[idx])
        pages_in_writer = 1

    flush()

    oversized = [i for i, p in enumerate(parts) if len(p) > limit_bytes]
    if oversized:
        raise PdfSplitError(f"분할 후에도 한도 초과 조각 있음: {oversized}")

    logger.info(
        "PDF split: %sB (%s pages) -> %s parts (%s)",
        len(data), total, len(parts), [len(p) for p in parts],
    )
    return parts
