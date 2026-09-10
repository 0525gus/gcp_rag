"""Transactional in-memory backend using the SDK's real retry decorator."""

from copy import deepcopy
from threading import RLock
from types import SimpleNamespace

from google.api_core.exceptions import Aborted


class MemoryDb:
    def __init__(self):
        self.lock = RLock()
        self.data = {}
        self.revisions = {}
        self.before_commit = None

    def collection(self, path):
        return Ref(self, path)

    def transaction(self):
        return Transaction(self)

    def batch(self):
        return Transaction(self)


class Ref:
    def __init__(self, db, path):
        self.db, self.path = db, path

    def document(self, name):
        return Ref(self.db, f"{self.path}/{name}")

    def collection(self, name):
        return Ref(self.db, f"{self.path}/{name}")

    def get(self, transaction=None):
        with self.db.lock:
            if transaction is not None:
                if transaction.writes:
                    raise AssertionError("Firestore cannot read after writes in a transaction")
                transaction.reads[self.path] = self.db.revisions.get(self.path, 0)
            data = deepcopy(self.db.data.get(self.path))
        return SimpleNamespace(exists=data is not None, to_dict=lambda: deepcopy(data))

    def set(self, payload, merge=False):
        with self.db.lock:
            self.db.data[self.path] = {
                **(self.db.data.get(self.path, {}) if merge else {}),
                **deepcopy(payload),
            }
            self.db.revisions[self.path] = self.db.revisions.get(self.path, 0) + 1

    def delete(self):
        with self.db.lock:
            self.db.data.pop(self.path, None)
            self.db.revisions[self.path] = self.db.revisions.get(self.path, 0) + 1


class Transaction:
    _read_only = False
    _max_attempts = 5
    _id = b"memory-transaction"

    def __init__(self, db):
        self.db = db
        self._clean_up()

    def _clean_up(self):
        self.reads, self.writes = {}, []

    def _begin(self, retry_id=None):
        pass

    def _rollback(self):
        self._clean_up()

    def set(self, ref, payload, merge=False):
        self.writes.append(lambda: ref.set(payload, merge=merge))

    def update(self, ref, payload):
        self.set(ref, payload, merge=True)

    def delete(self, ref):
        self.writes.append(ref.delete)

    def _commit(self):
        with self.db.lock:
            if self.db.before_commit is not None:
                callback, self.db.before_commit = self.db.before_commit, None
                callback()
            if any(self.db.revisions.get(path, 0) != rev for path, rev in self.reads.items()):
                raise Aborted("concurrent write")
            for write in self.writes:
                write()
        self._clean_up()

    commit = _commit
