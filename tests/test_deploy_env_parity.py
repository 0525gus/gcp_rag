from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _env_names_sent_by(script: str, var: str) -> set[str]:
    text = (ROOT / "scripts" / script).read_text(encoding="utf-8")
    m = re.search(rf'\${var}\s*=\s*"(\^\|\^[^"]*)"', text)
    assert m, f"{script} 에서 ${var} 를 못 찾았다"
    appended = re.findall(rf'\${var}\s*\+=\s*"([^"]*)"', text)
    return set(re.findall(r"[|^]([A-Z_][A-Z0-9_]*)=", m.group(1) + "".join(appended)))


def test_sync_always_gets_the_department_map() -> None:
    assert "DEPARTMENTS_JSON" in _env_names_sent_by("deploy.ps1", "syncEnv")
    assert "DEPARTMENTS_REQUIRED" in _env_names_sent_by("deploy.ps1", "syncEnv")
    assert "ENV DEPARTMENTS_REQUIRED=true" in (
        ROOT / "services/sync/Dockerfile"
    ).read_text(encoding="utf-8")


DROPPED_WITH_DOTENV = {
    "MAX_GCS_BYTES", "SYNC_MAX_CHANGES", "RAG_CHUNK_SIZE", "RAG_CHUNK_OVERLAP",
    "QG_DENSITY_THRESHOLD", "QG_TABLE_LOSS_RATIO", "QG_MIN_TEXT_LENGTH",
    "SEARCH_TOP_K_MAX",
    "SEARCH_DISTANCE_THRESHOLD", "SEARCH_LEXICAL_RERANK",
    "SEARCH_MAX_CHUNKS_PER_FILE", "SEARCH_MAX_TOTAL_CHUNKS",
    "DLQ_COLLECTION", "SPLIT_QUEUE_COLLECTION", "SYNC_TOKEN_COLLECTION",
    "SYNC_JOB_COLLECTION", "MCP_ALLOW_NO_AUTH", "SEARCH_CACHE_TTL_SECONDS",
    "SEARCH_CACHE_MAX_ENTRIES", "MCP_SERVICE_NAME_STAFF",
    "MCP_SERVICE_NAME_STUDENT", "SCHEDULER_SA",
}


def test_dropped_keys_are_not_sent_again() -> None:
    sent: set[str] = set()
    for script, var in (
        ("deploy.ps1", "parserEnv"),
        ("deploy.ps1", "syncEnv"),
        ("deploy_mcp.ps1", "envVars"),
    ):
        sent |= _env_names_sent_by(script, var)
    revived = sorted(DROPPED_WITH_DOTENV & sent)
    assert not revived, f"지운 키가 다시 배포된다: {revived}"


def test_parser_gets_separate_ocr_and_layout_processor_settings():
    sent = _env_names_sent_by("deploy.ps1", "parserEnv")
    assert {"DOCAI_PROCESSOR_ID", "DOCAI_LOCATION", "ENABLE_DOCAI_FALLBACK",
            "DOCAI_OCR_PROCESSOR_ID", "DOCAI_OCR_LOCATION"} <= sent
