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

SERVICES: dict[str, str | None] = {
    "sync": "rag-sync",
    "parser": "rag-parser",
    "mcp": "rag-mcp",
    "all": None,  # 세 서비스 전부
    "workflow": None,  # workflows 전용 필터
}

SEVERITY_ORDER = ("DEFAULT", "DEBUG", "INFO", "NOTICE", "WARNING", "ERROR", "CRITICAL", "ALERT", "EMERGENCY")


def _load_dotenv() -> None:
    env_path = ROOT / ".env"
    if not env_path.is_file():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip().strip('"').strip("'")
        os.environ.setdefault(key, val)


def _project() -> str:
    pid = os.environ.get("GCP_PROJECT_ID") or os.environ.get("GOOGLE_CLOUD_PROJECT")
    if not pid:
        raise SystemExit("GCP_PROJECT_ID 가 필요합니다 (.env 또는 환경변수).")
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
        svc = SERVICES[target]
        if svc:
            parts.append(f'resource.labels.service_name="{svc}"')
        else:
            names = " OR ".join(
                f'resource.labels.service_name="{n}"'
                for n in ("rag-sync", "rag-parser", "rag-mcp")
            )
            parts.append(f"({names})")

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
    proc = subprocess.run(cmd, check=False)
    return proc.returncode


def main() -> int:
    _load_dotenv()

    parser = argparse.ArgumentParser(
        description="Cloud Run / Workflows 로그를 조회하거나 Log Explorer를 연다."
    )
    parser.add_argument(
        "target",
        choices=list(SERVICES),
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
