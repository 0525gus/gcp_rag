"""DLQ 자동 회수(/sync/retry-failed) 계약 테스트."""

from __future__ import annotations

from typing import Any

import pytest

import services.sync.main as sync_main
from services.sync.main import RetryFailedBody, retry_failed
from shared.models import DocState, DocStatus


class _FakeStore:
    def __init__(self, docs: list[DocState], attempts: dict[str, int] | None = None) -> None:
        self._docs = docs
        self.attempts = attempts or {}
        self.cleared: list[str] = []
        self.recorded: list[str] = []

    def list_by_status(
        self,
        status: DocStatus,
        limit: int = 100,
        *,
        cursor_key: str | None = None,
    ) -> list[DocState]:
        assert status == DocStatus.FAILED
        assert cursor_key == "retry-failed"
        return self._docs[:limit]

    def get_dlq_attempts(self, file_id: str) -> int:
        return self.attempts.get(file_id, 0)

    def record_dlq_attempt(self, file_id: str) -> None:
        self.recorded.append(file_id)
        self.attempts[file_id] = self.attempts.get(file_id, 0) + 1

    def clear_dlq(self, file_id: str) -> None:
        self.cleared.append(file_id)


def _doc(file_id: str) -> DocState:
    return DocState(
        file_id=file_id,
        drive_id="d1",
        name=f"{file_id}.hwp",
        mime_type="application/x-hwp",
        status=DocStatus.FAILED,
    )


@pytest.fixture
def wire(monkeypatch):
    """store/ingest/index_gcs를 가짜로 교체하고 호출 기록을 돌려준다."""

    def _wire(docs: list[DocState], ingest_results: dict[str, Any], attempts=None):
        store = _FakeStore(docs, attempts)
        indexed: list[list[str]] = []

        # retry_failed 는 클라이언트를 run 당 하나만 만들려고 _ingest_with 를 직접
        # 부른다(문서마다 ingest() 를 부르면 DriveClient discovery build 가 반복된다).
        def fake_ingest(body, **_clients):
            res = ingest_results[body.file_id]
            if isinstance(res, Exception):
                raise res
            return res

        def fake_index_gcs(body):
            indexed.append(list(body.gcs_uris))
            return {"count": len(body.gcs_uris)}

        monkeypatch.setattr(sync_main, "DocStateStore", lambda *a, **k: store)
        monkeypatch.setattr(sync_main, "get_settings", lambda: object())
        monkeypatch.setattr(sync_main, "GcsClient", lambda *a, **k: object())
        monkeypatch.setattr(sync_main, "DriveClient", lambda *a, **k: object())
        monkeypatch.setattr(sync_main, "_ingest_with", fake_ingest)
        monkeypatch.setattr(sync_main, "index_gcs", fake_index_gcs)
        return store, indexed

    return _wire


def test_recovered_file_is_indexed_and_dequeued(wire) -> None:
    store, indexed = wire(
        [_doc("f1")],
        {"f1": {"status": "GCS_READY", "gcsUris": ["gs://b/f1.md"]}},
    )
    res = retry_failed(RetryFailedBody())

    assert res["totals"]["recovered"] == 1
    assert res["totals"]["indexed"] == 1
    assert res["ok"] is True
    assert store.cleared == ["f1"]
    assert indexed == [["gs://b/f1.md"]]


def test_attempt_is_recorded_before_ingest(wire) -> None:
    # 재시도 중 프로세스가 죽어도 카운트가 남아야 무한 재시도를 막는다
    store, _ = wire([_doc("f1")], {"f1": RuntimeError("parser down")})
    res = retry_failed(RetryFailedBody())

    assert store.recorded == ["f1"]
    assert res["totals"]["stillFailed"] == 1
    assert res["ok"] is False


def test_exhausted_files_are_skipped_not_retried(wire) -> None:
    store, _ = wire(
        [_doc("f1")],
        {"f1": {"status": "GCS_READY", "gcsUris": ["gs://b/f1.md"]}},
        attempts={"f1": 3},
    )
    res = retry_failed(RetryFailedBody(maxAttempts=3))

    assert res["totals"]["exhausted"] == 1
    assert res["totals"]["retried"] == 0
    assert store.recorded == []
    assert store.cleared == []
    # 영구 보류(parked)는 '이번 실행의 실패'가 아니다 — ok 를 내리면 손상된 문서
    # 1건 때문에 배치가 매일 실패로 보고돼 진짜 장애가 묻힌다. 별도 지표로 올린다.
    assert res["ok"] is True
    assert res["parked"] == 1


def test_under_cap_still_retries(wire) -> None:
    _, _ = wire(
        [_doc("f1")],
        {"f1": {"status": "GCS_READY", "gcsUris": ["gs://b/f1.md"]}},
        attempts={"f1": 2},
    )
    res = retry_failed(RetryFailedBody(maxAttempts=3))

    assert res["totals"]["exhausted"] == 0
    assert res["totals"]["recovered"] == 1


def test_terminal_statuses_count_as_recovered(wire) -> None:
    # SKIPPED/UNCHANGED는 더 이상 실패가 아니므로 DLQ에서 빼야 한다
    store, indexed = wire(
        [_doc("f1"), _doc("f2")],
        {"f1": {"status": "SKIPPED"}, "f2": {"status": "UNCHANGED"}},
    )
    res = retry_failed(RetryFailedBody())

    assert res["totals"]["recovered"] == 2
    assert sorted(store.cleared) == ["f1", "f2"]
    assert indexed == []


def test_still_dlq_status_is_not_cleared(wire) -> None:
    store, _ = wire([_doc("f1")], {"f1": {"status": "DLQ", "error": "size exceeded"}})
    res = retry_failed(RetryFailedBody())

    assert res["totals"]["stillFailed"] == 1
    assert store.cleared == []


def test_batch_flushes_at_index_batch_size(wire) -> None:
    docs = [_doc(f"f{i}") for i in range(1, 5)]
    results = {
        d.file_id: {"status": "GCS_READY", "gcsUris": [f"gs://b/{d.file_id}.md"]}
        for d in docs
    }
    _, indexed = wire(docs, results)
    res = retry_failed(RetryFailedBody(indexBatchSize=2))

    assert [len(b) for b in indexed] == [2, 2]
    assert res["totals"]["indexed"] == 4


def test_index_failure_does_not_abort_remaining_files(wire, monkeypatch) -> None:
    docs = [_doc("f1"), _doc("f2")]
    results = {
        d.file_id: {"status": "GCS_READY", "gcsUris": [f"gs://b/{d.file_id}.md"]}
        for d in docs
    }
    _, _ = wire(docs, results)
    monkeypatch.setattr(
        sync_main, "index_gcs", lambda body: (_ for _ in ()).throw(RuntimeError("rag down"))
    )

    res = retry_failed(RetryFailedBody(indexBatchSize=1))

    # 색인이 죽어도 두 파일 모두 ingest 재시도는 완료되어야 한다
    assert res["totals"]["retried"] == 2
    assert res["totals"]["indexed"] == 0
    assert res["totals"]["recovered"] == 0
    assert res["totals"]["stillFailed"] == 2
    assert res["ok"] is False


def test_gcs_ready_without_uri_remains_failed(wire) -> None:
    store, indexed = wire([_doc("f1")], {"f1": {"status": "GCS_READY", "gcsUris": []}})

    res = retry_failed(RetryFailedBody())

    assert res["totals"]["recovered"] == 0
    assert res["totals"]["stillFailed"] == 1
    assert res["ok"] is False
    assert store.cleared == []
    assert indexed == []


def test_no_failed_docs_is_a_noop(wire) -> None:
    _, indexed = wire([], {})
    res = retry_failed(RetryFailedBody())

    assert res["totals"] == {
        "candidates": 0,
        "retried": 0,
        "recovered": 0,
        "stillFailed": 0,
        "exhausted": 0,
        "indexed": 0,
    }
    assert res["ok"] is True
    assert indexed == []
