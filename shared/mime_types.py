"""MIME 분기 — 모든 지원 포맷은 GCS로 수렴 후 RAG import (Drive 커넥터 미사용)."""

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
    # 나머지(md/txt/html/json/pptx/csv)는 10MB
}
RAG_DEFAULT_LIMIT_BYTES = 10 * _MB


def rag_size_limit(filename_or_ext: str) -> int:
    """확장자에 해당하는 RAG Engine 파일 크기 한도(바이트)."""
    name = (filename_or_ext or "").lower()
    ext = name if name.startswith(".") else Path(name).suffix
    return _RAG_LIMIT_BY_EXT.get(ext, RAG_DEFAULT_LIMIT_BYTES)


class RouteKind(str, Enum):
    """워크플로우 라우팅 키."""

    HWP_PARSE = "HWP_PARSE"  # 파서 → 정규화 md → GCS
    GOOGLE_EXPORT = "GOOGLE_EXPORT"  # Drive export → GCS
    # 파서 서비스를 안 거치고 Drive→GCS 로 직행. 이름과 달리 단순 복사만은
    # 아니다(PDF 분할·XLSX 표 변환 포함) — 처리는 _ingest_direct 참고.
    FILE_COPY = "FILE_COPY"
    SKIP = "SKIP"
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

# Google Workspace → export 후 GCS
GOOGLE_NATIVE_MIME: frozenset[str] = frozenset(
    {
        "application/vnd.google-apps.document",
        "application/vnd.google-apps.presentation",
        "application/vnd.google-apps.spreadsheet",
    }
)

# Drive에서 바이너리 그대로 받아 GCS로 복사
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
        "text/csv",
        "application/rtf",
    }
)

# Google export 대상 MIME → (exportMime, 확장자)
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


def classify_route(mime_type: str, name: str = "", *, removed: bool = False) -> RouteKind:
    if removed:
        return RouteKind.DELETE
    if is_hwp_family(mime_type, name):
        return RouteKind.HWP_PARSE
    mt = (mime_type or "").lower()
    if mt in GOOGLE_NATIVE_MIME:
        return RouteKind.GOOGLE_EXPORT
    if mt in FILE_COPY_MIME:
        return RouteKind.FILE_COPY
    return RouteKind.SKIP


# 하위 호환 별칭
NATIVE_MIME_TYPES = FILE_COPY_MIME | GOOGLE_NATIVE_MIME
