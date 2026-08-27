"""학과 배포 설정(config/*.yaml) → 환경변수 줄.

PowerShell 배포 스크립트가 부른다. PS 에는 YAML 파서가 없고 이 저장소는 이미
PyYAML 을 의존(requirements.txt)하므로, 파싱은 파이썬이 하고 결과만 `KEY=VALUE`
줄로 넘긴다.

키 이름은 **서비스가 읽는 환경변수명 그대로** 낸다. 중간 번역 계층을 두면
"yaml 엔 있는데 서비스엔 안 들어갔다" 가 조용히 생긴다.

사용:
    python scripts/dept_config.py --list
    python scripts/dept_config.py --dept cs --audience staff
    python scripts/dept_config.py --departments-json   # rag-sync 학과 맵(한 줄)
"""

from __future__ import annotations

import argparse
import json
import os
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
    drive = dept_cfg.get("drive") or {}
    keys = dept_cfg.get("keys") or {}
    student_folders = _fmt(drive.get("studentFolderIds") or "")
    student_key = str(keys.get("student") or "").strip()
    student_parts = (bool(student_corpus), bool(student_folders), bool(student_key))
    if any(student_parts) and not all(student_parts):
        raise SystemExit(
            f"{dept}: 학생 분리는 corpora.student / drive.studentFolderIds / keys.student 가 모두 필요하다"
        )
    split_enabled = all(student_parts)
    if not staff_corpus:
        raise SystemExit(f"{dept}: corpora.staff 가 필요하다")
    if audience == "student" and not split_enabled:
        raise SystemExit(f"{dept}: 학생 코퍼스 분리를 사용하지 않는다")
    if split_enabled and staff_corpus == student_corpus:
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
    key = str(keys.get(audience) or "").strip()
    if not key:
        raise SystemExit(f"{dept}: keys.{audience} 가 비었다")
    if key in PLACEHOLDER_KEYS:
        raise SystemExit(f"{dept}: keys.{audience} 가 템플릿 값 그대로다")
    if split_enabled and str(keys.get("staff") or "") == student_key:
        # 같으면 학생 키로 교직원 코퍼스가 열린다.
        raise SystemExit(f"{dept}: staff 와 student 키가 같다")
    env["MCP_API_KEY"] = key
    # 대상이 아닌 쪽 키도 같이 내보낸다. 배포 검사가 **짝을 대조**하기 때문이다:
    #   Require-McpDeployEnv   학생이 교직원 키를 재사용했나 (STAFF 필요)
    #   Require-FullDeployEnv  분리가 켜졌는데 학생 키가 없나 (STUDENT 필요)
    # 한쪽만 내보내면 그 검사가 **멀쩡한 설정을 거부**한다. 실제로 그랬다 —
    # STUDENT 를 빠뜨려 deploy.ps1 이 첫 검증에서 죽었다(preflight 가 잡음).
    # 배포 스크립트가 이 값을 얻으려고 같은 학과를 한 번 더 조회하지 않아도 되고,
    # 약한 키 경고가 두 번씩 찍히지도 않는다.
    for other in AUDIENCES:
        other_key = str(keys.get(other) or "").strip()
        if other_key:
            env[f"MCP_API_KEY_{other.upper()}"] = other_key
    _warn_if_weak(dept, audience, key)

    # 4) 규칙으로 만드는 값 — 파일에 저장하지 않는다(두 곳에 적으면 갈라진다).
    env["MCP_SERVICE_NAME"] = f"rag-mcp-{dept}-{audience}"
    env["MCP_AUDIENCE"] = audience
    env["DEPT_CODE"] = dept
    if dept_cfg.get("name"):
        env["DEPT_NAME"] = _fmt(dept_cfg["name"])

    return env


# --- rag-sync 학과 맵 ------------------------------------------------------

# shared.config._departments_from_json 이 읽는 키 ← build_env 가 내는 환경변수명.
# 왼쪽 이름을 바꾸면 파싱은 그대로 성공하고 그 필드만 조용히 비어버린다
# (모르는 키는 무시된다). 그래서 이 표가 곧 계약이고, 아래 테스트가 대조한다.
_MAP_FIELDS: tuple[tuple[str, str, bool], ...] = (
    ("driveIds", "DRIVE_IDS", True),
    ("staffCorpus", "RAG_CORPUS_NAME", False),
    ("studentCorpus", "RAG_CORPUS_NAME_STUDENT", False),
    ("hwpBucket", "GCS_HWP_ORIGINAL_BUCKET", False),
    ("sourceBucket", "GCS_SOURCE_BUCKET", False),
    ("syncFolderIds", "SYNC_FOLDER_IDS", True),
    ("studentFolderIds", "STUDENT_FOLDER_IDS", True),
)

# 맵은 Cloud Run env 로 그대로 나간다 — `gcloud run services describe` 를 볼 수
# 있는 사람 전원에게 보인다. sync 는 MCP 키를 쓰지 않으므로 넣을 이유가 없다.
_SECRET_ENV_KEYS = ("MCP_API_KEY", "MCP_API_KEY_STAFF", "MCP_API_KEY_STUDENT")


def build_departments_map() -> dict[str, dict[str, object]]:
    """전 학과 → sync 라우팅 맵. **시크릿은 담지 않는다.**

    값은 build_env 를 거쳐 뽑는다 — common.yaml + 학과 yaml 병합 규칙과 거부
    조건(코퍼스 동일·버킷 반쪽 등)을 한 벌만 두기 위해서다. 여기서 따로
    yaml 을 읽으면 배포 env 와 sync 맵이 서로 다른 규칙으로 갈라진다.
    """
    codes = list_departments()
    if not codes:
        raise SystemExit("config/departments 에 학과 yaml 이 없다")

    out: dict[str, dict[str, object]] = {}
    drive_owner: dict[str, str] = {}
    for code in codes:
        # staff 로 한 번만 부른다 — build_env 가 학생 코퍼스까지 같이 내보낸다.
        env = build_env(code, "staff")
        entry: dict[str, object] = {}
        for field, env_key, is_list in _MAP_FIELDS:
            raw = env.get(env_key, "")
            if not raw:
                continue
            entry[field] = [v for v in raw.split(",") if v] if is_list else raw

        # driveIds 가 없으면 그 학과 문서는 **영영 처리되지 않는다**: 맵이 비지
        # 않은 이상 sync 는 맵에 없는 드라이브를 UnknownDriveError 로 건너뛴다.
        if not entry.get("driveIds"):
            raise SystemExit(f"{code}: drive.driveIds 가 없다 — sync 가 이 학과를 못 찾는다")
        # syncFolderIds 가 없으면 for_drive 가 **공용 기본값을 그대로 둔다** —
        # 그 학과가 남의 폴더를 훑게 된다. 배포도 예전부터 빈 값을 거부해 왔다.
        if not entry.get("syncFolderIds"):
            raise SystemExit(f"{code}: drive.syncFolderIds 가 없다 — 남의 폴더를 훑게 된다")

        for drive_id in entry["driveIds"]:  # type: ignore[union-attr]
            if drive_id in drive_owner:
                # department_for_drive 는 첫 일치를 준다. 겹치면 뒤 학과 문서가
                # 앞 학과 코퍼스로 들어가고, 되돌리려면 코퍼스에서 골라 지워야 한다.
                raise SystemExit(
                    f"driveId 중복: {code} 와 {drive_owner[drive_id]} 가 {drive_id} 를 함께 쓴다"
                )
            drive_owner[drive_id] = code

        leaked = [k for k in _SECRET_ENV_KEYS if k in entry]
        if leaked:  # 표가 잘못 늘어난 경우. 배포 전에 죽는 편이 낫다.
            raise SystemExit(f"학과 맵에 시크릿이 섞였다: {leaked}")
        out[code] = entry
    return out


def departments_json() -> str:
    """한 줄 JSON.

    공백을 넣지 않는다 — 이 값은 `--set-env-vars` 인자에 실려 명령줄로 나간다.
    공백이 있으면 셸이 인자를 어디서 끊을지에 값의 안전이 달리게 된다.
    비ASCII 도 이스케이프한다(ensure_ascii 기본값) — 한국어 Windows 콘솔은
    cp949 라 학과명 같은 값이 파이프를 타면서 깨질 수 있다.
    """
    return json.dumps(build_departments_map(), separators=(",", ":"), sort_keys=True)


# --- 로컬 스크립트용 로더 --------------------------------------------------

def load_config_env(dept: str | None = None, audience: str = "staff") -> str:
    """config 값을 `os.environ` 에 채운다. 채운 학과 코드를 돌려준다.

    `.env` 로더가 있던 자리다(scripts/_env). setdefault 인 이유도
    같다 — 명령줄·셸에서 준 값이 파일보다 우선해야 한다. 학과를 순회하는
    배포 스크립트(PowerShell `Set-DeptConfig`)는 반대로 **매번 비우고** 채운다:
    거기서는 앞 학과 값이 남는 쪽이 사고이기 때문이다.

    DEPARTMENTS_JSON 도 함께 넣는다. 그래야 로컬 도구가 `Settings.for_drive()`
    로 학과를 고를 수 있고, 서비스와 같은 라우팅 코드를 쓰게 된다.
    """
    codes = list_departments()
    if not codes:
        raise SystemExit("config/departments 에 학과 yaml 이 없다")
    code = dept or codes[0]
    if code not in codes:
        raise SystemExit(f"없는 학과: {code} (있는 것: {', '.join(codes)})")
    for key, val in build_env(code, audience).items():
        os.environ.setdefault(key, val)
    os.environ.setdefault("DEPARTMENTS_JSON", departments_json())
    return code


def configured_audiences(dept: str) -> tuple[str, ...]:
    """설정 검증을 거쳐 실제 배포할 MCP 범위를 반환한다."""
    env = build_env(dept, "staff")
    if env.get("RAG_CORPUS_NAME_STUDENT") and env.get("STUDENT_FOLDER_IDS"):
        build_env(dept, "student")
        return AUDIENCES
    return ("staff",)


def main() -> int:
    # 한국어 Windows 콘솔은 cp949 라 학과명(DEPT_NAME)이 깨진 채 넘어간다.
    force_utf8_stdout()
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true", help="학과 코드 목록")
    ap.add_argument(
        "--departments-json",
        action="store_true",
        help="rag-sync 용 DEPARTMENTS_JSON 값(한 줄)",
    )
    ap.add_argument("--audiences", action="store_true", help="학과에 설정된 MCP 범위")
    ap.add_argument("--dept")
    ap.add_argument("--audience", default="staff", choices=AUDIENCES)
    args = ap.parse_args()

    if args.list:
        for code in list_departments():
            print(code)
        return 0

    if args.departments_json:
        print(departments_json())
        return 0

    if args.audiences:
        if not args.dept:
            ap.error("--audiences 에는 --dept 가 필요하다")
        for audience in configured_audiences(args.dept):
            print(audience)
        return 0

    if not args.dept:
        ap.error("--dept · --list · --departments-json 중 하나가 필요하다")

    for key, val in build_env(args.dept, args.audience).items():
        print(f"{key}={val}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
