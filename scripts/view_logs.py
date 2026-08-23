"""Cloud Logging / Log Explorer 조회 헬퍼.

Cloud Run(rag-sync / rag-parser / rag-mcp) 로그를 터미널에서 보거나
콘솔 Log Explorer로 바로 연다. gcloud 인증이 필요하다.

사용:
  # 최근 sync 로그 50줄
  python scripts/view_logs.py sync

  # ERROR 이상만, 최근 6시간
  python scripts/view_logs.py mcp --severity=ERROR --freshness=6h

  # 텍스트 검색 + 콘솔 Log Explorer 열기
  python scripts/view_logs.py all -q "Reconciliation" --open

  # 필터만 출력 (복사용)
  python scripts/view_logs.py parser --filter-only
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import urllib.parse
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts._env import force_utf8_stdout  # noqa: E402
from scripts.dept_config import list_departments, load_config_env  # noqa: E402

SEVERITY_ORDER = ("DEFAULT", "DEBUG", "INFO", "NOTICE", "WARNING", "ERROR", "CRITICAL", "ALERT", "EMERGENCY")


def _mcp_service_names() -> list[str]:
    """전 학과 MCP 서비스 이름. 이름은 규칙으로 만든다(저장하지 않는다).

    학과가 늘면 로그 대상도 같이 는다 — 목록을 손으로 적어 두면 새 학과 로그가
    조용히 빠진다. config/departments 가 곧 목록이다.
    """
    return [
        f"rag-mcp-{code}-{audience}"
        for code in list_departments()
        for audience in ("staff", "student")
    ]


def _run_service_names(target: str) -> list[str] | None:
    if target == "sync":
        return ["rag-sync"]
    if target == "parser":
        return ["rag-parser"]
    if target == "mcp":
        return _mcp_service_names()
    if target == "all":
        return ["rag-sync", "rag-parser", *_mcp_service_names()]
    return None


def _project() -> str:
    pid = os.environ.get("GCP_PROJECT_ID") or os.environ.get("GOOGLE_CLOUD_PROJECT")
    if not pid:
        raise SystemExit("GCP_PROJECT_ID 가 필요합니다 (config/common.yaml 또는 환경변수).")
    return pid


def build_filter(
    target: str,
    *,
    query: str | None,
    severity: str | None,
) -> str:
    parts: list[str] = []

    if target == "workflow":
        parts.append('resource.type="workflows.googleapis.com/Workflow"')
        parts.append('resource.labels.workflow_id="rag-daily-sync"')
    else:
        parts.append('resource.type="cloud_run_revision"')
        names = _run_service_names(target)
        if names:
            if len(names) == 1:
                parts.append(f'resource.labels.service_name="{names[0]}"')
            else:
                joined = " OR ".join(f'resource.labels.service_name="{n}"' for n in names)
                parts.append(f"({joined})")

    if severity:
        sev = severity.upper()
        if sev not in SEVERITY_ORDER:
            raise SystemExit(f"알 수 없는 severity: {severity}")
        parts.append(f"severity>={sev}")

    if query:
        # textPayload / jsonPayload.message 둘 다
        q = query.replace('"', '\\"')
        parts.append(
            f'(textPayload:"{q}" OR jsonPayload.message:"{q}" OR protoPayload.status.message:"{q}")'
        )

    return "\n".join(parts)


def explorer_url(project: str, log_filter: str) -> str:
    # 콘솔 Log Explorer 딥링크 (query 파라미터)
    encoded = urllib.parse.quote(log_filter, safe="")
    return (
        f"https://console.cloud.google.com/logs/query"
        f";query={encoded}"
        f"?project={urllib.parse.quote(project)}"
    )


def run_gcloud_read(
    project: str,
    log_filter: str,
    *,
    limit: int,
    freshness: str,
    format_: str,
) -> int:
    if not shutil.which("gcloud"):
        raise SystemExit("gcloud CLI 가 PATH 에 없습니다.")
    cmd = [
        "gcloud",
        "logging",
        "read",
        log_filter,
        f"--project={project}",
        f"--limit={limit}",
        f"--freshness={freshness}",
        f"--format={format_}",
    ]
    print(f"# {' '.join(cmd)}\n", file=sys.stderr)
    # Windows의 gcloud는 .cmd 배치 스크립트라 shell=True 없이는 CreateProcess가 못 찾는다.
    proc = subprocess.run(cmd, check=False, shell=(os.name == "nt"))
    return proc.returncode


def main() -> int:
    force_utf8_stdout()

    parser = argparse.ArgumentParser(
        description="Cloud Run / Workflows 로그를 조회하거나 Log Explorer를 연다."
    )
    parser.add_argument(
        "target",
        choices=("sync", "parser", "mcp", "all", "workflow"),
        help="sync | parser | mcp | all | workflow",
    )
    parser.add_argument("-q", "--query", help="로그 본문 검색어")
    parser.add_argument(
        "--severity",
        default=None,
        help="최소 severity (예: WARNING, ERROR)",
    )
    parser.add_argument("--limit", type=int, default=50, help="조회 건수 (기본 50)")
    parser.add_argument(
        "--freshness",
        default="1h",
        help="조회 기간 (예: 30m, 1h, 6h, 1d)",
    )
    parser.add_argument(
        "--format",
        default="value(timestamp,severity,textPayload,jsonPayload.message)",
        help="gcloud --format",
    )
    parser.add_argument(
        "--open",
        action="store_true",
        help="콘솔 Log Explorer 브라우저로 열기",
    )
    parser.add_argument(
        "--filter-only",
        action="store_true",
        help="필터/URL만 출력하고 종료",
    )
    parser.add_argument(
        "--url-only",
        action="store_true",
        help="Log Explorer URL만 출력",
    )
    args = parser.parse_args()

    # parse_args 뒤에 부른다 — 앞에 두면 `--help` 조차 학과 yaml 을 요구한다.
    load_config_env()

    project = _project()
    log_filter = build_filter(args.target, query=args.query, severity=args.severity)
    url = explorer_url(project, log_filter)

    if args.filter_only or args.url_only:
        if args.url_only:
            print(url)
        else:
            print(f"# project={project}")
            print(log_filter)
            print()
            print(url)
        return 0

    if args.open:
        print(url, file=sys.stderr)
        webbrowser.open(url)

    return run_gcloud_read(
        project,
        log_filter,
        limit=args.limit,
        freshness=args.freshness,
        format_=args.format,
    )


if __name__ == "__main__":
    raise SystemExit(main())
