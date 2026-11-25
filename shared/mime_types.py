from __future__ import annotations

from enum import Enum
from pathlib import Path

_MB = 1024 * 1024

# RAG Engine 기본 파서의 타입별 파일 크기 한도.
# 우리가 정한 값이 아니라 Vertex 쪽 제약이라, 넘겨서 올려봐야 import 에서 거부된다.
# https://cloud.google.com/vertex-ai/generative-ai/docs/rag-engine/supported-documents
_RAG_LIMIT_BY_EXT: dict[str, int] = {
    ".pdf": 50 * _MB,
    ".docx": 50 * _MB,
}
RAG_DEFAULT_LIMIT_BYTES = 10 * _MB


def rag_size_limit(filename_or_ext: str) -> int:
    name = (filename_or_ext or "").lower()
    ext = name if name.startswith(".") else Path(name).suffix
    return _RAG_LIMIT_BY_EXT.get(ext, RAG_DEFAULT_LIMIT_BYTES)


class RouteKind(str, Enum):
    HWP_PARSE = "HWP_PARSE"
    IMAGE_OCR = "IMAGE_OCR"
    GOOGLE_EXPORT = "GOOGLE_EXPORT"
    FILE_COPY = "FILE_COPY"
    SKIP = "SKIP"
    EXCLUDE = "EXCLUDE"
    DELETE = "DELETE"


HWP_MIME_TYPES: frozenset[str] = frozenset(
    {
        "application/x-hwp",
        "application/haansofthwp",
        "application/vnd.hancom.hwp",
        "application/hwp",
        "application/x-hwp+zip",
        "application/hwp+zip",
        "application/haansofthwpx",
        "application/vnd.hancom.hwpx",
        "application/hwpx",
    }
)

GOOGLE_NATIVE_MIME: frozenset[str] = frozenset(
    {
        "application/vnd.google-apps.document",
        "application/vnd.google-apps.presentation",
        "application/vnd.google-apps.spreadsheet",
    }
)

SIDECAR_ONLY_MIME: frozenset[str] = frozenset(
    {
        "application/zip",
        "application/x-zip-compressed",
        "application/vnd.ms-excel",
    }
)

FILE_COPY_MIME: frozenset[str] = frozenset(
    {
        "application/pdf",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/msword",
        "text/plain",
        "text/markdown",
        "text/html",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.ms-excel.sheet.macroenabled.12",
        "text/csv",
        "application/rtf",
    }
    | SIDECAR_ONLY_MIME
)

GOOGLE_EXPORT_MAP: dict[str, tuple[str, str]] = {
    "application/vnd.google-apps.document": (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".docx",
    ),
    "application/vnd.google-apps.presentation": (
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        ".pptx",
    ),
    "application/vnd.google-apps.spreadsheet": (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ".xlsx",
    ),
}


def is_hwp_family(mime_type: str, name: str = "") -> bool:
    mt = (mime_type or "").lower().strip()
    if mt in HWP_MIME_TYPES:
        return True
    lower = name.lower()
    return lower.endswith(".hwp") or lower.endswith(".hwpx")


def is_hwpx(mime_type: str, name: str = "") -> bool:
    mt = (mime_type or "").lower()
    if "hwpx" in mt or "hwp+zip" in mt or "x-hwp+zip" in mt:
        return True
    return name.lower().endswith(".hwpx")


IMAGE_OCR_MIME = {"image/png": ".png", "image/jpeg": ".jpg"}
IMAGE_OCR_MAX_BYTES = 10 * _MB
IMAGE_OCR_MAX_PIXELS = 40_000_000

PDF_REVIEW_BYTES = 100 * _MB
# 이미지 위주 PDF OCR은 Document AI 온라인 처리 한도 안에서만 돌린다.
# 넘으면 기존처럼 원본(텍스트가 있으면) 또는 경로 사이드카만 색인한다.
PDF_OCR_MAX_BYTES = 20 * _MB
PDF_OCR_MAX_PAGES = 15
PDF_TEXT_PAGE_MIN_CHARS = 20


def classify_route(
    mime_type: str, name: str = "", *, removed: bool = False, enable_image_ocr: bool = False
) -> RouteKind:
    if removed:
        return RouteKind.DELETE
    if is_hwp_family(mime_type, name):
        return RouteKind.HWP_PARSE
    mt = (mime_type or "").lower()
    if mt in IMAGE_OCR_MIME:
        return RouteKind.IMAGE_OCR if enable_image_ocr else RouteKind.SKIP
    if mt in GOOGLE_NATIVE_MIME:
        return RouteKind.GOOGLE_EXPORT
    if mt in FILE_COPY_MIME:
        return RouteKind.FILE_COPY
    return RouteKind.SKIP


NATIVE_MIME_TYPES = FILE_COPY_MIME | GOOGLE_NATIVE_MIME
