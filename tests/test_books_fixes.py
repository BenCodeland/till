"""Books fixes: retainers keep their day, one bad retainer doesn't block the
rest, income posts on the invoice date, converted quotes get fresh terms,
indexes and a one-posting-per-invoice guard, a fast /people, and
`till backup` / `till restore`.

Everything goes through create_app over TestClient, the real Store on a file
database, or the real CLI main() with TILL_DB pointed at a scratch file. The
books are read back through a separate SQLite connection, so only committed
data counts.
"""

import socket
import sqlite3
import time
from datetime import date, timedelta
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from till.cli import main
from till.store import Store
from till.web import create_app

LOCAL = "http://127.0.0.1:7740"
LINE = {"description": "Retainer", "qty_milli": 1000, "unit_cents": 8000, "tax_bps": 0}


def _sql(db, query, args=()):
    con = sqlite3.connect(db)
    try:
        return con.execute(query, args).fetchall()
    finally:
        con.close()


def _error_of(response) -> str:
    return parse_qs(urlsplit(response.headers["location"]).query).get("error", [""])[0]


@pytest.fixture
def book(tmp_path):
    db = tmp_path / "till.db"
    app = create_app(db)
    store = app.state.store
    cid = store.create_customer(name="Maria Chen", email="maria@example.com")
    client = TestClient(app, base_url=LOCAL, raise_server_exceptions=False, follow_redirects=False)
    return db, store, cid, client


def _issue_dates(db):
    sql = "SELECT issue_date FROM invoices WHERE kind='invoice' ORDER BY issue_date"
    return [d for (d,) in _sql(db, sql)]


def _tick(client, as_of):
    r = client.post("/api/tick", json={"as_of": as_of})
    assert r.status_code == 200, r.text
    return r.json()


# --- (a) retainers keep their day of month ------------------------------------


def test_a_retainer_started_on_the_31st_keeps_its_day_every_month(book):
    db, _, cid, client = book
    r = client.post(
        "/api/recurring",
        json={"customer_id": cid, "cadence": "monthly", "next_date": "2027-01-31", "lines": [LINE]},
    )
    assert r.status_code == 200, r.text
    while _tick(client, "2028-04-30")["created"]:
        pass
    assert _issue_dates(db) == [
        "2027-01-31", "2027-02-28", "2027-03-31", "2027-04-30", "2027-05-31", "2027-06-30",
        "2027-07-31", "2027-08-31", "2027-09-30", "2027-10-31", "2027-11-30", "2027-12-31",
        "2028-01-31", "2028-02-29", "2028-03-31", "2028-04-30",
    ]
    assert _sql(db, "SELECT next_date, anchor_day FROM recurring") == [("2028-05-31", 31)]


def test_a_yearly_retainer_on_leap_day_comes_back_to_the_29th(book):
    db, _, cid, client = book
    client.post(
        "/api/recurring",
        json={"customer_id": cid, "cadence": "yearly", "next_date": "2028-02-29", "lines": [LINE]},
    )
    _tick(client, "2032-12-31")
    assert _issue_dates(db) == ["2028-02-29", "2029-02-28", "2030-02-28", "2031-02-28", "2032-02-29"]


def test_a_weekly_retainer_still_steps_seven_days(book):
    db, _, cid, client = book
    client.post(
        "/api/recurring",
        json={"customer_id": cid, "cadence": "weekly", "next_date": "2027-01-29", "lines": [LINE]},
    )
    _tick(client, "2027-02-12")
    assert _issue_dates(db) == ["2027-01-29", "2027-02-05", "2027-02-12"]


# --- (b) one bad retainer doesn't block the others ----------------------------


def _good_and_bad(db, client, cid):
    """Two retainers due 2026-11-01; the first one's saved template is broken,
    the way an old database or a hand edit could leave it."""
    bad = client.post(
        "/api/recurring",
        json={"customer_id": cid, "cadence": "monthly", "next_date": "2026-11-01", "lines": [LINE]},
    ).json()
    good = client.post(
        "/api/recurring",
        json={"customer_id": cid, "cadence": "monthly", "next_date": "2026-11-01", "lines": [LINE]},
    ).json()
    con = sqlite3.connect(db)
    con.execute(
        "UPDATE recurring SET template_json=? WHERE id=?",
        ('{"lines": [{"description": "Broken", "qty_milli": "lots", "unit_cents": 100}]}', bad["id"]),
    )
    con.commit()
    con.close()
    return bad["id"], good["id"]


def test_api_tick_issues_the_good_retainer_and_reports_the_bad_one(book):
    db, _, cid, client = book
    bad, good = _good_and_bad(db, client, cid)
    out = _tick(client, "2026-11-01")
    assert [i["issue_date"] for i in out["created"]] == ["2026-11-01"]
    assert len(out["failed"]) == 1
    assert out["failed"][0]["recurring_id"] == bad
    assert "quantity" in out["failed"][0]["error"]
    assert _sql(db, "SELECT id, next_date FROM recurring ORDER BY id") == [(bad, "2026-11-01"), (good, "2026-12-01")]
    assert _sql(db, "SELECT COUNT(*) FROM invoices")[0][0] == 1  # no junk draft for the bad one


def test_tick_page_shows_which_retainer_failed(book):
    db, store, cid, client = book
    bad, good = _good_and_bad(db, client, cid)
    con = sqlite3.connect(db)
    con.execute("UPDATE recurring SET next_date=? WHERE id IN (?,?)", (date.today().isoformat(), bad, good))
    con.commit()
    con.close()
    r = client.post("/tick")
    assert r.status_code == 303
    msg = _error_of(r)
    assert f"Retainer {bad}" in msg and "Maria Chen" in msg and "quantity" in msg
    assert _sql(db, "SELECT COUNT(*) FROM invoices")[0][0] == 1


def test_cli_tick_prints_the_failure_and_still_issues_the_rest(book, monkeypatch, capsys):
    db, store, cid, client = book
    bad, good = _good_and_bad(db, client, cid)
    con = sqlite3.connect(db)
    con.execute("UPDATE recurring SET next_date=? WHERE id IN (?,?)", (date.today().isoformat(), bad, good))
    con.commit()
    con.close()
    monkeypatch.setenv("TILL_DB", str(db))
    assert main(["tick"]) == 1
    out = capsys.readouterr()
    assert "INV-0001" in out.out
    assert f"retainer {bad}" in out.err and "quantity" in out.err
    assert _sql(db, "SELECT COUNT(*) FROM invoices")[0][0] == 1


# --- (c) sending posts on the issue date --------------------------------------


def test_send_posts_income_on_the_invoice_date_not_the_click_date(book):
    db, store, cid, client = book
    issued = (date.today() - timedelta(days=40)).isoformat()
    inv = client.post(
        "/api/invoices",
        json={"customer_id": cid, "issue_date": issued, "lines": [{"description": "Work", "qty_milli": 1000, "unit_cents": 10000}]},
    ).json()
    assert client.post(f"/invoices/{inv['id']}/send").status_code == 303
    assert _sql(db, "SELECT posted_at FROM journal WHERE ref_type='invoice' AND ref_id=?", (inv["id"],)) == [(issued,)]
    assert _sql(db, "SELECT sent_at FROM invoices WHERE id=?", (inv["id"],)) == [(date.today().isoformat(),)]
    pnl = client.get("/api/books/pnl", params={"start": issued, "end": issued}).json()
    assert pnl["income_cents"] == 10000
    # A void is dated the day it happens.
    assert client.post(f"/invoices/{inv['id']}/void").status_code == 303
    assert _sql(db, "SELECT posted_at FROM journal WHERE ref_type='invoice_void' AND ref_id=?", (inv["id"],)) == [
        (date.today().isoformat(),)
    ]


# --- (d) converting a quote ---------------------------------------------------


def _quote(client, cid, issue, due=None):
    body = {"kind": "quote", "customer_id": cid, "issue_date": issue, "lines": [{"description": "Work", "qty_milli": 1000, "unit_cents": 5000}]}
    if due:
        body["due_date"] = due
    r = client.post("/api/invoices", json=body)
    assert r.status_code == 200, r.text
    return r.json()["id"]


def test_a_late_accepted_quote_becomes_an_invoice_with_the_quotes_own_terms(book):
    db, store, cid, client = book
    today = date.today()
    # A 30-day quote, issued two months ago and long expired.
    qid = _quote(client, cid, (today - timedelta(days=60)).isoformat(), (today - timedelta(days=30)).isoformat())
    r = client.post(f"/quotes/{qid}/convert")
    assert r.status_code == 303
    inv = store.get_invoice(int(r.headers["location"].rsplit("/", 1)[-1]))
    assert inv["issue_date"] == today.isoformat()
    assert inv["due_date"] == (today + timedelta(days=30)).isoformat()
    assert not inv["overdue"]
    # A 7-day quote that hasn't expired yet still gets 7 days from today.
    q7 = _quote(client, cid, (today - timedelta(days=2)).isoformat(), (today + timedelta(days=5)).isoformat())
    inv7 = store.get_invoice(int(client.post(f"/quotes/{q7}/convert").headers["location"].rsplit("/", 1)[-1]))
    assert inv7["due_date"] == (today + timedelta(days=7)).isoformat()


def test_a_quote_without_a_due_date_converts_with_14_days(book):
    db, store, cid, client = book
    qid = _quote(client, cid, (date.today() - timedelta(days=3)).isoformat())
    inv = store.convert_quote(qid)
    assert inv["due_date"] == (date.today() + timedelta(days=14)).isoformat()


def test_a_void_quote_does_not_convert(book):
    db, store, cid, client = book
    qid = _quote(client, cid, date.today().isoformat())
    assert client.post(f"/invoices/{qid}/void").status_code == 303
    r = client.post(f"/quotes/{qid}/convert")
    assert r.status_code == 303 and "void" in _error_of(r)
    assert _sql(db, "SELECT COUNT(*) FROM invoices WHERE kind='invoice'")[0][0] == 0
    assert "Convert to invoice" not in client.get(f"/invoices/{qid}").text


# --- (e) indexes and one posting per invoice ----------------------------------


def test_hot_lookups_use_indexes(book):
    db, *_ = book
    plans = {
        "SELECT * FROM invoice_lines WHERE invoice_id=1": "invoice_lines",
        "SELECT * FROM payments WHERE invoice_id=1": "payments",
        "SELECT id FROM journal WHERE ref_type='invoice' AND ref_id=1": "journal",
        "SELECT * FROM journal_lines WHERE journal_id=1": "journal_lines",
        "SELECT * FROM journal_lines WHERE account_code='1000'": "journal_lines",
        "SELECT * FROM invoices WHERE customer_id=1": "invoices",
        "SELECT * FROM invoices WHERE kind='invoice' AND status='sent'": "invoices",
        "SELECT * FROM events WHERE start_at >= '2026-01-01'": "events",
        "SELECT * FROM jobs WHERE customer_id=1": "jobs",
    }
    for query, table in plans.items():
        detail = " ".join(r[3] for r in _sql(db, "EXPLAIN QUERY PLAN " + query))
        assert "USING" in detail and "INDEX" in detail, (query, detail)


def test_an_invoice_can_only_be_posted_once(book):
    db, store, cid, client = book
    inv = client.post(
        "/api/invoices",
        json={"customer_id": cid, "issue_date": "2026-11-01", "lines": [{"description": "Work", "qty_milli": 1000, "unit_cents": 100}]},
    ).json()
    client.post(f"/api/invoices/{inv['id']}/send")
    con = sqlite3.connect(db)
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("INSERT INTO journal (posted_at, memo, ref_type, ref_id) VALUES ('2026-11-01','again','invoice',?)", (inv["id"],))
    con.close()
    # The void's reversal is a different kind of entry and is still allowed.
    assert client.post(f"/invoices/{inv['id']}/void").status_code == 303
    assert _sql(db, "SELECT ref_type FROM journal WHERE ref_id=? ORDER BY id", (inv["id"],)) == [("invoice",), ("invoice_void",)]


# --- (f) /people in one query -------------------------------------------------


def test_people_page_is_fast_on_a_big_book(tmp_path):
    db = tmp_path / "big.db"
    app = create_app(db)
    store = app.state.store
    owed = {}
    with store.transaction():
        cids = [store.create_customer(name=f"Customer {n:03d}") for n in range(300)]
        for n in range(3000):
            cid = cids[n % 300]
            inv = store.create_document(
                "invoice",
                customer_id=cid,
                issue_date="2026-01-01",
                due_date="2026-01-15",  # the sent ones are all overdue, so Today lists 2,000
                lines=[{"description": "Work", "qty_milli": 1000, "unit_cents": 1000 + n}],
            )
            if n % 3:
                store.send_invoice(inv["id"])
                if n % 3 == 2:
                    store.record_payment(inv["id"], 500, "2026-01-02")
                owed[cid] = owed.get(cid, 0) + inv["total_cents"] - (500 if n % 3 == 2 else 0)
    client = TestClient(app, base_url=LOCAL)
    client.get("/people")  # warm up templates
    start = time.perf_counter()
    r = client.get("/people")
    elapsed = time.perf_counter() - start
    print(f"/people on 300 customers / 3,000 invoices: {elapsed:.3f}s")
    assert r.status_code == 200
    assert elapsed < 1.0
    # And the amounts are right: Customer 001's row shows what it owes.
    row = r.text.split("Customer 001</a>", 1)[1].split("</tr>", 1)[0]
    cents = owed[cids[1]]
    assert f"${cents // 100:,}.{cents % 100:02d}" in row
    # The person page and Today stay quick too.
    for path in (f"/people/{cids[1]}", "/"):
        start = time.perf_counter()
        assert client.get(path).status_code == 200
        assert time.perf_counter() - start < 1.0, path


def test_person_page_lists_only_that_customers_documents(book):
    db, store, cid, client = book
    other = store.create_customer(name="Rafi Cole")
    mine = client.post("/api/invoices", json={"customer_id": cid, "issue_date": "2026-11-01", "lines": [LINE]}).json()
    theirs = client.post("/api/invoices", json={"customer_id": other, "issue_date": "2026-11-01", "lines": [LINE]}).json()
    page = client.get(f"/people/{cid}").text
    assert mine["number"] in page and theirs["number"] not in page


# --- (h) backup and restore ---------------------------------------------------


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _names(db):
    return [n for (n,) in _sql(db, "SELECT name FROM customers ORDER BY id")]


def test_backup_while_another_connection_is_writing(book, tmp_path, monkeypatch, capsys):
    db, store, cid, client = book
    monkeypatch.setenv("TILL_DB", str(db))
    # A writer that has committed rows still sitting in the WAL (no
    # checkpoint) and holds an open write transaction with more.
    writer = sqlite3.connect(db, isolation_level=None)
    writer.execute("PRAGMA wal_autocheckpoint=0")
    writer.execute("INSERT INTO customers (name, created_at) VALUES ('Committed in WAL', '2026-11-14T09:00:00')")
    assert (tmp_path / "till.db-wal").stat().st_size > 0
    writer.execute("BEGIN IMMEDIATE")
    writer.execute("INSERT INTO customers (name, created_at) VALUES ('Not committed', '2026-11-14T09:00:00')")
    out = tmp_path / "copy.db"
    assert main(["backup", str(out)]) == 0
    printed = capsys.readouterr().out
    assert str(out) in printed
    assert _names(out) == ["Maria Chen", "Committed in WAL"]
    assert _sql(out, "PRAGMA integrity_check") == [("ok",)]
    writer.execute("ROLLBACK")
    writer.close()


def test_backup_without_a_path_goes_to_a_timestamped_file_next_to_the_db(book, monkeypatch, capsys):
    db, *_ = book
    monkeypatch.setenv("TILL_DB", str(db))
    assert main(["backup"]) == 0
    made = list((db.parent / "backups").glob("till-*.db"))
    assert len(made) == 1
    stamp = made[0].stem.removeprefix("till-")
    assert len(stamp) == 15 and stamp[8] == "-" and stamp.replace("-", "").isdigit()
    assert str(made[0]) in capsys.readouterr().out
    assert _names(made[0]) == ["Maria Chen"]


def test_backup_refuses_a_missing_database(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("TILL_DB", str(tmp_path / "nope.db"))
    assert main(["backup", str(tmp_path / "out.db")]) == 1
    assert not (tmp_path / "nope.db").exists()


def test_restore_puts_the_backup_back_and_keeps_a_copy_of_what_it_replaced(book, tmp_path, monkeypatch, capsys):
    db, store, cid, client = book
    monkeypatch.setenv("TILL_DB", str(db))
    monkeypatch.setenv("TILL_PORT", str(_free_port()))
    saved = tmp_path / "saved.db"
    assert main(["backup", str(saved)]) == 0
    store.create_customer(name="Added after the backup")
    before = _names(db)
    # An open store has the book in use; restore waits until it's closed.
    assert main(["restore", str(saved)]) == 1
    assert "in use" in capsys.readouterr().err
    store.close()
    assert main(["restore", str(saved)]) == 0
    assert _names(db) == ["Maria Chen"]
    assert _sql(db, "PRAGMA integrity_check") == [("ok",)]
    kept = list((db.parent / "backups").glob("till-before-restore-*.db"))
    assert len(kept) == 1 and _names(kept[0]) == before
    (moved,) = (db.parent / "backups").glob("till-replaced-*")
    assert _names(moved / "till.db") == before


def test_restore_refuses_while_the_server_is_running(book, tmp_path, monkeypatch, capsys):
    db, store, cid, client = book
    monkeypatch.setenv("TILL_DB", str(db))
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    monkeypatch.setenv("TILL_PORT", str(listener.getsockname()[1]))
    saved = tmp_path / "saved.db"
    main(["backup", str(saved)])
    store.create_customer(name="Keep me")
    try:
        assert main(["restore", str(saved)]) == 1
        assert "running" in capsys.readouterr().err
        assert "Keep me" in _names(db)
        assert main(["restore", "--force", str(saved)]) == 0
        assert _names(db) == ["Maria Chen"]
    finally:
        listener.close()


def test_restore_refuses_a_file_that_is_not_a_till_book(book, tmp_path, monkeypatch, capsys):
    db, *_ = book
    monkeypatch.setenv("TILL_DB", str(db))
    monkeypatch.setenv("TILL_PORT", str(_free_port()))
    junk = tmp_path / "junk.db"
    junk.write_bytes(b"not a database at all" * 100)
    assert main(["restore", str(junk)]) == 1
    assert _names(db) == ["Maria Chen"]


# --- (i) seed --force takes the WAL files with it -----------------------------


def test_seed_force_removes_the_wal_and_shm_files(tmp_path, monkeypatch):
    """`seed --force` while something still has the old book open (the
    server): the new book must not share the old book's -wal and -shm, which
    still belong to that open connection."""
    db = tmp_path / "till.db"
    monkeypatch.setenv("TILL_DB", str(db))
    assert main(["seed"]) == 0
    server = sqlite3.connect(db, isolation_level=None)
    server.execute("PRAGMA wal_autocheckpoint=0")
    server.execute("INSERT INTO customers (name, created_at) VALUES ('Real customer', '2026-11-14T09:00:00')")
    old = {s: (tmp_path / f"till.db{s}").stat().st_ino for s in ("-wal", "-shm")}
    assert main(["seed", "--force"]) == 0
    for suffix, inode in old.items():
        side = tmp_path / f"till.db{suffix}"
        # The old files are still open in `server`, so their inodes can't be reused.
        assert not side.exists() or side.stat().st_ino != inode, suffix
    # The old connection carries on without an I/O error, and neither book is damaged.
    server.execute("INSERT INTO customers (name, created_at) VALUES ('Written late', '2026-11-14T09:00:00')")
    server.close()
    assert _sql(db, "PRAGMA integrity_check") == [("ok",)]
    assert "Real customer" not in _names(db) and "Written late" not in _names(db)
