"""Fixes from the fourth review: `till restore` moves the current book aside
instead of judging it, and legacy books that mix currencies.

The restore tests run the real `till` CLI in a subprocess (or the real main())
with TILL_DB pointed at a scratch book, and read every book back through a
separate SQLite connection, so only what is on disk counts.
"""

import os
import socket
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

from till.store import Store

ROOT = Path(__file__).resolve().parent.parent
LINES = [{"description": "w" * 300, "qty_milli": 1000, "unit_cents": 1000, "tax_bps": 0}] * 3


def _sql(db, query, args=()):
    con = sqlite3.connect(db)
    try:
        return con.execute(query, args).fetchall()
    finally:
        con.close()


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _env(db):
    return dict(os.environ, TILL_DB=str(db), TILL_PORT=str(_free_port()), PYTHONPATH=str(ROOT))


def _till(db, *args):
    r = subprocess.run(
        [sys.executable, "-m", "till", "restore", *args], cwd=ROOT, env=_env(db), capture_output=True, text=True, timeout=120
    )
    text = r.stdout + r.stderr
    assert "Traceback" not in text, text
    return r.returncode, r.stdout, r.stderr


def _good_backup(tmp_path):
    """A real book with one customer, to restore from."""
    src = tmp_path / "good" / "till.db"
    store = Store(src)
    store.migrate()
    store.create_customer("From the backup")
    store.close()
    return src


def _snapshot(db):
    """Every file of the book, by name, byte for byte."""
    return {p.name: p.read_bytes() for p in db.parent.glob(db.name + "*") if p.is_file()}


def _content(db):
    """The book's data files byte for byte, leaving out the -shm index and any
    empty -wal that merely opening the book creates."""
    return {
        name: data
        for name, data in _snapshot(db).items()
        if not name.endswith("-shm") and not (name.endswith("-wal") and not data)
    }


def _replaced(db):
    (folder,) = (db.parent / "backups").glob(f"{db.stem}-replaced-*")
    return folder


def _before_copy(db):
    return list((db.parent / "backups").glob(f"{db.stem}-before-restore-*.db"))


def _restored_ok(db):
    assert _sql(db, "PRAGMA integrity_check") == [("ok",)]
    assert _sql(db, "SELECT name FROM customers") == [("From the backup",)]
    for side in ("-journal",):
        assert not Path(f"{db}{side}").exists()


# --- 1. a hot rollback journal goes with its database -------------------------------


def _hot_journal_book(tmp_path):
    """The reviewer's p8/p9 setup: a rollback-journal book whose writer died
    mid-transaction, leaving a hot journal beside it."""
    db = tmp_path / "data" / "till.db"
    store = Store(db)
    store.migrate()
    cid = store.create_customer(name="A")
    for _ in range(300):
        store.create_document("invoice", customer_id=cid, issue_date="2026-11-01", lines=LINES)
    store.db.execute("PRAGMA journal_mode=DELETE")
    store.close()
    lines = _sql(db, "SELECT count(*) FROM invoice_lines")[0][0]
    crash = f"""
import sqlite3, os
c = sqlite3.connect({str(db)!r}, isolation_level=None); c.execute('pragma cache_size=5'); c.execute('BEGIN')
c.execute("update invoice_lines set description = description || 'zzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzz'")
c.execute("delete from invoice_lines where id % 3 = 0")
os._exit(9)
"""
    subprocess.run([sys.executable, "-c", crash], check=False)
    assert Path(f"{db}-journal").stat().st_size > 0
    return db, lines


def test_restore_moves_a_hot_journal_aside_with_its_database_and_it_recovers(tmp_path):
    db, lines = _hot_journal_book(tmp_path)
    good = _good_backup(tmp_path)
    before = _snapshot(db)
    assert "till.db-journal" in before
    inodes = {p.name: p.stat().st_ino for p in db.parent.glob("till.db*")}

    # Without --force: Till can't tell whether a writer is live, and touches nothing.
    code, out, err = _till(db, str(good))
    assert code == 1
    assert "can't tell" in err and "--force" in err and "damaged" not in err
    assert _snapshot(db) == before
    assert not (db.parent / "backups").exists()

    code, out, err = _till(db, "--force", str(good))
    assert code == 0, err
    folder = _replaced(db)
    assert str(folder) in out
    # The database and its journal were moved together (the same files, not
    # copies), exactly as they were.
    assert {p.name: p.read_bytes() for p in folder.iterdir()} == before
    assert {p.name: p.stat().st_ino for p in folder.iterdir()} == inodes
    # Opening the moved database lets SQLite roll the journal back: every row is there.
    assert _sql(folder / "till.db", "PRAGMA integrity_check") == [("ok",)]
    assert _sql(folder / "till.db", "SELECT count(*) FROM invoice_lines") == [(lines,)]
    # The copy taken first is the recovered book too, and the original files weren't touched by it.
    (copy,) = _before_copy(db)
    assert _sql(copy, "PRAGMA integrity_check") == [("ok",)]
    assert _sql(copy, "SELECT count(*) FROM invoice_lines") == [(lines,)]
    _restored_ok(db)


# --- 2. a live WAL writer is "in use", never "damaged" ------------------------------

WRITER = """
import sys
from till.store import Store
p, ids = sys.argv[1], sys.argv[2]
s = Store(p); c = s.create_customer(name="Writer")
s.db.execute("pragma wal_autocheckpoint=20")
L = [{"description": "w" * 200, "qty_milli": 1000, "unit_cents": 1000, "tax_bps": 0}] * 5
out = open(ids, "a")
while True:
    d = s.create_document("invoice", customer_id=c, issue_date="2026-11-01", lines=L)
    s.send_invoice(d["id"])
    out.write(f"{d['id']}\\n"); out.flush()
"""


def _committed(ids):
    return {int(x) for x in ids.read_text().split()}


def test_restore_refuses_a_book_a_live_wal_writer_has_open_and_force_keeps_every_row(tmp_path):
    db = tmp_path / "data" / "till.db"
    store = Store(db)
    store.migrate()
    store.close()
    good = _good_backup(tmp_path)
    ids = tmp_path / "ids.txt"
    ids.write_text("")
    writer = subprocess.Popen([sys.executable, "-c", WRITER, str(db), str(ids)], cwd=ROOT, env=_env(db))
    try:
        deadline = time.time() + 30
        while len(_committed(ids)) < 20 and time.time() < deadline:
            time.sleep(0.05)
        assert writer.poll() is None and len(_committed(ids)) >= 20
        for _ in range(3):
            code, out, err = _till(db, str(good))
            assert code == 1
            assert "in use" in err and "--force" in err and "damaged" not in err
            assert not (db.parent / "backups").exists()

        code, out, err = _till(db, "--force", str(good))
        assert code == 0, err
        time.sleep(0.5)  # the writer carries on, into the moved files
    finally:
        writer.kill()
        writer.wait()
    committed = _committed(ids)
    folder = _replaced(db)
    assert (folder / "till.db").is_file() and (folder / "till.db-wal").is_file()
    assert _sql(folder / "till.db", "PRAGMA integrity_check") == [("ok",)]
    (copy,) = _before_copy(db)
    assert _sql(copy, "PRAGMA integrity_check") == [("ok",)]
    kept = {i for (i,) in _sql(folder / "till.db", "SELECT id FROM invoices WHERE status != 'draft'")}
    kept |= {i for (i,) in _sql(copy, "SELECT id FROM invoices WHERE status != 'draft'")}
    assert committed <= kept, sorted(committed - kept)[:5]
    _restored_ok(db)


# --- 3. a file SQLite can't read: "can't tell", then moved aside byte for byte -----


def test_restore_over_a_garbage_file_refuses_without_force_and_moves_it_aside_with_force(tmp_path):
    db = tmp_path / "data" / "till.db"
    db.parent.mkdir()
    db.write_bytes(b"this is not sqlite" * 300)
    Path(f"{db}-wal").write_bytes(b"left-over wal " * 50)
    Path(f"{db}-shm").write_bytes(b"left-over shm " * 50)
    good = _good_backup(tmp_path)
    before = _snapshot(db)

    code, out, err = _till(db, str(good))
    assert code == 1
    assert "can't tell" in err and "--force" in err and "damaged" not in err
    assert _snapshot(db) == before
    assert not (db.parent / "backups").exists()

    code, out, err = _till(db, "--force", str(good))
    assert code == 0, err
    folder = _replaced(db)
    assert str(folder) in out
    assert {p.name: p.read_bytes() for p in folder.iterdir()} == before
    assert _before_copy(db) == []  # it couldn't be copied, and said so
    assert "couldn't" in out + err
    _restored_ok(db)


# --- 4. a plain restore: the source's data, the old book kept twice ----------------


def test_restore_puts_the_source_data_in_and_keeps_the_old_book(tmp_path):
    db = tmp_path / "data" / "till.db"
    store = Store(db)
    store.migrate()
    store.create_customer(name="Old customer")
    store.close()
    good = _good_backup(tmp_path)
    code, out, err = _till(db, str(good))
    assert code == 0, err
    _restored_ok(db)
    (copy,) = _before_copy(db)
    assert _sql(copy, "SELECT name FROM customers") == [("Old customer",)]
    folder = _replaced(db)
    assert _sql(folder / "till.db", "SELECT name FROM customers") == [("Old customer",)]
    assert str(copy) in out and str(folder) in out
    # The source backup itself is untouched and still a good book.
    assert _sql(good, "SELECT name FROM customers") == [("From the backup",)]


# --- 5. a failure while copying the backup in puts the old files back --------------


def test_a_failure_copying_the_backup_in_puts_the_original_files_back(tmp_path, monkeypatch, capsys):
    import till.cli as cli

    db = tmp_path / "data" / "till.db"
    store = Store(db)
    store.migrate()
    store.create_customer(name="Old customer")
    store.close()
    # Leave committed rows in a WAL, the way a crashed Till does.
    crash = f"""
import sqlite3, os
c = sqlite3.connect({str(db)!r}, isolation_level=None); c.execute('pragma wal_autocheckpoint=0')
c.execute("insert into customers (name, created_at) values ('Only in the WAL', '2026-11-14T09:00:00')")
os._exit(9)
"""
    subprocess.run([sys.executable, "-c", crash], check=False)
    assert Path(f"{db}-wal").stat().st_size > 0
    good = _good_backup(tmp_path)
    before = _snapshot(db)
    real = cli._copy_book

    def failing(src, dst):
        if Path(dst) == db:
            Path(dst).write_bytes(b"half a copy")
            raise sqlite3.OperationalError("database or disk is full")
        return real(src, dst)

    monkeypatch.setattr(cli, "_copy_book", failing)
    monkeypatch.setenv("TILL_DB", str(db))
    monkeypatch.setenv("TILL_PORT", str(_free_port()))
    assert cli.main(["restore", str(good)]) == 1
    out = capsys.readouterr()
    assert "Traceback" not in out.out + out.err
    assert "put back" in out.err
    assert _snapshot(db) == before
    assert sorted(_sql(db, "SELECT name FROM customers")) == [("Old customer",), ("Only in the WAL",)]


@pytest.mark.parametrize("error", [OSError(28, "No space left on device"), RuntimeError("anything else")])
def test_any_failure_copying_the_backup_in_puts_the_original_files_back(tmp_path, monkeypatch, capsys, error):
    import till.cli as cli

    db = tmp_path / "data" / "till.db"
    store = Store(db)
    store.migrate()
    store.create_customer(name="Old customer")
    store.close()
    good = _good_backup(tmp_path)
    before = _content(db)

    def failing(src, dst):
        Path(dst).write_bytes(b"half a copy")
        raise error

    monkeypatch.setattr(cli, "_copy_book", failing)
    monkeypatch.setenv("TILL_DB", str(db))
    monkeypatch.setenv("TILL_PORT", str(_free_port()))
    assert cli.main(["restore", "--force", str(good)]) == 1
    out = capsys.readouterr()
    assert "put back" in out.err
    assert _content(db) == before
    assert _sql(db, "SELECT name FROM customers") == [("Old customer",)]


def test_ctrl_c_while_copying_the_backup_in_puts_the_original_files_back_then_stops(tmp_path, monkeypatch):
    import till.cli as cli

    db = tmp_path / "data" / "till.db"
    store = Store(db)
    store.migrate()
    store.create_customer(name="Old customer")
    store.close()
    good = _good_backup(tmp_path)
    before = _content(db)

    def interrupted(src, dst):
        Path(dst).write_bytes(b"half a copy")
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "_copy_book", interrupted)
    monkeypatch.setenv("TILL_DB", str(db))
    monkeypatch.setenv("TILL_PORT", str(_free_port()))
    with pytest.raises(KeyboardInterrupt):
        cli.main(["restore", "--force", str(good)])
    assert _content(db) == before
    assert _sql(db, "SELECT name FROM customers") == [("Old customer",)]


def test_restore_refuses_the_book_itself_as_the_backup(tmp_path):
    db = tmp_path / "data" / "till.db"
    store = Store(db)
    store.migrate()
    store.create_customer(name="Only copy")
    store.close()
    before = _snapshot(db)
    link = tmp_path / "alias.db"
    link.symlink_to(db)
    for source in (db, link):
        rc, _out, err = _till(db, "--force", str(source))
        assert rc == 1 and "is the book itself" in err
    assert _snapshot(db) == before
    assert not (db.parent / "backups").exists()


def test_two_restores_in_the_same_second_keep_both_old_books(tmp_path, monkeypatch, capsys):
    import till.cli as cli

    class Frozen(cli.datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 11, 15, 9, 0, 0)

    monkeypatch.setattr(cli, "datetime", Frozen)
    db = tmp_path / "data" / "till.db"
    store = Store(db)
    store.migrate()
    store.create_customer(name="First book")
    store.close()
    monkeypatch.setenv("TILL_DB", str(db))
    monkeypatch.setenv("TILL_PORT", str(_free_port()))
    first, second = _good_backup(tmp_path), tmp_path / "second" / "till.db"
    s2 = Store(second)
    s2.migrate()
    s2.create_customer("Second backup")
    s2.close()
    assert cli.main(["restore", str(first)]) == 0
    assert cli.main(["restore", str(second)]) == 0
    copies = _before_copy(db)
    assert len(copies) == 2
    kept = sorted(_sql(c, "SELECT name FROM customers")[0][0] for c in copies)
    assert kept == ["First book", "From the backup"]
    assert len(list((db.parent / "backups").glob(f"{db.stem}-replaced-*"))) == 2


# --- 6. legacy books that mix currencies --------------------------------------------

LOCAL = "http://127.0.0.1:7740"
LINE = [{"description": "Labour", "qty_milli": 1000, "unit_cents": 1000, "tax_bps": 0}]


def _error_of(response) -> str:
    from urllib.parse import parse_qs, urlsplit

    return parse_qs(urlsplit(response.headers["location"]).query).get("error", [""])[0]


@pytest.fixture
def book(tmp_path):
    from fastapi.testclient import TestClient

    from till.web import create_app

    db = tmp_path / "till.db"
    app = create_app(db)
    store = app.state.store
    cid = store.create_customer(name="Maria Chen", email="maria@example.com")
    client = TestClient(app, base_url=LOCAL, raise_server_exceptions=False, follow_redirects=False)
    yield db, store, client, cid
    store.close()


def _legacy_mixed(db, store, cid):
    """p2_lock's legacy book: a sent USD invoice, then an EUR draft and an EUR
    quote left by an older Till that let drafts keep another currency."""
    sent = store.create_document("invoice", customer_id=cid, issue_date="2026-11-01", lines=LINE)
    store.send_invoice(sent["id"])
    draft = store.create_document("invoice", customer_id=cid, issue_date="2026-11-01", lines=LINE)
    quote = store.create_document("quote", customer_id=cid, issue_date="2026-11-01", lines=LINE)
    con = sqlite3.connect(db)
    con.execute("UPDATE invoices SET currency='EUR' WHERE id IN (?, ?)", (draft["id"], quote["id"]))
    con.commit()
    con.close()
    return sent, draft, quote


def test_a_legacy_draft_in_another_currency_is_refused_at_send_and_works_after_an_edit(book):
    from till.store import Invalid

    db, store, client, cid = book
    sent, draft, quote = _legacy_mixed(db, store, cid)
    assert store.currency_locked()
    number = draft["number"]
    message = f"{number} is in EUR but this book is in USD. Edit the draft to save it in USD first."
    journal = _sql(db, "SELECT count(*) FROM journal")

    with pytest.raises(Invalid) as refused:
        store.send_invoice(draft["id"])
    assert str(refused.value) == message
    r = client.post(f"/api/invoices/{draft['id']}/send")
    assert r.status_code == 400 and r.json()["detail"] == message
    r = client.post(f"/invoices/{draft['id']}/send")
    assert r.status_code == 303 and _error_of(r) == message
    assert _sql(db, "SELECT status, currency FROM invoices WHERE id=?", (draft["id"],)) == [("draft", "EUR")]
    assert _sql(db, "SELECT count(*) FROM journal") == journal

    # The edit page says the save will change it; saving through the form does.
    page = client.get(f"/invoices/{draft['id']}/edit").text
    assert "This draft is in EUR. Saving it puts it in USD" in page
    form = {
        "customer_id": str(cid),
        "issue_date": "2026-11-01",
        "description": "Labour",
        "qty": "1",
        "unit": "10.00",
        "tax_pct": "0",
    }
    r = client.post(f"/invoices/{draft['id']}/edit", data=form)
    assert r.status_code == 303, r.text
    assert _sql(db, "SELECT currency FROM invoices WHERE id=?", (draft["id"],)) == [("USD",)]
    assert "Saving it puts it in" not in client.get(f"/invoices/{draft['id']}/edit").text
    r = client.post(f"/api/invoices/{draft['id']}/send")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "sent" and r.json()["currency"] == "USD"


def test_a_legacy_quote_in_another_currency_is_refused_at_convert_and_works_after_an_api_edit(book):
    from till.store import Invalid

    db, store, client, cid = book
    sent, draft, quote = _legacy_mixed(db, store, cid)
    message = f"{quote['number']} is in EUR but this book is in USD. Edit the draft to save it in USD first."
    invoices = _sql(db, "SELECT count(*) FROM invoices")

    with pytest.raises(Invalid) as refused:
        store.convert_quote(quote["id"])
    assert str(refused.value) == message
    r = client.post(f"/quotes/{quote['id']}/convert")
    assert r.status_code == 303 and _error_of(r) == message
    assert _sql(db, "SELECT count(*) FROM invoices") == invoices
    assert _sql(db, "SELECT status, currency FROM invoices WHERE id=?", (quote["id"],)) == [("draft", "EUR")]

    assert "This draft is in EUR" in client.get(f"/invoices/{quote['id']}/edit").text
    r = client.post(f"/api/invoices/{quote['id']}/edit", json={"notes": "Agreed on the phone"})
    assert r.status_code == 200 and r.json()["currency"] == "USD"
    r = client.post(f"/quotes/{quote['id']}/convert")
    assert r.status_code == 303, r.text
    new_id = int(r.headers["location"].rsplit("/", 1)[1])
    assert _sql(db, "SELECT kind, currency FROM invoices WHERE id=?", (new_id,)) == [("invoice", "USD")]
    assert store.send_invoice(new_id)["status"] == "sent"


def test_a_currency_switch_moves_only_drafts(book):
    db, store, client, cid = book
    draft = store.create_document("invoice", customer_id=cid, issue_date="2026-11-01", lines=LINE)
    draft_quote = store.create_document("quote", customer_id=cid, issue_date="2026-11-01", lines=LINE)
    # Quotes past draft on a book with nothing sent: one accepted (converted
    # into a draft invoice) and one voided. Till has no "sent" quote; these are
    # the quotes already given to a customer.
    accepted = store.create_document("quote", customer_id=cid, issue_date="2026-11-01", lines=LINE)
    from_quote = store.convert_quote(accepted["id"])
    voided = store.create_document("quote", customer_id=cid, issue_date="2026-11-01", lines=LINE)
    store.void_invoice(voided["id"])
    deleted = store.create_document("invoice", customer_id=cid, issue_date="2026-11-01", lines=LINE)
    store.delete_draft(deleted["id"])
    assert not store.currency_locked()

    r = client.post("/settings", data={"name": "Shop", "currency": "EUR"})
    assert r.status_code == 303 and "saved=1" in r.headers["location"], r.headers["location"]
    cur = dict(_sql(db, "SELECT id, currency FROM invoices"))
    assert cur[draft["id"]] == cur[draft_quote["id"]] == cur[from_quote["id"]] == "EUR"
    assert cur[accepted["id"]] == cur[voided["id"]] == cur[deleted["id"]] == "USD"


def _at_version_6(db, company, docs):
    con = sqlite3.connect(db)
    con.execute("UPDATE company SET currency=?", (company,))
    for iid, value in docs.items():
        con.execute("UPDATE invoices SET currency=? WHERE id=?", (value, iid))
    con.execute("PRAGMA user_version = 6")
    con.commit()
    con.close()


@pytest.mark.parametrize("company, expected", [("GBP", "GBP"), ("", "USD"), ("  ", "USD")])
def test_migration_fills_a_blank_or_missing_document_currency(tmp_path, company, expected):
    db = tmp_path / "till.db"
    store = Store(db)
    store.migrate()
    cid = store.create_customer(name="A")
    ids = [store.create_document("invoice", customer_id=cid, issue_date="2026-11-01", lines=LINE)["id"] for _ in range(4)]
    store.close()
    _at_version_6(db, company, {ids[0]: None, ids[1]: "", ids[2]: "   ", ids[3]: "CAD"})

    store = Store(db)
    store.migrate()
    store.close()
    assert _sql(db, "PRAGMA user_version") == [(7,)]
    assert _sql(db, "SELECT currency FROM invoices ORDER BY id") == [(expected,)] * 3 + [("CAD",)]
    assert _sql(db, "SELECT currency FROM company") == [(expected,)]
