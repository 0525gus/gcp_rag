from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import common_config
from scripts._env import force_utf8_stdout
from shared.config import PER_DEPLOYMENT_ENV

CONFIG_DIR = ROOT / "config"

AUDIENCES = ("staff", "student")

PLACEHOLDER_KEYS = {"CHANGE_ME", "changeme", "change-me", ""}

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
        print(
            f"!! {dept}/{audience} MCP 키가 약하다 — {' / '.join(reasons)}",
            file=sys.stderr,
        )


def _fmt(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, tuple)):
        return ",".join(str(v).strip() for v in value if str(v).strip())
    return str(value)


def _common() -> dict:
    path = CONFIG_DIR / common_config.FILE_NAME
    try:
        return common_config.load_common(CONFIG_DIR)
    except FileNotFoundError:
        raise SystemExit(f"없는 설정 파일: {path}") from None
    except TypeError:
        raise SystemExit(f"최상위가 매핑이 아니다: {path}") from None


def registry_configs() -> dict[str, dict]:
    common = _common()
    project = str(common.get("GCP_PROJECT_ID") or "").strip()
    region = str(common["GCP_REGION"]).strip()
    database = str(common["FIRESTORE_DATABASE"]).strip()
    if not project:
        raise SystemExit("config/common.yaml에 GCP_PROJECT_ID가 없다")
    gcloud = shutil.which("gcloud")
    if not gcloud:
        raise SystemExit("gcloud를 찾을 수 없다")
    result = subprocess.run(
        [gcloud, "auth", "print-access-token", "--quiet"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    token = result.stdout.strip()
    if result.returncode != 0 or not token:
        raise SystemExit("gcloud 로그인이 필요하다")
    from scripts.mcp_registry import RegistryAdmin

    _snap, record, configs = RegistryAdmin(project, region, database, token).read()
    if not record:
        raise SystemExit("Cloud 학과 등록부가 초기화되지 않았다")
    return configs


def list_departments() -> list[str]:
    return sorted(registry_configs())


def _id_list(value: object) -> list[str]:
    if isinstance(value, str):
        values = value.replace("\r", "\n").replace(",", "\n").split("\n")
    elif isinstance(value, (list, tuple)):
        values = value
    else:
        values = []
    return list(dict.fromkeys(str(item).strip() for item in values if str(item).strip()))


def _deployment_metadata_b64(
    dept: str,
    audience: str,
    dept_cfg: dict,
    env: dict[str, str],
    *,
    staff_corpus: str,
    student_corpus: str,
) -> str:
    drive = dept_cfg.get("drive") or {}
    metadata = {
        "schemaVersion": 2,
        "managedBy": "gcp-rag",
        "code": dept,
        "name": str(dept_cfg.get("name") or dept),
        "audience": audience,
        "corpusMode": "split" if student_corpus else "single",
        "yaml": dept_cfg,
        "corpora": {"staff": staff_corpus, "student": student_corpus},
        "buckets": {
            "hwpOriginal": env.get("GCS_HWP_ORIGINAL_BUCKET", ""),
            "source": env.get("GCS_SOURCE_BUCKET", ""),
        },
        "drive": {
            "driveIds": _id_list(drive.get("driveIds")),
            "syncFolderIds": _id_list(drive.get("syncFolderIds")),
            "studentFolderIds": _id_list(drive.get("studentFolderIds")),
        },
        "minInstances": {
            key: int((dept_cfg.get("minInstances") or {}).get(key, 0) or 0)
            for key in AUDIENCES
        },
    }
    raw = json.dumps(metadata, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")


def build_env(dept: str, audience: str) -> dict[str, str]:
    configs = registry_configs()
    if dept not in configs:
        raise SystemExit(f"Cloud 등록부에 없는 학과: {dept}")
    return build_env_from_config(dept, audience, configs[dept])


def build_env_from_config(dept: str, audience: str, dept_cfg: dict) -> dict[str, str]:
    if audience not in AUDIENCES:
        raise SystemExit(f"audience 는 {AUDIENCES} 중 하나여야 한다: {audience}")

    env: dict[str, str] = {}

    for key, val in _common().items():
        env[key] = _fmt(val)

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
        raise SystemExit(f"{dept}: staff 와 student 코퍼스가 같다")

    env["RAG_CORPUS_NAME"] = staff_corpus if audience == "staff" else student_corpus
    env["RAG_CORPUS_NAME_STUDENT"] = student_corpus

    buckets = dept_cfg.get("buckets") or {}
    bucket_map = (
        ("hwpOriginal", "GCS_HWP_ORIGINAL_BUCKET"),
        ("source", "GCS_SOURCE_BUCKET"),
    )
    named = [yk for yk, _ in bucket_map if buckets.get(yk)]
    if named and len(named) != len(bucket_map):
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

    key = str(keys.get(audience) or "").strip()
    if not key:
        raise SystemExit(f"{dept}: keys.{audience} 가 비었다")
    if key in PLACEHOLDER_KEYS:
        raise SystemExit(f"{dept}: keys.{audience} 가 템플릿 값 그대로다")
    if split_enabled and str(keys.get("staff") or "") == student_key:
        raise SystemExit(f"{dept}: staff 와 student 키가 같다")
    env["MCP_API_KEY"] = key
    for other in AUDIENCES:
        other_key = str(keys.get(other) or "").strip()
        if other_key:
            env[f"MCP_API_KEY_{other.upper()}"] = other_key
    _warn_if_weak(dept, audience, key)

    env["MCP_SERVICE_NAME"] = f"rag-mcp-{dept}-{audience}"
    env["MCP_AUDIENCE"] = audience
    env["DEPT_CODE"] = dept
    if dept_cfg.get("name"):
        env["DEPT_NAME"] = _fmt(dept_cfg["name"])
    env["DEPLOYMENT_METADATA_B64"] = _deployment_metadata_b64(
        dept,
        audience,
        dept_cfg,
        env,
        staff_corpus=staff_corpus,
        student_corpus=student_corpus,
    )

    for key in (*PER_DEPLOYMENT_ENV, *_OPTIONAL_DEPT_ENV):
        env.setdefault(key, "")

    return env


_OPTIONAL_DEPT_ENV = ("MCP_API_KEY_STAFF", "MCP_API_KEY_STUDENT", "DEPT_NAME")


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


def departments_map_from_configs(
    configs: dict[str, dict],
) -> dict[str, dict[str, object]]:
    if not configs:
        raise SystemExit("학과 설정이 하나도 없다")

    out: dict[str, dict[str, object]] = {}
    drive_owner: dict[str, str] = {}
    for code in sorted(configs):
        env = build_env_from_config(code, "staff", configs[code])
        entry: dict[str, object] = {}
        enabled = configs[code].get("enableDocaiFallback", False)
        if not isinstance(enabled, bool):
            raise SystemExit(f"{code}: enableDocaiFallback 은 boolean 이어야 한다")
        entry["enableDocaiFallback"] = enabled
        image_ocr = configs[code].get("enableImageOcr", False)
        if not isinstance(image_ocr, bool):
            raise SystemExit(f"{code}: enableImageOcr 은 boolean 이어야 한다")
        entry["enableImageOcr"] = image_ocr
        for option in ("enableDocaiFallback", "enableImageOcr"):
            for audience in ("Staff", "Student"):
                key = option + audience
                if key in configs[code]:
                    if not isinstance(configs[code][key], bool):
                        raise SystemExit(f"{code}: {key} 은 boolean 이어야 한다")
                    entry[key] = configs[code][key]
        for field, env_key, is_list in _MAP_FIELDS:
            raw = env.get(env_key, "")
            if not raw:
                continue
            entry[field] = [v for v in raw.split(",") if v] if is_list else raw

        if not entry.get("driveIds"):
            raise SystemExit(f"{code}: drive.driveIds 가 없다 — sync 가 이 학과를 못 찾는다")
        if not entry.get("syncFolderIds"):
            raise SystemExit(f"{code}: drive.syncFolderIds 가 없다 — 남의 폴더를 훑게 된다")

        for drive_id in entry["driveIds"]:  # type: ignore[union-attr]
            if drive_id in drive_owner:
                raise SystemExit(
                    f"driveId 중복: {code} 와 {drive_owner[drive_id]} 가 {drive_id} 를 함께 쓴다"
                )
            drive_owner[drive_id] = code

        leaked = [k for k in _SECRET_ENV_KEYS if k in entry]
        if leaked:
            raise SystemExit(f"학과 맵에 시크릿이 섞였다: {leaked}")
        out[code] = entry
    return out


def build_departments_map() -> dict[str, dict[str, object]]:
    return departments_map_from_configs(registry_configs())


def _departments_json(mapping: dict[str, dict[str, object]]) -> str:
    """한 줄 JSON.

    공백을 넣지 않는다 — 이 값은 `--set-env-vars` 인자에 실려 명령줄로 나간다.
    공백이 있으면 셸이 인자를 어디서 끊을지에 값의 안전이 달리게 된다.
    비ASCII 도 이스케이프한다(ensure_ascii 기본값) — 한국어 Windows 콘솔은
    cp949 라 학과명 같은 값이 파이프를 타면서 깨질 수 있다.
    """
    return json.dumps(mapping, separators=(",", ":"), sort_keys=True)


def departments_json() -> str:
    return _departments_json(build_departments_map())


def departments_json_from_configs(configs: dict[str, dict]) -> str:
    return _departments_json(departments_map_from_configs(configs))


def load_config_env(dept: str | None = None, audience: str = "staff") -> str:
    configs = registry_configs()
    codes = sorted(configs)
    if not codes:
        raise SystemExit("Cloud 등록부에 학과 설정이 없다")
    code = dept or codes[0]
    if code not in codes:
        raise SystemExit(f"없는 학과: {code} (있는 것: {', '.join(codes)})")
    for key, val in build_env_from_config(code, audience, configs[code]).items():
        if val:
            os.environ.setdefault(key, val)
    os.environ.setdefault("DEPARTMENTS_JSON", departments_json_from_configs(configs))
    return code


def configured_audiences(dept: str) -> tuple[str, ...]:
    configs = registry_configs()
    if dept not in configs:
        raise SystemExit(f"Cloud 등록부에 없는 학과: {dept}")
    env = build_env_from_config(dept, "staff", configs[dept])
    if env.get("RAG_CORPUS_NAME_STUDENT") and env.get("STUDENT_FOLDER_IDS"):
        build_env_from_config(dept, "student", configs[dept])
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

    if not args.dept:
        ap.error("--dept · --list · --departments-json 중 하나가 필요하다")

    for key, val in build_env(args.dept, args.audience).items():
        print(f"{key}={val}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
