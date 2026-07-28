"""Vertex AI RAG Engine 클라이언트 (import / delete / retrieve)."""

from __future__ import annotations

import logging
import time
from typing import Any

from google.api_core import exceptions as gcp_exceptions
from vertexai import rag
import vertexai

from shared.config import Settings, get_settings
from shared.models import SearchHit, SearchSource
from shared.search_postprocess import extract_file_id, unescape_chunk_text

logger = logging.getLogger(__name__)

# Vertex AI RAG import_files 는 호출 1회당 GCS URI 25개까지만 허용한다
# (초과 시 InvalidArgument: "GCS URIs cannot be specified more than 25 times.")
_MAX_IMPORT_URIS = 25

# corpus_name → (만든 시각, fileId→resource_name 인덱스)
_CORPUS_INDEX_CACHE: dict[str, tuple[float, dict[str, list[str]]]] = {}
# 인덱스를 만든 뒤 import 된 fileId — 이 id 를 건드릴 때만 다시 만든다
_CORPUS_DIRTY_IDS: dict[str, set[str]] = {}
_INDEX_TTL_SECONDS = 300.0


class RagEngineClient:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        vertexai.init(
            project=self.settings.gcp_project_id,
            location=self.settings.gcp_region,
        )
        self.corpus_name = self.settings.rag_corpus_name

    def import_from_gcs(
        self,
        gcs_uris: list[str],
        *,
        chunk_size: int | None = None,
        chunk_overlap: int | None = None,
        max_retries: int = 5,
    ) -> list[str]:
        """GCS 마크다운을 코퍼스로 증분 import. RPM 제한 대비 재시도.

        청킹 값은 RAG_CHUNK_SIZE / RAG_CHUNK_OVERLAP 로 조정한다(인자 우선).
        """
        if not gcs_uris:
            return []

        size = chunk_size if chunk_size is not None else self.settings.rag_chunk_size
        overlap = (
            chunk_overlap if chunk_overlap is not None else self.settings.rag_chunk_overlap
        )
        transformation = rag.TransformationConfig(
            chunking_config=rag.ChunkingConfig(
                chunk_size=size,
                chunk_overlap=overlap,
            )
        )
        logger.info("RAG import chunking size=%s overlap=%s", size, overlap)

        imported: list[str] = []
        for i in range(0, len(gcs_uris), _MAX_IMPORT_URIS):
            batch = gcs_uris[i : i + _MAX_IMPORT_URIS]
            imported.extend(self._import_batch(batch, transformation, max_retries))
            if i + _MAX_IMPORT_URIS < len(gcs_uris):
                time.sleep(2.0)  # 서브배치 사이 쿼터 여유 확보
        # 캐시를 통째로 버리면 다음 배치가 또 코퍼스를 훑는다.
        # 이번에 올린 id 만 dirty 로 두고 나머지 캐시는 살린다.
        self._mark_dirty({extract_file_id(u) for u in imported})
        return imported

    def _import_batch(
        self,
        gcs_uris: list[str],
        transformation: Any,
        max_retries: int,
    ) -> list[str]:
        delay = 1.0
        last_err: Exception | None = None
        for attempt in range(max_retries):
            try:
                response = rag.import_files(
                    self.corpus_name,
                    gcs_uris,
                    transformation_config=transformation,
                )
                imported = getattr(response, "imported_rag_files_count", None)
                logger.info(
                    "RAG import done: uris=%s imported=%s", len(gcs_uris), imported
                )
                return list(gcs_uris)
            except Exception as exc:  # noqa: BLE001
                # vertexai SDK가 내부에서 ResourceExhausted 를 잡아 RuntimeError 로
                # 재포장하므로(__cause__ 에 원본이 남음), 타입 매칭만으로는 못 잡는다.
                throttled = isinstance(exc, gcp_exceptions.ResourceExhausted) or isinstance(
                    exc.__cause__, gcp_exceptions.ResourceExhausted
                )
                if not throttled:
                    logger.exception("RAG import failed")
                    raise
                last_err = exc
                logger.warning(
                    "RAG import throttled (attempt %s): %s", attempt + 1, exc
                )
                time.sleep(delay)
                delay = min(delay * 2, 60)
        raise RuntimeError(f"RAG import failed after retries: {last_err}")

    def import_drive_files(
        self,
        drive_folder_or_file_ids: list[str],
        *,
        chunk_size: int = 512,
        chunk_overlap: int = 100,
    ) -> Any:
        """네이티브 Drive 커넥터 경로 (기타 지원 포맷)."""
        transformation = rag.TransformationConfig(
            chunking_config=rag.ChunkingConfig(
                chunk_size=chunk_size,
                chunk_overlap=chunk_overlap,
            )
        )
        return rag.import_files(
            self.corpus_name,
            drive_folder_or_file_ids,
            transformation_config=transformation,
        )

    def list_files(self) -> list[Any]:
        return list(rag.list_files(corpus_name=self.corpus_name))

    def _file_index(self, wanted: set[str] | None = None) -> dict[str, list[str]]:
        """fileId → RagFile resource_name 목록.

        list_files() 는 코퍼스 전체를 훑으므로 배치마다 부르면
        O(배치수 × 코퍼스)가 된다. 1,209건 재색인 때 이게 지배적인 비용이었다.
        같은 프로세스 안에서는 짧은 TTL 로 재사용한다.

        import 이 끝날 때마다 캐시를 통째로 버리면 재색인(배치별 delete→import)
        에서는 캐시가 한 번도 안 맞는다. 그래서 import 된 id 만 dirty 로 두고,
        **그 id 를 실제로 건드릴 때만** 다시 만든다.
        """
        dirty = _CORPUS_DIRTY_IDS.get(self.corpus_name, set())
        cached = _CORPUS_INDEX_CACHE.get(self.corpus_name)
        if (
            cached
            and (time.monotonic() - cached[0]) < _INDEX_TTL_SECONDS
            and not (wanted and (wanted & dirty))
        ):
            return cached[1]

        index: dict[str, list[str]] = {}
        for f in self.list_files():
            resource_name = getattr(f, "name", None)
            if not resource_name:
                continue
            display = getattr(f, "display_name", "") or ""
            # 정규화 산출물은 {fileId}{.partN}{확장자} 꼴이라 여기서 되돌린다
            index.setdefault(extract_file_id(display), []).append(resource_name)
        _CORPUS_INDEX_CACHE[self.corpus_name] = (time.monotonic(), index)
        _CORPUS_DIRTY_IDS[self.corpus_name] = set()
        logger.info("RAG corpus index built: %s files", sum(map(len, index.values())))
        return index

    def _mark_dirty(self, file_ids: set[str]) -> None:
        """import 으로 새 RagFile 이 생긴 id 표시 — 다음에 건드리면 재구축."""
        if file_ids:
            _CORPUS_DIRTY_IDS.setdefault(self.corpus_name, set()).update(file_ids)

    def invalidate_file_index(self) -> None:
        """코퍼스가 크게 바뀐 뒤 캐시를 통째로 버린다."""
        _CORPUS_INDEX_CACHE.pop(self.corpus_name, None)
        _CORPUS_DIRTY_IDS.pop(self.corpus_name, None)

    def find_rag_file_by_display_name(self, display_name: str) -> Any | None:
        for f in self.list_files():
            name = getattr(f, "display_name", "") or ""
            if name == display_name or display_name in name:
                return f
        return None

    def delete_by_file_id(self, file_id: str) -> bool:
        """fileId 기반 청크 제거 (display_name / 메타데이터 매칭)."""
        return self.delete_files_by_ids([file_id]) > 0

    def delete_files_by_ids(self, file_ids: list[str]) -> int:
        """여러 fileId 의 RagFile 을 제거. 인덱스 조회라 코퍼스 크기와 무관하다."""
        wanted = {fid for fid in file_ids if fid}
        if not wanted:
            return 0

        index = self._file_index(wanted)
        targets: list[tuple[str, str]] = []
        for fid in wanted:
            for resource_name in index.get(fid, ()):
                targets.append((fid, resource_name))
        if not targets:
            return 0

        # 삭제 1건은 호출 자체가 ~0.4초 걸린다. 순차로 돌리면 그 지연이 그대로
        # 쌓이므로(1,373건이면 15분) 동시에 여러 건을 보낸다.
        # 쿼터는 초당 (동시수 / 호출지연) 로 소모되니 페이싱과 함께 조절할 것.
        workers = max(1, self.settings.rag_delete_concurrency)
        pacing = self.settings.rag_delete_pacing_seconds

        def _delete_one(item: tuple[str, str]) -> tuple[str, str] | None:
            fid, resource_name = item
            try:
                rag.delete_file(name=resource_name)
                logger.info("Deleted RAG file: %s (%s)", resource_name, fid)
                return item
            except Exception:  # noqa: BLE001
                # 이미 지워졌거나 일시 오류 — 재색인 자체를 막지는 않는다
                logger.warning("delete failed: %s (%s)", resource_name, fid)
                return None

        deleted = 0
        if workers == 1:
            for i, item in enumerate(targets):
                if _delete_one(item):
                    deleted += 1
                    index.get(item[0], []).remove(item[1])
                if pacing > 0 and i < len(targets) - 1:
                    time.sleep(pacing)
            return deleted

        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=workers) as pool:
            # 한 묶음(=동시 실행 수)을 보내고 페이싱만큼 쉬는 식으로 속도를 제어한다
            for start in range(0, len(targets), workers):
                chunk = targets[start : start + workers]
                for done in pool.map(_delete_one, chunk):
                    if done:
                        deleted += 1
                        # 지운 건 인덱스에서도 빼야 다음 호출이 없는 파일을 노리지 않는다
                        index.get(done[0], []).remove(done[1])
                if pacing > 0 and start + workers < len(targets):
                    time.sleep(pacing)
        return deleted

    def retrieve(
        self,
        query: str,
        *,
        top_k: int = 5,
        vector_distance_threshold: float | None = None,
        metadata_filter: str | None = None,
    ) -> list[SearchHit]:
        # vector_distance_threshold / metadata_filter 는 RagRetrievalConfig 의
        # 최상위 필드가 아니라 filter=rag.Filter(...) 안에 있다. 평평한 kwarg 로
        # 넘기면 TypeError 로 터진다 (그래서 이 경로는 여태 죽어 있었다).
        cfg_kwargs: dict[str, Any] = {"top_k": top_k}
        filter_kwargs: dict[str, Any] = {}
        if vector_distance_threshold is not None:
            filter_kwargs["vector_distance_threshold"] = vector_distance_threshold
        if metadata_filter:
            filter_kwargs["metadata_filter"] = metadata_filter
        if filter_kwargs:
            cfg_kwargs["filter"] = rag.Filter(**filter_kwargs)

        rag_resources = [rag.RagResource(rag_corpus=self.corpus_name)]
        response = rag.retrieval_query(
            rag_resources=rag_resources,
            text=query,
            rag_retrieval_config=rag.RagRetrievalConfig(**cfg_kwargs),
        )

        hits: list[SearchHit] = []
        contexts = getattr(response, "contexts", None)
        ctx_list = getattr(contexts, "contexts", None) if contexts else None
        if not ctx_list:
            return hits

        for ctx in ctx_list:
            text = unescape_chunk_text(getattr(ctx, "text", "") or "")
            score = float(getattr(ctx, "score", 0.0) or 0.0)
            source_uri = getattr(ctx, "source_uri", None) or getattr(
                ctx, "source_display_name", None
            )
            display = getattr(ctx, "source_display_name", "") or ""
            file_id = extract_file_id(display, source_uri)
            hits.append(
                SearchHit(
                    text=text,
                    score=score,
                    source=SearchSource(
                        file_id=file_id,
                        name=display,
                        source_uri=source_uri,
                    ),
                )
            )
        return hits
