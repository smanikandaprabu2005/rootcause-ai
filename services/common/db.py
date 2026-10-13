"""Bounded database connection pool for the demo shop.

``mysql://user:pass@host:3306/shop``  MySQL 8 via PyMySQL: what ``docker compose`` runs.
``sqlite:////abs/path/shop.db``       SQLite: a no-Docker fallback so the whole stack can run on a laptop.

The pool is deliberately small and strict: a checkout that waits longer than ``acquire_timeout`` raises
``PoolExhausted``. That is the failure the ``db_pool_exhaustion`` fault provokes, so it has to be observable.
This module has no OpenTelemetry or web imports, so it can be tested on its own.
"""
from __future__ import annotations

import collections
import sqlite3
import threading
import time
from contextlib import contextmanager
from typing import Any, Callable, Deque, Optional
from urllib.parse import unquote, urlparse


class PoolExhausted(Exception):
    pass


# --------------------------------------------------------------------------- connections
def mysql_connect(dsn: str) -> Any:
    import pymysql
    from pymysql.constants import CLIENT

    u = urlparse(dsn)
    return pymysql.connect(
        host=u.hostname or "localhost", port=u.port or 3306, user=unquote(u.username or ""),
        password=unquote(u.password or ""), database=u.path.lstrip("/"), charset="utf8mb4",
        autocommit=False, connect_timeout=5,
        client_flag=CLIENT.FOUND_ROWS,  # UPDATE reports rows *matched*, like most other databases
    )


_SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS items (id INTEGER PRIMARY KEY, name TEXT NOT NULL, price REAL NOT NULL, stock INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS orders (id INTEGER PRIMARY KEY AUTOINCREMENT, item_id INTEGER NOT NULL, qty INTEGER NOT NULL,
                                   status TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS payments (id INTEGER PRIMARY KEY AUTOINCREMENT, order_id INTEGER NOT NULL, amount REAL NOT NULL,
                                     status TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
"""


class _SqliteCursor:
    """Makes sqlite3 look like PyMySQL: ``%s`` placeholders, ``lastrowid``, ``rowcount``, ``with``."""

    def __init__(self, cur: sqlite3.Cursor):
        self._c = cur

    def execute(self, sql: str, params: Any = ()) -> "_SqliteCursor":
        self._c.execute(sql.replace("%s", "?"), params)
        return self

    def fetchone(self):
        return self._c.fetchone()

    def fetchall(self):
        return self._c.fetchall()

    @property
    def lastrowid(self):
        return self._c.lastrowid

    @property
    def rowcount(self):
        return self._c.rowcount

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._c.close()


class _SqliteConn:
    def __init__(self, raw: sqlite3.Connection):
        self._raw = raw

    def cursor(self) -> _SqliteCursor:
        return _SqliteCursor(self._raw.cursor())

    def commit(self) -> None:
        self._raw.commit()

    def rollback(self) -> None:
        self._raw.rollback()

    def close(self) -> None:
        self._raw.close()


def sqlite_connect(dsn: str) -> Any:
    path = dsn.split("sqlite:///", 1)[1] or ":memory:"
    raw = sqlite3.connect(path, timeout=5.0, check_same_thread=False)
    raw.execute("PRAGMA journal_mode=WAL")
    raw.executescript(_SQLITE_SCHEMA)
    # 20 items, same prices as db/init.sql; INSERT OR IGNORE keeps concurrent first starts safe.
    raw.executemany("INSERT OR IGNORE INTO items (id, name, price, stock) VALUES (?, ?, ?, 100000000)",
                    [(g, f"Item {g}", float(5 + (g * 3) % 95)) for g in range(1, 21)])
    raw.commit()
    return _SqliteConn(raw)


def default_connect(dsn: str) -> Callable[[], Any]:
    if dsn.startswith("mysql://"):
        return lambda: mysql_connect(dsn)
    if dsn.startswith("sqlite:///"):
        return lambda: sqlite_connect(dsn)
    raise ValueError(f"unsupported DATABASE_URL scheme in {dsn.split('://')[0]}://; use mysql:// or sqlite:///")


# --------------------------------------------------------------------------- pool
class Db:
    def __init__(self, dsn: Optional[str] = None, maxconn: int = 5, acquire_timeout: float = 2.0,
                 connect: Optional[Callable[[], Any]] = None, connect_wait: float = 60.0):
        """``connect`` (a zero-argument factory returning a DB-API connection) overrides the DSN, for tests."""
        self.maxconn = maxconn
        self.acquire_timeout = acquire_timeout
        self.in_use = 0
        self._connect = connect or default_connect(dsn or "")
        self._sem = threading.BoundedSemaphore(maxconn)
        self._count_lock = threading.Lock()
        self._release_hold = threading.Event()
        self._idle: Deque[Any] = collections.deque()
        deadline = time.time() + connect_wait
        while True:  # the database container may still be starting
            try:
                self._idle.append(self._connect())
                break
            except Exception:
                if time.time() >= deadline:
                    raise
                time.sleep(1)

    def _checkout(self) -> Any:
        while self._idle:
            c = self._idle.pop()
            ping = getattr(c, "ping", None)
            try:
                if ping:
                    ping(reconnect=True)  # PyMySQL: transparently replaces a connection the server dropped
                return c
            except Exception:
                self._discard(c)
        return self._connect()

    @staticmethod
    def _discard(c: Any) -> None:
        try:
            c.close()
        except Exception:
            pass

    @contextmanager
    def conn(self):
        if not self._sem.acquire(timeout=self.acquire_timeout):
            raise PoolExhausted(
                f"database connection pool exhausted: waited {self.acquire_timeout}s "
                f"for one of {self.maxconn} connections"
            )
        with self._count_lock:
            self.in_use += 1
        connection = None
        try:
            connection = self._checkout()
            yield connection
            connection.commit()
        except BaseException:
            if connection is not None:
                try:
                    connection.rollback()
                except Exception:
                    self._discard(connection)
                    connection = None  # broken: do not return it to the pool
            raise
        finally:
            if connection is not None:
                self._idle.append(connection)
            with self._count_lock:
                self.in_use -= 1
            self._sem.release()

    # Fault helper: occupy every slot, like a connection leak or a stuck long transaction.
    def hold_all(self, seconds: float) -> None:
        self._release_hold.clear()

        def _hold() -> None:
            taken = 0
            for _ in range(self.maxconn):
                if self._sem.acquire(timeout=30):
                    taken += 1
                    with self._count_lock:
                        self.in_use += 1
            self._release_hold.wait(timeout=seconds)
            for _ in range(taken):
                with self._count_lock:
                    self.in_use -= 1
                self._sem.release()

        threading.Thread(target=_hold, daemon=True, name="db-hold").start()

    def release_held(self) -> None:
        self._release_hold.set()
