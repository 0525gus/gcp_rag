"""환경 설정 (asia-northeast3 고정)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache


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


@dataclass(frozen=True)
class Settings:
    gcp_project_id: str
    gcp_region: str = "asia-northeast3"
    gcs_raw_bucket: str = ""
    gcs_normalized_bucket: str = ""
    firestore_collection: str = "doc_state"
    firestore_database: str = "(default)"
    sync_token_collection: str = "sync_tokens"
    rag_corpus_name: str = ""
    docai_processor_id: str = ""
    docai_location: str = "asia-northeast3"
    drive_ids: str = ""
    # 공유 드라이브 내부에서 RAG/GCS 대상 폴더만 (비우면 드라이브 전체)
    sync_folder_ids: str = ""
    # 품질 게이트 — 실측 코퍼스 기준 완화 (이미지 많은 공문 G1 오탐 방지)
    # 예: 업적평가 안내 ~0.00085, 개인정보 캠페인 rhwp ~0.00090
    qg_density_threshold: float = 0.0005
    qg_table_fail_ratio: float = 0.3
    qg_image_ratio: float = 0.5
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
    # RAG 청킹 — Vertex 기본값. 코퍼스마다 최적값이 달라 env 로 조정 가능하게 둔다
    # (scripts/analyze_chunking.py 로 표 절단율을 재서 고를 것).
    # 실측(공문 136건/표 220개): 512 는 표의 16% 를 자르고 1024 는 6%.
    # 768 이상은 개선이 1%p 대로 평평해지며, 문서의 81% 는 768/1024 결과가 동일하다.
    rag_chunk_size: int = 1024
    rag_chunk_overlap: int = 256
    # RagFile 삭제 호출 사이 간격(초). VertexRagDataService 쿼터가 분당 60건이라
    # 배치 하나가 다 써버리지 않게 벌려 둔 값이다. 쿼터를 올렸다면 같이 낮출 것
    # (300rpm 이면 0.2 정도). 0 이면 페이싱 없음.
    rag_delete_pacing_seconds: float = 1.1
    mcp_auth_audience: str = ""
    max_gcs_bytes: int = 50 * 1024 * 1024
    enable_docai_fallback: bool = False
    dlq_collection: str = "doc_dlq"
    split_queue_collection: str = "doc_split_queue"
    # Drive→GCS ingest 병렬 워커 (무료/소형 인스턴스 기준 8 권장)
    raw_upload_concurrency: int = 8

    @property
    def drive_id_list(self) -> list[str]:
        return [d.strip() for d in self.drive_ids.split(",") if d.strip()]

    @property
    def sync_folder_id_list(self) -> list[str]:
        return [d.strip() for d in self.sync_folder_ids.split(",") if d.strip()]

    @classmethod
    def from_env(cls) -> Settings:
        mode = os.environ.get("QG_MODE", "log").strip().lower()
        if mode not in {"log", "reject", "fallback"}:
            mode = "log"
        return cls(
            gcp_project_id=_env("GCP_PROJECT_ID"),
            gcp_region=os.environ.get("GCP_REGION", "asia-northeast3"),
            gcs_raw_bucket=_env("GCS_RAW_BUCKET"),
            gcs_normalized_bucket=_env("GCS_NORMALIZED_BUCKET"),
            firestore_collection=os.environ.get("FIRESTORE_COLLECTION", "doc_state"),
            firestore_database=os.environ.get("FIRESTORE_DATABASE", "(default)"),
            sync_token_collection=os.environ.get(
                "SYNC_TOKEN_COLLECTION", "sync_tokens"
            ),
            rag_corpus_name=_env("RAG_CORPUS_NAME"),
            docai_processor_id=os.environ.get("DOCAI_PROCESSOR_ID", ""),
            docai_location=os.environ.get("DOCAI_LOCATION", "asia-northeast3"),
            drive_ids=os.environ.get("DRIVE_IDS", ""),
            sync_folder_ids=os.environ.get("SYNC_FOLDER_IDS", ""),
            qg_density_threshold=_env_float("QG_DENSITY_THRESHOLD", 0.0005),
            qg_table_fail_ratio=_env_float("QG_TABLE_FAIL_RATIO", 0.3),
            qg_image_ratio=_env_float("QG_IMAGE_RATIO", 0.5),
            qg_min_text_length=_env_int("QG_MIN_TEXT_LENGTH", 20),
            qg_mode=mode,
            top_k_default=_env_int("TOP_K_DEFAULT", 5),
            search_top_k_max=_env_int("SEARCH_TOP_K_MAX", 20),
            search_fetch_multiplier=_env_int("SEARCH_FETCH_MULTIPLIER", 3),
            search_fetch_max=_env_int("SEARCH_FETCH_MAX", 60),
            search_distance_threshold=_env_float("SEARCH_DISTANCE_THRESHOLD", 0.30),
            search_lexical_rerank=_env_bool("SEARCH_LEXICAL_RERANK", True),
            rag_chunk_size=_env_int("RAG_CHUNK_SIZE", 1024),
            rag_chunk_overlap=_env_int("RAG_CHUNK_OVERLAP", 256),
            rag_delete_pacing_seconds=_env_float("RAG_DELETE_PACING_SECONDS", 1.1),
            mcp_auth_audience=os.environ.get("MCP_AUTH_AUDIENCE", ""),
            max_gcs_bytes=_env_int("MAX_GCS_BYTES", 50 * 1024 * 1024),
            enable_docai_fallback=_env_bool("ENABLE_DOCAI_FALLBACK", False),
            dlq_collection=os.environ.get("DLQ_COLLECTION", "doc_dlq"),
            split_queue_collection=os.environ.get(
                "SPLIT_QUEUE_COLLECTION", "doc_split_queue"
            ),
            raw_upload_concurrency=max(
                1, min(_env_int("RAW_UPLOAD_CONCURRENCY", 8), 32)
            ),
        )


@lru_cache
def get_settings() -> Settings:
    return Settings.from_env()
