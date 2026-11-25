from __future__ import annotations

import logging
import random
import time
import uuid
from dataclasses import dataclass
from typing import Any

import agentplatform
from google.api_core import exceptions as gcp_exceptions
from agentplatform import rag

from shared.config import Settings, get_settings
from shared.index_guard import check_active_mutation
from shared.gcs import GcsClient, gs_uri
from shared.models import SearchHit, SearchSource
from shared.rag_import_result import RagImportResult, parse_import_results
from shared.rag_mapping import RagFileMapping, RagFileMappingStore
from shared.search_postprocess import extract_file_id, unescape_chunk_text

logger = logging.getLogger(__name__)

# Vertex AI RAG import_files 는 호출 1회당 GCS URI 25개까지만 허용한다
# (초과 시 InvalidArgument: "GCS URIs cannot be specified more than 25 times.")
MAX_IMPORT_URIS = 25

_CORPUS_INDEX_CACHE: dict[str, tuple[float, dict[str, list[str]]]] = {}
_CORPUS_DIRTY_IDS: dict[str, set[str]] = {}
_INDEX_TTL_SECONDS = 300.0

# ragFiles.list 는 페이지당 최대 100건이다. 큰 코퍼스를 pager에 한 번에 맡기면
# 중간 페이지의 429에서 전체 순회가 첫 페이지부터 재시작돼 영원히 완주하지 못한다.
# 페이지 토큰을 직접 보존하고 60 RPM 지역 쿼터에 여유를 둔다.
_RAG_LIST_PAGE_SIZE = 100
_RAG_LIST_PAGE_INTERVAL_SECONDS = 1.5
_RAG_LIST_PAGE_RETRIES = 10


@dataclass(frozen=True)
class ImportOutcome:
    uris: list[str]
    imported: int
    failed: int
    skipped: int
    results: tuple[RagImportResult, ...] = ()
    result_sinks: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return self.failed == 0 and (self.imported + self.skipped) >= len(self.uris)


class RagImportError(RuntimeError):
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


class RagImportThrottledError(RuntimeError):
    pass


def _contains_exception(exc: BaseException, kinds: tuple[type[BaseException], ...]) -> bool:
    pending: list[BaseException] = [exc]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, kinds):
            return True
        for nested in (current.__cause__, current.__context__, *current.args):
            if isinstance(nested, BaseException):
                pending.append(nested)
    return False


def _with_throttle_retry(fn: Any, *, what: str, max_retries: int = 6) -> Any:
    delay = 1.0
    last: Exception | None = None
    for attempt in range(max_retries):
        try:
            return fn()
        except Exception as exc:
            if not _contains_exception(exc, (gcp_exceptions.ResourceExhausted,)):
                raise
            last = exc
            logger.warning("RAG %s throttled (attempt %s): %s", what, attempt + 1, exc)
            time.sleep(random.uniform(0, delay))
            delay = min(delay * 2, 60)
    raise RagImportThrottledError(
        f"Vertex RAG quota still exhausted after {max_retries} retries ({what})"
    ) from last


class RagEngineClient:
    def __init__(
        self,
        settings: Settings | None = None,
        *,
        corpus_name: str | None = None,
        credentials: Any | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        agentplatform.init(
            project=self.settings.gcp_project_id,
            location=self.settings.gcp_region,
            credentials=credentials,
        )
        self.corpus_name = corpus_name or self.settings.rag_corpus_name

    def import_from_gcs(
        self,
        gcs_uris: list[str],
        *,
        chunk_size: int | None = None,
        chunk_overlap: int | None = None,
        max_retries: int = 5,
    ) -> ImportOutcome:
        if not gcs_uris:
            return ImportOutcome(uris=[], imported=0, failed=0, skipped=0)

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

        batches: list[ImportOutcome] = []
        for i in range(0, len(gcs_uris), MAX_IMPORT_URIS):
            batch = gcs_uris[i : i + MAX_IMPORT_URIS]
            batches.append(self._import_batch(batch, transformation, max_retries))
            if i + MAX_IMPORT_URIS < len(gcs_uris):
                time.sleep(2.0)

        total = ImportOutcome(
            uris=[u for b in batches for u in b.uris],
            imported=sum(b.imported for b in batches),
            failed=sum(b.failed for b in batches),
            skipped=sum(b.skipped for b in batches),
            results=tuple(result for b in batches for result in b.results),
            result_sinks=tuple(sink for b in batches for sink in b.result_sinks),
        )
        self._mark_dirty({extract_file_id(u) for u in total.uris})
        return total

    @staticmethod
    def _read_outcome(gcs_uris: list[str], response: Any) -> ImportOutcome:
        def _count(field: str) -> int | None:
            raw = getattr(response, field, None)
            return int(raw) if raw is not None else None

        imported = _count("imported_rag_files_count")
        if imported is None:
            # 카운트를 못 읽으면 예전처럼 낙관하되 그 사실을 남긴다. 여기서
            # 비관하면 SDK 가 바뀌었을 때 파이프라인이 통째로 멈춘다.
            logger.warning(
                "import 응답에 카운트가 없다 — 성공으로 가정한다 uris=%s", len(gcs_uris)
            )
            imported = len(gcs_uris)
        return ImportOutcome(
            uris=list(gcs_uris),
            imported=imported,
            failed=_count("failed_rag_files_count") or 0,
            skipped=_count("skipped_rag_files_count") or 0,
        )

    def _import_batch(
        self,
        gcs_uris: list[str],
        transformation: Any,
        max_retries: int,
    ) -> ImportOutcome:
        delay = 1.0
        last_err: Exception | None = None
        for attempt in range(max_retries):
            check_active_mutation()
            try:
                result_sink = self._new_import_result_sink()
                import_kwargs: dict[str, Any] = {}
                if result_sink:
                    import_kwargs["import_result_sink"] = result_sink
                response = rag.import_files(
                    self.corpus_name,
                    gcs_uris,
                    transformation_config=transformation,
                    **import_kwargs,
                )
                outcome = self._read_outcome(gcs_uris, response)
                if result_sink:
                    results = self._read_import_result_sink(result_sink)
                    outcome = ImportOutcome(
                        uris=outcome.uris,
                        imported=outcome.imported,
                        failed=outcome.failed,
                        skipped=outcome.skipped,
                        results=tuple(results),
                        result_sinks=(result_sink,),
                    )
                if outcome.skipped:
                    logger.warning(
                        "RAG import skipped=%s corpus=%s uris=%s — pre-delete 누락 의심",
                        outcome.skipped, self.corpus_name, len(gcs_uris),
                    )
                if outcome.ok:
                    logger.info(
                        "RAG import done: uris=%s imported=%s",
                        len(gcs_uris),
                        outcome.imported,
                    )
                else:
                    # 여기가 유일한 신호다. 예외가 아니므로 위로 안 올라간다.
                    # partial_failures_* 는 Vertex 가 실패 상세를 적어 두는 경로라
                    # 어느 파일이 왜 죽었는지 확인하려면 이것부터 봐야 한다.
                    partial_failures = getattr(
                        response, "partial_failures_gcs_path", None
                    ) or getattr(response, "partial_failures_bigquery_table", None)
                    logger.error(
                        "RAG import 부분 실패 corpus=%s uris=%s imported=%s "
                        "failed=%s skipped=%s partial_failures=%s sample=%s",
                        self.corpus_name,
                        len(gcs_uris),
                        outcome.imported,
                        outcome.failed,
                        outcome.skipped,
                        partial_failures,
                        [u.rsplit("/", 1)[-1] for u in gcs_uris[:5]],
                    )
                return outcome
            except Exception as exc:
                # agentplatform SDK가 내부에서 원인 예외를 잡아 RuntimeError 로
                # 재포장하므로(__cause__ 에 원본이 남음), 타입 매칭만으로는 못 잡는다.
                #
                # 기다리면 풀리는 두 가지를 재시도한다.
                #   ResourceExhausted   쿼터(RPM) 초과
                #   FailedPrecondition  같은 코퍼스에 다른 작업이 실행 중.
                #                       코퍼스는 import/delete 를 동시에 못 받는다.
                #                       (2026-07-29 재색인에서 48건이 이걸로 즉시
                #                        실패했다 — 재시도 대상이 아니었기 때문)
                retryable = tuple(
                    e
                    for e in (
                        gcp_exceptions.ResourceExhausted,
                        gcp_exceptions.FailedPrecondition,
                    )
                )
                transient = _contains_exception(exc, retryable)
                if not transient:
                    logger.exception("RAG import failed")
                    raise
                last_err = exc
                logger.warning(
                    "RAG import retryable (attempt %s): %s", attempt + 1, exc
                )
                time.sleep(random.uniform(0, delay))
                delay = min(delay * 2, 60)
        if last_err is not None and _contains_exception(
            last_err, (gcp_exceptions.ResourceExhausted,)
        ):
            # 호출자가 HTTP 429로 보존할 수 있게 타입과 원인 체인을 남긴다.
            # 문자열 RuntimeError만 올리면 Workflow가 일반 500으로 보고 바깥
            # 재시도를 반복해 한 파일이 수 분 동안 같은 쿼터를 두드린다.
            raise RagImportThrottledError(
                f"Vertex RAG quota still exhausted after {max_retries} retries"
            ) from last_err
        raise RuntimeError(f"RAG import failed after retries: {last_err}") from last_err

    def _new_import_result_sink(self) -> str:
        settings = getattr(self, "settings", None)
        if not getattr(settings, "rag_mapping_write_enabled", False):
            return ""
        bucket = str(getattr(settings, "rag_metadata_bucket", "") or "").strip()
        if not bucket:
            logger.error(
                "RAG_MAPPING_WRITE_ENABLED=true but RAG_METADATA_BUCKET is empty"
            )
            return ""
        corpus_id = self.corpus_name.rstrip("/").rsplit("/", 1)[-1] or "unknown"
        return gs_uri(
            bucket,
            f"import-results/{corpus_id}/{uuid.uuid4().hex}.ndjson",
        )

    def _read_import_result_sink(self, sink_uri: str) -> list[RagImportResult]:
        delay = 0.25
        last_error: Exception | None = None
        for attempt in range(4):
            try:
                payload = GcsClient(self.settings).download_bytes(sink_uri)
                results = parse_import_results(
                    payload,
                    corpus_name=self.corpus_name,
                    sink_uri=sink_uri,
                )
                logger.info(
                    "RAG import result sink read rows=%s sink=%s",
                    len(results),
                    sink_uri,
                )
                return results
            except Exception as exc:
                last_error = exc
                if attempt < 3:
                    time.sleep(delay)
                    delay *= 2
        logger.error("RAG import result sink unavailable sink=%s: %s", sink_uri, last_error)
        return []

    def list_files(self) -> list[Any]:
        files: list[Any] = []
        page_token: str | None = None
        page_number = 0

        while True:
            current_token = page_token

            def _fetch_page(token: str | None = current_token) -> tuple[list[Any], str]:
                pager = rag.list_files(
                    corpus_name=self.corpus_name,
                    page_size=_RAG_LIST_PAGE_SIZE,
                    page_token=token,
                )
                # pager.pages의 첫 값은 생성 시 이미 받은 응답이다. 다음 페이지는
                # 새 토큰으로 별도 호출해야 429 재시도 때 앞 페이지를 반복하지 않는다.
                try:
                    response = next(iter(pager.pages))
                except StopIteration:
                    return [], ""
                return (
                    list(getattr(response, "rag_files", ())),
                    str(getattr(response, "next_page_token", "") or ""),
                )

            page_files, page_token = _with_throttle_retry(
                _fetch_page,
                what=f"list_files page={page_number + 1}",
                max_retries=_RAG_LIST_PAGE_RETRIES,
            )
            files.extend(page_files)
            page_number += 1
            if not page_token:
                break
            time.sleep(_RAG_LIST_PAGE_INTERVAL_SECONDS)

        logger.info(
            "RAG files listed corpus=%s pages=%s files=%s",
            self.corpus_name,
            page_number,
            len(files),
        )
        return files

    def _file_index(self, wanted: set[str] | None = None) -> dict[str, list[str]]:
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
            source_uri = getattr(f, "source_uri", None)
            index.setdefault(extract_file_id(display, source_uri), []).append(
                resource_name
            )
        _CORPUS_INDEX_CACHE[self.corpus_name] = (time.monotonic(), index)
        _CORPUS_DIRTY_IDS[self.corpus_name] = set()
        logger.info("RAG corpus index built: %s files", sum(map(len, index.values())))
        return index

    def _mark_dirty(self, file_ids: set[str]) -> None:
        if file_ids:
            _CORPUS_DIRTY_IDS.setdefault(self.corpus_name, set()).update(file_ids)

    def invalidate_file_index(self) -> None:
        _CORPUS_INDEX_CACHE.pop(self.corpus_name, None)
        _CORPUS_DIRTY_IDS.pop(self.corpus_name, None)

    def delete_by_file_id(self, file_id: str) -> bool:
        return self.delete_files_by_ids([file_id]) > 0

    def delete_files_by_ids(self, file_ids: list[str]) -> int:
        check_active_mutation()
        wanted = {fid for fid in file_ids if fid}
        if not wanted:
            return 0

        mapping_store: RagFileMappingStore | None = None
        mappings_by_target: dict[tuple[str, str], list[RagFileMapping]] = {}
        direct_index: dict[str, list[str]] = {}
        missing = set(wanted)
        if getattr(self.settings, "rag_mapping_read_enabled", False):
            try:
                mapping_store = RagFileMappingStore(self.settings)
                corpus_id = self.corpus_name.rstrip("/").rsplit("/", 1)[-1]
                for fid in wanted:
                    rows = [
                        row
                        for row in mapping_store.list_for_file(fid)
                        if row.corpus_name.rstrip("/").rsplit("/", 1)[-1]
                        == corpus_id
                    ]
                    if not rows:
                        continue
                    missing.discard(fid)
                    for row in rows:
                        rag_file_id = row.rag_file_name.rstrip("/").rsplit("/", 1)[-1]
                        target = (fid, rag_file_id)
                        direct_index.setdefault(fid, []).append(row.rag_file_name)
                        mappings_by_target.setdefault(target, []).append(row)
                logger.info(
                    "RAG mapping lookup corpus=%s requested=%s hit=%s missing=%s",
                    self.corpus_name,
                    len(wanted),
                    len(wanted - missing),
                    len(missing),
                )
            except Exception:
                logger.exception("RAG mapping lookup failed; using corpus scan fallback")
                mapping_store = None
                direct_index = {}
                mappings_by_target = {}
                missing = set(wanted)

        index = direct_index
        if missing and (
            not getattr(self.settings, "rag_mapping_read_enabled", False)
            or getattr(self.settings, "rag_mapping_fallback_scan_enabled", True)
        ):
            if getattr(self.settings, "cloud_tasks_enabled", False):
                self.invalidate_file_index()
            scanned = self._file_index(missing)
            index = {**direct_index}
            for fid in missing:
                index.setdefault(fid, scanned.get(fid, []))
        elif missing:
            raise RuntimeError("RAG mapping missing and fallback scan disabled; deletion unverified")

        targets: list[tuple[str, str]] = []
        seen_targets: set[tuple[str, str]] = set()
        for fid in wanted:
            for resource_name in index.get(fid, ()):
                key = (fid, resource_name.rstrip("/").rsplit("/", 1)[-1])
                if key in seen_targets:
                    continue
                seen_targets.add(key)
                targets.append((fid, resource_name))
        if not targets:
            return 0

        workers = max(1, self.settings.rag_delete_concurrency)
        pacing = self.settings.rag_delete_pacing_seconds

        def _delete_one(item: tuple[str, str]) -> tuple[tuple[str, str], bool]:
            fid, resource_name = item
            removed = True
            try:
                _with_throttle_retry(
                    lambda name=resource_name: rag.delete_file(name=name),
                    what="delete_file",
                )
            except Exception as exc:
                # SDK는 원인 예외를 RuntimeError로 감싸기도 한다. 명시적인
                # NotFound만 성공과 동등하게 취급하고 권한/쿼터/통신 실패는 전파한다.
                if not _contains_exception(exc, (gcp_exceptions.NotFound,)):
                    logger.exception("RAG delete failed: %s (%s)", resource_name, fid)
                    raise
                removed = False
            logger.info("RAG file absent: %s (%s) deleted=%s", resource_name, fid, removed)
            if mapping_store is not None:
                key = (fid, resource_name.rstrip("/").rsplit("/", 1)[-1])
                for mapping in mappings_by_target.get(key, ()):
                    mapping_store.delete(mapping)
            return item, removed

        deleted = 0
        if workers == 1:
            for i, item in enumerate(targets):
                _, removed = _delete_one(item)
                deleted += int(removed)
                index.get(item[0], []).remove(item[1])
                if pacing > 0 and i < len(targets) - 1:
                    time.sleep(pacing)
            return deleted

        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=workers) as pool:
            for start in range(0, len(targets), workers):
                chunk = targets[start : start + workers]
                for done, removed in pool.map(_delete_one, chunk):
                    deleted += int(removed)
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
