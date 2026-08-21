"""배포 env 등가성. `.env` → `config/` 이관이 값을 흘리지 않았는지 대조한다.

`--set-env-vars` 는 Cloud Run env 를 **통째로 치환한다.** 넘기지 않은 값은
사라지므로, 이관 중 키 하나가 빠지면 그 순간 서비스가 코드 기본값으로 조용히
내려앉는다(예: FIRESTORE_DATABASE 가 빠지면 (default) Datastore 를 보게 되어
검색 결과의 파일명·경로가 null 이 된다).

그래서 기준을 **지금 돌고 있는 서비스**로 잡는다. `tests/_golden/deployed_env.json`
은 이관 전 Cloud Run 스펙 스냅샷이고(키 값은 길이만 기록), 여기서는

  1. 배포 스크립트가 보내는 env **이름 집합**이 그때와 같은가
  2. config 가 만드는 **값**이 그때와 같은가 (의도한 변경 제외)

를 본다. 골든을 다시 뜨려면 gcloud 로 describe 해서 갱신할 것 —
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

# 이관하면서 **일부러** 바꾼 것. 여기 없는 차이는 전부 실수로 본다.
INTENDED_CHANGES = {
    # 추측 가능한 값(TEST-STAFF)에서 무작위 43자로 교체. 아직 미배포라 골든과 다르다.
    "MCP_API_KEY",
}


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
def test_mcp_env_values_match_deployment(service: str, audience: str) -> None:
    """config 가 만드는 값이 배포된 값과 같아야 한다(의도한 변경 제외)."""
    golden = _golden()
    if service not in golden:
        pytest.skip(f"{service} 스냅샷 없음")
    deployed = golden[service]["env"]
    generated = build_env("cs", audience)

    diffs = []
    for name in _env_names_sent_by("deploy_mcp.ps1", "envVars"):
        if name in INTENDED_CHANGES:
            continue
        old = deployed.get(name)
        new = generated.get(name)
        # 버킷은 MCP 가 안 쓰므로 배포가 "unused" 로 채우던 자리다.
        if old == "unused" and new:
            continue
        if new is not None and old != new:
            diffs.append(f"{name}: 배포={old!r} config={new!r}")
    assert not diffs, "값이 달라졌다\n  " + "\n  ".join(diffs)


def test_intended_changes_are_actually_different() -> None:
    """의도한 변경 목록이 낡으면 실제 회귀를 가려준다.

    이미 반영돼 값이 같아졌다면 목록에서 빼야 한다.
    """
    golden = _golden()
    deployed = golden.get("rag-mcp-cs-staff", {}).get("env", {})
    if "MCP_API_KEY" not in deployed:
        pytest.skip("스냅샷에 키 없음")
    # 골든에는 길이만 저장돼 있다(<len:N>).
    m = re.match(r"<len:(\d+)>", str(deployed["MCP_API_KEY"]))
    assert m, "키는 길이로만 기록해야 한다 — 골든에 평문이 들어갔다"
    assert int(m.group(1)) != len(build_env("cs", "staff")["MCP_API_KEY"]), (
        "키가 이미 교체·배포됐다면 INTENDED_CHANGES 에서 MCP_API_KEY 를 뺄 것"
    )


def test_golden_has_no_plaintext_secrets() -> None:
    """골든은 커밋된다. 키가 들어가면 이력에 영구히 남는다."""
    raw = GOLDEN.read_text(encoding="utf-8") if GOLDEN.is_file() else ""
    for name in ("MCP_API_KEY",):
        for match in re.findall(rf'"{name}":\s*"([^"]*)"', raw):
            assert match.startswith("<len:"), f"{name} 이 평문으로 저장됐다"
