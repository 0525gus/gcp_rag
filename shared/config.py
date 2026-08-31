"""환경 설정 (asia-northeast3 고정)."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, replace
from functools import lru_cache
from typing import Any

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
    """학과 맵이 있는데 이 드라이브가 거기 없다.

    조용히 기본 코퍼스로 떨어뜨리면 **남의 학과 자료가 섞인다.** 그건 되돌리려면
    코퍼스에서 파일을 골라 지워야 하는 사고라, 모르는 드라이브는 처리하지 않고
    소리를 낸다. 호출부가 그 드라이브만 건너뛰고 나머지를 계속 돌리면 된다.
    """


@dataclass(frozen=True)
class Department:
    """학과 하나. 공유 드라이브가 곧 학과 경계다.

    드라이브가 학과마다 다르므로 `driveId` 가 학과 키가 된다 — doc_state 에
    이미 driveId 가 있어서 문서에 학과 필드를 따로 둘 필요가 없다.
    (같은 드라이브를 여러 학과가 나눠 쓰면 이 전제가 깨진다. 그때는
    sync_tokens 커서도 드라이브 단위라 학과별 sync 자체가 성립하지 않는다.)
    """

    code: str
    drive_ids: tuple[str, ...] = ()
    staff_corpus: str = ""
    student_corpus: str = ""
    hwp_bucket: str = ""
    source_bucket: str = ""
    student_folder_ids: tuple[str, ...] = ()
    sync_folder_ids: tuple[str, ...] = ()


def _departments_from_json(raw: str) -> tuple[Department, ...]:
    """DEPARTMENTS_JSON 파싱. 깨졌으면 **비운다**(=단일 학과 동작).

    여기서 예외를 올리면 설정 오타 하나로 sync 가 기동조차 못 한다. 반대로
    잘못된 맵을 반쯤 들고 도는 것이 더 위험하므로, 파싱 실패는 전부 버리고
    경고를 남긴 뒤 기존 단일 코퍼스 경로로 간다.
    """
    raw = (raw or "").strip()
    if not raw:
        return ()
    try:
        data = json.loads(raw)
    except Exception:
        logger.exception("DEPARTMENTS_JSON 파싱 실패 — 단일 학과로 동작한다")
        return ()
    if not isinstance(data, dict):
        logger.error("DEPARTMENTS_JSON 이 매핑이 아니다 — 단일 학과로 동작한다")
        return ()

    def _tuple(v: object) -> tuple[str, ...]:
        if isinstance(v, str):
            return tuple(x.strip() for x in v.split(",") if x.strip())
        if isinstance(v, (list, tuple)):
            return tuple(str(x).strip() for x in v if str(x).strip())
        return ()

    out = []
    for code, d in data.items():
        if not isinstance(d, dict):
            continue
        out.append(
            Department(
                code=str(code),
                drive_ids=_tuple(d.get("driveIds")),
                staff_corpus=str(d.get("staffCorpus") or ""),
                student_corpus=str(d.get("studentCorpus") or ""),
                hwp_bucket=str(d.get("hwpBucket") or ""),
                source_bucket=str(d.get("sourceBucket") or ""),
                student_folder_ids=_tuple(d.get("studentFolderIds")),
                sync_folder_ids=_tuple(d.get("syncFolderIds")),
            )
        )
    return tuple(out)


@dataclass(frozen=True)
class Settings:
    gcp_project_id: str
    gcp_region: str = "asia-northeast3"
    gcs_hwp_original_bucket: str = ""
    gcs_source_bucket: str = ""
    firestore_database: str = "rag-sync-state"
    # 컬렉션 5종은 {이름}_COLLECTION 한 축으로 읽는다.
    doc_state_collection: str = "doc_state"
    sync_token_collection: str = "sync_tokens"
    rag_corpus_name: str = ""

    # 학생 공개용 코퍼스. **비우면 분리 기능 자체가 꺼진다**(현행 단일 코퍼스 동작).
    # rag_corpus_name 쪽은 전체(학생+교직원)를 담는 교직원용이므로 이름을 바꾸지
    # 않는다 — 기존 배포·env 가 그대로 교직원용이 된다.
    rag_corpus_name_student: str = ""
    docai_processor_id: str = ""
    docai_location: str = "asia-northeast3"
    drive_ids: str = ""

    # 공유 드라이브 내부에서 RAG/GCS 대상 폴더만. 배포는 필수(비우면 거부).
    # 런타임에 비면 드라이브 전체 — 테스트/우회용이지 운영 기본이 아니다.
    sync_folder_ids: str = ""

    # 학과 맵. 비어 있으면 **지금까지와 똑같이** 단일 학과로 동작한다 —
    # 이 필드가 도입돼도 기존 배포가 달라지지 않게 하는 장치다.
    departments: tuple[Department, ...] = ()

    # 이 폴더 트리 아래 문서만 학생 코퍼스에 실린다. sync_folder_ids 의 부분집합이며
    # 여기 없는 문서는 전부 교직원 전용이다(판정 불가도 교직원 — 안전한 쪽).
    student_folder_ids: str = ""

    # 품질 게이트 — 실측 코퍼스 기준 완화 (이미지 많은 공문 G1 오탐 방지)
    # 예: 업적평가 안내 ~0.00085, 개인정보 캠페인 rhwp ~0.00090
    qg_density_threshold: float = 0.0005
    # 문서 구조상 표 N개 중 마크다운에 남지 않은 비율의 허용 상한.
    # (구 QG_TABLE_FAIL_RATIO 는 셀 단위 실패율이었으나 그 지표를 채우는 파서가
    #  없어 발동한 적이 없다. 의미가 달라졌으므로 이름도 바꾼다.)
    qg_table_loss_ratio: float = 0.3
    qg_min_text_length: int = 20

    # log: 경고만 / reject: 422·DLQ / fallback: Document AI(enable_docai_fallback 필요)
    qg_mode: str = "log"
    top_k_default: int = 5

    # 검색 여유분 — retrieve 는 청크 단위인데 postprocess 가 '파일당 1청크' 로
    # 접으므로, k 개 문서를 채우려면 k 보다 많이 뽑아야 한다.
    # 실측(운영 코퍼스 1,209건): 청크 30개당 고유 문서 16.4개 = 문서당 청크 ~1.8개.
    # 상한이 k*배수보다 작으면 여유분이 소리 없이 사라지므로
    # search_fetch_max >= search_top_k_max * search_fetch_multiplier 를 지킬 것.
    # (Vertex retrieveContexts 는 topK=100 까지 허용, 200 은 거부)
    search_top_k_max: int = 20
    search_fetch_multiplier: int = 3
    search_fetch_max: int = 60

    # 벡터 거리 상한 — 이 값보다 먼 청크는 버린다(값이 작을수록 유사).
    # 코퍼스 범위 밖 질문에 엉뚱한 공문을 물어다 주는 걸 막는 게 목적.
    # 실측(운영 코퍼스, 정답 있는 질의 15 + 무관 질의 5):
    #   정답 청크        0.120 ~ 0.214
    #   같은 질의의 오답  0.113 ~ 0.285
    #   무관한 질의      0.330 ~ 0.396   ← 0.285 와 0.330 사이가 비어 있다
    # 그 사이인 0.30 을 기본값으로 둔다. 0 이면 필터를 끈다(롤백용).
    search_distance_threshold: float = 0.30

    # 어휘(BM25) 순위를 벡터 순위와 RRF 로 합쳐 상위를 다시 세운다.
    # 점수가 아니라 **순위만** 쓰므로, 예전에 걷어낸 추측 기반 재정렬(83563ad)과
    # 달리 score 의미를 몰라도 안전하다. 재검색이 아니라 이미 받은 후보만
    # 다시 세우는 것이라 recall 은 그대로다.
    # 실측(골든 15): hit@1 7/15 -> 11/15, MRR 0.647 -> 0.802. RRF 상수에는 둔감.
    # 다만 골든 질의는 문서 어휘와 겹치게 쓰인 편향이 있고, 로그의 실사용 질의
    # 10건에서는 top1 변화가 없었다(해롭지도 않았다).
    search_lexical_rerank: bool = True

    # 한 문서에서 이어 붙일 최대 청크 수. 1 이면 예전처럼 파일당 1청크만 준다.
    # 긴 규정 문서는 답이 여러 조문에 걸쳐 있어 1이면 필요한 조문이 통째로
    # 탈락한다(실측: 제15·16·25조 청크가 후순위라 버려짐).

    search_max_chunks_per_file: int = 3
    # 한 응답에 실어 보낼 **총** 청크 수 상한. top_k × max_chunks_per_file 이
    # 곱셈이라 그냥 두면 터진다: top_k=20 이면 최대 60청크 ≈ 6만 토큰이 한 번에
    # 나가고, 그게 호출마다 에이전트 컨텍스트에 쌓인다(실측: 한 질문에 7회 호출,
    # top_k 를 10→20 으로 스스로 올림).
    # 기본 15 = top_k_default(5) × 3 이라 기본 호출의 동작은 그대로다.
    # 문서 다양성이 우선이라 top_k 보다 낮게는 못 내려간다(문서당 1청크는 보장).
    search_max_total_chunks: int = 15

    # RAG 청킹 — Vertex 기본값. 코퍼스마다 최적값이 달라 env 로 조정 가능하게 둔다
    # (scripts/analyze_chunking.py 로 표 절단율을 재서 고를 것).
    # 실측(공문 136건/표 220개): 512 는 표의 16% 를 자르고 1024 는 6%.
    # 768 이상은 개선이 1%p 대로 평평해지며, 문서의 81% 는 768/1024 결과가 동일하다.
    rag_chunk_size: int = 1024
    rag_chunk_overlap: int = 256

    # RagFile resource-name mapping rollout switches.  Write/read are separate so
    # production can accumulate and audit mappings before direct deletion trusts
    # them.  A missing mapping falls back to the legacy corpus scan until coverage
    # has been proven.
    rag_metadata_bucket: str = ""
    rag_mapping_write_enabled: bool = False
    rag_mapping_read_enabled: bool = False
    rag_mapping_fallback_scan_enabled: bool = True

    # Cloud Tasks 기반 코퍼스 분리. 기능 플래그를 켜기 전에는 기존 동기
    # /sync/index-gcs 경로를 그대로 쓴다.
    cloud_tasks_enabled: bool = False
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
    dlq_collection: str = "doc_dlq"
    split_queue_collection: str = "doc_split_queue"
    # 장시간 작업 진행률.
    sync_job_collection: str = "sync_jobs"
    
    # Drive→GCS ingest 병렬 워커 (무료/소형 인스턴스 기준 8 권장).
    # HWP 뿐 아니라 FILE_COPY·GOOGLE_EXPORT 까지 전 라우트를 묶는다.
    ingest_concurrency: int = 8
    # /sync/changes 가 한 번에 반환할 최대 변경 건수.
    # Cloud Workflows 는 실행당 변수 누적 512KB 가 상한인데, 변경 1건이 응답·복사본·
    # URI 까지 합쳐 워크플로우 변수를 ~900B 먹는다(≈586건에서 초과). 200건이면
    # ~174KB 로 안전 마진이 남는다. 초과분은 hasMore 로 알리고 다음 호출에서 잇는다.
    sync_max_changes: int = 200

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
        """학생/교직원 코퍼스 분리가 켜져 있는가.

        둘 다 있어야 켠다. 코퍼스만 있고 폴더가 없으면 학생 코퍼스가 영원히
        비고, 폴더만 있고 코퍼스가 없으면 판정 결과를 쓸 곳이 없다.
        """
        return bool(self.rag_corpus_name_student and self.student_folder_id_list)

    def department_for_drive(self, drive_id: str) -> Department | None:
        if not self.departments:
            return None
        for d in self.departments:
            if drive_id in d.drive_ids:
                return d
        return None

    def for_drive(self, drive_id: str) -> Settings:
        """이 드라이브(=학과)용 설정으로 바꾼 사본.

        핸들러 진입점에서 한 번만 갈아끼우면 그 아래 호출들(RagEngineClient,
        GcsClient, 폴더 스코프)이 전부 학과 값을 쓴다 — 25곳을 각각 고치지
        않아도 되는 이유다.

        학과가 값을 안 적었으면 공용값(common.yaml)을 그대로 둔다. 기존 학과를
        옮기지 않고 새 학과만 자기 버킷을 갖는 이관 방식을 그대로 따른다.
        """
        if not self.departments:
            return self
        dept = self.department_for_drive(drive_id)
        if dept is None:
            raise UnknownDriveError(f"학과 맵에 없는 드라이브: {drive_id}")

        changed: dict[str, Any] = {}
        if dept.staff_corpus:
            changed["rag_corpus_name"] = dept.staff_corpus
        if dept.student_corpus:
            changed["rag_corpus_name_student"] = dept.student_corpus
        if dept.hwp_bucket:
            changed["gcs_hwp_original_bucket"] = dept.hwp_bucket
        if dept.source_bucket:
            changed["gcs_source_bucket"] = dept.source_bucket
        if dept.student_folder_ids:
            changed["student_folder_ids"] = ",".join(dept.student_folder_ids)
        if dept.sync_folder_ids:
            changed["sync_folder_ids"] = ",".join(dept.sync_folder_ids)
        return replace(self, **changed) if changed else self

    @classmethod
    def from_env(cls) -> Settings:
        mode = os.environ.get("QG_MODE", "log").strip().lower()
        if mode not in {"log", "reject", "fallback"}:
            mode = "log"
        return cls(
            gcp_project_id=_env("GCP_PROJECT_ID"),
            gcp_region=os.environ.get("GCP_REGION", "asia-northeast3"),
            gcs_hwp_original_bucket=_env("GCS_HWP_ORIGINAL_BUCKET"),
            gcs_source_bucket=_env("GCS_SOURCE_BUCKET"),
            firestore_database=os.environ.get("FIRESTORE_DATABASE", "rag-sync-state"),
            doc_state_collection=os.environ.get("DOC_STATE_COLLECTION", "doc_state"),
            sync_token_collection=os.environ.get(
                "SYNC_TOKEN_COLLECTION", "sync_tokens"
            ),
            rag_corpus_name=_env("RAG_CORPUS_NAME"),
            rag_corpus_name_student=os.environ.get("RAG_CORPUS_NAME_STUDENT", ""),
            docai_processor_id=os.environ.get("DOCAI_PROCESSOR_ID", ""),
            docai_location=os.environ.get("DOCAI_LOCATION", "asia-northeast3"),
            drive_ids=os.environ.get("DRIVE_IDS", ""),
            sync_folder_ids=os.environ.get("SYNC_FOLDER_IDS", ""),
            student_folder_ids=os.environ.get("STUDENT_FOLDER_IDS", ""),
            departments=_departments_from_json(
                os.environ.get("DEPARTMENTS_JSON", "")
            ),
            qg_density_threshold=_env_float("QG_DENSITY_THRESHOLD", 0.0005),
            qg_table_loss_ratio=_env_float("QG_TABLE_LOSS_RATIO", 0.3),
            qg_min_text_length=_env_int("QG_MIN_TEXT_LENGTH", 20),
            qg_mode=mode,
            top_k_default=_env_int("TOP_K_DEFAULT", 5),
            search_top_k_max=_env_int("SEARCH_TOP_K_MAX", 20),
            search_fetch_multiplier=_env_int("SEARCH_FETCH_MULTIPLIER", 3),
            search_fetch_max=_env_int("SEARCH_FETCH_MAX", 60),
            search_distance_threshold=_env_float("SEARCH_DISTANCE_THRESHOLD", 0.30),
            search_lexical_rerank=_env_bool("SEARCH_LEXICAL_RERANK", True),
            search_max_chunks_per_file=max(
                1, _env_int("SEARCH_MAX_CHUNKS_PER_FILE", 3)
            ),
            search_max_total_chunks=max(
                1, _env_int("SEARCH_MAX_TOTAL_CHUNKS", 15)
            ),
            rag_chunk_size=_env_int("RAG_CHUNK_SIZE", 1024),
            rag_chunk_overlap=_env_int("RAG_CHUNK_OVERLAP", 256),
            rag_metadata_bucket=os.environ.get("RAG_METADATA_BUCKET", ""),
            rag_mapping_write_enabled=_env_bool("RAG_MAPPING_WRITE_ENABLED", False),
            rag_mapping_read_enabled=_env_bool("RAG_MAPPING_READ_ENABLED", False),
            rag_mapping_fallback_scan_enabled=_env_bool(
                "RAG_MAPPING_FALLBACK_SCAN_ENABLED", True
            ),
            cloud_tasks_enabled=_env_bool("CLOUD_TASKS_ENABLED", False),
            task_queue_location=os.environ.get(
                "TASK_QUEUE_LOCATION", os.environ.get("GCP_REGION", "asia-northeast3")
            ),
            task_queue_faculty=os.environ.get(
                "TASK_QUEUE_FACULTY", "faculty-rag-sync-queue"
            ),
            task_queue_student=os.environ.get(
                "TASK_QUEUE_STUDENT", "student-rag-sync-queue"
            ),
            task_service_account=os.environ.get("TASK_SERVICE_ACCOUNT", ""),
            sync_task_base_url=os.environ.get("SYNC_TASK_BASE_URL", ""),
            index_job_timeout_seconds=max(
                60, _env_int("INDEX_JOB_TIMEOUT_SECONDS", 900)
            ),
            rag_delete_pacing_seconds=_env_float("RAG_DELETE_PACING_SECONDS", 1.1),
            rag_delete_concurrency=max(
                1, min(_env_int("RAG_DELETE_CONCURRENCY", 1), 16)
            ),
            max_gcs_bytes=_env_int("MAX_GCS_BYTES", 150 * 1024 * 1024),
            enable_docai_fallback=_env_bool("ENABLE_DOCAI_FALLBACK", False),
            dlq_collection=os.environ.get("DLQ_COLLECTION", "doc_dlq"),
            split_queue_collection=os.environ.get(
                "SPLIT_QUEUE_COLLECTION", "doc_split_queue"
            ),
            sync_job_collection=os.environ.get("SYNC_JOB_COLLECTION", "sync_jobs"),
            ingest_concurrency=max(
                1, min(_env_int("INGEST_CONCURRENCY", 8), 32)
            ),
            sync_max_changes=max(1, min(_env_int("SYNC_MAX_CHANGES", 200), 2000)),
        )


@lru_cache
def get_settings() -> Settings:
    return Settings.from_env()
