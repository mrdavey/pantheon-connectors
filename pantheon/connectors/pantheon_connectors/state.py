"""Owner-only atomic binding/credential files and a payload-free crash journal."""
import json
import os
import pathlib
import sqlite3
import stat
import tempfile
import fcntl
import hashlib
from contextlib import contextmanager

class JournalBusy(RuntimeError):
    pass


def save_private(path, value):
    path = pathlib.Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as output:
            json.dump(value, output)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def load_private(path):
    path = pathlib.Path(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd) as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("Binding file must be a regular owner-only file")
        return json.load(source)

class Journal:
    def __init__(self, path, binding):
        path = pathlib.Path(path)
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        if path.stat().st_mode & 0o077 or path.stat().st_uid != os.getuid() or not path.is_file():
            raise ValueError("Journal must be owner-only")
        self.path = path
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("CREATE TABLE IF NOT EXISTS binding (id INTEGER PRIMARY KEY, value TEXT NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS deliveries (id TEXT PRIMARY KEY, state TEXT NOT NULL, receipt TEXT)")
        self.db.execute("CREATE TABLE IF NOT EXISTS controls (id TEXT PRIMARY KEY, value TEXT NOT NULL)")
        if "metadata" not in {row[1] for row in self.db.execute("PRAGMA table_info(deliveries)")}:
            self.db.execute("ALTER TABLE deliveries ADD COLUMN metadata TEXT")
        encoded = json.dumps(binding, sort_keys=True)
        row = self.db.execute("SELECT value FROM binding WHERE id=1").fetchone()
        if row and row[0] != encoded:
            raise ValueError("Pinned runtime binding changed; deliberate rejoin required")
        self.db.execute("INSERT OR IGNORE INTO binding VALUES (1, ?)", (encoded,))
        self.db.commit()

    def pin_scope(self, origin, credential_digest):
        encoded=json.dumps([origin,credential_digest])
        row=self.db.execute("SELECT value FROM binding WHERE id=2").fetchone()
        if row and row[0]!=encoded:
            raise ValueError("Journal belongs to another service or scoped membership")
        self.db.execute("INSERT OR IGNORE INTO binding VALUES (2, ?)",(encoded,))
        self.db.commit()

    def state(self, identity):
        row = self.db.execute("SELECT state FROM deliveries WHERE id=?", (identity,)).fetchone()
        return row[0] if row else None

    def record(self, identity):
        row = self.db.execute("SELECT state, receipt, metadata FROM deliveries WHERE id=?", (identity,)).fetchone()
        return dict(state=row[0], receipt=row[1], **json.loads(row[2] or "{}")) if row else None

    def begin(self, identity, state="dispatching", **metadata):
        # Also fences concurrent consumers sharing the same local journal.
        result = self.db.execute("INSERT OR IGNORE INTO deliveries (id,state,metadata) VALUES (?, ?, ?)", (identity, state, json.dumps(metadata)))
        self.db.commit()
        return result.rowcount == 1

    @contextmanager
    def exclusive(self):
        # OS ownership ends on process exit; a recovering process can reuse an
        # exact saved claim without racing a still-live consumer of this journal.
        fd = os.open(str(self.path) + ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise JournalBusy("Another process is consuming this journal") from None
            yield
        finally:
            os.close(fd)

    def unreported(self):
        return [(identity, self.record(identity)) for identity, in self.db.execute("SELECT id FROM deliveries WHERE state='received'")
                if not self.record(identity).get("reported")]

    def accepted(self):
        return [receipt for (receipt,) in self.db.execute("SELECT receipt FROM deliveries WHERE state='received' AND receipt IS NOT NULL")]

    def control(self, identity, value=None):
        if value is not None:
            self.db.execute("INSERT INTO controls VALUES (?,?) ON CONFLICT(id) DO UPDATE SET value=excluded.value",(identity,value))
            self.db.commit()
        row=self.db.execute("SELECT value FROM controls WHERE id=?",(identity,)).fetchone()
        return row[0] if row else None

    def notice_completed(self, token):
        return self.control('hermes-completed-token-' + hashlib.sha256(token.encode()).hexdigest()) == '1'

    def complete_notice(self, notice):
        # Tombstone first: a crash must never turn a known token into fresh work.
        self.control('hermes-completed-token-' + hashlib.sha256(notice['token'].encode()).hexdigest(), '1')
        self.control('hermes-notice', '{}')

    def put(self, identity, state, receipt=None, **metadata):
        previous = self.record(identity) or {}
        previous.pop("state", None)
        saved_receipt = previous.pop("receipt", None)
        previous.update(metadata)
        self.db.execute("INSERT INTO deliveries (id,state,receipt,metadata) VALUES (?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET state=excluded.state, receipt=excluded.receipt, metadata=excluded.metadata", (identity, state, receipt or saved_receipt, json.dumps(previous)))
        self.db.commit()
