"""
MCP 서버 (Cloud Run) — FactChat 등 원격 MCP 커넥터용 Streamable HTTP.

tool: search
인증: MCP_API_KEY 설정 시 Authorization: Bearer <key> 또는 X-API-Key 필수
"""

from __future__ import annotations

import copy
import asyncio
import hmac
import logging
import os
import sys
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import Context, FastMCP
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from shared.config import get_settings  # noqa: E402
from shared.firestore_state import DocStateStore  # noqa: E402
from shared.logging_config import setup_logging  # noqa: E402
from shared.lexical_rerank import normalize_title, rrf_rerank  # noqa: E402
from shared.models import DocStatus  # noqa: E402
from shared.rag_engine import RagEngineClient  # noqa: E402
from shared.search_postprocess import (  # noqa: E402
    citation_label,
    postprocess_hits,
)

from shared.html_text import clean_html_evidence  # noqa: E402
from shared.source_links import citation_view_uri  # noqa: E402
from shared.mcp_routing import RouteRegistry, SearchScope  # noqa: E402
from shared.model_reranker import (  # noqa: E402
    RerankRecord,
    cross_encoder_order,
    llm_reranker_order,
)
from shared.query_rewriter import rewrite_query, should_rewrite_query  # noqa: E402
from shared.retrieval_channels import RETRIEVAL_CHANNELS, rank_channel  # noqa: E402
from shared.search_fusion import rrf_merge_hit_lists  # noqa: E402
from shared.search_response import (  # noqa: E402
    EvidenceDocument,
    SearchResponse,
    build_search_response,
)

setup_logging()
logger = logging.getLogger("mcp_server")

settings = get_settings()
MCP_API_KEY = os.environ.get("MCP_API_KEY", "").strip()
ROUTING_MODE = os.environ.get("MCP_ROUTING_MODE", "single")
route_registry = RouteRegistry(settings)
# 키가 없으면 ApiKeyMiddleware 가 통째로 무력화된다(401 이 아니라 그냥 통과).
# 배포가 공개(allUsers)로 바뀌는 순간 코퍼스 전체가 무인증 노출이므로, 인증 없이
# 뜨는 것은 반드시 의도한 선택이어야 한다 — 명시적 opt-in 없이는 기동을 거부한다.
# IAM(ID 토큰) 으로만 여는 scripts/deploy.ps1 경로에서 이 값을 켠다.
MCP_ALLOW_NO_AUTH = os.environ.get("MCP_ALLOW_NO_AUTH", "").strip().lower() in (
    "1",
    "true",
    "yes",
)

# search 가 노출하는 top_k 상한. 사용자 요청 k 는 이 값으로 clamp 된다.
MAX_TOP_K = 20
# 필터·중복 제거가 걷어낼 몫. k 가 커도 최소 이만큼은 더 받아 자리를 채운다.
_FETCH_HEADROOM = 10
_MAX_FETCH = MAX_TOP_K + _FETCH_HEADROOM

mcp = FastMCP(
    "rag-search",
    host="0.0.0.0",
    port=int(os.environ.get("PORT", "8080")),
    stateless_http=True,
)


class ApiKeyMiddleware(BaseHTTPMiddleware):
    """FactChat 등 외부 커넥터용 단순 API 키 게이트."""

    async def dispatch(self, request: Request, call_next):  # type: ignore[no-untyped-def]
        if request.url.path in {"/health", "/"}:
            return await call_next(request)

        auth = request.headers.get("authorization") or ""
        x_key = request.headers.get("x-api-key") or ""
        token = ""
        if auth.lower().startswith("bearer "):
            token = auth[7:].strip()
        elif x_key:
            token = x_key.strip()

        if ROUTING_MODE == "registry":
            try:
                scope = await asyncio.to_thread(route_registry.resolve, token)
            except Exception:
                logger.error("MCP route registry lookup failed")
                return JSONResponse({"error": "routing_unavailable"}, status_code=503)
            if scope is None:
                return JSONResponse({"error": "unauthorized"}, status_code=401)
            request.state.mcp_scope = scope
        elif MCP_API_KEY and not hmac.compare_digest(token, MCP_API_KEY):
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
_EXPERIMENT_DIAGNOSTICS = os.environ.get("SEARCH_EXPERIMENT_DIAGNOSTICS", "").strip().lower() in (
    "1",
    "true",
    "yes",
)
_CacheKey = tuple
_cache: OrderedDict[_CacheKey, tuple[float, SearchResponse]] = OrderedDict()
_cache_lock = threading.Lock()


def _cache_get(key: _CacheKey) -> SearchResponse | None:
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


def _cache_put(key: _CacheKey, value: SearchResponse) -> None:
    if _CACHE_TTL <= 0:
        return
    with _cache_lock:
        _cache[key] = (time.monotonic(), copy.deepcopy(value))
        _cache.move_to_end(key)
        while len(_cache) > _CACHE_MAX:
            _cache.popitem(last=False)


def search(
    query: str,
    top_k: int | None = None,
    drive_id: str | None = None,
    *,
    scope: SearchScope | None = None,
) -> SearchResponse:
    """질문과 관련된 근거 문서를 검색해 본문 청크와 출처를 함께 반환합니다.

    이 도구 한 번으로 검색과 인용 정보 수집이 완료됩니다. 최종 답변은 호출 LLM이
    작성합니다. documents의 각 항목은 fileId로 구분한 문서이고, chunks는 그
    문서에서 검색된 부분입니다. 청크는 원문 전체나 연속된 구간을 보장하지 않습니다.
    citationId와 source를 사용해 인용하고, 문서 사이의 관계를 근거 없이 추론하지
    마세요. sourceUri는 일반 문서의 미리보기 링크이며 HWP/HWPX 문서에서는 다운로드
    링크입니다. 검색 결과만으로 답할 수 없으면 확인되지 않은 부분을 밝혀 주세요.
    같은 정보를 얻기 위해 표현만 바꿔 반복 호출할 필요는 없습니다.

    Args:
        query: 검색 질문 또는 검색어.
        top_k: 반환할 최대 문서 수. 생략하면 서버의 TOP_K_DEFAULT를 사용하며 현재
            운영 기본값은 7입니다. 특별한 이유가 없으면 생략하거나 7을 사용하세요.
            청크 수가 아닙니다. documentCount와 chunkCount는 실제 반환된 문서 수와
            청크 수입니다. 청크 점수는 Vertex 원값이며 답변 확률이 아닙니다. 결과
            배열 순서가 문서 관련도 순위입니다.
        drive_id: 특정 공유 드라이브로 필터(선택).
    """
    if ROUTING_MODE == "registry" and scope is None:
        raise PermissionError("Authenticated search scope is required")
    if scope and drive_id and drive_id not in scope.drive_ids:
        raise PermissionError("This credential cannot search the requested drive")
    request_settings = replace(settings, rag_corpus_name=scope.corpus) if scope else settings
    k = top_k or request_settings.top_k_default
    k = max(1, min(k, settings.search_top_k_max))
    logger.info("search query=%r top_k=%s drive_id=%s", query, k, drive_id)

    rewrite_policy = (
        "rewrite-v1",
        settings.search_rewrite_enabled,
        settings.search_rewrite_model if settings.search_rewrite_enabled else "",
        settings.search_rewrite_weight if settings.search_rewrite_enabled else 0,
    )
    rank_policy = (
        "title-rank-v1",
        settings.search_lexical_rerank,
        settings.search_title_rerank_weight,
        settings.search_temporal_rank_weight,
        settings.search_sequence_rank_weight,
        settings.search_document_kind_rank_weight,
        settings.search_revision_rank_weight,
        settings.search_reranker_mode,
        settings.search_reranker_candidate_k,
        settings.search_cross_encoder_model,
        settings.search_llm_reranker_model,
    )
    cache_key = (
        scope.cache_partition if scope else (settings.rag_corpus_name,),
        rewrite_policy,
        rank_policy,
        query,
        k,
        drive_id,
    )
    cached = _cache_get(cache_key)
    if cached is not None:
        logger.info("search cache hit query=%r top_k=%s", query, k)
        return cached

    # 여유분 retrieve 후 후처리(파일당 청크 병합)로 k개.
    # 상한을 k*배수보다 낮게 두면 큰 k 에서 여유분이 사라져 k 개를 못 채운다.
    fetch_k = min(
        settings.search_fetch_max,
        max(k * settings.search_fetch_multiplier, k),
    )
    if settings.search_reranker_mode != "none":
        fetch_k = min(
            settings.search_fetch_max,
            max(fetch_k, settings.search_reranker_candidate_k * settings.search_fetch_multiplier),
        )
    # 거리 상한 — 코퍼스 범위 밖 질문에 무관한 문서를 물어다 주지 않도록.
    # 0 이하면 필터를 끈다.
    threshold = settings.search_distance_threshold

    def _retrieve(search_query: str) -> list[Any]:
        return RagEngineClient(request_settings).retrieve(
            search_query,
            top_k=fetch_k,
            vector_distance_threshold=threshold if threshold > 0 else None,
        )

    # Raw retrieval and Gemini rewriting run in parallel. When a rewrite passes
    # validation, its retrieval occupies the freed worker. The raw query is never
    # replaced: a failed model call, rejected rewrite, or second retrieval failure
    # still returns its evidence.
    raw_vertex_hit_count: int | None = None
    rewrite_vertex_hit_count: int | None = None
    with ThreadPoolExecutor(max_workers=2) as pool:
        raw_future = pool.submit(_retrieve, query)
        rewrite_future = (
            pool.submit(rewrite_query, query, request_settings)
            if should_rewrite_query(query)
            else None
        )
        rewritten_query = rewrite_future.result() if rewrite_future else None
        raw_query_hits = raw_future.result()
        raw_vertex_hit_count = len(raw_query_hits)
        if rewritten_query:
            rewrite_future = pool.submit(_retrieve, rewritten_query)
            try:
                rewrite_hits = rewrite_future.result()
                rewrite_vertex_hit_count = len(rewrite_hits)
                raw_hits = rrf_merge_hit_lists(
                    [raw_query_hits, rewrite_hits],
                    weights=(1.0, request_settings.search_rewrite_weight),
                )
            except Exception:
                logger.warning("rewritten search unavailable; returning raw search", exc_info=True)
                raw_hits = raw_query_hits
        else:
            raw_hits = raw_query_hits
    retrieval_diagnostics = (
        {
            "rawHitCount": len(raw_hits),
            "rawVertexHitCount": raw_vertex_hit_count,
            "rewriteVertexHitCount": rewrite_vertex_hit_count,
        }
        if _EXPERIMENT_DIAGNOSTICS
        else None
    )
    store = DocStateStore(request_settings)
    # 상태·드라이브 필터는 **postprocess 앞**에 둔다. 뒤에 두면 postprocess 가 이미
    # k 개 문서로 잘라 놓은 뒤라, 걸러낸 자리가 빈 채로 남아 top_k 보다 적게 나간다.
    # 앞에서 걷어내면 청크 병합 예산(max_total_chunks)도 살아남을 문서에만 쓰인다.
    meta_cache: dict[str, Any] = {}

    def _meta(file_id: str) -> Any:
        if file_id not in meta_cache:
            meta_cache[file_id] = store.get(file_id)
        return meta_cache[file_id]

    def _servable(hit: Any) -> bool:
        meta = _meta(hit.source.file_id)
        if scope and (
            not meta
            or meta.drive_id not in scope.drive_ids
            or (scope.audience == "student" and meta.audience != "STUDENT")
        ):
            return False
        if meta and (
            # EXCLUDED = 대상 폴더 밖. 코퍼스 정리가 비동기라 청크가 남아 있을 수
            # 있으므로 검색 단에서도 막는다.
            meta.status in {DocStatus.SKIPPED, DocStatus.EXCLUDED, DocStatus.DELETED}
            or (
                meta.status == DocStatus.FAILED
                and (meta.error or "").startswith("out_of_folder_scope_cleanup_failed")
            )
        ):
            # 비동기 코퍼스 정리·재시도가 수렴하는 동안의 이중 방어.
            return False
        if drive_id and (not meta or meta.drive_id != drive_id):
            return False
        return True

    # 기존 HTML 색인도 응답 단계에서 정제한다. 원본 MIME/파일명으로 한정해
    # 프로그래밍 문서의 HTML 예제 등 일반 텍스트를 임의로 지우지 않는다.
    cleaned = []
    for hit in raw_hits:
        meta = _meta(hit.source.file_id)
        name = (meta.name if meta and meta.name else hit.source.name) or ""
        if (meta and meta.mime_type == "text/html") or name.lower().endswith((".html", ".htm")):
            hit = replace(hit, text=clean_html_evidence(hit.text))
        if hit.text.strip():
            cleaned.append(hit)
    raw_hits = cleaned

    # 어휘 순위를 섞어 상위를 다시 세운다(후보 안에서만, recall 불변).
    # postprocess_hits 는 들어온 순서를 그대로 존중하므로 여기서 정렬해 넘긴다.
    if settings.search_lexical_rerank and len(raw_hits) > 1:
        titles = []
        temporal_metadata = []
        for hit in raw_hits:
            meta = _meta(hit.source.file_id)
            name = ((meta.name if meta else None) or hit.source.name or "")
            bundle = (meta.bundle if meta else None) or ""
            titles.append(name)
            # Strip ingestion timestamps before period extraction so path suffixes
            # such as _20260912153000 do not look like document metadata.
            temporal_metadata.append(f"{normalize_title(name)} {normalize_title(bundle)}")
        order = rrf_rerank(
            query,
            [h.text for h in raw_hits],
            titles=titles,
            title_weight=settings.search_title_rerank_weight,
            temporal_metadata=temporal_metadata,
            temporal_weight=settings.search_temporal_rank_weight,
            sequence_weight=settings.search_sequence_rank_weight,
            document_kind_weight=settings.search_document_kind_rank_weight,
            revision_weight=settings.search_revision_rank_weight,
        )
        raw_hits = [raw_hits[i] for i in order]

    raw_hits = [h for h in raw_hits if _servable(h)]

    if settings.search_reranker_mode != "none":
        # Reranker breadth is defined in documents, not raw chunks. Merge duplicate
        # chunks by file before presenting exactly Top10/20/30 document candidates.
        raw_hits = postprocess_hits(
            raw_hits,
            top_k=settings.search_reranker_candidate_k,
            max_chunks_per_file=settings.search_max_chunks_per_file,
            max_total_chunks=(
                settings.search_reranker_candidate_k * settings.search_max_chunks_per_file
            ),
        )

    if settings.search_reranker_mode in {"cross_encoder", "llm"} and len(raw_hits) > 1:
        records = []
        for index, hit in enumerate(raw_hits):
            meta = _meta(hit.source.file_id)
            records.append(RerankRecord(
                id=str(index),
                title=((meta.name if meta else None) or hit.source.name or ""),
                path=(meta.path if meta else None) or "",
                bundle=(meta.bundle if meta else None) or "",
                metadata=(
                    f"mime={meta.mime_type if meta else ''}; "
                    f"modified={meta.modified_time if meta else hit.source.modified_time or ''}"
                ),
                chunk=hit.text,
            ))
        if settings.search_reranker_mode == "cross_encoder":
            model_order = cross_encoder_order(
                query,
                records,
                project_id=settings.gcp_project_id,
                model=settings.search_cross_encoder_model,
                timeout=settings.search_reranker_timeout_seconds,
            )
        else:
            model_order = llm_reranker_order(
                query,
                records,
                project_id=settings.gcp_project_id,
                model=settings.search_llm_reranker_model,
                timeout=settings.search_reranker_timeout_seconds,
            )
        if model_order is not None:
            raw_hits = [raw_hits[i] for i in model_order]

    response_chunk_budget = settings.search_max_total_chunks
    if settings.search_reranker_mode != "none":
        # Normal requests still return their requested top_k, while breadth
        # benchmarks may request the full document candidate set.
        response_chunk_budget = max(
            response_chunk_budget,
            min(k, settings.search_reranker_candidate_k),
        )

    hits = postprocess_hits(
        raw_hits,
        top_k=k,
        max_chunks_per_file=settings.search_max_chunks_per_file,
        max_total_chunks=response_chunk_budget,
    )
    if not hits:
        # 필터가 전부 걸러낸 경우 — 임계값 조정 판단 근거로 남긴다
        logger.info(
            "search no-hit query=%r fetched=%s threshold=%s",
            query,
            len(raw_hits),
            threshold,
        )

    documents: list[EvidenceDocument] = []
    for hit in hits:
        # 상태·드라이브 필터는 postprocess 전에 이미 걸렀다(_servable). 여기서는
        # 그때 읽어 둔 메타를 재사용만 한다 — 같은 문서를 두 번 조회하지 않는다.
        meta = _meta(hit.source.file_id)
        display_name = (
            (meta.name if meta and meta.name else None) or hit.source.name or hit.source.file_id
        )
        # displayName이 fileId.ext 형태면 숨김
        if display_name.endswith((".pdf", ".md", ".txt", ".pptx", ".xlsx")) and (
            not meta or not meta.name
        ):
            display_name = meta.path if meta and meta.path else display_name

        source = {
            "fileId": hit.source.file_id,
            "name": display_name,
            "path": meta.path if meta else None,
            "bundle": meta.bundle if meta else None,
            "sourceUri": citation_view_uri(
                (meta.source_uri if meta and meta.source_uri else None) or hit.source.source_uri,
                display_name,
            ),
            "modifiedTime": (meta.modified_time if meta else hit.source.modified_time),
            "driveId": meta.drive_id if meta else None,
        }
        # 파일명만으로는 문서를 못 가리는 코퍼스라(게시판 수집물 27건이 전부
        # content.txt, 게시글당 첨부 중앙값 2개) 자료묶음을 붙인 표시용 이름을
        # 함께 싣는다. name 은 원래 파일명 그대로 둔다.
        source["label"] = citation_label(source)
        documents.append(
            {
                "citationId": len(documents) + 1,
                "source": source,
                "chunks": [
                    {
                        "text": chunk.text,
                        "score": round(chunk.score, 6),
                        "scoreType": "vertex_raw",
                    }
                    for chunk in hit.chunks
                ],
            }
        )
    response = build_search_response(documents, retrieval_diagnostics=retrieval_diagnostics)
    _cache_put(cache_key, response)
    return response


def retrieve_channel(
    query: str,
    channel: str,
    top_k: int = 20,
    candidate_k: int = 30,
    drive_id: str | None = None,
    *,
    scope: SearchScope | None = None,
) -> dict[str, Any]:
    """Expose one retrieval signal over a shared vector-created candidate pool.

    ``vector`` is the only candidate generator in the current repository.
    ``body``, ``title``, ``bundle``, and ``metadata`` independently reorder the
    same vector candidates; the response states this explicitly so channel-local
    Hit@k is not mistaken for corpus-wide lexical recall.
    """
    if channel not in RETRIEVAL_CHANNELS:
        raise ValueError(f"channel must be one of: {', '.join(RETRIEVAL_CHANNELS)}")
    if not (query or "").strip():
        raise ValueError("query is required")
    if ROUTING_MODE == "registry" and scope is None:
        raise PermissionError("Authenticated retrieval scope is required")
    if scope and drive_id and drive_id not in scope.drive_ids:
        raise PermissionError("This credential cannot search the requested drive")

    request_settings = replace(settings, rag_corpus_name=scope.corpus) if scope else settings
    result_k = int(top_k)
    pool_k = int(candidate_k)
    if not 1 <= result_k <= pool_k <= 30:
        raise ValueError("require 1 <= top_k <= candidate_k <= 30")
    raw_fetch_k = min(
        request_settings.search_fetch_max,
        max(pool_k * request_settings.search_fetch_multiplier, pool_k),
    )
    threshold = request_settings.search_distance_threshold
    started = time.perf_counter()
    raw_hits = RagEngineClient(request_settings).retrieve(
        query,
        top_k=raw_fetch_k,
        vector_distance_threshold=threshold if threshold > 0 else None,
    )
    retrieve_ms = (time.perf_counter() - started) * 1000

    store_started = time.perf_counter()
    store = DocStateStore(request_settings)
    meta_cache: dict[str, Any] = {}

    def _meta(file_id: str) -> Any:
        if file_id not in meta_cache:
            meta_cache[file_id] = store.get(file_id)
        return meta_cache[file_id]

    def _servable(hit: Any) -> bool:
        meta = _meta(hit.source.file_id)
        if scope and (
            not meta
            or meta.drive_id not in scope.drive_ids
            or (scope.audience == "student" and meta.audience != "STUDENT")
        ):
            return False
        if meta and (
            meta.status in {DocStatus.SKIPPED, DocStatus.EXCLUDED, DocStatus.DELETED}
            or (
                meta.status == DocStatus.FAILED
                and (meta.error or "").startswith("out_of_folder_scope_cleanup_failed")
            )
        ):
            return False
        return not drive_id or bool(meta and meta.drive_id == drive_id)

    cleaned = []
    for hit in raw_hits:
        meta = _meta(hit.source.file_id)
        name = ((meta.name if meta else None) or hit.source.name or "")
        if (meta and meta.mime_type == "text/html") or name.lower().endswith((".html", ".htm")):
            hit = replace(hit, text=clean_html_evidence(hit.text))
        if hit.text.strip() and _servable(hit):
            cleaned.append(hit)

    candidates = postprocess_hits(
        cleaned,
        top_k=pool_k,
        max_chunks_per_file=request_settings.search_max_chunks_per_file,
        max_total_chunks=pool_k * request_settings.search_max_chunks_per_file,
    )
    metadata_ms = (time.perf_counter() - store_started) * 1000

    titles: list[str] = []
    bundles: list[str] = []
    metadata_texts: list[str] = []
    for hit in candidates:
        meta = _meta(hit.source.file_id)
        title = ((meta.name if meta else None) or hit.source.name or "")
        bundle = (meta.bundle if meta else None) or ""
        path = (meta.path if meta else None) or ""
        titles.append(title)
        bundles.append(bundle)
        metadata_texts.append(
            " ".join(
                value
                for value in (
                    normalize_title(title),
                    normalize_title(bundle),
                    normalize_title(path),
                    meta.mime_type if meta else "",
                    meta.modified_time if meta else (hit.source.modified_time or ""),
                )
                if value
            )
        )

    rank_started = time.perf_counter()
    ranking = rank_channel(
        query,
        channel,  # type: ignore[arg-type]
        bodies=[hit.text for hit in candidates],
        titles=titles,
        bundles=bundles,
        metadata=metadata_texts,
    )
    rank_ms = (time.perf_counter() - rank_started) * 1000

    results: list[dict[str, Any]] = []
    for rank, index in enumerate(ranking.order[:result_k], 1):
        hit = candidates[index]
        meta = _meta(hit.source.file_id)
        source = {
            "fileId": hit.source.file_id,
            "name": titles[index] or hit.source.file_id,
            "path": (meta.path if meta else None),
            "bundle": bundles[index] or None,
            "sourceUri": citation_view_uri(
                (meta.source_uri if meta and meta.source_uri else None) or hit.source.source_uri,
                titles[index],
            ),
            "modifiedTime": (meta.modified_time if meta else hit.source.modified_time),
            "driveId": (meta.drive_id if meta else None),
        }
        source["label"] = citation_label(source)
        row: dict[str, Any] = {
            "rank": rank,
            "vectorCandidateRank": index + 1,
            "channelScore": round(ranking.scores[index], 8),
            "vertexRawScore": hit.score,
            "source": source,
            "chunks": [
                {"text": chunk.text, "score": chunk.score, "scoreType": "vertex_raw"}
                for chunk in hit.chunks
            ],
        }
        if ranking.signals is not None:
            row["signals"] = ranking.signals[index].to_dict()
        results.append(row)

    total_ms = (time.perf_counter() - started) * 1000
    return {
        "schemaVersion": 1,
        "channel": channel,
        "candidateSource": "vertex_vector",
        "candidateScope": "fixed_vector_document_pool",
        "candidateK": pool_k,
        "candidateCount": len(candidates),
        "topK": result_k,
        "scoreType": ranking.score_type,
        "queryRewrite": False,
        "distanceThreshold": threshold if threshold > 0 else None,
        "serviceRevision": os.environ.get("K_REVISION", ""),
        "commit": os.environ.get("GIT_COMMIT", ""),
        "gitDirty": os.environ.get("GIT_DIRTY", "").lower() == "true",
        "latencyMs": {
            "retrieve": round(retrieve_ms, 2),
            "metadata": round(metadata_ms, 2),
            "rank": round(rank_ms, 2),
            "total": round(total_ms, 2),
        },
        "results": results,
    }


@mcp.tool(name="search", description=search.__doc__)
async def search_tool(
    query: str, ctx: Context, top_k: int | None = None, drive_id: str | None = None
) -> SearchResponse:
    # Scope comes from the authenticated HTTP request, never from tool arguments.
    request = ctx.request_context.request
    scope = getattr(request.state, "mcp_scope", None) if request is not None else None
    return await asyncio.to_thread(search, query, top_k, drive_id, scope=scope)


@mcp.custom_route("/health", methods=["GET"])
async def health(_request: Request):  # type: ignore[no-untyped-def]
    return JSONResponse(
        {
            "status": "ok",
            "service": os.environ.get("K_SERVICE") or "rag-mcp",
            "auth": "scoped_api_key"
            if ROUTING_MODE == "registry"
            else "api_key"
            if MCP_API_KEY
            else "none",
            "retrievalChannels": [f"/retrieval/{channel}" for channel in RETRIEVAL_CHANNELS],
        }
    )


@mcp.custom_route("/retrieval/{channel}", methods=["POST"])
async def retrieval_channel_route(request: Request):  # type: ignore[no-untyped-def]
    """Authenticated benchmark route: /retrieval/vector|body|title|bundle|metadata."""
    channel = request.path_params.get("channel", "")
    if channel not in RETRIEVAL_CHANNELS:
        return JSONResponse(
            {"error": "invalid_channel", "allowed": list(RETRIEVAL_CHANNELS)},
            status_code=404,
        )
    try:
        body = await request.json()
        if not isinstance(body, dict):
            raise TypeError("JSON body must be an object")
        scope = getattr(request.state, "mcp_scope", None)
        result = await asyncio.to_thread(
            retrieve_channel,
            str(body.get("query") or ""),
            channel,
            int(body.get("top_k", 20)),
            int(body.get("candidate_k", 30)),
            body.get("drive_id"),
            scope=scope,
        )
        return JSONResponse(result)
    except (TypeError, ValueError) as exc:
        return JSONResponse({"error": "invalid_request", "detail": str(exc)}, status_code=400)
    except PermissionError as exc:
        return JSONResponse({"error": "forbidden", "detail": str(exc)}, status_code=403)


def build_app():
    """ASGI 앱 (+ API 키 미들웨어). 인증이 없으면 기동을 거부한다."""
    if ROUTING_MODE not in {"single", "registry"}:
        raise RuntimeError("Unknown MCP routing mode")
    if ROUTING_MODE != "registry" and not MCP_API_KEY and not MCP_ALLOW_NO_AUTH:
        raise RuntimeError(
            "MCP_API_KEY is not set and MCP_ALLOW_NO_AUTH is not enabled — "
            "refusing to serve the corpus without authentication"
        )
    if ROUTING_MODE != "registry" and not MCP_API_KEY:
        logger.warning(
            "starting WITHOUT app-level auth (MCP_ALLOW_NO_AUTH=true) — "
            "the deployment must stay IAM-protected"
        )
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
