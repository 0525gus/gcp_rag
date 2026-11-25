import io
import json
from datetime import UTC, datetime, timedelta

import pytest

from scripts import view_logs


@pytest.fixture(autouse=True)
def departments(monkeypatch):
    monkeypatch.setattr(view_logs, "list_departments", lambda: ["cs"])


def fake_urlopen(pages):
    sent = []

    def urlopen(request, timeout):
        sent.append(json.loads(request.data))
        return io.BytesIO(json.dumps(pages[len(sent) - 1]).encode())

    return urlopen, sent


def test_mcp_target_includes_the_unified_service():
    log_filter = view_logs.build_filter("mcp", query=None, severity=None)
    assert 'resource.labels.service_name="rag-mcp"' in log_filter
    assert 'resource.labels.service_name="rag-mcp-cs-student"' in log_filter


def test_filter_reaches_the_api_intact_and_limit_spans_pages():
    log_filter = view_logs.build_filter("parser", query='a&b "c" \\d', severity="warning")
    urlopen, sent = fake_urlopen([
        {"entries": [{"timestamp": "t1"}, {"timestamp": "t2"}], "nextPageToken": "p2"},
        {"entries": [{"timestamp": "t3"}, {"timestamp": "t4"}], "nextPageToken": "p3"},
    ])
    since = datetime(2026, 11, 25, 9, 0, tzinfo=UTC)

    entries = view_logs.read_entries(
        "proj", log_filter, limit=3, since=since, token="tok", urlopen=urlopen
    )

    assert [e["timestamp"] for e in entries] == ["t1", "t2", "t3"]
    assert sent[0]["filter"] == (
        'resource.type="cloud_run_revision"\n'
        'resource.labels.service_name="rag-parser"\n'
        "severity>=WARNING\n"
        '(textPayload:"a&b \\"c\\" \\\\d" OR jsonPayload.message:"a&b \\"c\\" \\\\d" '
        'OR protoPayload.status.message:"a&b \\"c\\" \\\\d")\n'
        'timestamp>="2026-11-25T09:00:00Z"'
    )
    assert sent[0]["resourceNames"] == ["projects/proj"]
    assert "pageToken" not in sent[0] and sent[1]["pageToken"] == "p2"


@pytest.mark.parametrize(
    ("entry", "message"),
    [
        ({"textPayload": "plain\n"}, "plain"),
        ({"jsonPayload": {"message": "structured"}}, "structured"),
        ({"httpRequest": {"requestMethod": "POST", "status": 200, "requestUrl": "/mcp"}},
         "POST 200 /mcp"),
        ({"protoPayload": {"methodName": "Services.ReplaceService"}}, "Services.ReplaceService"),
    ],
)
def test_entry_line_shows_time_severity_service_and_message(entry, message):
    entry = {"timestamp": "t", "severity": "INFO",
             "resource": {"labels": {"service_name": "rag-mcp"}}, **entry}
    assert view_logs.format_entry(entry) == f"t\tINFO\trag-mcp\t{message}"


def test_freshness_accepts_gcloud_style_durations():
    assert view_logs.parse_freshness("90m") == timedelta(minutes=90)
    assert view_logs.parse_freshness("2d") == timedelta(days=2)
    with pytest.raises(SystemExit):
        view_logs.parse_freshness("yesterday")
