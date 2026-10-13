"""The demo shop's database layer: pool behaviour, the services' SQL, and MySQL-dialect validity.

There is no MySQL server in the unit-test environment, so MySQL correctness is covered three ways:
  1. pool logic is tested with fake connections (the pool takes an injectable connect factory);
  2. every statement the services run is parsed with the MySQL dialect (catches e.g. Postgres-only RETURNING);
  3. the same service code runs end to end on the SQLite fallback.
A real MySQL run happens in CI (SHOP_TEST_MYSQL), see the last test.
"""
import ast
import importlib.util
import os
import sys
import threading
import time
from pathlib import Path

import pytest
import sqlglot
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "services"))

from common.db import Db, PoolExhausted  # noqa: E402


# --------------------------------------------------------------------------- pool logic
class FakeConn:
    def __init__(self, broken_rollback=False):
        self.commits = self.rollbacks = 0
        self.closed = False
        self.broken_rollback = broken_rollback

    def commit(self): self.commits += 1

    def rollback(self):
        if self.broken_rollback:
            raise RuntimeError("connection lost")
        self.rollbacks += 1

    def close(self): self.closed = True


def make_db(maxconn=2, timeout=0.15, **kw):
    made = []

    def connect():
        c = FakeConn(**kw)
        made.append(c)
        return c

    return Db(maxconn=maxconn, acquire_timeout=timeout, connect=connect, connect_wait=0), made


def test_commit_on_success_and_connection_is_reused():
    db, made = make_db()
    with db.conn() as c1:
        pass
    with db.conn() as c2:
        pass
    assert c1 is c2 and c1.commits == 2 and len(made) == 1 and db.in_use == 0


def test_rollback_on_error_and_slot_is_released():
    db, _ = make_db()
    with pytest.raises(ValueError):
        with db.conn() as c:
            raise ValueError("boom")
    assert c.rollbacks == 1 and c.commits == 0 and db.in_use == 0


def test_exhaustion_raises_after_timeout_and_recovers():
    db, _ = make_db(maxconn=2)
    with db.conn(), db.conn():
        assert db.in_use == 2
        started = time.time()
        with pytest.raises(PoolExhausted, match="exhausted"):
            with db.conn():
                pass
        assert time.time() - started >= 0.15
    with db.conn():  # capacity is back once the others return
        pass


def test_hold_all_exhausts_the_pool_until_released():
    db, _ = make_db(maxconn=3)
    db.hold_all(30)
    deadline = time.time() + 2
    while db.in_use < 3 and time.time() < deadline:
        time.sleep(0.01)
    assert db.in_use == 3
    with pytest.raises(PoolExhausted):
        with db.conn():
            pass
    db.release_held()
    deadline = time.time() + 2
    while db.in_use and time.time() < deadline:
        time.sleep(0.01)
    with db.conn():
        pass


def test_failed_checkout_does_not_leak_a_slot():
    """Regression: a connect() that raises used to leave the semaphore taken, silently shrinking the pool."""
    calls = {"n": 0}

    def connect():
        calls["n"] += 1
        if calls["n"] == 2:        # 1st call is the startup probe; the 2nd is the first real checkout... after reuse
            raise ConnectionError("db down")
        return FakeConn()

    db = Db(maxconn=1, acquire_timeout=0.1, connect=connect, connect_wait=0)
    with db.conn():                  # takes the pooled startup connection
        pass
    db._idle.clear()                 # force the next checkout to open a new connection (call #2, which fails)
    with pytest.raises(ConnectionError):
        with db.conn():
            pass
    assert db.in_use == 0
    with db.conn():                  # the single slot is still usable
        pass


def test_broken_connection_is_discarded_not_returned():
    db, made = make_db(maxconn=1, broken_rollback=True)
    with pytest.raises(ValueError):
        with db.conn():
            raise ValueError("x")
    assert made[0].closed and not db._idle


def test_threads_never_exceed_pool_size():
    db, _ = make_db(maxconn=3, timeout=2.0)
    peak, lock = [0], threading.Lock()

    def work():
        with db.conn():
            with lock:
                peak[0] = max(peak[0], db.in_use)
            time.sleep(0.02)

    ts = [threading.Thread(target=work) for _ in range(12)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert peak[0] <= 3 and db.in_use == 0


# --------------------------------------------------------------------------- MySQL dialect
def _executed_sql(path: Path):
    for node in ast.walk(ast.parse(path.read_text())):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "execute"
                and node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)):
            yield node.args[0].value


def test_every_service_statement_is_valid_mysql():
    seen = 0
    for svc in ("orders", "inventory", "payments", "notifications", "gateway"):
        for sql in _executed_sql(ROOT / "services" / svc / "main.py"):
            seen += 1
            assert "RETURNING" not in sql.upper(), f"{svc}: RETURNING does not exist in MySQL: {sql}"
            sqlglot.parse_one(sql.replace("%s", "?"), read="mysql", error_level=sqlglot.ErrorLevel.RAISE)
    assert seen >= 5


def test_init_sql_is_valid_mysql_and_seeds_catalogue():
    text = (ROOT / "db" / "init.sql").read_text()
    stmts = [s for s in sqlglot.parse(text, read="mysql", error_level=sqlglot.ErrorLevel.RAISE) if s]
    assert len(stmts) == 4                                    # 3 tables + 1 seed insert
    assert "AUTO_INCREMENT" in text and "SERIAL" not in text.upper()
    assert text.count("('Item ") == 20


# --------------------------------------------------------------------------- services on the SQLite fallback
def _load(service: str, tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/shop.db")
    monkeypatch.setenv("FAULT_STATE_FILE", str(tmp_path / f"{service}.json"))
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    spec = importlib.util.spec_from_file_location(f"svc_{service}", ROOT / "services" / service / "main.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_inventory_lists_reserves_and_rejects_oversell(tmp_path, monkeypatch):
    inv = _load("inventory", tmp_path, monkeypatch)
    client = TestClient(inv.app)
    items = client.get("/items").json()
    assert len(items) == 20 and items[0] == {"id": 1, "name": "Item 1", "price": 8.0, "stock": 100000000}
    ok = client.post("/reserve", json={"item_id": 3, "qty": 2}).json()
    assert ok == {"item_id": 3, "qty": 2, "price": 14.0}
    assert client.get("/items").json()[2]["stock"] == 100000000 - 2
    assert client.post("/reserve", json={"item_id": 3, "qty": 10**9}).status_code == 409   # not enough stock
    assert client.post("/reserve", json={"item_id": 999, "qty": 1}).status_code == 409     # no such item


def test_payments_records_a_ledger_row(tmp_path, monkeypatch):
    pay = _load("payments", tmp_path, monkeypatch)
    client = TestClient(pay.app)
    first = client.post("/charge", json={"order_id": 7, "amount": 12.5}).json()
    second = client.post("/charge", json={"order_id": 8, "amount": 3.0}).json()
    assert first["status"] == "captured" and second["payment_id"] == first["payment_id"] + 1


def test_orders_confirms_and_reports_pool_exhaustion_as_503(tmp_path, monkeypatch):
    orders = _load("orders", tmp_path, monkeypatch)
    calls = []

    def fake_upstream(name, method, url, **kw):
        calls.append(name)
        return {"price": 10.0} if name == "inventory" else {"ok": True}

    orders.ctx.upstream = fake_upstream
    client = TestClient(orders.app)
    out = client.post("/orders", json={"item_id": 1, "qty": 3}).json()
    assert out == {"order_id": 1, "status": "confirmed", "amount": 30.0}
    assert calls == ["inventory", "payments", "notifications"]
    with orders.ctx.db.conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT status FROM orders WHERE id = %s", (1,))
        assert cur.fetchone()[0] == "confirmed"

    # the injected fault makes the next request fail the way production would: 503, with a pool-exhaustion log
    orders.ctx.faults.apply("db_pool_exhaustion", {"hold_seconds": 30})
    deadline = time.time() + 3
    while orders.ctx.db.in_use < orders.ctx.db.maxconn and time.time() < deadline:
        time.sleep(0.02)
    assert client.post("/orders", json={"item_id": 1, "qty": 1}).status_code == 503
    orders.ctx.faults.clear("db_pool_exhaustion")


def test_default_connect_rejects_unknown_scheme():
    with pytest.raises(ValueError, match="mysql:// or sqlite:///"):
        Db("postgresql://x/y", connect_wait=0)


# --------------------------------------------------------------------------- real MySQL (CI)
@pytest.mark.skipif(not os.getenv("SHOP_TEST_MYSQL"), reason="set SHOP_TEST_MYSQL=mysql://user:pass@host:3306/db")
def test_real_mysql_roundtrip(tmp_path, monkeypatch):
    """Applies db/init.sql to a real MySQL, then runs the services' SQL through the real PyMySQL pool."""
    import pymysql

    dsn = os.environ["SHOP_TEST_MYSQL"]
    from common.db import mysql_connect

    raw = mysql_connect(dsn)
    cur = raw.cursor()
    for t in ("payments", "orders", "items"):
        cur.execute(f"DROP TABLE IF EXISTS {t}")
    for stmt in sqlglot.parse((ROOT / "db" / "init.sql").read_text(), read="mysql"):
        if stmt:
            cur.execute(stmt.sql(dialect="mysql"))
    raw.commit()
    raw.close()

    monkeypatch.setenv("DATABASE_URL", dsn)
    monkeypatch.setenv("FAULT_STATE_FILE", str(tmp_path / "inventory.json"))
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    spec = importlib.util.spec_from_file_location("svc_inventory_mysql", ROOT / "services" / "inventory" / "main.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    client = TestClient(mod.app)
    assert len(client.get("/items").json()) == 20
    assert client.post("/reserve", json={"item_id": 2, "qty": 1}).json()["price"] == 11.0
    assert client.post("/reserve", json={"item_id": 2, "qty": 10**9}).status_code == 409
    assert pymysql  # driver actually used
