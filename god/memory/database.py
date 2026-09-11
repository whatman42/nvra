"""SQLite database connection, migrations, transactions, integrity."""

from __future__ import annotations

import random
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Generator, Optional, TypeVar

from .schema import MIGRATIONS, SCHEMA_VERSION

T = TypeVar("T")

# Process-wide write locks keyed by resolved DB path.
# Multiple Database instances pointing at the same file must serialize
# writers so SQLite does not raise "database is locked" under contention
# (Phase 2 compatibility: concurrent MemoryStore writers in tests/CI).
_PATH_LOCKS: dict[str, threading.RLock] = {}
_PATH_LOCKS_GUARD = threading.Lock()


def _path_lock(db_path: Path) -> threading.RLock:
    key = str(db_path.resolve())
    with _PATH_LOCKS_GUARD:
        lock = _PATH_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _PATH_LOCKS[key] = lock
        return lock


def _is_locked_error(exc: BaseException) -> bool:
    if isinstance(exc, sqlite3.OperationalError):
        msg = str(exc).lower()
        return "locked" in msg or "busy" in msg
    return False


def _with_busy_retry(fn: Callable[[], T], *, attempts: int = 12) -> T:
    """Retry on SQLite busy/locked with exponential backoff + jitter."""
    last: Optional[BaseException] = None
    for attempt in range(attempts):
        try:
            return fn()
        except sqlite3.OperationalError as e:
            if not _is_locked_error(e) or attempt == attempts - 1:
                raise
            last = e
            # 5ms → ~50ms base, with jitter; total wait stays well under CI timeouts
            delay = min(0.05 * (2**attempt), 0.5) + random.uniform(0, 0.02)
            time.sleep(delay)
    assert last is not None
    raise last


class DatabaseError(Exception):
    """Base error for memory layer."""


class IntegrityError(DatabaseError):
    """Raised when integrity checks fail or FK violations occur."""


def utc_now() -> str:
    """Return current UTC time as ISO-8601 string with Z suffix."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class Database:
    """Thread-aware SQLite wrapper with migrations and integrity checks.

    Design:
    - WAL mode for concurrent readers
    - foreign_keys ON
    - process-wide path lock + busy retry for concurrent writers
    - schema_version tracked
    - check_integrity() for corruption detection
    - transaction() context manager with automatic rollback
    """

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._lock = _path_lock(self.db_path)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(
                str(self.db_path),
                check_same_thread=False,
                isolation_level=None,  # we manage transactions explicitly
                timeout=60.0,
            )
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA busy_timeout = 60000")
            # NORMAL is safe with WAL and reduces fsync contention under multi-writer tests
            conn.execute("PRAGMA synchronous = NORMAL")
            self._local.conn = conn
        return conn

    def _initialize(self) -> None:
        with self._lock:
            conn = self._connect()
            # Ensure schema_version table exists even before migrations
            conn.execute("""
                CREATE TABLE IF NOT EXISTS schema_version (
                    version INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL,
                    description TEXT
                )
            """)
            current = self.get_schema_version()
            self._apply_migrations(current)

    def get_schema_version(self) -> int:
        conn = self._connect()
        row = conn.execute(
            "SELECT MAX(version) AS v FROM schema_version"
        ).fetchone()
        if row is None or row["v"] is None:
            return 0
        return int(row["v"])

    def _apply_migrations(self, current: int) -> None:
        conn = self._connect()
        for version in sorted(MIGRATIONS.keys()):
            if version <= current:
                continue
            sql = MIGRATIONS[version]
            try:
                # executescript issues implicit commits; run statements carefully
                conn.execute("PRAGMA foreign_keys = ON")
                # Split and run non-empty statements; schema DDL is idempotent (IF NOT EXISTS)
                conn.executescript(sql)
                # Record version (may already exist on race — ignore)
                try:
                    conn.execute(
                        "INSERT INTO schema_version (version, applied_at, description) VALUES (?, ?, ?)",
                        (version, utc_now(), f"migration_{version}"),
                    )
                except sqlite3.IntegrityError:
                    pass  # concurrent migration already recorded
            except Exception as e:
                raise DatabaseError(f"Migration {version} failed: {e}") from e

    def execute(self, sql: str, params: tuple | list = ()) -> sqlite3.Cursor:
        def _do() -> sqlite3.Cursor:
            with self._lock:
                return self._connect().execute(sql, params)

        try:
            return _with_busy_retry(_do)
        except sqlite3.IntegrityError as e:
            raise IntegrityError(str(e)) from e
        except sqlite3.Error as e:
            raise DatabaseError(str(e)) from e

    def executemany(self, sql: str, seq: list) -> sqlite3.Cursor:
        def _do() -> sqlite3.Cursor:
            with self._lock:
                return self._connect().executemany(sql, seq)

        try:
            return _with_busy_retry(_do)
        except sqlite3.IntegrityError as e:
            raise IntegrityError(str(e)) from e
        except sqlite3.Error as e:
            raise DatabaseError(str(e)) from e

    def fetchone(self, sql: str, params: tuple | list = ()) -> Optional[sqlite3.Row]:
        return self.execute(sql, params).fetchone()

    def fetchall(self, sql: str, params: tuple | list = ()) -> list[sqlite3.Row]:
        return list(self.execute(sql, params).fetchall())

    @contextmanager
    def transaction(self) -> Generator[sqlite3.Connection, None, None]:
        """Atomic transaction. Rolls back on exception."""
        conn = self._connect()
        with self._lock:
            try:
                def _begin() -> None:
                    conn.execute("BEGIN IMMEDIATE")

                _with_busy_retry(_begin)
                yield conn
                conn.execute("COMMIT")
            except Exception:
                try:
                    conn.execute("ROLLBACK")
                except Exception:
                    pass
                raise

    def check_integrity(self) -> dict[str, Any]:
        """Run PRAGMA integrity_check and foreign_key_check."""
        conn = self._connect()
        integrity = conn.execute("PRAGMA integrity_check").fetchone()
        fk_violations = conn.execute("PRAGMA foreign_key_check").fetchall()
        ok = integrity is not None and integrity[0] == "ok" and len(fk_violations) == 0
        return {
            "ok": ok,
            "integrity_check": integrity[0] if integrity else None,
            "foreign_key_violations": [dict(r) for r in fk_violations],
            "schema_version": self.get_schema_version(),
        }

    def close(self) -> None:
        """Close the thread-local connection, checkpointing WAL first.

        WAL checkpoint is required so Windows can delete temporary DB files
        after tests (WinError 32 if -wal/-shm remain locked).
        """
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            try:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except Exception:
                pass
            try:
                conn.close()
            finally:
                self._local.conn = None

    def __enter__(self) -> "Database":
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()
