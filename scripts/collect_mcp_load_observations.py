"""Add redacted Cloud Run request log and metric aggregates to a load report."""

import json
import sys
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.dept_gui import _common, _provision_access_token


def main():
    path = Path(sys.argv[1])
    report = json.loads(path.read_text(encoding="utf-8"))
    common = _common()
    project = common["GCP_PROJECT_ID"]
    start = report["startedAt"]
    end = report["finishedAt"]
    entries = []
    with httpx.Client(headers={"Authorization": f"Bearer {_provision_access_token()}"}, timeout=45) as client:
        payload = {"resourceNames": [f"projects/{project}"], "pageSize": 1000,
                   "filter": f'resource.type="cloud_run_revision" AND resource.labels.service_name="rag-mcp" '
                             f'AND httpRequest.userAgent="{report["userAgent"]}" '
                             f'AND timestamp>="{start}" AND timestamp<="{end}"'}
        while True:
            response = client.post("https://logging.googleapis.com/v2/entries:list", json=payload)
            response.raise_for_status()
            body = response.json()
            entries.extend(body.get("entries", []))
            if not body.get("nextPageToken"):
                break
            payload["pageToken"] = body["nextPageToken"]
        stage_logs = []
        for stage in report["stages"]:
            low, high = datetime.fromisoformat(stage["startedAt"]), datetime.fromisoformat(stage["finishedAt"])
            selected = [e for e in entries if low <= datetime.fromisoformat(e["timestamp"]) <= high]
            stage_logs.append({"concurrency": stage["concurrency"], "requestLogs": len(selected),
                               "statusCounts": dict(Counter(str(e.get("httpRequest", {}).get("status")) for e in selected)),
                               "distinctInstances": len({e.get("labels", {}).get("instanceId") for e in selected
                                                          if e.get("labels", {}).get("instanceId")})})
        metrics = {}
        # A minute of padding includes samples aggregating the final request burst.
        metric_end = min(datetime.now(UTC), datetime.fromisoformat(end) + timedelta(minutes=1)).isoformat()
        for metric in ("cpu/utilizations", "memory/utilizations", "instance_count"):
            response = client.get(f"https://monitoring.googleapis.com/v3/projects/{project}/timeSeries", params={
                "filter": f'metric.type="run.googleapis.com/container/{metric}" AND '
                          'resource.type="cloud_run_revision" AND resource.labels.service_name="rag-mcp"',
                "interval.startTime": start, "interval.endTime": metric_end, "view": "FULL"})
            if response.status_code != 200:
                metrics[metric] = {"httpStatus": response.status_code}
                continue
            rows = []
            for series in response.json().get("timeSeries", []):
                for point in series.get("points", []):
                    value = point.get("value", {})
                    distribution = value.get("distributionValue", {})
                    rows.append({"revision": series["resource"]["labels"].get("revision_name"),
                                 "endTime": point["interval"]["endTime"],
                                 "state": series.get("metric", {}).get("labels", {}).get("state"),
                                 "value": value.get("doubleValue", value.get("int64Value", distribution.get("mean")))})
            metrics[metric] = rows
    report["cloudObservations"] = {"collectedAt": datetime.now(UTC).isoformat(), "requestLogCount": len(entries),
                                   "stages": stage_logs, "metrics": metrics,
                                   "notes": "Cloud metrics can arrive late; distinct instances per stage is not peak concurrent instances. CPU/memory distribution means are sampled aggregates."}
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report["cloudObservations"], ensure_ascii=False))


if __name__ == "__main__":
    main()
