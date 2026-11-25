from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts._env import force_utf8_stdout  # noqa: E402
from scripts.dept_config import list_departments, load_config_env  # noqa: E402

SEVERITY_ORDER = ("DEFAULT", "DEBUG", "INFO", "NOTICE", "WARNING", "ERROR", "CRITICAL", "ALERT", "EMERGENCY")
ENTRIES_URL = "https://logging.googleapis.com/v2/entries:list"
_FRESHNESS_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def _mcp_service_names() -> list[str]:
    return [
        "rag-mcp",
        *(
            f"rag-mcp-{code}-{audience}"
            for code in list_departments()
            for audience in ("staff", "student")
        ),
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
        q = query.replace("\\", "\\\\").replace('"', '\\"')
        parts.append(
            f'(textPayload:"{q}" OR jsonPayload.message:"{q}" OR protoPayload.status.message:"{q}")'
        )

    return "\n".join(parts)


def explorer_url(project: str, log_filter: str) -> str:
    encoded = urllib.parse.quote(log_filter, safe="")
    return (
        f"https://console.cloud.google.com/logs/query"
        f";query={encoded}"
        f"?project={urllib.parse.quote(project)}"
    )


def parse_freshness(text: str) -> timedelta:
    match = re.fullmatch(r"(\d+)([smhdw])", text.strip().lower())
    if not match:
        raise SystemExit(f"조회 기간 형식이 잘못됐습니다: {text} (예: 30m, 1h, 6h, 1d)")
    return timedelta(seconds=int(match.group(1)) * _FRESHNESS_UNITS[match.group(2)])


def _access_token() -> str:
    gcloud = (shutil.which("gcloud.cmd") if os.name == "nt" else None) or shutil.which("gcloud")
    if not gcloud:
        raise SystemExit("gcloud CLI 가 PATH 에 없습니다.")
    proc = subprocess.run(
        [gcloud, "auth", "print-access-token"], capture_output=True, text=True, check=False
    )
    token = proc.stdout.strip()
    if proc.returncode != 0 or not token:
        raise SystemExit("gcloud 로그인이 필요합니다: gcloud auth login")
    return token


def read_entries(
    project: str,
    log_filter: str,
    *,
    limit: int,
    since: datetime,
    token: str,
    urlopen: Any = urllib.request.urlopen,
) -> list[dict[str, Any]]:
    body: dict[str, Any] = {
        "resourceNames": [f"projects/{project}"],
        "filter": f'{log_filter}\ntimestamp>="{since.astimezone(UTC):%Y-%m-%dT%H:%M:%SZ}"',
        "orderBy": "timestamp desc",
        "pageSize": min(limit, 1000),
    }
    entries: list[dict[str, Any]] = []
    while len(entries) < limit:
        request = urllib.request.Request(
            ENTRIES_URL,
            data=json.dumps(body).encode("utf-8"),
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request, timeout=60) as response:
            page = json.load(response)
        entries.extend(page.get("entries") or [])
        next_token = page.get("nextPageToken")
        if not next_token:
            break
        body["pageToken"] = next_token
    return entries[:limit]


def format_entry(entry: dict[str, Any]) -> str:
    labels = (entry.get("resource") or {}).get("labels") or {}
    proto = entry.get("protoPayload") or {}
    http = entry.get("httpRequest") or {}
    request_line = " ".join(
        str(value)
        for value in (http.get("requestMethod"), http.get("status"), http.get("requestUrl"))
        if value
    )
    message = (
        entry.get("textPayload")
        or (entry.get("jsonPayload") or {}).get("message")
        or (proto.get("status") or {}).get("message")
        or request_line
        or proto.get("methodName")
        or ""
    )
    return "\t".join(
        [
            entry.get("timestamp", ""),
            entry.get("severity", "DEFAULT"),
            labels.get("service_name") or labels.get("workflow_id") or "",
            str(message).rstrip(),
        ]
    )


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
    parser.add_argument("--json", action="store_true", help="로그 항목 원본을 JSON 한 줄씩 출력")
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
    if args.limit < 1:
        parser.error("--limit 은 1 이상이어야 합니다.")

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

    since = datetime.now(UTC) - parse_freshness(args.freshness)
    try:
        entries = read_entries(
            project, log_filter, limit=args.limit, since=since, token=_access_token()
        )
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:500]
        print(f"로그 조회 실패 (HTTP {exc.code}): {detail}", file=sys.stderr)
        return 1
    for entry in entries:
        print(json.dumps(entry, ensure_ascii=False) if args.json else format_entry(entry))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
