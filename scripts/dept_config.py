"""학과 배포 설정(config/*.yaml) → 환경변수 줄.

PowerShell 배포 스크립트가 부른다. PS 에는 YAML 파서가 없고 이 저장소는 이미
PyYAML 을 의존(requirements.txt)하므로, 파싱은 파이썬이 하고 결과만 `KEY=VALUE`
줄로 넘긴다.

키 이름은 **서비스가 읽는 환경변수명 그대로** 낸다. 중간 번역 계층을 두면
"yaml 엔 있는데 서비스엔 안 들어갔다" 가 조용히 생긴다.

사용:
    python scripts/dept_config.py --list
    python scripts/dept_config.py --dept cs --audience staff
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts._env import force_utf8_stdout

CONFIG_DIR = ROOT / "config"
DEPT_DIR = CONFIG_DIR / "departments"

AUDIENCES = ("staff", "student")

# 템플릿을 복사만 하고 안 채운 경우. 그대로 배포되면 키가 공개된 것과 같다.
PLACEHOLDER_KEYS = {"CHANGE_ME", "changeme", "change-me", ""}

# 사람이 지은 키를 걸러내기 위한 단어. ALLOW_UNAUTH=true 라 엔드포인트가 공개이므로
# 추측 가능한 키는 유출 없이도 뚫린다. 막지는 않고 경고만 낸다 — 판단은 사람이 한다.
_WEAK_HINTS = ("test", "demo", "sample", "staff", "student", "admin", "password", "key")
_MIN_KEY_LEN = 24


def _warn_if_weak(dept: str, audience: str, key: str) -> None:
    reasons = []
    if len(key) < _MIN_KEY_LEN:
        reasons.append(f"{len(key)}자 (권장 {_MIN_KEY_LEN}자 이상)")
    lowered = key.lower()
    hit = [w for w in _WEAK_HINTS if w in lowered]
    if hit:
        reasons.append("사전 단어 포함: " + ", ".join(hit))
    if reasons:
        # stdout 은 KEY=VALUE 전용이다. 경고는 stderr 로 내보낸다.
        print(
            f"!! {dept}/{audience} MCP 키가 약하다 — {' / '.join(reasons)}",
            file=sys.stderr,
        )


def _fmt(value: object) -> str:
    """YAML 값 → 환경변수 문자열.

    bool 을 파이썬 표기(True)로 내보내면 Get-EnvOr 비교(`-ne "true"`)가 조용히
    뒤집혀 ALLOW_UNAUTH 가 반대로 동작한다. 리스트는 이 저장소 관례대로 쉼표.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, tuple)):
        return ",".join(str(v).strip() for v in value if str(v).strip())
    return str(value)


def list_departments() -> list[str]:
    if not DEPT_DIR.is_dir():
        return []
    return sorted(p.stem for p in DEPT_DIR.glob("*.yaml"))


def _load_yaml(path: Path) -> dict:
    if not path.is_file():
        raise SystemExit(f"없는 설정 파일: {path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise SystemExit(f"최상위가 매핑이 아니다: {path}")
    return data


def build_env(dept: str, audience: str) -> dict[str, str]:
    if audience not in AUDIENCES:
        raise SystemExit(f"audience 는 {AUDIENCES} 중 하나여야 한다: {audience}")

    env: dict[str, str] = {}

    # 1) 공통. 키를 그대로 환경변수로 쓴다.
    for key, val in _load_yaml(CONFIG_DIR / "common.yaml").items():
        if val is not None:
            env[key] = _fmt(val)

    # 2) 학과. 같은 키는 학과가 이긴다.
    dept_cfg = _load_yaml(DEPT_DIR / f"{dept}.yaml")

    corpora = dept_cfg.get("corpora") or {}
    staff_corpus = str(corpora.get("staff") or "").strip()
    student_corpus = str(corpora.get("student") or "").strip()
    if not staff_corpus or not student_corpus:
        raise SystemExit(f"{dept}: corpora.staff / corpora.student 가 둘 다 필요하다")
    if staff_corpus == student_corpus:
        # 같으면 학생 서비스가 교직원 전량을 검색하게 된다. 조용히 통과시키면 안 된다.
        raise SystemExit(f"{dept}: staff 와 student 코퍼스가 같다")

    env["RAG_CORPUS_NAME"] = staff_corpus if audience == "staff" else student_corpus
    # 배포 검사(Require-McpDeployEnv)가 이 값으로 대상을 대조한다. 대상이
    # 교직원이어도 넘겨야 "학생 코퍼스가 교직원 서비스에 실렸다" 를 잡아낸다.
    env["RAG_CORPUS_NAME_STUDENT"] = student_corpus

    # 학과별 버킷. 없으면 common.yaml 값(=공용)을 그대로 쓴다.
    # 객체 키가 Drive fileId 라 공용이어도 충돌은 없다 — 나누는 이유는 격리다:
    # 학과에 GCS 권한을 줄 때 남의 원본까지 열리는 것을 막고, 학과 이탈 시
    # 버킷 삭제 한 번으로 끝난다. 다만 **이미 적재된 학과를 옮기는 것은 비싸다**
    # (코퍼스가 옛 gs:// URI 를 참조하므로 전량 삭제 후 재import 가 따라온다).
    # 그래서 기존 학과는 쓰던 버킷을 그대로 두고, 새 학과부터 각자 갖는다.
    buckets = dept_cfg.get("buckets") or {}
    bucket_map = (
        ("hwpOriginal", "GCS_HWP_ORIGINAL_BUCKET"),
        ("source", "GCS_SOURCE_BUCKET"),
    )
    named = [yk for yk, _ in bucket_map if buckets.get(yk)]
    if named and len(named) != len(bucket_map):
        # 한쪽만 적으면 원본은 학과 버킷, 산출물은 공용으로 갈라진다.
        missing = [yk for yk, _ in bucket_map if not buckets.get(yk)]
        raise SystemExit(f"{dept}: buckets 는 짝이어야 한다 — 빠진 것: {missing}")
    for yaml_key, env_key in bucket_map:
        if buckets.get(yaml_key):
            env[env_key] = _fmt(buckets[yaml_key])

    drive = dept_cfg.get("drive") or {}
    for yaml_key, env_key in (
        ("driveIds", "DRIVE_IDS"),
        ("syncFolderIds", "SYNC_FOLDER_IDS"),
        ("studentFolderIds", "STUDENT_FOLDER_IDS"),
    ):
        if drive.get(yaml_key):
            env[env_key] = _fmt(drive[yaml_key])

    min_instances = dept_cfg.get("minInstances") or {}
    env["MCP_MIN_INSTANCES"] = _fmt(min_instances.get(audience, 0))

    # 3) MCP 키. 학과 yaml 이 커밋되지 않는 유일한 이유다.
    keys = dept_cfg.get("keys") or {}
    key = str(keys.get(audience) or "").strip()
    if not key:
        raise SystemExit(f"{dept}: keys.{audience} 가 비었다")
    if key in PLACEHOLDER_KEYS:
        raise SystemExit(f"{dept}: keys.{audience} 가 템플릿 값 그대로다")
    if str(keys.get("staff") or "") == str(keys.get("student") or ""):
        # 같으면 학생 키로 교직원 코퍼스가 열린다.
        raise SystemExit(f"{dept}: staff 와 student 키가 같다")
    env["MCP_API_KEY"] = key
    # Require-McpDeployEnv 의 '학생이 교직원 키를 재사용했나' 검사용.
    # 대상이 교직원이어도 같이 내보낸다 — 안 그러면 배포 스크립트가 이 값을 얻으려고
    # 같은 학과를 한 번 더 조회해야 하고, 약한 키 경고도 두 번씩 찍힌다.
    env["MCP_API_KEY_STAFF"] = str(keys.get("staff") or "").strip()
    _warn_if_weak(dept, audience, key)

    # 4) 규칙으로 만드는 값 — 파일에 저장하지 않는다(두 곳에 적으면 갈라진다).
    env["MCP_SERVICE_NAME"] = f"rag-mcp-{dept}-{audience}"
    env["MCP_AUDIENCE"] = audience
    env["DEPT_CODE"] = dept
    if dept_cfg.get("name"):
        env["DEPT_NAME"] = _fmt(dept_cfg["name"])

    return env


def main() -> int:
    # 한국어 Windows 콘솔은 cp949 라 학과명(DEPT_NAME)이 깨진 채 넘어간다.
    force_utf8_stdout()
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true", help="학과 코드 목록")
    ap.add_argument("--dept")
    ap.add_argument("--audience", default="staff", choices=AUDIENCES)
    args = ap.parse_args()

    if args.list:
        for code in list_departments():
            print(code)
        return 0

    if not args.dept:
        ap.error("--dept 또는 --list 가 필요하다")

    for key, val in build_env(args.dept, args.audience).items():
        print(f"{key}={val}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
