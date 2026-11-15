"""One writer at a time, all or nothing.

Concurrency runs against a real uvicorn on a free local port with a scratch
database. Mid-operation failures are injected with SQLite triggers created
through a second connection, so the failure happens inside the real SQL the
store runs, on the real HTTP route.
"""

import json
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from till.store import Store
from till.web import create_app

REPO = Path(__file__).resolve().parent.parent
LOCAL = "http://127.0.0.1:7740"


# --- helpers ----------------------------------------------------------------


def _sql(db, query, args=()):
    """Read the file through a separate connection: only committed data shows."""
    con = sqlite3.connect(db)
    try:
        return con.execute(query, args).fetchall()
    finally:
        con.close()


def _exec(db, script):
    con = sqlite3.connect(db)
    try:
        con.executescript(script)
        con.commit()
    finally:
        con.close()


def _books_check(db) -> dict:
    """Every invariant that a half-done or doubled write would break."""
    debit, credit = _sql(db, "SELECT COALESCE(SUM(debit_cents),0), COALESCE(SUM(credit_cents),0) FROM journal_lines")[0]
    ar = _sql(
        db,
        "SELECT COALESCE(SUM(debit_cents),0) - COALESCE(SUM(credit_cents),0) FROM journal_lines WHERE account_code='1100'",
    )[0][0]
    cash = _sql(
        db,
        "SELECT COALESCE(SUM(debit_cents),0) - COALESCE(SUM(credit_cents),0) FROM journal_lines WHERE account_code='1000'",
    )[0][0]
    open_balances = _sql(
        db,
        "SELECT COALESCE(SUM(total_cents - paid_cents),0) FROM invoices WHERE kind='invoice' AND status IN ('sent','partial')",
    )[0][0]
    payments = _sql(db, "SELECT COALESCE(SUM(amount_cents),0) FROM payments")[0][0]
    overpaid = _sql(
        db,
        """SELECT i.number FROM invoices i JOIN payments p ON p.invoice_id = i.id
           GROUP BY i.id HAVING SUM(p.amount_cents) > i.total_cents""",
    )
    paid_mismatch = _sql(
        db,
        """SELECT i.number FROM invoices i
           WHERE i.paid_cents != (SELECT COALESCE(SUM(amount_cents),0) FROM payments WHERE invoice_id=i.id)""",
    )
    posted_twice = _sql(
        db, "SELECT ref_id FROM journal WHERE ref_type='invoice' GROUP BY ref_id HAVING COUNT(*) > 1"
    )
    bad_entries = _sql(
        db,
        """SELECT j.id FROM journal j LEFT JOIN journal_lines l ON l.journal_id = j.id
           GROUP BY j.id HAVING COUNT(l.id) = 0 OR SUM(l.debit_cents) != SUM(l.credit_cents)""",
    )
    return {
        "balanced": debit == credit,
        "ar_equals_open": ar == open_balances,
        "cash_equals_payments": cash == payments,
        "overpaid": overpaid,
        "paid_mismatch": paid_mismatch,
        "posted_twice": posted_twice,
        "bad_entries": bad_entries,
        "ar": ar,
        "open_balances": open_balances,
    }


def _assert_books_exact(db):
    check = _books_check(db)
    assert check["balanced"], check
    assert check["ar_equals_open"], check
    assert check["cash_equals_payments"], check
    assert check["overpaid"] == [], check
    assert check["paid_mismatch"] == [], check
    assert check["posted_twice"] == [], check
    assert check["bad_entries"] == [], check


def _free_port() -> int:
    for port in range(7790, 7798):
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    pytest.skip("no free port in 7790-7797")


@pytest.fixture
def server(tmp_path):
    """Yields (db_path, start); start() launches uvicorn on that DB and returns its URL."""
    db = tmp_path / "till.db"
    procs = []

    def start():
        port = _free_port()
        code = (
            "import sys, uvicorn; from till.web import create_app; "
            "uvicorn.run(create_app(sys.argv[1]), host='127.0.0.1', port=int(sys.argv[2]), log_level='warning')"
        )
        proc = subprocess.Popen(
            [sys.executable, "-c", code, str(db), str(port)],
            cwd=REPO,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        procs.append(proc)
        base = f"http://127.0.0.1:{port}"
        deadline = time.time() + 15
        while time.time() < deadline:
            try:
                if httpx.get(f"{base}/api/health", timeout=0.5).status_code == 200:
                    return base
            except httpx.HTTPError:
                time.sleep(0.1)
        proc.kill()
        raise RuntimeError(proc.stderr.read().decode())

    yield db, start
    for proc in procs:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


# --- (a) real concurrency ---------------------------------------------------


def test_concurrent_payments_and_sends_over_http_keep_the_books_exact(server):
    db, start = server
    store = Store(db)
    store.migrate()
    cid = store.create_customer(name="Ada West")
    to_pay, to_send = [], []
    for _ in range(30):
        inv = store.create_invoice(customer_id=cid, issue_date="2026-11-01", due_date="2026-11-15")
        store.add_line(inv["id"], "Labour", 1000, 10000, 825)
        store.send_invoice(inv["id"], sent_at="2026-11-01")
        to_pay.append(store.get_invoice(inv["id"]))
        draft = store.create_invoice(customer_id=cid, issue_date="2026-11-01", due_date="2026-11-15")
        store.add_line(draft["id"], "Parts", 1000, 5000, 0)
        to_send.append(draft["id"])
    store.close()
    base = start()

    jobs = []
    for inv, draft_id in zip(to_pay, to_send):
        for _ in range(3):  # a triple-click on Pay, at the same moment
            jobs.append(("POST", f"/api/invoices/{inv['id']}/pay", {"amount_cents": inv["total_cents"], "paid_at": "2026-11-10"}))
        for _ in range(3):
            jobs.append(("POST", f"/api/invoices/{draft_id}/send", None))
        jobs.append(("GET", "/books", None))
        jobs.append(("GET", "/", None))

    with httpx.Client(base_url=base, timeout=10, limits=httpx.Limits(max_connections=64)) as client:

        def fire(job):
            method, path, body = job
            try:
                if method == "GET":
                    return path, client.get(path).status_code
                return path, client.post(path, json=body).status_code
            except httpx.HTTPError as exc:  # a wedged server counts as a failure, not a hang
                return path, f"{type(exc).__name__}"

        with ThreadPoolExecutor(max_workers=24) as pool:
            results = list(pool.map(fire, jobs))

    errors = [(p, s) for p, s in results if not isinstance(s, int) or s >= 500]
    pay_ok = {}
    for path, status in results:
        if path.endswith("/pay") and status == 200:
            pay_ok[path] = pay_ok.get(path, 0) + 1
    _assert_books_exact(db)
    assert errors == []
    # Each invoice was paid exactly once; the other two clicks were refused.
    assert sorted(pay_ok.values()) == [1] * len(to_pay)
    sent_posts = _sql(db, "SELECT COUNT(*) FROM journal WHERE ref_type='invoice'")[0][0]
    assert sent_posts == 60  # 30 sent beforehand + 30 drafts sent once each


PAYER_PROCESS = """
import json, sqlite3, sys
from till.store import Store

store = Store(sys.argv[1])
outcome = {"paid": 0, "refused": 0, "errors": []}
for invoice_id in json.loads(sys.argv[2]):
    try:
        inv = store.get_invoice(invoice_id)
        store.record_payment(invoice_id, inv["total_cents"], "2026-11-11", method="cash")
        outcome["paid"] += 1
    except ValueError:
        outcome["refused"] += 1
    except sqlite3.Error as exc:
        outcome["errors"].append(str(exc))
print(json.dumps(outcome))
"""


def test_a_second_process_paying_the_same_invoices_never_double_pays(server):
    """`till` commands open their own connection, so the lock in one process
    can't protect them. BEGIN IMMEDIATE makes the two processes take turns."""
    db, start = server
    store = Store(db)
    store.migrate()
    cid = store.create_customer(name="Ada West")
    invoices = [_sent_invoice(store, cid, 10000) for _ in range(40)]
    store.close()
    base = start()
    ids = [inv["id"] for inv in invoices]
    child = subprocess.Popen(
        [sys.executable, "-c", PAYER_PROCESS, str(db), json.dumps(ids)],
        cwd=REPO,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    with httpx.Client(base_url=base, timeout=10) as client:

        def pay(invoice_id):
            try:
                return client.post(f"/api/invoices/{invoice_id}/pay", json={"amount_cents": 10000}).status_code
            except httpx.HTTPError as exc:
                return type(exc).__name__

        with ThreadPoolExecutor(max_workers=8) as pool:
            statuses = list(pool.map(pay, ids))
    out, err = child.communicate(timeout=60)
    assert child.returncode == 0, err
    outcome = json.loads(out)
    assert outcome["errors"] == []
    assert [s for s in statuses if s not in (200, 400)] == []
    assert statuses.count(200) + outcome["paid"] == len(ids)  # each invoice paid exactly once
    _assert_books_exact(db)


# --- (b) a failure part-way leaves no trace ---------------------------------


@pytest.fixture
def book(tmp_path):
    db = tmp_path / "till.db"
    app = create_app(db)
    store = app.state.store
    cid = store.create_customer(name="Maria Chen", email="maria@example.com")
    client = TestClient(app, base_url=LOCAL, raise_server_exceptions=False)
    return db, store, cid, client


def _sent_invoice(store, cid, cents=20000):
    inv = store.create_invoice(customer_id=cid, issue_date="2026-11-01", due_date="2026-11-15")
    store.add_line(inv["id"], "Labour", 1000, cents, 0)
    return store.send_invoice(inv["id"], sent_at="2026-11-01")


def _state(db):
    return {
        "journal": _sql(db, "SELECT * FROM journal ORDER BY id"),
        "journal_lines": _sql(db, "SELECT * FROM journal_lines ORDER BY id"),
        "invoices": _sql(db, "SELECT * FROM invoices ORDER BY id"),
        "invoice_lines": _sql(db, "SELECT * FROM invoice_lines ORDER BY id"),
        "payments": _sql(db, "SELECT * FROM payments ORDER BY id"),
        "expenses": _sql(db, "SELECT * FROM expenses ORDER BY id"),
        "recurring": _sql(db, "SELECT * FROM recurring ORDER BY id"),
    }


def _unrelated_write(client):
    """The next request's commit used to write out a failed request's leftovers."""
    assert client.post("/people/new", data={"name": "Next customer"}, follow_redirects=False).status_code == 303


def test_payment_failing_after_the_journal_entry_leaves_no_trace(book):
    db, store, cid, client = book
    inv = _sent_invoice(store, cid)
    before = _state(db)
    _exec(db, "CREATE TRIGGER boom BEFORE INSERT ON payments BEGIN SELECT RAISE(ABORT, 'injected failure'); END;")
    r = client.post(f"/invoices/{inv['id']}/pay", data={"amount": "200.00", "paid_at": "2026-11-10"}, follow_redirects=False)
    assert r.status_code == 500
    _unrelated_write(client)
    after = _state(db)
    assert after["journal"] == before["journal"]
    assert after["journal_lines"] == before["journal_lines"]
    assert after["invoices"] == before["invoices"]
    assert after["payments"] == []
    _exec(db, "DROP TRIGGER boom;")
    # The owner retries: cash is counted once.
    r = client.post(f"/invoices/{inv['id']}/pay", data={"amount": "200.00", "paid_at": "2026-11-10"}, follow_redirects=False)
    assert r.status_code == 303
    assert store.get_invoice(inv["id"])["status"] == "paid"
    assert store.trial_balance()["1000"] == 20000
    _assert_books_exact(db)


def test_payment_failing_on_the_invoice_update_leaves_no_trace(book):
    db, store, cid, client = book
    inv = _sent_invoice(store, cid)
    before = _state(db)
    _exec(
        db,
        "CREATE TRIGGER boom BEFORE UPDATE OF paid_cents ON invoices BEGIN SELECT RAISE(ABORT, 'injected failure'); END;",
    )
    r = client.post(f"/api/invoices/{inv['id']}/pay", json={"amount_cents": 5000})
    assert r.status_code == 500
    _unrelated_write(client)
    after = _state(db)
    assert after["journal"] == before["journal"]
    assert after["payments"] == []
    assert after["invoices"] == before["invoices"]
    _assert_books_exact(db)


def test_send_failing_on_the_status_update_leaves_no_trace(book):
    db, store, cid, client = book
    inv = store.create_invoice(customer_id=cid, issue_date="2026-11-01", due_date="2026-11-15")
    store.add_line(inv["id"], "Labour", 1000, 30000, 825)
    before = _state(db)
    _exec(
        db,
        """CREATE TRIGGER boom BEFORE UPDATE OF status ON invoices WHEN NEW.status = 'sent'
           BEGIN SELECT RAISE(ABORT, 'injected failure'); END;""",
    )
    r = client.post(f"/invoices/{inv['id']}/send", follow_redirects=False)
    assert r.status_code == 500
    _unrelated_write(client)
    after = _state(db)
    assert after["journal"] == before["journal"] == []
    assert after["invoices"] == before["invoices"]
    _exec(db, "DROP TRIGGER boom;")
    assert client.post(f"/invoices/{inv['id']}/send", follow_redirects=False).status_code == 303
    assert len(_sql(db, "SELECT id FROM journal WHERE ref_type='invoice' AND ref_id=?", (inv["id"],))) == 1
    _assert_books_exact(db)


def test_send_failing_on_its_second_journal_line_leaves_no_half_entry(book):
    db, store, cid, client = book
    inv = store.create_invoice(customer_id=cid, issue_date="2026-11-01", due_date="2026-11-15")
    store.add_line(inv["id"], "Labour", 1000, 55000, 0)
    _exec(
        db,
        """CREATE TRIGGER boom BEFORE INSERT ON journal_lines WHEN NEW.account_code = '4000'
           BEGIN SELECT RAISE(ABORT, 'injected failure'); END;""",
    )
    r = client.post(f"/api/invoices/{inv['id']}/send")
    assert r.status_code == 500
    assert client.post("/api/customers", json={"name": "Next"}).status_code == 200
    assert _sql(db, "SELECT COUNT(*) FROM journal")[0][0] == 0
    assert _sql(db, "SELECT COUNT(*) FROM journal_lines")[0][0] == 0
    _exec(db, "DROP TRIGGER boom;")
    # Before the fix, Send now saw the half entry and never reposted it.
    assert client.post(f"/api/invoices/{inv['id']}/send").json()["status"] == "sent"
    assert store.trial_balance()["1100"] == 55000
    _assert_books_exact(db)


def test_expense_failing_after_the_journal_entry_leaves_no_trace(book):
    db, _, _, client = book
    _exec(db, "CREATE TRIGGER boom BEFORE INSERT ON expenses BEGIN SELECT RAISE(ABORT, 'injected failure'); END;")
    r = client.post("/books/expenses", data={"vendor_name": "Landlord", "amount": "1200.00", "account_code": "5300"}, follow_redirects=False)
    assert r.status_code == 500
    _unrelated_write(client)
    assert _sql(db, "SELECT COUNT(*) FROM journal")[0][0] == 0
    assert _sql(db, "SELECT COUNT(*) FROM expenses")[0][0] == 0


def test_failed_new_invoice_form_saves_nothing_and_keeps_the_number(book):
    db, store, cid, client = book
    first = store.create_invoice(customer_id=cid, issue_date="2026-11-01")
    assert first["number"] == "INV-0001"
    form = {
        "customer_id": str(cid),
        "description": ["Shop hours", "Parts"],
        "qty": ["2", "abc"],
        "unit": ["80.00", "10.00"],
        "tax_bps": ["0", "0"],
    }
    r = client.post("/invoices/new", data=form, follow_redirects=False)
    assert r.status_code == 400
    assert _sql(db, "SELECT number FROM invoices") == [("INV-0001",)]
    # A failure inside the store, after the invoice row and first line are written.
    _exec(db, "CREATE TRIGGER boom BEFORE INSERT ON invoice_lines WHEN NEW.position = 2 BEGIN SELECT RAISE(ABORT, 'injected failure'); END;")
    form["qty"] = ["2", "1"]
    r = client.post("/invoices/new", data=form, follow_redirects=False)
    assert r.status_code >= 400
    _unrelated_write(client)
    assert _sql(db, "SELECT number FROM invoices") == [("INV-0001",)]
    assert _sql(db, "SELECT COUNT(*) FROM invoice_lines")[0][0] == 0
    _exec(db, "DROP TRIGGER boom;")
    r = client.post("/invoices/new", data=form, follow_redirects=False)
    assert r.status_code == 303
    inv = store.get_invoice(int(r.headers["location"].rsplit("/", 1)[-1]))
    assert inv["number"] == "INV-0002"
    assert len(inv["lines"]) == 2


def test_failed_api_invoice_create_saves_nothing(book):
    db, _, cid, client = book
    bad = {"customer_id": cid, "lines": [{"description": "Labour", "qty": 1, "unit_cents": 9000}, {"qty": 1}]}
    r = client.post("/api/invoices", json=bad)
    assert r.status_code == 400
    assert _sql(db, "SELECT COUNT(*) FROM invoices")[0][0] == 0
    _exec(db, "CREATE TRIGGER boom BEFORE INSERT ON invoice_lines WHEN NEW.position = 2 BEGIN SELECT RAISE(ABORT, 'injected failure'); END;")
    good = {"customer_id": cid, "lines": [{"description": "A", "unit_cents": 100}, {"description": "B", "unit_cents": 200}]}
    assert client.post("/api/invoices", json=good).status_code == 500
    _unrelated_write(client)
    assert _sql(db, "SELECT COUNT(*) FROM invoices")[0][0] == 0
    _exec(db, "DROP TRIGGER boom;")
    made = client.post("/api/invoices", json=good).json()
    assert made["number"] == "INV-0001"
    assert made["total_cents"] == 300


def test_quote_conversion_failing_midway_leaves_no_invoice(book):
    db, store, cid, client = book
    quote = store.create_quote(customer_id=cid, issue_date="2026-11-01")
    store.add_line(quote["id"], "Work", 1000, 5000, 0)
    _exec(
        db,
        """CREATE TRIGGER boom BEFORE UPDATE OF status ON invoices WHEN NEW.status = 'converted'
           BEGIN SELECT RAISE(ABORT, 'injected failure'); END;""",
    )
    r = client.post(f"/quotes/{quote['id']}/convert", follow_redirects=False)
    assert r.status_code == 500
    _unrelated_write(client)
    assert _sql(db, "SELECT kind, status FROM invoices") == [("quote", "draft")]


def test_tick_issues_each_period_with_its_next_date_or_not_at_all(book):
    db, store, cid, client = book
    rec = store.create_recurring(
        customer_id=cid,
        cadence="monthly",
        next_date="2026-01-01",
        lines=[{"description": "Retainer", "qty_milli": 1000, "unit_cents": 8000, "tax_bps": 0}],
    )
    # The February issue fails after its invoice row is written, on its line.
    _exec(
        db,
        """CREATE TRIGGER boom BEFORE INSERT ON invoice_lines
           WHEN (SELECT issue_date FROM invoices WHERE id = NEW.invoice_id) = '2026-02-01'
           BEGIN SELECT RAISE(ABORT, 'injected failure'); END;""",
    )
    # The failure is reported, not raised: the tick itself succeeds.
    r = client.post("/api/tick", json={"as_of": "2026-03-15"})
    assert r.status_code == 200
    assert [i["issue_date"] for i in r.json()["created"]] == ["2026-01-01"]
    assert [f["recurring_id"] for f in r.json()["failed"]] == [rec["id"]]
    assert "injected failure" in r.json()["failed"][0]["error"]
    _unrelated_write(client)
    assert _sql(db, "SELECT issue_date, total_cents FROM invoices ORDER BY id") == [("2026-01-01", 8000)]
    assert _sql(db, "SELECT next_date FROM recurring WHERE id=?", (rec["id"],)) == [("2026-02-01",)]
    _exec(db, "DROP TRIGGER boom;")
    r = client.post("/api/tick", json={"as_of": "2026-03-15"})
    assert r.status_code == 200
    assert [i["issue_date"] for i in r.json()["created"]] == ["2026-02-01", "2026-03-01"]
    assert r.json()["failed"] == []
    dates = [d for (d,) in _sql(db, "SELECT issue_date FROM invoices ORDER BY issue_date")]
    assert dates == ["2026-01-01", "2026-02-01", "2026-03-01"]  # each period once
    assert _sql(db, "SELECT next_date FROM recurring") == [("2026-04-01",)]


def test_tick_failing_on_the_next_date_update_leaves_that_period_unissued(book):
    db, store, cid, client = book
    store.create_recurring(
        customer_id=cid,
        cadence="monthly",
        next_date="2026-01-01",
        lines=[{"description": "Retainer", "qty_milli": 1000, "unit_cents": 8000, "tax_bps": 0}],
    )
    # The February invoice is written, then moving next_date on to March fails.
    _exec(
        db,
        """CREATE TRIGGER boom BEFORE UPDATE OF next_date ON recurring WHEN NEW.next_date = '2026-03-01'
           BEGIN SELECT RAISE(ABORT, 'injected failure'); END;""",
    )
    r = client.post("/api/tick", json={"as_of": "2026-03-15"})
    assert r.status_code == 200 and len(r.json()["failed"]) == 1
    _unrelated_write(client)
    assert _sql(db, "SELECT issue_date FROM invoices ORDER BY id") == [("2026-01-01",)]
    assert _sql(db, "SELECT next_date FROM recurring") == [("2026-02-01",)]
    _exec(db, "DROP TRIGGER boom;")
    assert client.post("/api/tick", json={"as_of": "2026-03-15"}).status_code == 200
    dates = [d for (d,) in _sql(db, "SELECT issue_date FROM invoices ORDER BY issue_date")]
    assert dates == ["2026-01-01", "2026-02-01", "2026-03-01"]


# --- (c) nested transactions and the lock -----------------------------------


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "till.db")
    s.migrate()
    yield s
    s.close()


def _names(store):
    return [n for (n,) in _sql(store.path, "SELECT name FROM customers ORDER BY id")]


def test_nested_transaction_commits_only_when_the_outermost_ends(store):
    with store.transaction():
        store.create_customer(name="A")
        with store.transaction():
            store.create_customer(name="B")
        assert _names(store) == []  # the inner block did not commit
        store.create_customer(name="C")
    assert _names(store) == ["A", "B", "C"]


def test_outer_failure_undoes_inner_work_that_already_finished(store):
    with pytest.raises(RuntimeError):
        with store.transaction():
            inv = store.create_invoice(customer_id=store.create_customer(name="A"), issue_date="2026-11-01")
            store.add_line(inv["id"], "Labour", 1000, 100, 0)
            raise RuntimeError("boom")
    assert _names(store) == []
    assert store.list_invoices() == []
    assert store.create_invoice(customer_id=store.create_customer(name="B"), issue_date="2026-11-01")["number"] == "INV-0001"


def test_inner_failure_caught_by_the_outer_undoes_only_the_inner_block(store):
    with store.transaction():
        store.create_customer(name="kept")
        try:
            with store.transaction():
                store.create_customer(name="undone")
                raise RuntimeError("boom")
        except RuntimeError:
            pass
        store.create_customer(name="also kept")
    assert _names(store) == ["kept", "also kept"]


def test_a_refused_write_inside_a_transaction_leaves_the_outer_work_intact(store):
    cid = store.create_customer(name="A")
    with store.transaction():
        inv = store.create_invoice(customer_id=cid, issue_date="2026-11-01")
        store.add_line(inv["id"], "Labour", 1000, 100, 0)
        with pytest.raises(ValueError):
            store.record_payment(inv["id"], 100, "2026-11-02")  # a draft can't be paid
        store.send_invoice(inv["id"], sent_at="2026-11-02")
    assert store.get_invoice(inv["id"])["status"] == "sent"
    assert store.journal_is_balanced()


READER_DURING_FAILED_SEND = """
import json, sqlite3, sys, threading, time
from till.store import Store

store = Store(sys.argv[1])
store.migrate()
cid = store.create_customer(name="A")
inv = store.create_invoice(customer_id=cid, issue_date="2026-11-01")
store.add_line(inv["id"], "Labour", 1000, 10000, 0)
inside = threading.Event()

def pause():
    inside.set()
    time.sleep(0.3)
    return 0

store.db.create_function("pause", 0, pause)
side = sqlite3.connect(sys.argv[1])
side.executescript('''
    CREATE TRIGGER slow AFTER INSERT ON journal_lines WHEN NEW.account_code = '1100'
    BEGIN SELECT pause(); END;
    CREATE TRIGGER boom BEFORE UPDATE OF status ON invoices WHEN NEW.status = 'sent'
    BEGIN SELECT RAISE(ABORT, 'injected failure'); END;
''')
side.close()
seen = {}

def writer():
    try:
        store.send_invoice(inv["id"], sent_at="2026-11-02")
    except sqlite3.Error as exc:
        seen["writer_error"] = str(exc)

def reader():
    inside.wait(5)
    seen["ar"] = store.trial_balance()["1100"]

threads = [threading.Thread(target=writer), threading.Thread(target=reader)]
for t in threads:
    t.start()
for t in threads:
    t.join()
print(json.dumps(seen))
"""


def test_other_threads_wait_and_never_see_uncommitted_rows(tmp_path):
    """A Send pauses inside SQLite right after writing its receivable line,
    then fails; another thread reads the books during the pause. It runs in a
    child process because, without the lock, the two threads can deadlock
    inside the sqlite3 module."""
    db = tmp_path / "till.db"
    try:
        proc = subprocess.run(
            [sys.executable, "-c", READER_DURING_FAILED_SEND, str(db)],
            cwd=REPO,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except subprocess.TimeoutExpired:
        pytest.fail("reader and writer threads deadlocked on the shared connection")
    assert proc.returncode == 0, proc.stderr
    seen = json.loads(proc.stdout)
    assert "injected failure" in seen["writer_error"]
    assert seen["ar"] == 0  # the reader waited, and saw only committed books
    assert _sql(db, "SELECT COUNT(*) FROM journal")[0][0] == 0


def test_concurrent_store_payments_from_threads_pay_once(store):
    cid = store.create_customer(name="A")
    invoices = [_sent_invoice(store, cid, 10000) for _ in range(20)]
    barrier = threading.Barrier(4)
    outcomes = []

    def pay(inv):
        barrier.wait()
        try:
            store.record_payment(inv["id"], inv["total_cents"], "2026-11-10")
            outcomes.append("paid")
        except ValueError:
            outcomes.append("refused")

    for inv in invoices:
        threads = [threading.Thread(target=pay, args=(inv,)) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(5)
    assert outcomes.count("paid") == 20
    assert outcomes.count("refused") == 60
    _assert_books_exact(store.path)
