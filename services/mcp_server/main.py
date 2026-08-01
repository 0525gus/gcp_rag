"""
MCP 서버 (Cloud Run) — FactChat 등 원격 MCP 커넥터용 Streamable HTTP.

tool: search / answer
인증: MCP_API_KEY 설정 시 Authorization: Bearer <key> 또는 X-API-Key 필수
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from shared.config import get_settings  # noqa: E402
from shared.firestore_state import DocStateStore  # noqa: E402
from shared.logging_config import setup_logging  # noqa: E402
from shared.models import DocStatus  # noqa: E402
from shared.rag_engine import RagEngineClient  # noqa: E402
from shared.search_postprocess import postprocess_hits  # noqa: E402

setup_logging()
logger = logging.getLogger("mcp_server")

settings = get_settings()
MCP_API_KEY = os.environ.get("MCP_API_KEY", "").strip()

mcp = FastMCP(
    "rag-search",
    host="0.0.0.0",
    port=int(os.environ.get("PORT", "8080")),
    stateless_http=True,
)


class ApiKeyMiddleware(BaseHTTPMiddleware):
    """FactChat 등 외부 커넥터용 단순 API 키 게이트."""

    async def dispatch(self, request: Request, call_next):  # type: ignore[no-untyped-def]
        if not MCP_API_KEY:
            return await call_next(request)
        if request.url.path in {"/health", "/"}:
            return await call_next(request)

        auth = request.headers.get("authorization") or ""
        x_key = request.headers.get("x-api-key") or ""
        token = ""
        if auth.lower().startswith("bearer "):
            token = auth[7:].strip()
        elif x_key:
            token = x_key.strip()

        if token != MCP_API_KEY:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return await call_next(request)


@mcp.tool()
def search(
    query: str,
    top_k: int | None = None,
    drive_id: str | None = None,
) -> list[dict[str, Any]]:
    """사내/공공 문서 벡터스토어에서 관련 청크를 검색합니다.

    Args:
        query: 검색 질의 (자연어)
        top_k: 반환할 최대 청크 수 (기본 5)
        drive_id: 특정 공유 드라이브로 필터 (선택)
    """
    k = top_k or settings.top_k_default
    k = max(1, min(k, 20))
    logger.info("search query=%r top_k=%s drive_id=%s", query, k, drive_id)

    rag = RagEngineClient(settings)
    # 여유분 retrieve 후 후처리(중복 제거)로 좁힌다.
    fetch_k = min(20, max(k * 3, k))
    raw_hits = rag.retrieve(query, top_k=fetch_k)
    # 여기서 k 로 자르면 아래 필터(SKIPPED/DELETED/driveId)가 걷어낸 자리가 빈 채로
    # 남아 top_k 보다 적게 반환된다. 자르기는 필터를 통과한 뒤에 한다.
    hits = postprocess_hits(raw_hits, top_k=fetch_k)

    store = DocStateStore(settings)
    results: list[dict[str, Any]] = []
    for hit in hits:
        meta = store.get(hit.source.file_id)
        if meta and (
            meta.status in {DocStatus.SKIPPED, DocStatus.DELETED}
            or (
                meta.status == DocStatus.FAILED
                and (meta.error or "").startswith("out_of_folder_scope_cleanup_failed")
            )
        ):
            # Defense in depth while asynchronous corpus cleanup/retry converges.
            continue
        if drive_id and meta and meta.drive_id != drive_id:
            continue
        display_name = (
            (meta.name if meta and meta.name else None)
            or hit.source.name
            or hit.source.file_id
        )
        # displayName이 fileId.ext 형태면 숨김
        if display_name.endswith((".pdf", ".md", ".txt", ".pptx", ".xlsx")) and (
            not meta or not meta.name
        ):
            display_name = (meta.path if meta and meta.path else display_name)

        source = {
            "fileId": hit.source.file_id,
            "name": display_name,
            "path": meta.path if meta else None,
            "bundle": meta.bundle if meta else None,
            "sourceUri": (
                (meta.source_uri if meta and meta.source_uri else None)
                or hit.source.source_uri
            ),
            "modifiedTime": (
                meta.modified_time if meta else hit.source.modified_time
            ),
            "driveId": meta.drive_id if meta else None,
        }
        results.append(
            {
                "text": hit.text,
                # Vertex 원값. 거리/유사도 여부가 확정되지 않아 정규화하지 않는다.
                # 관련도 순위는 배열 순서(rank)가 기준.
                "score": round(hit.score, 6),
                "scoreType": "vertex_raw",
                "rank": len(results) + 1,
                "source": source,
            }
        )
        if len(results) >= k:
            break
    return results


@mcp.tool()
def answer(query: str, top_k: int | None = None) -> dict[str, Any]:
    """검색 청크+출처를 묶어 반환합니다. 최종 답변 생성은 호출 LLM이 담당합니다."""
    chunks = search(query=query, top_k=top_k)
    citations = [
        {
            "fileId": c["source"]["fileId"],
            "name": c["source"]["name"],
            "sourceUri": c["source"]["sourceUri"],
        }
        for c in chunks
    ]
    return {
        "context": "\n\n---\n\n".join(c["text"] for c in chunks),
        "citations": citations,
        "chunk_count": len(chunks),
    }


@mcp.custom_route("/health", methods=["GET"])
async def health(_request: Request):  # type: ignore[no-untyped-def]
    return JSONResponse(
        {
            "status": "ok",
            "service": "rag-mcp",
            "auth": "api_key" if MCP_API_KEY else "none",
        }
    )


def build_app():
    """ASGI 앱 (+ API 키 미들웨어)."""
    app = mcp.streamable_http_app()
    app.add_middleware(ApiKeyMiddleware)
    return app


def main() -> None:
    import uvicorn

    port = int(os.environ.get("PORT", "8080"))
    app = build_app()
    logger.info(
        "Starting MCP streamable-http port=%s api_key=%s",
        port,
        "set" if MCP_API_KEY else "disabled",
    )
    uvicorn.run(app, host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
