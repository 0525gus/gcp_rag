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
