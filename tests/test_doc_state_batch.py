from fake_firestore import MemoryDb

from shared.firestore_state import DocStateStore
from shared.models import DocState, DocStatus


def _store() -> DocStateStore:
    store = object.__new__(DocStateStore)
    store._db = MemoryDb()
    store._col = store._db.collection("doc_state")
    return store


def test_get_many_reads_every_state_in_one_batch():
    store = _store()
    store.upsert(DocState(file_id="a", drive_id="d", status=DocStatus.INDEXED))
    store.upsert(DocState(file_id="b", drive_id="d", status=DocStatus.DELETED))
    batches = []
    get_all = store._db.get_all
    store._db.get_all = lambda refs, **kw: (batches.append(list(refs)), get_all(batches[-1], **kw))[1]

    found = store.get_many(["a", "missing", "b", "a"])

    assert [[ref.id for ref in batch] for batch in batches] == [["a", "missing", "b"]]
    assert found["a"].status == DocStatus.INDEXED
    assert found["b"].status == DocStatus.DELETED
    assert found["missing"] is None


def test_get_many_matches_single_get():
    store = _store()
    store.upsert(DocState(file_id="a", drive_id="d", name="안내.pdf", status=DocStatus.INDEXED))

    assert store.get_many(["a"])["a"] == store.get("a")


def test_get_many_without_ids_does_not_call_firestore():
    store = _store()
    store._db.get_all = lambda *a, **k: (_ for _ in ()).throw(AssertionError("no read expected"))

    assert store.get_many([]) == {}
