"""Opening a database that is not yet in write-ahead-log mode.

Several `regcompass run` processes may open one database at the same moment
(the pre-run job starts them side by side). Each one's first act is to put the
file in write-ahead-log mode. The switch needs the write lock, and a connection
that already reads the file and then asks for the write lock is told "database
is locked" at once: SQLite does not wait on the busy timeout for it, because
waiting there could deadlock. So when two processes switch at once, one of
them used to fail on its first line.

The race is made certain here rather than left to timing: this test holds the
write lock itself while the processes open the file, which is what the first
process to reach the switch does to the others, then lets go.
"""

from __future__ import annotations

import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

from regcompass import storage as storage_module
from regcompass.storage import Storage

PROCESSES = 4

# Each child says it is about to open the file, opens it through Storage,
# closes it and reports what happened.
CHILD = """
import sys
from pathlib import Path
from regcompass.storage import Storage

db, marker = sys.argv[1], Path(sys.argv[2])
marker.write_text("opening")
try:
    Storage(db).close()
except Exception as e:
    print(f"FAILED {type(e).__name__}: {e}")
    sys.exit(1)
print("OPENED")
"""


def rollback_journal_db(path: Path, *, rows: int = 20000) -> Path:
    """A database with the real schema and some bulk, left in rollback-journal
    mode, the state a copied or freshly built database arrives in."""
    storage = Storage(path)
    storage.apply_schema()
    storage.close()
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode = DELETE")
    conn.execute("CREATE TABLE bulk (id INTEGER PRIMARY KEY, body TEXT)")
    conn.executemany(
        "INSERT INTO bulk (body) VALUES (?)", (("x" * 200,) for _ in range(rows))
    )
    conn.commit()
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    conn.close()
    return path


def journal_mode(path: Path) -> str:
    conn = sqlite3.connect(path)
    try:
        return conn.execute("PRAGMA journal_mode").fetchone()[0]
    finally:
        conn.close()


class TestSeveralProcessesOpenAtOnce:
    def test_every_process_opens_and_the_file_ends_in_wal(self, tmp_path):
        db = rollback_journal_db(tmp_path / "work.db")
        script = tmp_path / "child.py"
        script.write_text(CHILD, encoding="utf-8")

        # Hold the write lock, as the process that reaches the switch first does.
        holder = sqlite3.connect(db, isolation_level=None)
        holder.execute("BEGIN IMMEDIATE")
        markers = [tmp_path / f"child-{i}.opening" for i in range(PROCESSES)]
        children = [
            subprocess.Popen(
                [sys.executable, str(script), str(db), str(marker)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            for marker in markers
        ]
        try:
            deadline = time.monotonic() + 60
            while not all(m.exists() for m in markers):
                assert time.monotonic() < deadline, "the processes never started"
                if any(c.poll() is not None for c in children):
                    break
                time.sleep(0.02)
            # Every process is now at (or past) the switch, meeting the lock.
            time.sleep(0.5)
        finally:
            holder.execute("COMMIT")
            holder.close()

        results = [c.communicate(timeout=120) for c in children]
        for child, (out, err) in zip(children, results):
            assert child.returncode == 0, f"{out}{err}"
            assert "OPENED" in out
        assert journal_mode(db) == "wal"


class TestOpeningAWalDatabase:
    def test_it_does_not_ask_for_the_switch(self, tmp_path, monkeypatch):
        db = tmp_path / "work.db"
        Storage(db).close()
        assert journal_mode(db) == "wal"

        statements: list[str] = []
        real_connect = sqlite3.connect

        def traced_connect(*args, **kwargs):
            conn = real_connect(*args, **kwargs)
            conn.set_trace_callback(statements.append)
            return conn

        monkeypatch.setattr(storage_module.sqlite3, "connect", traced_connect)
        Storage(db).close()

        pragmas = [s for s in statements if "journal_mode" in s.lower()]
        assert pragmas, "the mode is read before anything else"
        assert all("=" not in s for s in pragmas), pragmas

    def test_it_opens_while_another_process_holds_the_write_lock(self, tmp_path):
        db = tmp_path / "work.db"
        Storage(db).close()
        holder = sqlite3.connect(db, isolation_level=None)
        holder.execute("BEGIN IMMEDIATE")
        try:
            storage = Storage(db)
            assert storage.conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
            storage.close()
        finally:
            holder.execute("COMMIT")
            holder.close()


class TestASwitchThatNeverGetsTheLock:
    def test_it_gives_up_at_the_busy_timeout_and_says_why(self, tmp_path, monkeypatch):
        db = rollback_journal_db(tmp_path / "work.db", rows=10)
        monkeypatch.setattr(storage_module, "BUSY_TIMEOUT_S", 0.5)
        holder = sqlite3.connect(db, isolation_level=None)
        holder.execute("BEGIN IMMEDIATE")
        try:
            started = time.monotonic()
            with pytest.raises(sqlite3.OperationalError) as caught:
                Storage(db)
            waited = time.monotonic() - started
        finally:
            holder.execute("COMMIT")
            holder.close()
        message = str(caught.value)
        assert "database is locked" in message
        assert "write-ahead-log" in message
        assert str(db) in message
        assert 0.4 <= waited < 10
        assert journal_mode(db) == "delete"
