"""도메인 모델 (표준 라이브러리 기반)."""

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
    """모르는 값이면 fallback. Firestore 를 읽다 죽지 않게 한다.

    parser·sync·mcp 가 각각 배포되므로 한쪽이 새 enum 값을 쓰기 시작하면
    구버전인 다른 쪽이 그 문서를 읽다가 ValueError 로 죽는다. 예를 들어 parser 가
    parseRoute="HWPX" 를 기록한 뒤 구버전 mcp 가 검색마다 doc_state 를 읽으면
    검색 전체가 실패한다. 배포 순서에 의존하는 대신 여기서 흡수한다.
    """
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
    """문서 상태.

    `SKIPPED` 와 `EXCLUDED` 의 구분이 중요하다 — 한동안 둘이 뭉쳐 있었다.

      SKIPPED   우리 **대상인데** 처리하지 못했다
                (미지원 MIME, 암호 걸린 xlsx/PDF 등)
                → 집계 대상. 늘어나면 손봐야 할 신호다.
      EXCLUDED  **애초에 대상이 아니다** (동기화 지정 폴더 밖)
                → 집계에서 뺀다. 우리가 할 일이 없는 문서다.

    뭉쳐 있을 때는 393건 전부가 폴더 밖인데도 `SKIPPED` 로 잡혀
    `accounted` 에 들어갔고, 그래서 "대상인데 처리 못 한 것"이 몇 건인지
    알 수 없었다.
    """

    PENDING = "PENDING"
    PARSED = "PARSED"
    INDEXED = "INDEXED"
    FAILED = "FAILED"
    DELETED = "DELETED"
    SKIPPED = "SKIPPED"
    EXCLUDED = "EXCLUDED"


class Audience(str, Enum):
    """이 문서를 어느 코퍼스에 실을 것인가.

    STAFF 가 기본값인 것이 이 enum 의 핵심이다. 판정에 실패했거나, 필드가 없는
    구 문서이거나, 모르는 값이 들어오면 **전부 STAFF 로 떨어진다** — 즉 학생
    코퍼스에는 실리지 않는다. 반대로 기본을 STUDENT 로 두면 판정 실패 한 번이
    곧 노출이 된다.
    """

    STUDENT = "STUDENT"  # 학생자료 폴더 트리 — 학생·교직원 코퍼스 양쪽에 실림
    STAFF = "STAFF"  # 그 외 전부 — 교직원 코퍼스에만


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
    # 노출 범위. 필드가 없는 구 문서는 from_firestore 가 STAFF 로 읽는다.
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
            # 모르는 값은 흡수한다 — 서비스별 배포 시차로 신규 enum 값을 만날 수 있다.
            # status 는 PENDING 으로 떨어뜨려 '스킵'이 아니라 '재처리' 쪽으로 안전하게 실패시킨다.
            status=_coerce_enum(DocStatus, status_raw, DocStatus.PENDING),
            parse_route=_coerce_enum(ParseRoute, route_raw, ParseRoute.NONE),
            last_synced_at=data.get("lastSyncedAt") or data.get("last_synced_at"),
            error=data.get("error"),
            source_uri=data.get("sourceUri") or data.get("source_uri"),
            rag_file_id=data.get("ragFileId") or data.get("rag_file_id"),
            path=data.get("path"),
            bundle=data.get("bundle"),
            # 필드가 없는 구 문서(= 분리 도입 이전 1,155건)는 STAFF 로 읽힌다.
            # 학생 코퍼스는 재색인으로 명시적으로 채워야 한다 — 기본값이 조용히
            # 학생에게 문서를 여는 일은 없다.
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
    created_time: str | None = None
    modified_time: str | None = None
    removed: bool = False
    trashed: bool = False
    web_view_link: str | None = None
    md5_checksum: str | None = None
    # Drive 가 주는 blob 크기. 다운로드 전에 거르는 데 쓴다.
    # Google 네이티브(Docs/Sheets/Slides)는 blob 이 아니라 None.
    size_bytes: int | None = None
    parents: list[str] = field(default_factory=list)
