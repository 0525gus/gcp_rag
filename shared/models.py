"""도메인 모델 (표준 라이브러리 기반)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class DocStatus(str, Enum):
    PENDING = "PENDING"
    PARSED = "PARSED"
    INDEXED = "INDEXED"
    FAILED = "FAILED"
    DELETED = "DELETED"
    SKIPPED = "SKIPPED"


class ParseRoute(str, Enum):
    RHWP = "RHWP"  # rhwp-python (HWP 바이너리 → MD)
    HWPX = "HWPX"  # python-hwpx (HWPX ZIP+XML → MD)
    PDF_DOCAI = "PDF_DOCAI"  # 선택: QG_MODE=fallback
    GCS_EXPORT = "GCS_EXPORT"  # Google Workspace export → GCS
    GCS_COPY = "GCS_COPY"  # 원본 바이너리 → GCS
    NONE = "NONE"

    # 하위 호환 (구 상태 문서)
    KORDOC = "KORDOC"
    OSS_PARSER = "OSS_PARSER"


@dataclass
class DocState:
    file_id: str
    drive_id: str
    name: str = ""
    mime_type: str = ""
    modified_time: str | None = None
    content_hash: str | None = None
    status: DocStatus = DocStatus.PENDING
    parse_route: ParseRoute = ParseRoute.NONE
    last_synced_at: datetime | None = None
    error: str | None = None
    source_uri: str | None = None
    rag_file_id: str | None = None
    # Drive 폴더 경로 (예: 컴공/문서결재/2026 digital training/안내.pdf)
    path: str | None = None
    # 직계 자료묶음 폴더명
    bundle: str | None = None

    def to_firestore(self) -> dict[str, Any]:
        return {
            "fileId": self.file_id,
            "driveId": self.drive_id,
            "name": self.name,
            "mimeType": self.mime_type,
            "modifiedTime": self.modified_time,
            "contentHash": self.content_hash,
            "status": self.status.value if isinstance(self.status, DocStatus) else self.status,
            "parseRoute": (
                self.parse_route.value
                if isinstance(self.parse_route, ParseRoute)
                else self.parse_route
            ),
            "lastSyncedAt": self.last_synced_at,
            "error": self.error,
            "sourceUri": self.source_uri,
            "ragFileId": self.rag_file_id,
            "path": self.path,
            "bundle": self.bundle,
        }

    @classmethod
    def from_firestore(cls, data: dict[str, Any]) -> DocState:
        status_raw = data.get("status", DocStatus.PENDING.value)
        route_raw = data.get("parseRoute", ParseRoute.NONE.value)
        return cls(
            file_id=data.get("fileId") or data.get("file_id") or "",
            drive_id=data.get("driveId") or data.get("drive_id") or "",
            name=data.get("name") or "",
            mime_type=data.get("mimeType") or data.get("mime_type") or "",
            modified_time=data.get("modifiedTime") or data.get("modified_time"),
            content_hash=data.get("contentHash") or data.get("content_hash"),
            status=DocStatus(status_raw),
            parse_route=ParseRoute(route_raw),
            last_synced_at=data.get("lastSyncedAt") or data.get("last_synced_at"),
            error=data.get("error"),
            source_uri=data.get("sourceUri") or data.get("source_uri"),
            rag_file_id=data.get("ragFileId") or data.get("rag_file_id"),
            path=data.get("path"),
            bundle=data.get("bundle"),
        )


@dataclass
class ParseRequest:
    gcs_uri: str
    mime_type: str
    file_id: str

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ParseRequest:
        return cls(
            gcs_uri=data.get("gcsUri") or data["gcs_uri"],
            mime_type=data.get("mimeType") or data["mime_type"],
            file_id=data.get("fileId") or data["file_id"],
        )


@dataclass
class ParseResult:
    gcs_markdown_uri: str
    route: ParseRoute
    content_hash: str
    table_count: int = 0
    warnings: list[str] = field(default_factory=list)
    text_length: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "gcsMarkdownUri": self.gcs_markdown_uri,
            "route": self.route.value,
            "contentHash": self.content_hash,
            "tableCount": self.table_count,
            "warnings": self.warnings,
            "textLength": self.text_length,
        }


@dataclass
class SearchSource:
    file_id: str
    name: str = ""
    source_uri: str | None = None
    modified_time: str | None = None
    drive_id: str | None = None


@dataclass
class SearchHit:
    text: str
    score: float
    source: SearchSource


@dataclass
class DriveChange:
    file_id: str
    drive_id: str
    name: str = ""
    mime_type: str = ""
    modified_time: str | None = None
    removed: bool = False
    trashed: bool = False
    web_view_link: str | None = None
    md5_checksum: str | None = None
    parents: list[str] = field(default_factory=list)
