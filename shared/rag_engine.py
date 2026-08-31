"""Vertex AI RAG Engine 클라이언트 (import / delete / retrieve)."""

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
from shared.gcs import GcsClient, gs_uri
from shared.models import SearchHit, SearchSource
from shared.rag_import_result import RagImportResult, parse_import_results
from shared.rag_mapping import RagFileMapping, RagFileMappingStore
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

# ragFiles.list 는 페이지당 최대 100건이다. 큰 코퍼스를 pager에 한 번에 맡기면
# 중간 페이지의 429에서 전체 순회가 첫 페이지부터 재시작돼 영원히 완주하지 못한다.
# 페이지 토큰을 직접 보존하고 60 RPM 지역 쿼터에 여유를 둔다.
_RAG_LIST_PAGE_SIZE = 100
_RAG_LIST_PAGE_INTERVAL_SECONDS = 1.5
_RAG_LIST_PAGE_RETRIES = 10


@dataclass(frozen=True)
class ImportOutcome:
    """import 한 번의 실제 결과.

    `uris` 는 우리가 **보낸** 것이고 나머지 셋은 Vertex 가 **돌려준** 것이다.
    이 둘을 구분하지 않은 것이 오래된 결함이었다 — 예전에는 보낸 목록을 그대로
    성공으로 돌려줘서, import 에서 거부된 파일이 그대로 INDEXED 로 기록됐다.
    실측 사례: xlsx 27건이 상시 import 거부 중이었는데 집계에는 전부 성공으로
    잡혀 있었다(services/sync/main.py `_ingest_direct` 주석). 그때는 xlsx 라는
    증상만 개별 차단했고 **검출 능력은 생기지 않았다.** 이 타입이 그 자리다.

    `rag.import_files` 는 호출 자체가 실패할 때만 예외를 던진다. 파일 단위
    실패는 예외가 아니라 응답의 카운트로 오므로, 여기서 읽지 않으면 볼 방법이 없다.
    """

    uris: list[str]
    imported: int
    failed: int
    skipped: int
    results: tuple[RagImportResult, ...] = ()
    result_sinks: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        """보낸 것이 코퍼스에 들어가 있는가.

        `skipped` 는 **성공 쪽으로 센다.** 의미가 확정돼 있지 않은데, 유력한
        해석은 '이미 코퍼스에 있어 건너뜀'이고 그건 우리가 원하는 최종 상태다.
        실패로 세면 pre-delete 를 생략하는 경로(`reindex-pending` 비-force)에서
        **그 문서가 영원히 PARSED 에 머물며 매일 다시 import 된다** — 회수하려고
        만든 장치가 무한 루프가 되는 것이다.

        `skipped` 가 사실은 '지원하지 않아 건너뜀'이더라도 손실은 없다. 이
        작업의 본질은 **관측**이고, skipped 는 아래 로그에 WARNING 으로 그대로
        남는다. 상태 전이 가드는 부차적이며, 틀리더라도 루프를 만드는 쪽으로
        틀려서는 안 된다.
        """
        return self.failed == 0 and (self.imported + self.skipped) >= len(self.uris)


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


class RagImportThrottledError(RuntimeError):
    """Vertex RAG 요청 쿼터가 재시도 한도까지 복구되지 않았을 때의 오류."""


def _contains_exception(exc: BaseException, kinds: tuple[type[BaseException], ...]) -> bool:
    """SDK wrapper의 cause/context/args 안에 든 실제 API 예외까지 찾는다."""
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
    ) -> None:
        """corpus_name 을 주면 그 코퍼스를, 없으면 설정의 기본(=교직원용)을 쓴다.

        학생/교직원 코퍼스를 나누면서 인스턴스마다 대상이 달라진다. 아래 인덱스
        캐시는 처음부터 corpus_name 으로 키잉돼 있어(_CORPUS_INDEX_CACHE) 코퍼스가
        여럿이어도 서로 섞이지 않는다 — 인스턴스 속성 스냅샷으로는 코퍼스가 둘
        이상일 때 서로 덮어쓴다.
        """
        self.settings = settings or get_settings()
        agentplatform.init(
            project=self.settings.gcp_project_id,
            location=self.settings.gcp_region,
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
        """GCS 마크다운을 코퍼스로 증분 import. RPM 제한 대비 재시도.

        청킹 값은 RAG_CHUNK_SIZE / RAG_CHUNK_OVERLAP 로 조정한다(인자 우선).

        반환값은 **보낸 목록이 아니라 실제 결과**다(`ImportOutcome`). 호출측은
        `ok` 를 보고 상태 전이를 결정할 것 — 그러라고 있는 값이다.
        """
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
        for i in range(0, len(gcs_uris), _MAX_IMPORT_URIS):
            batch = gcs_uris[i : i + _MAX_IMPORT_URIS]
            batches.append(self._import_batch(batch, transformation, max_retries))
            if i + _MAX_IMPORT_URIS < len(gcs_uris):
                time.sleep(2.0)  # 서브배치 사이 쿼터 여유 확보

        total = ImportOutcome(
            uris=[u for b in batches for u in b.uris],
            imported=sum(b.imported for b in batches),
            failed=sum(b.failed for b in batches),
            skipped=sum(b.skipped for b in batches),
            results=tuple(result for b in batches for result in b.results),
            result_sinks=tuple(sink for b in batches for sink in b.result_sinks),
        )
        # 캐시를 통째로 버리면 다음 배치가 또 코퍼스를 훑는다.
        # 이번에 건드린 id 만 dirty 로 두고 나머지 캐시는 살린다.
        # (실패분도 포함한다 — 코퍼스에 반쯤 들어갔을 수 있어 인덱스를 믿으면 안 된다)
        self._mark_dirty({extract_file_id(u) for u in total.uris})
        return total

    @staticmethod
    def _read_outcome(gcs_uris: list[str], response: Any) -> ImportOutcome:
        """응답에서 실제 색인 결과를 꺼낸다.

        예전에는 `imported_rag_files_count` 를 로그로 흘리고 버렸다. 응답에는
        `failed_rag_files_count` / `skipped_rag_files_count` 도 함께 실려 있다.
        """

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
                    # ok 판정에는 성공으로 세지만(위 docstring) 정상 경로에서
                    # 나올 값이 아니다 — pre-delete 가 놓친 것이 있다는 뜻이다.
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
            except Exception as exc:  # mapping 관측 실패가 실제 색인을 실패시키면 안 됨
                last_error = exc
                if attempt < 3:
                    time.sleep(delay)
                    delay *= 2
        logger.error("RAG import result sink unavailable sink=%s: %s", sink_uri, last_error)
        return []

    def list_files(self) -> list[Any]:
        """코퍼스 파일을 페이지별로, 진행 지점을 잃지 않고 조회한다.

        SDK pager 전체를 ``list()``로 감싸 재시도하면 60번째 페이지에서 429가 난
        경우에도 첫 페이지부터 다시 읽는다. 페이지 토큰을 호출자가 관리해야 현재
        페이지에서만 기다렸다가 이어갈 수 있다.
        """
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
            source_uri = getattr(f, "source_uri", None)
            # source 산출물은 {fileId}{.partN}{확장자} 꼴이라 여기서 되돌린다.
            # display_name 이 잘렸을 때를 대비해 source_uri 도 같이 넘긴다 —
            # 부분문자열 비교로 떨어지면 f1 을 지우며 f10 까지 지운다.
            index.setdefault(extract_file_id(display, source_uri), []).append(
                resource_name
            )
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

    def delete_by_file_id(self, file_id: str) -> bool:
        """fileId 기반 청크 제거 (display_name / 메타데이터 매칭)."""
        return self.delete_files_by_ids([file_id]) > 0

    def delete_files_by_ids(self, file_ids: list[str]) -> int:
        """여러 fileId의 RagFile을 제거한다.

        매핑 read가 켜져 있으면 Firestore 단건 조회로 바로 resource name을 얻는다.
        매핑이 없는 파일만 기존 코퍼스 전체 스캔으로 보완한다. rollout 중에는
        fallback을 끄지 않아 불완전한 backfill이 문서 삭제 누락으로 이어지지 않게
        한다.
        """
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
                        # project ID/number 표기가 달라도 마지막 RagFile ID가 같으면
                        # 동일 리소스다. backfill+dual-write 중복 행을 한 호출로 접는다.
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
            except Exception:  # 매핑 장애 시 기존 스캔으로 안전하게 복귀한다.
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
            scanned = self._file_index(missing)
            index = {**direct_index}
            for fid in missing:
                # 캐시 안의 리스트 참조를 유지해야 아래 성공 삭제의 remove가
                # 캐시에도 반영된다. 복사하면 같은 프로세스의 다음 호출이 이미
                # 삭제한 RagFile을 다시 삭제하려 든다.
                index.setdefault(fid, scanned.get(fid, []))

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

        # 삭제 1건은 호출 자체가 ~0.4초 걸린다. 순차로 돌리면 그 지연이 그대로
        # 쌓이므로(1,373건이면 15분) 동시에 여러 건을 보낸다.
        # 쿼터는 초당 (동시수 / 호출지연) 로 소모되니 페이싱과 함께 조절할 것.
        workers = max(1, self.settings.rag_delete_concurrency)
        pacing = self.settings.rag_delete_pacing_seconds

        def _delete_one(item: tuple[str, str]) -> tuple[str, str] | None:
            fid, resource_name = item
            try:
                # 페이싱으로도 쿼터를 다 못 막는다(동시 실행 + 다른 배치와 경합).
                # 429 는 기다리면 풀리므로 여기서 한 번 더 삼킨다.
                _with_throttle_retry(
                    lambda name=resource_name: rag.delete_file(name=name),
                    what="delete_file",
                )
                logger.info("Deleted RAG file: %s (%s)", resource_name, fid)
                if mapping_store is not None:
                    key = (fid, resource_name.rstrip("/").rsplit("/", 1)[-1])
                    for mapping in mappings_by_target.get(key, ()):
                        try:
                            mapping_store.delete(mapping)
                        except Exception:
                            logger.exception(
                                "RAG mapping cleanup failed fileId=%s mapping=%s",
                                fid,
                                mapping.mapping_id,
                            )
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
