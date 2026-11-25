from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, fields, replace
from functools import lru_cache

logger = logging.getLogger(__name__)


def _env(key: str, default: str | None = None) -> str:
    val = os.environ.get(key, default)
    if val is None:
        raise ValueError(f"Missing required env: {key}")
    return val


def _env_float(key: str, default: float) -> float:
    raw = os.environ.get(key)
    return float(raw) if raw is not None else default


def _env_int(key: str, default: int) -> int:
    raw = os.environ.get(key)
    return int(raw) if raw is not None else default


def _env_bool(key: str, default: bool = False) -> bool:
    raw = os.environ.get(key)
    if raw is None:
        return default
    return raw.lower() in ("1", "true", "yes")


class UnknownDriveError(RuntimeError):
    pass


@dataclass(frozen=True)
class Department:
    code: str
    drive_ids: tuple[str, ...] = ()
    staff_corpus: str = ""
    student_corpus: str = ""
    hwp_bucket: str = ""
    source_bucket: str = ""
    student_folder_ids: tuple[str, ...] = ()
    sync_folder_ids: tuple[str, ...] = ()
    enable_docai_fallback: bool = False
    enable_docai_fallback_staff: bool | None = None
    enable_docai_fallback_student: bool | None = None
    enable_image_ocr: bool = False
    enable_image_ocr_staff: bool | None = None
    enable_image_ocr_student: bool | None = None


def _departments_from_json(raw: str) -> tuple[Department, ...]:
    raw = (raw or "").strip()
    if not raw:
        return ()

    def _unique_mapping(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("DEPARTMENTS_JSON contains a duplicate key")
            result[key] = value
        return result

    try:
        data = json.loads(raw, object_pairs_hook=_unique_mapping)
    except ValueError as exc:
        raise ValueError("DEPARTMENTS_JSON must be valid JSON") from exc
    if not isinstance(data, dict) or not data:
        raise ValueError("DEPARTMENTS_JSON must be a nonempty mapping")

    def _tuple(v: object) -> tuple[str, ...]:
        if isinstance(v, str):
            return tuple(x.strip() for x in v.split(",") if x.strip())
        if isinstance(v, (list, tuple)):
            if any(not isinstance(x, str) or not x.strip() for x in v):
                raise ValueError("DEPARTMENTS_JSON IDs must be nonempty strings")
            return tuple(x.strip() for x in v)
        if v is not None:
            raise ValueError("DEPARTMENTS_JSON IDs must be a list or comma-separated string")
        return ()

    def _string(v: object) -> str:
        if v is None:
            return ""
        if not isinstance(v, str):
            raise ValueError("DEPARTMENTS_JSON resource names must be strings")
        return v.strip()

    out = []
    for code, d in data.items():
        if not isinstance(d, dict):
            raise ValueError(f"DEPARTMENTS_JSON department {code} must be a mapping")
        if not isinstance(d.get("enableDocaiFallback", False), bool):
            raise ValueError(f"DEPARTMENTS_JSON department {code}: enableDocaiFallback must be boolean")
        if not isinstance(d.get("enableImageOcr", False), bool):
            raise ValueError(f"DEPARTMENTS_JSON department {code}: enableImageOcr must be boolean")
        if "enableDocaiFallbackStaff" in d and not isinstance(d["enableDocaiFallbackStaff"], bool):
            raise ValueError("DEPARTMENTS_JSON enableDocaiFallbackStaff must be boolean")
        if "enableDocaiFallbackStudent" in d and not isinstance(d["enableDocaiFallbackStudent"], bool):
            raise ValueError("DEPARTMENTS_JSON enableDocaiFallbackStudent must be boolean")
        if "enableImageOcrStaff" in d and not isinstance(d["enableImageOcrStaff"], bool):
            raise ValueError("DEPARTMENTS_JSON enableImageOcrStaff must be boolean")
        if "enableImageOcrStudent" in d and not isinstance(d["enableImageOcrStudent"], bool):
            raise ValueError("DEPARTMENTS_JSON enableImageOcrStudent must be boolean")
        out.append(
            Department(
                code=str(code),
                drive_ids=_tuple(d.get("driveIds")),
                staff_corpus=_string(d.get("staffCorpus")),
                student_corpus=_string(d.get("studentCorpus")),
                hwp_bucket=_string(d.get("hwpBucket")),
                source_bucket=_string(d.get("sourceBucket")),
                student_folder_ids=_tuple(d.get("studentFolderIds")),
                sync_folder_ids=_tuple(d.get("syncFolderIds")),
                enable_docai_fallback=d.get("enableDocaiFallback", False),
                enable_docai_fallback_staff=d.get("enableDocaiFallbackStaff", d.get("enableDocaiFallback", False)),
                enable_docai_fallback_student=d.get("enableDocaiFallbackStudent", d.get("enableDocaiFallback", False)),
                enable_image_ocr=d.get("enableImageOcr", False),
                enable_image_ocr_staff=d.get("enableImageOcrStaff", d.get("enableImageOcr", False)),
                enable_image_ocr_student=d.get("enableImageOcrStudent", d.get("enableImageOcr", False)),
            )
        )
    result = tuple(out)
    _validate_departments(result)
    return result


def _validate_departments(departments: tuple[Department, ...]) -> None:
    drives: set[str] = set()
    codes: set[str] = set()
    corpora: set[str] = set()
    for dept in departments:
        required = (dept.code, dept.staff_corpus, dept.hwp_bucket, dept.source_bucket)
        if any(not isinstance(value, str) or not value.strip() for value in required):
            raise ValueError(f"department {dept.code}: code, staffCorpus and both buckets required")
        if not dept.drive_ids or not dept.sync_folder_ids:
            raise ValueError(f"department {dept.code}: driveIds and syncFolderIds required")
        for ids in (dept.drive_ids, dept.sync_folder_ids, dept.student_folder_ids):
            if any(not isinstance(value, str) or not value.strip() for value in ids):
                raise ValueError(f"department {dept.code}: invalid folder/Drive ID")
            if len(ids) != len(set(ids)):
                raise ValueError(f"department {dept.code}: duplicate folder/Drive ID")
        if dept.code in codes or drives.intersection(dept.drive_ids):
            raise ValueError(f"department {dept.code}: duplicate department or Drive ID")
        codes.add(dept.code)
        drives.update(dept.drive_ids)
        if bool(dept.student_corpus) != bool(dept.student_folder_ids):
            raise ValueError(
                f"department {dept.code}: studentCorpus and studentFolderIds required together"
            )
        for corpus in (dept.staff_corpus, dept.student_corpus):
            if corpus:
                normalized = corpus.rstrip("/")
                if normalized in corpora:
                    raise ValueError(f"department {dept.code}: duplicate corpus")
                corpora.add(normalized)


@dataclass(frozen=True)
class Settings:
    gcp_project_id: str
    gcp_region: str = "asia-northeast3"
    gcs_hwp_original_bucket: str = ""
    gcs_source_bucket: str = ""
    firestore_database: str = "rag-sync-state"
    doc_state_collection: str = "doc_state"
    sync_token_collection: str = "sync_tokens"
    rag_corpus_name: str = ""

    rag_corpus_name_student: str = ""
    docai_processor_id: str = ""
    docai_location: str = "asia-northeast3"
    docai_ocr_processor_id: str = ""
    docai_ocr_location: str = ""
    enable_image_ocr: bool = False
    enable_image_ocr_staff: bool | None = None
    enable_image_ocr_student: bool | None = None
    drive_ids: str = ""

    sync_folder_ids: str = ""

    departments: tuple[Department, ...] = ()
    departments_required: bool = False

    student_folder_ids: str = ""

    # 품질 게이트 — 실측 코퍼스 기준 완화 (이미지 많은 공문 G1 오탐 방지)
    # 예: 업적평가 안내 ~0.00085, 개인정보 캠페인 rhwp ~0.00090
    qg_density_threshold: float = 0.0005
    qg_table_loss_ratio: float = 0.3
    qg_min_text_length: int = 20

    qg_mode: str = "log"
    # top_k_default 와 search_fetch_multiplier 는 Golden 200 평가로 고정한 Arm 2 값이다
    # (docs/verification/golden200-normalized-boundary-audit-2026-09-15.md).
    top_k_default: int = 7

    # 검색 여유분 — retrieve 는 청크 단위인데 postprocess 가 '파일당 1청크' 로
    # 접으므로, k 개 문서를 채우려면 k 보다 많이 뽑아야 한다.
    # 실측(운영 코퍼스 1,209건): 청크 30개당 고유 문서 16.4개 = 문서당 청크 ~1.8개.
    # (Vertex retrieveContexts 는 topK=100 까지 허용, 200 은 거부)
    search_top_k_max: int = 20
    search_fetch_multiplier: int = 9
    search_fetch_max: int = 60

    # 벡터 거리 상한 — 이 값보다 먼 청크는 버린다(값이 작을수록 유사).
    # 코퍼스 범위 밖 질문에 엉뚱한 공문을 물어다 주는 걸 막는 게 목적.
    # 실측(운영 코퍼스, 정답 있는 질의 15 + 무관 질의 5):
    #   정답 청크        0.120 ~ 0.214
    #   같은 질의의 오답  0.113 ~ 0.285
    #   무관한 질의      0.330 ~ 0.396   ← 0.285 와 0.330 사이가 비어 있다
    # 그 사이인 0.30 을 기본값으로 둔다. 0 이면 필터를 끈다(롤백용).
    search_distance_threshold: float = 0.30

    search_lexical_rerank: bool = True
    search_title_rerank_weight: float = 0.0
    search_temporal_rank_weight: float = 0.0
    search_sequence_rank_weight: float = 0.0
    search_document_kind_rank_weight: float = 0.0
    search_revision_rank_weight: float = 0.0
    search_reranker_mode: str = "none"
    search_reranker_candidate_k: int = 30
    search_cross_encoder_model: str = "semantic-ranker-default-004"
    search_llm_reranker_model: str = "gemini-2.5-flash-lite"
    search_reranker_timeout_seconds: float = 15.0

    search_rewrite_enabled: bool = False
    search_rewrite_model: str = "gemini-2.5-flash-lite"
    search_rewrite_timeout_seconds: float = 2.0
    search_rewrite_weight: float = 2.0

    # 한 문서에서 이어 붙일 최대 청크 수. 1 이면 예전처럼 파일당 1청크만 준다.
    # 긴 규정 문서는 답이 여러 조문에 걸쳐 있어 1이면 필요한 조문이 통째로
    # 탈락한다(실측: 제15·16·25조 청크가 후순위라 버려짐).

    search_max_chunks_per_file: int = 3
    # 한 응답에 실어 보낼 **총** 청크 수 상한. top_k × max_chunks_per_file 이
    # 곱셈이라 그냥 두면 터진다: top_k=20 이면 최대 60청크 ≈ 6만 토큰이 한 번에
    # 나가고, 그게 호출마다 에이전트 컨텍스트에 쌓인다(실측: 한 질문에 7회 호출,
    # top_k 를 10→20 으로 스스로 올림).
    # 문서 다양성이 우선이라 top_k 보다 낮게는 못 내려간다(문서당 1청크는 보장).
    search_max_total_chunks: int = 15

    # RAG 청킹 — Vertex 기본값. 코퍼스마다 최적값이 달라 env 로 조정 가능하게 둔다
    # (scripts/analyze_chunking.py 로 표 절단율을 재서 고를 것).
    # 실측(공문 136건/표 220개): 512 는 표의 16% 를 자르고 1024 는 6%.
    # 768 이상은 개선이 1%p 대로 평평해지며, 문서의 81% 는 768/1024 결과가 동일하다.
    rag_chunk_size: int = 1024
    rag_chunk_overlap: int = 256

    rag_metadata_bucket: str = ""
    rag_mapping_write_enabled: bool = True
    rag_mapping_read_enabled: bool = True
    rag_mapping_fallback_scan_enabled: bool = True

    cloud_tasks_enabled: bool = True
    task_queue_location: str = "asia-northeast3"
    task_queue_faculty: str = "faculty-rag-sync-queue"
    task_queue_student: str = "student-rag-sync-queue"
    task_service_account: str = ""
    sync_task_base_url: str = ""
    index_job_timeout_seconds: int = 900

    # RagFile 삭제 호출 사이 간격(초). VertexRagDataService 쿼터가 분당 60건이라
    # 배치 하나가 다 써버리지 않게 벌려 둔 값이다. 쿼터를 올렸다면 같이 낮출 것
    # (300rpm 이면 0.2 정도). 0 이면 페이싱 없음.
    rag_delete_pacing_seconds: float = 1.1

    # 삭제 동시 실행 수. 호출 1건이 ~0.4초 걸려 순차로는 지연이 그대로 쌓인다.
    # 실측 소모율 ≈ 동시수 / (0.4 + 페이싱) 건/초. 300rpm(=5건/초) 기준
    # 동시 4 + 페이싱 0.25 면 약 6건/초라 여유가 빠듯하니 그보다 낮게 잡을 것.
    rag_delete_concurrency: int = 1
    # 우리가 정한 '메모리에 올려도 되는 최대치'. RAG import 한도와는 다른 값이다
    # (그건 shared/mime_types.rag_size_limit — PDF/DOCX 50MB, 나머지 10MB).
    #
    # **RAG PDF 한도(50MB)보다 커야 한다.** 같으면 분할 구간이 열리지 않는다:
    # 다운로드 전 게이트가 이 값으로 막는데 분할 트리거는 min(이 값, 50MB) 초과라,
    # 둘이 같으면 쪼갤 문서가 다운로드조차 되지 않는다. 07-23 부터 대용량 PDF 2건이
    # 그렇게 SPLIT_QUEUED 에 갇혀 있었다(소비자 없는 큐라 영구 정체).
    #
    # 150MB 근거 — 실측 최대 문서가 135.8MB(455쪽)이고, 그 한 건을 쪼갤 때
    # 원본 136MB + 파이썬 힙 피크 375MB ≈ 511MB 를 쓴다. sync 2Gi 기준 동시 4건이
    # 한계라 deploy.ps1 에서 concurrency 를 4 로 묶어 뒀다. 이 값을 더 올리려면
    # 그 동시성이나 메모리도 같이 봐야 한다.
    max_gcs_bytes: int = 150 * 1024 * 1024
    enable_docai_fallback: bool = False
    enable_docai_fallback_staff: bool | None = None
    enable_docai_fallback_student: bool | None = None
    dlq_collection: str = "doc_dlq"
    split_queue_collection: str = "doc_split_queue"
    sync_job_collection: str = "sync_jobs"

    ingest_concurrency: int = 8
    # /sync/changes 가 한 번에 반환할 최대 변경 건수.
    # Cloud Workflows 는 실행당 변수 누적 512KB 가 상한인데, 변경 1건이 응답·복사본·
    # URI 까지 합쳐 워크플로우 변수를 ~900B 먹는다(≈586건에서 초과). 200건이면
    # ~174KB 로 안전 마진이 남는다. 초과분은 hasMore 로 알리고 다음 호출에서 잇는다.
    sync_max_changes: int = 200

    def __post_init__(self) -> None:
        if self.departments_required and not self.departments:
            raise ValueError("DEPARTMENTS_JSON is required in multi-department mode")
        _validate_departments(self.departments)

    @property
    def drive_id_list(self) -> list[str]:
        return [d.strip() for d in self.drive_ids.split(",") if d.strip()]

    @property
    def sync_folder_id_list(self) -> list[str]:
        return [d.strip() for d in self.sync_folder_ids.split(",") if d.strip()]

    @property
    def student_folder_id_list(self) -> list[str]:
        return [d.strip() for d in self.student_folder_ids.split(",") if d.strip()]

    @property
    def audience_split_enabled(self) -> bool:
        return bool(self.rag_corpus_name_student and self.student_folder_id_list)

    def department_for_drive(self, drive_id: str) -> Department | None:
        if not self.departments:
            return None
        for d in self.departments:
            if drive_id in d.drive_ids:
                return d
        return None

    def for_drive(self, drive_id: str) -> Settings:
        if not self.departments:
            return self
        dept = self.department_for_drive(drive_id)
        if dept is None:
            raise UnknownDriveError(f"학과 맵에 없는 드라이브: {drive_id}")

        return replace(
            self,
            rag_corpus_name=dept.staff_corpus,
            rag_corpus_name_student=dept.student_corpus,
            gcs_hwp_original_bucket=dept.hwp_bucket,
            gcs_source_bucket=dept.source_bucket,
            student_folder_ids=",".join(dept.student_folder_ids),
            sync_folder_ids=",".join(dept.sync_folder_ids),
            enable_docai_fallback=dept.enable_docai_fallback,
            enable_docai_fallback_staff=dept.enable_docai_fallback_staff,
            enable_docai_fallback_student=dept.enable_docai_fallback_student,
            enable_image_ocr=any(v if v is not None else dept.enable_image_ocr for v in (dept.enable_image_ocr_staff, dept.enable_image_ocr_student)),
            enable_image_ocr_staff=dept.enable_image_ocr_staff,
            enable_image_ocr_student=dept.enable_image_ocr_student,
        )

    def parser_option_for(self, option: str, audience: object) -> bool:
        suffix = "student" if str(getattr(audience, "value", audience)).upper() == "STUDENT" else "staff"
        explicit = getattr(self, f"{option}_{suffix}")
        return explicit if explicit is not None else getattr(self, option)

    @classmethod
    def from_env(cls) -> Settings:
        d = {f.name: f.default for f in fields(cls)}
        mode = os.environ.get("QG_MODE", d["qg_mode"]).strip().lower()
        if mode not in {"log", "reject", "fallback"}:
            mode = d["qg_mode"]
        region = os.environ.get("GCP_REGION", d["gcp_region"])
        return cls(
            departments_required=(
                _env_bool("DEPARTMENTS_REQUIRED") or "DEPARTMENTS_JSON" in os.environ
            ),
            gcp_project_id=_env("GCP_PROJECT_ID"),
            gcp_region=region,
            gcs_hwp_original_bucket=_env("GCS_HWP_ORIGINAL_BUCKET"),
            gcs_source_bucket=_env("GCS_SOURCE_BUCKET"),
            firestore_database=os.environ.get("FIRESTORE_DATABASE", d["firestore_database"]),
            doc_state_collection=os.environ.get("DOC_STATE_COLLECTION", d["doc_state_collection"]),
            sync_token_collection=os.environ.get(
                "SYNC_TOKEN_COLLECTION", d["sync_token_collection"]
            ),
            rag_corpus_name=_env("RAG_CORPUS_NAME"),
            rag_corpus_name_student=os.environ.get("RAG_CORPUS_NAME_STUDENT", ""),
            docai_processor_id=os.environ.get("DOCAI_PROCESSOR_ID", ""),
            docai_location=os.environ.get("DOCAI_LOCATION", d["docai_location"]),
            docai_ocr_processor_id=os.environ.get("DOCAI_OCR_PROCESSOR_ID", ""),
            docai_ocr_location=os.environ.get("DOCAI_OCR_LOCATION", ""),
            drive_ids=os.environ.get("DRIVE_IDS", ""),
            sync_folder_ids=os.environ.get("SYNC_FOLDER_IDS", ""),
            student_folder_ids=os.environ.get("STUDENT_FOLDER_IDS", ""),
            departments=_departments_from_json(os.environ.get("DEPARTMENTS_JSON", "")),
            qg_density_threshold=_env_float("QG_DENSITY_THRESHOLD", d["qg_density_threshold"]),
            qg_table_loss_ratio=_env_float("QG_TABLE_LOSS_RATIO", d["qg_table_loss_ratio"]),
            qg_min_text_length=_env_int("QG_MIN_TEXT_LENGTH", d["qg_min_text_length"]),
            qg_mode=mode,
            top_k_default=_env_int("TOP_K_DEFAULT", d["top_k_default"]),
            search_top_k_max=_env_int("SEARCH_TOP_K_MAX", d["search_top_k_max"]),
            search_fetch_multiplier=_env_int(
                "SEARCH_FETCH_MULTIPLIER", d["search_fetch_multiplier"]
            ),
            search_fetch_max=_env_int("SEARCH_FETCH_MAX", d["search_fetch_max"]),
            search_distance_threshold=_env_float(
                "SEARCH_DISTANCE_THRESHOLD", d["search_distance_threshold"]
            ),
            search_lexical_rerank=_env_bool("SEARCH_LEXICAL_RERANK", d["search_lexical_rerank"]),
            search_title_rerank_weight=max(
                0.0, _env_float("SEARCH_TITLE_RERANK_WEIGHT", d["search_title_rerank_weight"])
            ),
            search_temporal_rank_weight=max(
                0.0, _env_float("SEARCH_TEMPORAL_RANK_WEIGHT", d["search_temporal_rank_weight"])
            ),
            search_sequence_rank_weight=max(
                0.0, _env_float("SEARCH_SEQUENCE_RANK_WEIGHT", d["search_sequence_rank_weight"])
            ),
            search_document_kind_rank_weight=max(
                0.0,
                _env_float(
                    "SEARCH_DOCUMENT_KIND_RANK_WEIGHT", d["search_document_kind_rank_weight"]
                ),
            ),
            search_revision_rank_weight=max(
                0.0, _env_float("SEARCH_REVISION_RANK_WEIGHT", d["search_revision_rank_weight"])
            ),
            search_reranker_mode=os.environ.get(
                "SEARCH_RERANKER_MODE", d["search_reranker_mode"]
            ).strip().lower(),
            search_reranker_candidate_k=max(
                10,
                min(_env_int("SEARCH_RERANKER_CANDIDATE_K", d["search_reranker_candidate_k"]), 30),
            ),
            search_cross_encoder_model=os.environ.get(
                "SEARCH_CROSS_ENCODER_MODEL", d["search_cross_encoder_model"]
            ).strip(),
            search_llm_reranker_model=os.environ.get(
                "SEARCH_LLM_RERANKER_MODEL", d["search_llm_reranker_model"]
            ).strip(),
            search_reranker_timeout_seconds=max(
                0.1,
                _env_float("SEARCH_RERANKER_TIMEOUT_SECONDS", d["search_reranker_timeout_seconds"]),
            ),
            search_rewrite_enabled=_env_bool(
                "SEARCH_REWRITE_ENABLED", d["search_rewrite_enabled"]
            ),
            search_rewrite_model=os.environ.get(
                "SEARCH_REWRITE_MODEL", d["search_rewrite_model"]
            ).strip(),
            search_rewrite_timeout_seconds=max(
                0.1,
                _env_float("SEARCH_REWRITE_TIMEOUT_SECONDS", d["search_rewrite_timeout_seconds"]),
            ),
            search_rewrite_weight=max(
                0.0, _env_float("SEARCH_REWRITE_WEIGHT", d["search_rewrite_weight"])
            ),
            search_max_chunks_per_file=max(
                1, _env_int("SEARCH_MAX_CHUNKS_PER_FILE", d["search_max_chunks_per_file"])
            ),
            search_max_total_chunks=max(
                1, _env_int("SEARCH_MAX_TOTAL_CHUNKS", d["search_max_total_chunks"])
            ),
            rag_chunk_size=_env_int("RAG_CHUNK_SIZE", d["rag_chunk_size"]),
            rag_chunk_overlap=_env_int("RAG_CHUNK_OVERLAP", d["rag_chunk_overlap"]),
            rag_metadata_bucket=os.environ.get("RAG_METADATA_BUCKET", ""),
            rag_mapping_write_enabled=_env_bool(
                "RAG_MAPPING_WRITE_ENABLED", d["rag_mapping_write_enabled"]
            ),
            rag_mapping_read_enabled=_env_bool(
                "RAG_MAPPING_READ_ENABLED", d["rag_mapping_read_enabled"]
            ),
            rag_mapping_fallback_scan_enabled=_env_bool(
                "RAG_MAPPING_FALLBACK_SCAN_ENABLED", d["rag_mapping_fallback_scan_enabled"]
            ),
            cloud_tasks_enabled=_env_bool("CLOUD_TASKS_ENABLED", d["cloud_tasks_enabled"]),
            task_queue_location=os.environ.get("TASK_QUEUE_LOCATION", region),
            task_queue_faculty=os.environ.get("TASK_QUEUE_FACULTY", d["task_queue_faculty"]),
            task_queue_student=os.environ.get("TASK_QUEUE_STUDENT", d["task_queue_student"]),
            task_service_account=os.environ.get("TASK_SERVICE_ACCOUNT", ""),
            sync_task_base_url=os.environ.get("SYNC_TASK_BASE_URL", ""),
            index_job_timeout_seconds=max(
                60, _env_int("INDEX_JOB_TIMEOUT_SECONDS", d["index_job_timeout_seconds"])
            ),
            rag_delete_pacing_seconds=_env_float(
                "RAG_DELETE_PACING_SECONDS", d["rag_delete_pacing_seconds"]
            ),
            rag_delete_concurrency=max(
                1, min(_env_int("RAG_DELETE_CONCURRENCY", d["rag_delete_concurrency"]), 16)
            ),
            max_gcs_bytes=_env_int("MAX_GCS_BYTES", d["max_gcs_bytes"]),
            enable_docai_fallback=_env_bool("ENABLE_DOCAI_FALLBACK", d["enable_docai_fallback"]),
            dlq_collection=os.environ.get("DLQ_COLLECTION", d["dlq_collection"]),
            split_queue_collection=os.environ.get(
                "SPLIT_QUEUE_COLLECTION", d["split_queue_collection"]
            ),
            sync_job_collection=os.environ.get("SYNC_JOB_COLLECTION", d["sync_job_collection"]),
            ingest_concurrency=max(
                1, min(_env_int("INGEST_CONCURRENCY", d["ingest_concurrency"]), 32)
            ),
            sync_max_changes=max(
                1, min(_env_int("SYNC_MAX_CHANGES", d["sync_max_changes"]), 2000)
            ),
        )


_FIELDS_NOT_FROM_ENV = frozenset({
    "departments",
    "departments_required",
    "enable_image_ocr",
    "enable_image_ocr_staff",
    "enable_image_ocr_student",
    "enable_docai_fallback_staff",
    "enable_docai_fallback_student",
})

PER_DEPLOYMENT_ENV = frozenset({
    "GCP_PROJECT_ID",
    "GCS_HWP_ORIGINAL_BUCKET",
    "GCS_SOURCE_BUCKET",
    "RAG_CORPUS_NAME",
    "RAG_CORPUS_NAME_STUDENT",
    "DRIVE_IDS",
    "SYNC_FOLDER_IDS",
    "STUDENT_FOLDER_IDS",
    "TASK_SERVICE_ACCOUNT",
    "SYNC_TASK_BASE_URL",
})


def env_defaults(region: str | None = None) -> dict[str, object]:
    values = {
        f.name.upper(): f.default
        for f in fields(Settings)
        if f.name not in _FIELDS_NOT_FROM_ENV and f.name.upper() not in PER_DEPLOYMENT_ENV
    }
    if region:
        values["GCP_REGION"] = region
    values["TASK_QUEUE_LOCATION"] = values["GCP_REGION"]
    return values


@lru_cache
def get_settings() -> Settings:
    return Settings.from_env()
