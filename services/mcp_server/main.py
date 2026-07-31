"""
MCP 서버 (Cloud Run) — FactChat 등 원격 MCP 커넥터용 Streamable HTTP.

tool: search / answer
인증: MCP_API_KEY 설정 시 Authorization: Bearer <key> 또는 X-API-Key 필수
"""

from __future__ import annotations

import copy
import logging
import os
import sys
import threading
import time
from collections import OrderedDict
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
from shared.lexical_rerank import query_terms, rrf_rerank, term_coverage  # noqa: E402
from shared.rag_engine import RagEngineClient  # noqa: E402
from shared.search_postprocess import (  # noqa: E402
    build_answer_payload,
    citation_label,
    postprocess_hits,
)

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


# --------------------------------------------------------------- 동일 질의 캐시
# 호출측 에이전트가 같은 질의를 그대로 반복한다. 실측(7/29~7/30 운영 로그):
# 50회 호출 중 고유 질의 37개 — 13회(26%)가 **바이트 단위 동일**한 재질의였고,
# 한 질의는 8번 반복됐다(18분간 5차례 버스트, 21초에 8회가 몰린 구간도 있다).
#
# 툴 설명에 "같은 의도로 다시 검색하지 마세요"를 넣어 봤지만(4eaeea8) 지시문은
# 지켜지지 않았다. 그 커밋도 "지시문은 무시당해도 데이터는 남으므로"라고 적어
# 한계를 예상했다 — 남은 레버는 서버 쪽이다.
#
# 반환값이 완전히 같으므로 호출측 동작은 달라지지 않는다. 코퍼스는 하루 한 번
# 바뀌므로 짧은 TTL 로 stale 위험이 사실상 없다. 0 이면 캐시를 끈다.
_CACHE_TTL = float(os.environ.get("SEARCH_CACHE_TTL_SECONDS", "60"))
_CACHE_MAX = int(os.environ.get("SEARCH_CACHE_MAX_ENTRIES", "128"))
_CacheKey = tuple[str, int, str | None]
_cache: OrderedDict[_CacheKey, tuple[float, list[dict[str, Any]]]] = OrderedDict()
_cache_lock = threading.Lock()


def _cache_get(key: _CacheKey) -> list[dict[str, Any]] | None:
    if _CACHE_TTL <= 0:
        return None
    with _cache_lock:
        hit = _cache.get(key)
        if hit is None:
            return None
        stored_at, value = hit
        if time.monotonic() - stored_at > _CACHE_TTL:
            del _cache[key]
            return None
        _cache.move_to_end(key)
    # 호출측이 리스트를 만지더라도 캐시가 오염되지 않게 사본을 준다
    return copy.deepcopy(value)


def _cache_put(key: _CacheKey, value: list[dict[str, Any]]) -> None:
    if _CACHE_TTL <= 0:
        return
    with _cache_lock:
        _cache[key] = (time.monotonic(), copy.deepcopy(value))
        _cache.move_to_end(key)
        while len(_cache) > _CACHE_MAX:
            _cache.popitem(last=False)


@mcp.tool()
def search(
    query: str,
    top_k: int | None = None,
    drive_id: str | None = None,
) -> list[dict[str, Any]]:
    """사내/공공 문서 벡터스토어에서 관련 청크를 검색합니다.

    반환된 청크는 **서로 독립적인 문서**입니다. 같은 사실을 함께 뒷받침한다는
    보장이 없으므로, 여러 청크의 내용을 하나의 서술로 합치지 마세요.
    문장마다 근거가 된 문서를 구분하고, 청크에 적혀 있지 않은 관계
    (예: 어느 문서의 인물이 다른 문서의 업무 담당자라는 연결)를 지어내지 마세요.

    각 청크의 `missingTerms` 는 그 청크 본문에 **없는** 질의어입니다.
    질문의 핵심어가 `missingTerms` 에 들어 있으면, 그 청크는 그 부분에 대한
    근거가 아닙니다. 어떤 청크도 질문에 답하지 못할 때는 주변 정보를 나열하는
    대신 '확인되지 않는다'는 결론을 먼저 밝혀 주세요.

    **같은 의도로 다시 검색하지 마세요.** `missingTerms` 는 '그 내용이 코퍼스에
    없다'는 뜻이지 다시 찾으라는 뜻이 아닙니다. 표현을 바꿔 재질의하거나 top_k 를
    올려도 없는 문서가 생기지는 않으며, 호출마다 수만 토큰이 누적됩니다.
    한 질문에는 원칙적으로 **한 번만** 호출하고, 다시 부를 때는 앞선 결과로는
    답할 수 없는 **새로운 정보**를 찾을 때로 한정하세요.
    기본 top_k 로 충분합니다 — 결과가 부족하면 값을 키우지 말고 없다고 답하세요.

    반환 배열의 한 항목은 청크 하나가 아니라 **문서 하나**입니다. 한 문서에서
    여러 청크가 걸리면 `[...]` 로 이어 붙여 한 항목으로 옵니다.

    Args:
        query: 검색 질의 (자연어)
        top_k: 반환할 최대 **문서** 수 (기본 5, 청크 수가 아님).
            문서마다 청크가 여러 개 붙을 수 있어 실제 청크 수는 이 값보다
            많습니다(top_k=5 면 대략 5~15청크). 총 청크 수에는 별도 상한이
            있어, top_k 를 올려도 받는 본문 양은 그만큼 늘지 않고 **문서가
            얕게 여러 건 오는 쪽으로만** 바뀝니다.
        drive_id: 특정 공유 드라이브로 필터 (선택)
    """
    k = top_k or settings.top_k_default
    k = max(1, min(k, settings.search_top_k_max))
    logger.info("search query=%r top_k=%s drive_id=%s", query, k, drive_id)

    cache_key = (query, k, drive_id)
    cached = _cache_get(cache_key)
    if cached is not None:
        logger.info("search cache hit query=%r top_k=%s", query, k)
        return cached

    rag = RagEngineClient(settings)
    # 여유분 retrieve 후 후처리(파일당 1청크 중복 제거)로 k개.
    # 상한을 k*배수보다 낮게 두면 큰 k 에서 여유분이 사라져 k 개를 못 채운다.
    fetch_k = min(
        settings.search_fetch_max,
        max(k * settings.search_fetch_multiplier, k),
    )
    # 거리 상한 — 코퍼스 범위 밖 질문에 무관한 문서를 물어다 주지 않도록.
    # 0 이하면 필터를 끈다.
    threshold = settings.search_distance_threshold
    raw_hits = rag.retrieve(
        query,
        top_k=fetch_k,
        vector_distance_threshold=threshold if threshold > 0 else None,
    )
    # 어휘 순위를 섞어 상위를 다시 세운다(후보 안에서만, recall 불변).
    # postprocess_hits 는 들어온 순서를 그대로 존중하므로 여기서 정렬해 넘긴다.
    if settings.search_lexical_rerank and len(raw_hits) > 1:
        order = rrf_rerank(query, [h.text for h in raw_hits])
        raw_hits = [raw_hits[i] for i in order]
    hits = postprocess_hits(
        raw_hits,
        top_k=k,
        max_chunks_per_file=settings.search_max_chunks_per_file,
        max_total_chunks=settings.search_max_total_chunks,
    )
    if not hits:
        # 필터가 전부 걸러낸 경우 — 임계값 조정 판단 근거로 남긴다
        logger.info(
            "search no-hit query=%r fetched=%s threshold=%s",
            query, len(raw_hits), threshold,
        )

    store = DocStateStore(settings)
    # 질의어별 근거 유무를 청크마다 붙인다. 지시문은 무시당해도 데이터는
    # 남으므로, 호출 LLM 이 '이 문서는 질의의 어느 부분을 덮는가'를 스스로
    # 판단할 수 있어야 서로 다른 문서를 하나로 합치는 답이 줄어든다.
    terms = query_terms(query)
    results: list[dict[str, Any]] = []
    for hit in hits:
        meta = store.get(hit.source.file_id)
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
        # 파일명만으로는 문서를 못 가리는 코퍼스라(게시판 수집물 27건이 전부
        # content.txt, 게시글당 첨부 중앙값 2개) 자료묶음을 붙인 표시용 이름을
        # 함께 싣는다. name 은 원래 파일명 그대로 둔다.
        source["label"] = citation_label(source)
        matched, missing = term_coverage(terms, hit.text)
        results.append(
            {
                "text": hit.text,
                # Vertex 원값. 거리/유사도 여부가 확정되지 않아 정규화하지 않는다.
                # 관련도 순위는 배열 순서(rank)가 기준.
                "score": round(hit.score, 6),
                "scoreType": "vertex_raw",
                "rank": len(results) + 1,
                "matchedTerms": matched,
                "missingTerms": missing,
                "source": source,
            }
        )
    _cache_put(cache_key, results)
    return results


@mcp.tool()
def answer(query: str, top_k: int | None = None) -> dict[str, Any]:
    """검색 청크+출처를 묶어 반환합니다. 최종 답변 생성은 호출 LLM이 담당합니다.

    `context` 는 문서마다 `[n] 파일명` 라벨이 붙은 블록입니다. 인용은 그 번호를
    따르고, **블록 경계를 넘어 내용을 합치지 마세요.** 라벨이 없던 이전 형식에서는
    어느 문장이 어느 문서에서 왔는지 복원할 수 없었습니다.

    - `uncoveredTerms` 가 비어 있지 않으면, 그 검색어를 담은 근거가 하나도
      없다는 뜻입니다. 답을 지어내지 말고 확인되지 않음을 밝히거나 되물으세요.
    - `coverage="partial"` 은 어떤 문서 하나도 질의 전체를 덮지 못했다는 뜻이며,
      여러 문서를 이어 붙여 답을 만들면 근거 없는 결합이 됩니다.

    Args:
        query: 검색 질의 (자연어)
        top_k: 근거로 쓸 최대 **문서** 수 (기본 5, 청크 수가 아님).
            자세한 의미는 `search` 의 같은 인자 설명을 참고하세요.
    """
    return build_answer_payload(search(query=query, top_k=top_k), query)


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
