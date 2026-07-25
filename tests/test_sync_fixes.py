"""sync 파이프라인 회귀 수정 검증 (#1~#5).

#1 URI vs 파일 카운팅 (reconcile index_ok / uris)
#2 reconcile 이중 집계 (dlq/split ⊂ failed)
#3 색인 누락 복구 (should_skip_reindex)
#4 status 대소문자 (out-of-scope → SKIPPED)
#5 배치 삭제 코퍼스 1회 순회 (delete_files_by_ids)
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from shared.firestore_state import DocStateStore  # noqa: E402
from shared.models import DocState, DocStatus  # noqa: E402
from shared import rag_engine  # noqa: E402
from shared.rag_engine import RagEngineClient  # noqa: E402
import services.sync.main as sync_main  # noqa: E402
from services.sync.main import ReconcileBody, reconcile  # noqa: E402


# ---------------------------------------------------------------- #3
class _FakeStore(DocStateStore):
    def __init__(self, doc: DocState | None) -> None:  # firestore client 우회
        self._doc = doc

    def get(self, file_id: str) -> DocState | None:  # type: ignore[override]
        return self._doc


def test_should_skip_reindex_indexed_same_hash() -> None:
    doc = DocState(file_id="f1", drive_id="d", content_hash="h1", status=DocStatus.INDEXED)
    assert _FakeStore(doc).should_skip_reindex("f1", "h1") is True


def test_should_skip_reindex_parsed_not_indexed() -> None:
    # PARSED(색인만 실패) + 같은 해시 → 스킵 금지 (색인 복구 필요)
    doc = DocState(file_id="f1", drive_id="d", content_hash="h1", status=DocStatus.PARSED)
    assert _FakeStore(doc).should_skip_reindex("f1", "h1") is False


def test_should_skip_reindex_diff_hash() -> None:
    doc = DocState(file_id="f1", drive_id="d", content_hash="h1", status=DocStatus.INDEXED)
    assert _FakeStore(doc).should_skip_reindex("f1", "h2") is False


def test_should_skip_reindex_missing() -> None:
    assert _FakeStore(None).should_skip_reindex("f1", "h1") is False


# ---------------------------------------------------------------- #5
class _FakeRagFile:
    def __init__(self, display: str, name: str) -> None:
        self.display_name = display
        self.name = name


def test_delete_files_by_ids_single_list(monkeypatch) -> None:
    client = object.__new__(RagEngineClient)  # __init__(vertexai.init) 우회
    calls = {"list": 0, "deleted": []}

    def fake_list() -> list[_FakeRagFile]:
        calls["list"] += 1
        return [
            _FakeRagFile("f1.md", "rn1"),
            _FakeRagFile("f1.meta.md", "rn2"),
            _FakeRagFile("f2.pdf", "rn3"),
            _FakeRagFile("other.md", "rn4"),
        ]

    client.list_files = fake_list  # type: ignore[method-assign]
    monkeypatch.setattr(
        rag_engine.rag, "delete_file", lambda name: calls["deleted"].append(name)
    )

    n = client.delete_files_by_ids(["f1", "f2"])
    assert calls["list"] == 1  # 핵심: 배치 전체에 코퍼스 1회 순회
    assert n == 3
    assert set(calls["deleted"]) == {"rn1", "rn2", "rn3"}


def test_delete_files_by_ids_empty(monkeypatch) -> None:
    client = object.__new__(RagEngineClient)

    def boom() -> list:
        raise AssertionError("list_files should not be called for empty ids")

    client.list_files = boom  # type: ignore[method-assign]
    assert client.delete_files_by_ids([]) == 0


# ---------------------------------------------------------------- #1 / #2
def test_reconcile_no_double_count_dlq() -> None:
    # 5 listed = 2 gcs + 1 unchanged + 1 skipped + 1 failed(=dlq). dlq는 failed 하위.
    body = ReconcileBody(
        driveId="d", listed=5, gcsUploaded=2, uris=2, indexed=2,
        failed=1, skipped=1, deleted=0, unchanged=1, dlq=1, splitQueued=0,
    )
    r = reconcile(body)
    assert r["unaccounted"] == 0
    assert r["ok"] is True


def test_reconcile_index_ok_uses_uris() -> None:
    # 파일 1개 = 2 URI → indexed=2 > gcs=1 이지만 uris=2라 정합해야 함
    body = ReconcileBody(
        driveId="d", listed=1, gcsUploaded=1, uris=2, indexed=2,
        failed=0, skipped=0, deleted=0, unchanged=0,
    )
    r = reconcile(body)
    assert r["indexConsistent"] is True
    assert r["ok"] is True


def test_reconcile_uris_fallback_to_gcs() -> None:
    body = ReconcileBody(
        driveId="d", listed=1, gcsUploaded=1, indexed=1,
        failed=0, skipped=0, deleted=0, unchanged=0,
    )
    r = reconcile(body)
    assert r["indexConsistent"] is True


def test_reconcile_detects_real_gap() -> None:
    # listed=5, 실제 4만 계정 → 진짜 누락 1건은 여전히 잡아야 함
    body = ReconcileBody(
        driveId="d", listed=5, gcsUploaded=2, uris=2, indexed=2,
        failed=1, skipped=1, deleted=0, unchanged=0,
    )
    r = reconcile(body)
    assert r["unaccounted"] == 1
    assert r["ok"] is False


# ---------------------------------------------------------------- #4
def test_ingest_out_of_scope_returns_uppercase_skipped(monkeypatch) -> None:
    from services.sync.main import IngestBody, ingest

    class FakeSettings:
        sync_folder_id_list = ["folderX"]

    class FakeStore:
        def upsert(self, *a, **k) -> None:
            pass

    class FakeDrive:
        def is_in_sync_scope(self, file_id, folder_ids) -> bool:
            return False

    monkeypatch.setattr(sync_main, "get_settings", lambda: FakeSettings())
    monkeypatch.setattr(sync_main, "DocStateStore", lambda *a, **k: FakeStore())
    monkeypatch.setattr(sync_main, "GcsClient", lambda *a, **k: object())
    monkeypatch.setattr(sync_main, "DriveClient", lambda *a, **k: FakeDrive())

    res = ingest(IngestBody(fileId="f1", driveId="d", mimeType="application/pdf"))
    assert res["status"] == DocStatus.SKIPPED.value == "SKIPPED"
    assert res["reason"] == "out_of_folder_scope"


# ---------------------------------------------------------------- workflow YAML
def test_workflow_yaml_parses_and_uses_uris_gate() -> None:
    import yaml

    p = ROOT / "workflows" / "daily_sync.yaml"
    txt = p.read_text(encoding="utf-8")
    data = yaml.safe_load(txt)
    assert "main" in data
    assert "drive_indexed == drive_uris" in txt
    assert "drive_indexed == drive_gcs" not in txt
    assert "uris: ${drive_uris}" in txt


def test_workflow_runs_recovery_after_all_drives() -> None:
    """자동 회수 단계가 드라이브 루프 뒤에 남아 있어야 DLQ가 고착되지 않는다."""
    import yaml

    data = yaml.safe_load((ROOT / "workflows" / "daily_sync.yaml").read_text(encoding="utf-8"))
    names = [next(iter(step)) for step in data["main"]["steps"]]

    assert names.index("for_each_drive") < names.index("recover_pending_index")
    assert names.index("recover_pending_index") < names.index("recover_failed")
    assert names.index("recover_failed") < names.index("return_summary")


def test_workflow_recovery_calls_both_endpoints() -> None:
    txt = (ROOT / "workflows" / "daily_sync.yaml").read_text(encoding="utf-8")
    assert "/sync/reindex-pending" in txt
    assert "/sync/retry-failed" in txt
