"""배포 env 불변식. `--set-env-vars` 가 값을 흘리지 않는지 본다.

`--set-env-vars` 는 Cloud Run env 를 **통째로 치환한다.** 넘기지 않은 값은
사라지므로, 키 하나가 빠지면 그 순간 서비스가 코드 기본값으로 조용히
내려앉는다(예: FIRESTORE_DATABASE 가 빠지면 (default) Datastore 를 보게 되어
검색 결과의 파일명·경로가 null 이 된다).

예전에는 `tests/_golden/deployed_env.json`(운영 Cloud Run 스펙 스냅샷)과
대조했다. 그 골든은 이관 전 프로젝트의 것이라 프로젝트를 새로 파면서 지웠다 —
비교 대상이 없는 스냅샷은 회귀가 아니라 프로젝트 차이만 잡아낸다. 여기 남은
것은 스냅샷 없이도 성립하는 불변식뿐이다.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _env_names_sent_by(script: str, var: str) -> set[str]:
    """배포 스크립트의 `--set-env-vars` 템플릿에서 env 이름만 뽑는다."""
    text = (ROOT / "scripts" / script).read_text(encoding="utf-8")
    m = re.search(rf'\${var}\s*=\s*"(\^\|\^[^"]*)"', text)
    assert m, f"{script} 에서 ${var} 를 못 찾았다"
    return set(re.findall(r"[|^]([A-Z_][A-Z0-9_]*)=", m.group(1)))


def test_sync_always_gets_the_department_map() -> None:
    """DEPARTMENTS_JSON 은 sync 에 **항상** 실려야 한다.

    빠지면 sync 는 예외 없이 조용히 단일 학과로 폴백한다 — 전 학과 문서가 기본
    코퍼스 하나로 들어가고, 되돌리려면 코퍼스에서 파일을 골라 지워야 한다.
    """
    assert "DEPARTMENTS_JSON" in _env_names_sent_by("deploy.ps1", "syncEnv")


# `.env` 와 함께 지운 키들. 배포는 이 값들을 Cloud Run 에 넘긴 적이 없어서
# **운영에서는 줄곧 코드 기본값이 돌고 있었다** — 파일에만 있고 반영되지 않는
# 값이었다는 뜻이다(docs/ENV_MIGRATION.md 6번).
#
# 부작용도 여기서 없앴다: 예전에는 로컬 스크립트만 이 값들을 읽어서 **평가와
# 운영이 다른 파라미터로 측정됐다.**
DROPPED_WITH_DOTENV = {
    "MAX_GCS_BYTES", "SYNC_MAX_CHANGES", "RAG_CHUNK_SIZE", "RAG_CHUNK_OVERLAP",
    "QG_DENSITY_THRESHOLD", "QG_TABLE_LOSS_RATIO", "QG_MIN_TEXT_LENGTH",
    "ENABLE_DOCAI_FALLBACK", "DOCAI_LOCATION", "SEARCH_TOP_K_MAX",
    "SEARCH_DISTANCE_THRESHOLD", "SEARCH_LEXICAL_RERANK",
    "SEARCH_MAX_CHUNKS_PER_FILE", "SEARCH_MAX_TOTAL_CHUNKS",
    "DLQ_COLLECTION", "SPLIT_QUEUE_COLLECTION", "SYNC_TOKEN_COLLECTION",
    "SYNC_JOB_COLLECTION", "MCP_ALLOW_NO_AUTH", "SEARCH_CACHE_TTL_SECONDS",
    "SEARCH_CACHE_MAX_ENTRIES", "MCP_SERVICE_NAME_STAFF",
    "MCP_SERVICE_NAME_STUDENT", "SCHEDULER_SA",
}


def test_dropped_keys_are_not_sent_again() -> None:
    """되살아나면 '이제 반영되는 값' 이 되어 동작이 조용히 바뀐다.

    되살릴 이유가 생겼다면 근거를 남기고 이 목록에서 빼는 것이 먼저다 —
    근거 수치는 shared/config.py 주석에 있다.
    """
    sent: set[str] = set()
    for script, var in (
        ("deploy.ps1", "parserEnv"),
        ("deploy.ps1", "syncEnv"),
        ("deploy_mcp.ps1", "envVars"),
    ):
        sent |= _env_names_sent_by(script, var)
    revived = sorted(DROPPED_WITH_DOTENV & sent)
    assert not revived, f"지운 키가 다시 배포된다: {revived}"
