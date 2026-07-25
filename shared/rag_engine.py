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
            except gcp_exceptions.ResourceExhausted as exc:
                last_err = exc
                logger.warning(
                    "RAG import throttled (attempt %s): %s", attempt + 1, exc
                )
                time.sleep(delay)
                delay = min(delay * 2, 60)
            except Exception as exc:  # noqa: BLE001
                last_err = exc
                logger.exception("RAG import failed")
                raise
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
        """여러 fileId를 코퍼스 1회 순회로 일괄 제거.

        파일마다 list_files()를 부르면 O(N×코퍼스)라 코퍼스가 커질수록 느리고
        list RPM을 소모한다. 여기서는 list_files()를 한 번만 호출한다.
        """
        wanted = {fid for fid in file_ids if fid}
        if not wanted:
            return 0
        deleted = 0
        for f in self.list_files():
            display = getattr(f, "display_name", "") or ""
            resource_name = getattr(f, "name", None)
            if not resource_name:
                continue
            # 정규화 md는 {fileId}.md, Drive 원본은 fileId가 이름에 포함될 수 있음
            if any(fid in display or display.startswith(fid) for fid in wanted):
                rag.delete_file(name=resource_name)
                deleted += 1
                logger.info("Deleted RAG file: %s (%s)", resource_name, display)
        return deleted

    def retrieve(
        self,
        query: str,
        *,
        top_k: int = 5,
        vector_distance_threshold: float | None = None,
        metadata_filter: str | None = None,
    ) -> list[SearchHit]:
        cfg_kwargs: dict[str, Any] = {"top_k": top_k}
        if vector_distance_threshold is not None:
            cfg_kwargs["vector_distance_threshold"] = vector_distance_threshold

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
