"""배포 env 등가성. `.env` → `config/` 이관이 값을 흘리지 않았는지 대조한다.

`--set-env-vars` 는 Cloud Run env 를 **통째로 치환한다.** 넘기지 않은 값은
사라지므로, 이관 중 키 하나가 빠지면 그 순간 서비스가 코드 기본값으로 조용히
내려앉는다(예: FIRESTORE_DATABASE 가 빠지면 (default) Datastore 를 보게 되어
검색 결과의 파일명·경로가 null 이 된다).

그래서 기준을 **지금 돌고 있는 서비스**로 잡는다. `tests/_golden/deployed_env.json`
은 Cloud Run 스펙 스냅샷이고(키 값은 길이만 기록), 여기서는

  1. 배포 스크립트가 보내는 env **이름 집합**이 그때와 같은가
  2. config 가 만드는 **값**이 그때와 같은가 (의도한 변경 제외)
  3. 키는 평문을 못 남기므로 **길이**가 같은가

를 본다. 골든은 2026-08-23 이관 배포 직후 상태다(rag-sync 에 DEPARTMENTS_JSON
이 실린 시점). 다시 뜨려면 gcloud 로 describe 해서 갱신할 것 —
**의도한 변경일 때만** 갱신한다. 그게 이 파일의 존재 이유다.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.dept_config import build_env

GOLDEN = ROOT / "tests" / "_golden" / "deployed_env.json"

# 골든은 cs 배포 스냅샷이라 값 대조에는 cs.yaml 실파일이 필요하다. 그 파일은
# 커밋되지 않으므로(키가 들어간다) 새 클론·CI 에는 없다 — 이름 집합 검사는
# 파일 없이도 돌고, 값 검사만 건너뛴다.
requires_cs_config = pytest.mark.skipif(
    not (ROOT / "config" / "departments" / "cs.yaml").is_file(),
    reason="config/departments/cs.yaml 없음 (gitignore 대상)",
)

# 골든에 평문으로 못 남기는 값. **길이로만** 대조한다
# (test_secret_lengths_match_deployment). 값 비교에서는 빼야 한다 —
# 골든에는 "<len:10>" 이 들어 있어 실제 키와는 당연히 다르기 때문이다.
SECRETS_COMPARED_BY_LENGTH = {"MCP_API_KEY"}

# 아직 배포되지 않은, **일부러 만든** 차이. 여기 없는 차이는 전부 실수로 본다.
# 지금은 비어 있다 — 이관분이 전부 배포됐다(2026-08-23). 다시 채울 일이 생기면
# 배포 직후 비우는 것까지 한 묶음으로 할 것. 낡은 목록은 실제 회귀를 가려준다.
INTENDED_CHANGES: set[str] = set()

# 배포 전 단계에서 **일부러 더한** env. 서비스별로 적는다 — 한 서비스에 더한 것이
# 다른 서비스에서도 통과하면 "빠뜨렸다" 를 못 잡는다.
# DEPARTMENTS_JSON 은 배포돼 골든에 들어갔으므로 여기서 뺐다.
INTENDED_ADDITIONS: dict[str, set[str]] = {}


def _golden() -> dict:
    if not GOLDEN.is_file():
        pytest.skip("골든 스냅샷 없음 — gcloud describe 로 생성할 것")
    return json.loads(GOLDEN.read_text(encoding="utf-8"))


def _env_names_sent_by(script: str, var: str) -> set[str]:
    """배포 스크립트의 `--set-env-vars` 템플릿에서 env 이름만 뽑는다."""
    text = (ROOT / "scripts" / script).read_text(encoding="utf-8")
    m = re.search(rf'\${var}\s*=\s*"(\^\|\^[^"]*)"', text)
    assert m, f"{script} 에서 ${var} 를 못 찾았다"
    return set(re.findall(r"[|^]([A-Z_][A-Z0-9_]*)=", m.group(1)))


@pytest.mark.parametrize(
    ("service", "script", "var"),
    [
        ("rag-parser", "deploy.ps1", "parserEnv"),
        ("rag-sync", "deploy.ps1", "syncEnv"),
    ],
)
def test_service_env_names_unchanged(service: str, script: str, var: str) -> None:
    """parser·sync 가 보내는 env 이름이 배포된 것과 같아야 한다.

    `.env` → `config/` 이관에서 키 하나가 빠지면 `--set-env-vars` 가 env 를 통째로
    치환하므로 그 값이 **사라진다**. 일부러 더한 것만 INTENDED_ADDITIONS 에 적는다.
    """
    golden = _golden()
    if service not in golden:
        pytest.skip(f"{service} 스냅샷 없음")
    sent = _env_names_sent_by(script, var)
    deployed = set(golden[service]["env"])
    added = INTENDED_ADDITIONS.get(service, set())
    assert sent - added == deployed, (
        f"env 이름이 달라졌다\n  빠짐: {sorted(deployed - sent)}"
        f"\n  추가: {sorted(sent - deployed - added)}"
    )


def test_sync_always_gets_the_department_map() -> None:
    """DEPARTMENTS_JSON 은 sync 에 **항상** 실려야 한다.

    빠지면 sync 는 예외 없이 조용히 단일 학과로 폴백한다 — 전 학과 문서가 기본
    코퍼스 하나로 들어가고, 되돌리려면 코퍼스에서 파일을 골라 지워야 한다.
    배포가 끝나 INTENDED_ADDITIONS 가 비었어도 이 불변식은 남아야 하므로
    그 목록과 무관하게 따로 검사한다.
    """
    assert "DEPARTMENTS_JSON" in _env_names_sent_by("deploy.ps1", "syncEnv")


@pytest.mark.parametrize(
    ("service", "audience"),
    [("rag-mcp-cs-staff", "staff"), ("rag-mcp-cs-student", "student")],
)
def test_mcp_env_names_unchanged(service: str, audience: str) -> None:
    """보내는 env 이름이 배포된 것과 같아야 한다. 빠지면 그 값이 사라진다."""
    golden = _golden()
    if service not in golden:
        pytest.skip(f"{service} 스냅샷 없음")
    sent = _env_names_sent_by("deploy_mcp.ps1", "envVars")
    deployed = set(golden[service]["env"])
    assert sent == deployed, (
        f"env 이름이 달라졌다\n  빠짐: {sorted(deployed - sent)}"
        f"\n  추가: {sorted(sent - deployed)}"
    )


@pytest.mark.parametrize(
    ("service", "audience"),
    [("rag-mcp-cs-staff", "staff"), ("rag-mcp-cs-student", "student")],
)
@requires_cs_config
def test_mcp_env_values_match_deployment(service: str, audience: str) -> None:
    """config 가 만드는 값이 배포된 값과 같아야 한다(의도한 변경 제외)."""
    golden = _golden()
    if service not in golden:
        pytest.skip(f"{service} 스냅샷 없음")
    deployed = golden[service]["env"]
    generated = build_env("cs", audience)

    diffs = []
    for name in _env_names_sent_by("deploy_mcp.ps1", "envVars"):
        if name in INTENDED_CHANGES or name in SECRETS_COMPARED_BY_LENGTH:
            continue
        old = deployed.get(name)
        new = generated.get(name)
        # 버킷은 MCP 가 안 쓰므로 배포가 "unused" 로 채우던 자리다.
        if old == "unused" and new:
            continue
        if new is not None and old != new:
            diffs.append(f"{name}: 배포={old!r} config={new!r}")
    assert not diffs, "값이 달라졌다\n  " + "\n  ".join(diffs)


@requires_cs_config
@pytest.mark.parametrize(
    ("service", "audience"),
    [("rag-mcp-cs-staff", "staff"), ("rag-mcp-cs-student", "student")],
)
def test_secret_lengths_match_deployment(service: str, audience: str) -> None:
    """키는 평문을 못 남기므로 **길이**로 대조한다.

    키를 바꾸고 재배포를 안 하면 여기서 갈린다 — 그 상태는 커넥터가 조용히
    401 을 받는 것으로만 드러나므로, 배포 전에 잡을 신호가 이것뿐이다.
    반대로 배포를 끝냈는데 여기가 어긋나 있으면 골든이 낡은 것이다.
    """
    golden = _golden()
    if service not in golden:
        pytest.skip(f"{service} 스냅샷 없음")
    deployed = golden[service]["env"]
    generated = build_env("cs", audience)
    for name in SECRETS_COMPARED_BY_LENGTH:
        if name not in deployed:
            continue
        m = re.match(r"<len:(\d+)>", str(deployed[name]))
        assert m, f"{name} 은 길이로만 기록해야 한다 — 골든에 평문이 들어갔다"
        assert int(m.group(1)) == len(generated[name]), (
            f"{name} 길이가 다르다: 배포={m.group(1)}자 config={len(generated[name])}자"
            " — config 를 바꿨으면 재배포하고 골든을 다시 뜰 것"
        )


def test_stale_intent_lists_are_emptied_after_deploy() -> None:
    """배포가 끝났으면 '의도한 차이' 목록도 비어야 한다.

    낡은 목록은 그 키에 대한 검사를 통째로 꺼 버리므로 **실제 회귀를 가려준다.**
    골든에 이미 들어간 이름이 목록에 남아 있으면 그건 정리를 빠뜨린 것이다.
    """
    golden = _golden()
    for service, names in INTENDED_ADDITIONS.items():
        already = sorted(names & set(golden.get(service, {}).get("env", {})))
        assert not already, (
            f"{service}: {already} 는 이미 배포됐다 — INTENDED_ADDITIONS 에서 뺄 것"
        )


def test_golden_has_no_plaintext_secrets() -> None:
    """골든은 커밋된다. 키가 들어가면 이력에 영구히 남는다."""
    raw = GOLDEN.read_text(encoding="utf-8") if GOLDEN.is_file() else ""
    for name in ("MCP_API_KEY",):
        for match in re.findall(rf'"{name}":\s*"([^"]*)"', raw):
            assert match.startswith("<len:"), f"{name} 이 평문으로 저장됐다"


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


def test_dropped_keys_were_never_deployed() -> None:
    """지운 키가 스냅샷에 있었다면 분류가 틀린 것이다 — 그 값은 사라진다."""
    golden = _golden()
    deployed: set[str] = set()
    for spec in golden.values():
        deployed |= set(spec.get("env", {}))
    overlap = sorted(DROPPED_WITH_DOTENV & deployed)
    assert not overlap, f"배포되던 값을 지웠다: {overlap}"


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
