"""소스 조회 실패가 Drive 루프와 후속 복구를 중단하지 않는지 검증한다."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / "workflows" / "daily_sync.yaml"


def _named_step(steps: list[dict[str, Any]], name: str) -> dict[str, Any]:
    return next(step[name] for step in steps if name in step)


def _source_call_steps() -> tuple[dict[str, Any], dict[str, Any]]:
    data = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    drive_loop = _named_step(data["main"]["steps"], "for_each_drive")["for"]
    fetch_source = _named_step(drive_loop["steps"], "fetch_source")
    branches = fetch_source["switch"]
    run_backfill = _named_step(branches[0]["steps"], "run_backfill")
    fetch_changes = _named_step(branches[1]["steps"], "fetch_changes")
    return run_backfill, fetch_changes


def _assert_exhausted_retry_continues_drive_loop(
    call_step: dict[str, Any], *, response_variable: str
) -> None:
    assert "retry" in call_step
    assert "except" in call_step

    error_steps = call_step["except"]["steps"]
    serialized = yaml.safe_dump(error_steps)

    # 소스 응답이 없을 때 Drive 단위 실패를 기록하고 오류 로그를 남긴다.
    assert "totals.failed + 1" in serialized
    assert "severity: ERROR" in serialized

    # 현재 Drive만 중단한다. 다음 Drive 및 루프 밖 복구 단계는 계속 실행된다.
    terminal = next(iter(error_steps[-1].values()))
    assert terminal["next"] == "continue"

    # 예외 경로에서 생성되지 않은 성공 응답 변수를 읽어서는 안 된다.
    assert response_variable not in serialized


def test_backfill_retry_exhaustion_continues_with_next_drive() -> None:
    run_backfill, _ = _source_call_steps()
    _assert_exhausted_retry_continues_drive_loop(
        run_backfill, response_variable="backfill_resp"
    )


def test_changes_retry_exhaustion_continues_with_next_drive() -> None:
    _, fetch_changes = _source_call_steps()
    _assert_exhausted_retry_continues_drive_loop(
        fetch_changes, response_variable="changes_resp"
    )


def test_recovery_steps_remain_after_resilient_drive_loop() -> None:
    data = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    top_level_names = [next(iter(step)) for step in data["main"]["steps"]]

    drive_loop = top_level_names.index("for_each_drive")
    recover_pending = top_level_names.index("recover_pending_index")
    recover_failed = top_level_names.index("recover_failed")

    assert drive_loop < recover_pending < recover_failed


def test_commit_retry_exhaustion_also_continues_drive_loop() -> None:
    data = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    drive_steps = _named_step(data["main"]["steps"], "for_each_drive")["for"]["steps"]
    commit_switch = _named_step(drive_steps, "commit_token")["switch"]
    do_commit = _named_step(commit_switch[0]["steps"], "do_commit")

    assert "retry" in do_commit
    assert "except" in do_commit
    assert do_commit["except"]["steps"][-1]["continue_after_commit_failure"]["next"] == "continue"


def test_recovery_false_results_are_reported_as_errors() -> None:
    data = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    top_steps = data["main"]["steps"]

    reindex_check = _named_step(top_steps, "check_reindex_result")["switch"][0]
    retry_check = _named_step(top_steps, "check_retry_result")["switch"][0]
    assert "reindex_resp.body.ok" in reindex_check["condition"]
    assert "retry_resp.body.ok" in retry_check["condition"]
    assert "severity: ERROR" in yaml.safe_dump(reindex_check["steps"])
    assert "severity: ERROR" in yaml.safe_dump(retry_check["steps"])

    summary = _named_step(top_steps, "return_summary")["return"]
    # 색인 실패는 별도 지표라, ok 에서 명시적으로 함께 봐야 묻히지 않는다.
    assert summary["ok"] == "${totals.failed == 0 and totals.indexFailed == 0}"
