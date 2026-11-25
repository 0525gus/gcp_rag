from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, TypeVar

logger = logging.getLogger(__name__)

_E = TypeVar("_E", bound=Enum)


def _coerce_enum(enum_cls: type[_E], raw: Any, fallback: _E) -> _E:  # noqa: UP047
    # UP047(PEP 695 제네릭)은 3.12+ 전용 문법이라 requires-python=">=3.11" 을 깬다.
    if isinstance(raw, enum_cls):
        return raw
    try:
        return enum_cls(raw)
    except ValueError:
        logger.warning(
            "unknown %s value %r — falling back to %s",
            enum_cls.__name__,
            raw,
            fallback.value,
        )
        return fallback


class DocStatus(str, Enum):
    PENDING = "PENDING"
    PARSED = "PARSED"
    INDEXED = "INDEXED"
    FAILED = "FAILED"
    DELETED = "DELETED"
    SKIPPED = "SKIPPED"
    EXCLUDED = "EXCLUDED"


class Audience(str, Enum):
    STUDENT = "STUDENT"
    STAFF = "STAFF"


class ParseRoute(str, Enum):
    RHWP = "RHWP"
    HWPX = "HWPX"
    PDF_DOCAI = "PDF_DOCAI"
    IMAGE_DOCAI = "IMAGE_DOCAI"
    GCS_EXPORT = "GCS_EXPORT"
    GCS_COPY = "GCS_COPY"
    NONE = "NONE"

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
    path: str | None = None
    bundle: str | None = None
    source_title: str | None = None
    website_url: str | None = None
    metadata_file_id: str | None = None
    audience: Audience = Audience.STAFF

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
            "sourceTitle": self.source_title,
            "websiteUrl": self.website_url,
            "metadataFileId": self.metadata_file_id,
            "audience": (
                self.audience.value
                if isinstance(self.audience, Audience)
                else self.audience
            ),
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
            status=_coerce_enum(DocStatus, status_raw, DocStatus.PENDING),
            parse_route=_coerce_enum(ParseRoute, route_raw, ParseRoute.NONE),
            last_synced_at=data.get("lastSyncedAt") or data.get("last_synced_at"),
            error=data.get("error"),
            source_uri=data.get("sourceUri") or data.get("source_uri"),
            rag_file_id=data.get("ragFileId") or data.get("rag_file_id"),
            path=data.get("path"),
            bundle=data.get("bundle"),
            source_title=data.get("sourceTitle"),
            website_url=data.get("websiteUrl"),
            metadata_file_id=data.get("metadataFileId"),
            audience=_coerce_enum(
                Audience, data.get("audience", Audience.STAFF.value), Audience.STAFF
            ),
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
class SearchChunk:
    text: str
    score: float


@dataclass
class SearchHit:
    text: str
    score: float
    source: SearchSource
    chunks: tuple[SearchChunk, ...] = ()


@dataclass
class DriveChange:
    file_id: str
    drive_id: str
    name: str = ""
    mime_type: str = ""
    created_time: str | None = None
    modified_time: str | None = None
    removed: bool = False
    trashed: bool = False
    web_view_link: str | None = None
    md5_checksum: str | None = None
    size_bytes: int | None = None
    parents: list[str] = field(default_factory=list)
