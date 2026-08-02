"""Vertex AI RAG Engine 클라이언트 (import / delete / retrieve)."""

from __future__ import annotations

import logging
import time
from typing import Any

import vertexai
from google.api_core import exceptions as gcp_exceptions
from vertexai import rag

from shared.config import Settings, get_settings
from shared.models import SearchHit, SearchSource
from shared.search_postprocess import extract_file_id, unescape_chunk_text

logger = logging.getLogger(__name__)


class RagImportError(RuntimeError):
    """RAG import가 요청 전체를 확실히 처리하지 못했을 때 발생한다."""

    def __init__(
        self,
        *,
        requested: int,
        imported: int,
        failed: int,
        skipped: int,
        partial_failures: str | None = None,
    ) -> None:
        self.requested = requested
        self.imported = imported
        self.failed = failed
        self.skipped = skipped
        self.partial_failures = partial_failures
        detail = (
            f"requested={requested} imported={imported} "
            f"failed={failed} skipped={skipped}"
        )
        if partial_failures:
            detail += f" partialFailures={partial_failures}"
        super().__init__(f"RAG import incomplete: {detail}")


def _with_throttle_retry(fn: Any, *, what: str, max_retries: int = 5) -> Any:
    """RPM 초과(429)만 지수 백오프로 재시도한다.

    import 에만 재시도가 걸려 있었다. list/delete 는 배치 하나가 수십 번 부르는데도
    무방비라, 쿼터를 넘기면 그대로 호출측까지 튀어 500 이 되고 — SKIP 분기처럼
    워크플로우 재시도가 없는 경로에서는 pageToken 이 영구 미커밋으로 굳는다.
    """
    delay = 1.0
    last: Exception | None = None
    for attempt in range(max_retries):
        try:
            return fn()
        except gcp_exceptions.ResourceExhausted as exc:
            last = exc
            logger.warning("RAG %s throttled (attempt %s): %s", what, attempt + 1, exc)
            time.sleep(delay)
            delay = min(delay * 2, 60)
    raise RuntimeError(f"RAG {what} failed after {max_retries} retries: {last}")


class RagEngineClient:
    # 코퍼스 스냅샷 {fileId: [ragFile resourceName]}. 클래스 속성으로 두면
    # object.__new__ 로 만든 인스턴스(테스트)에서도 안전하게 읽힌다.
    _file_index: dict[str, list[str]] | None = None

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
                imported = int(
                    getattr(response, "imported_rag_files_count", 0) or 0
                )
                failed = int(getattr(response, "failed_rag_files_count", 0) or 0)
                skipped = int(getattr(response, "skipped_rag_files_count", 0) or 0)
                partial_failures = (
                    getattr(response, "partial_failures_gcs_path", None)
                    or getattr(response, "partial_failures_bigquery_table", None)
                )
                logger.info(
                    "RAG import done: uris=%s imported=%s failed=%s skipped=%s "
                    "partial_failures=%s",
                    len(gcs_uris),
                    imported,
                    failed,
                    skipped,
                    partial_failures,
                )

                # import_files 는 파일 단위 부분 실패를 예외 대신 응답 카운트로
                # 돌려줄 수 있다. 정확히 처리됐다고 확인된 배치만 후속 상태를
                # INDEXED 로 전환한다. skipped 는 이미 같은 파일이 코퍼스에 있어
                # 재사용된 정상 결과이므로 처리 완료로 센다.
                requested = len(gcs_uris)
                if failed > 0 or imported + skipped != requested:
                    raise RagImportError(
                        requested=requested,
                        imported=imported,
                        failed=failed,
                        skipped=skipped,
                        partial_failures=partial_failures,
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

    def list_files(self) -> list[Any]:
        return list(
            _with_throttle_retry(
                lambda: rag.list_files(corpus_name=self.corpus_name), what="list_files"
            )
        )

    def delete_by_file_id(self, file_id: str) -> bool:
        """fileId 기반 청크 제거 (display_name / 메타데이터 매칭)."""
        return self.delete_files_by_ids([file_id]) > 0

    def delete_files_by_ids(self, file_ids: list[str]) -> int:
        """여러 fileId를 코퍼스 1회 순회로 일괄 제거.

        파일마다 list_files()를 부르면 O(N×코퍼스)라 코퍼스가 커질수록 느리고
        list RPM을 소모한다. **인스턴스 하나를 재사용하면** 첫 호출에서 뜬 스냅샷을
        이후 호출이 그대로 쓰므로, 배치를 몇 번 돌든 순회는 1회로 끝난다.

        스냅샷이 스테일해지지 않는 근거: 한 run 안에서 같은 파일을 두 번 import
        하지 않는다. 따라서 '이번에 지워야 할 기존 청크'는 전부 첫 순회 시점에
        이미 존재하고, run 중 새로 들어간 청크는 애초에 삭제 대상이 아니다.
        """
        wanted = {fid for fid in file_ids if fid}
        if not wanted:
            return 0
        if self._file_index is None:
            self._file_index = self._snapshot_file_index()
        deleted = 0
        for fid in wanted:
            # pop: 같은 fileId 를 다시 지우라고 해도 이미 지운 것은 건너뛴다.
            for resource_name in self._file_index.pop(fid, []):
                _with_throttle_retry(
                    lambda name=resource_name: rag.delete_file(name=name),
                    what="delete_file",
                )
                deleted += 1
                logger.info("Deleted RAG file: %s (%s)", resource_name, fid)
        return deleted

    def _snapshot_file_index(self) -> dict[str, list[str]]:
        """코퍼스를 한 번 순회해 {fileId: [resourceName]} 으로 접는다."""
        index: dict[str, list[str]] = {}
        for f in self.list_files():
            resource_name = getattr(f, "name", None)
            if not resource_name:
                continue
            display = getattr(f, "display_name", "") or ""
            source_uri = getattr(f, "source_uri", None)
            # GCS display name/URI에서 확장자를 제거한 정확한 fileId만 키로 쓴다.
            # 부분문자열 비교는 f1 삭제 시 f10까지 지우는 조용한 유실을 만든다.
            index.setdefault(extract_file_id(display, source_uri), []).append(
                resource_name
            )
        return index

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
