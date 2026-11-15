"""Fixes from the third review: one currency per book, API edit details, and
send's check of a half-finished posting.

Everything goes through create_app over TestClient on a real file database,
the real Store, or the real CLI main(), and the books are read back through a
separate SQLite connection, so only committed data counts.
"""

import re
import sqlite3
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from till.store import Invalid, Store
from till.web import create_app

LOCAL = "http://127.0.0.1:7740"
LOCK_TEXT = "Till keeps one currency per book. To work in another currency, start a new book."


def _sql(db, query, args=()):
    con = sqlite3.connect(db)
    try:
        return con.execute(query, args).fetchall()
    finally:
        con.close()


def _exec(db, *statements):
    """Write straight to the file, the way an older Till left it."""
    con = sqlite3.connect(db)
    try:
        for s in statements:
            con.execute(s)
        con.commit()
    finally:
        con.close()


def _error_of(response) -> str:
    return parse_qs(urlsplit(response.headers["location"]).query).get("error", [""])[0]


def _line(unit=10000, tax=0, description="Labour"):
    return [{"description": description, "qty_milli": 1000, "unit_cents": unit, "tax_bps": tax}]


@pytest.fixture
def book(tmp_path):
    db = tmp_path / "till.db"
    app = create_app(db)
    store = app.state.store
    cid = store.create_customer(name="Maria Chen", email="maria@example.com")
    client = TestClient(app, base_url=LOCAL, raise_server_exceptions=False, follow_redirects=False)
    return db, app, store, client, cid


def _client(app):
    return TestClient(app, base_url=LOCAL, raise_server_exceptions=False, follow_redirects=False)


# --- 1. one currency per book ------------------------------------------------------


def test_the_currency_cant_change_once_an_invoice_has_been_sent(book):
    """The reviewer's case: a sent and paid USD invoice, then a switch to EUR."""
    db, app, store, client, cid = book
    a = store.create_document("invoice", cid, "2026-11-01", _line(10000))
    store.send_invoice(a["id"])
    store.record_payment(a["id"], 10000, "2026-11-02")
    quote = store.create_document("quote", cid, "2026-11-01", _line(7000))
    before = _sql(db, "SELECT id, currency FROM invoices ORDER BY id")

    r = client.post("/settings", data={"name": "Shop", "currency": "EUR"})
    assert r.status_code == 303 and "one currency per book" in _error_of(r)
    with pytest.raises(Invalid, match="one currency per book"):
        store.save_company(currency="EUR")
    assert _sql(db, "SELECT currency FROM company") == [("USD",)]
    assert _sql(db, "SELECT id, currency FROM invoices ORDER BY id") == before
    # Nothing else in the refused save was written either.
    assert _sql(db, "SELECT name FROM company") != [("Shop",)]
    # Saving the same code (any case), or the rest of Settings, still works.
    assert store.save_company(currency=" usd ")["currency"] == "USD"
    r = client.post("/settings", data={"name": "Shop", "currency": "USD"})
    assert r.status_code == 303 and "saved=1" in r.headers["location"]
    r = client.post("/settings", data={"name": "Shop 2"})
    assert r.status_code == 303 and _sql(db, "SELECT name, currency FROM company") == [("Shop 2", "USD")]
    # The quote kept its currency; nothing anywhere needs the mixed note.
    assert store.get_invoice(quote["id"])["currency"] == "USD"
    for page in ("/", "/books", "/people"):
        assert "another currency" not in client.get(page).text, page


@pytest.mark.parametrize("make", ["sent", "partial", "paid", "void", "expense"])
def test_any_entry_in_the_books_locks_the_currency(book, make):
    db, app, store, client, cid = book
    if make == "expense":
        store.record_expense("City Hardware", 6320, "5100", "2026-11-01")
    else:
        inv = store.create_document("invoice", cid, "2026-11-01", _line(10000))
        store.send_invoice(inv["id"])
        if make == "partial":
            store.record_payment(inv["id"], 4000, "2026-11-02")
        if make == "paid":
            store.record_payment(inv["id"], 10000, "2026-11-02")
        if make == "void":
            store.void_invoice(inv["id"])
    with pytest.raises(Invalid, match="one currency per book"):
        store.save_company(currency="GBP")
    assert store.get_company()["currency"] == "USD"


@pytest.mark.parametrize("status", ["sent", "partial", "paid", "void"])
def test_a_sent_invoice_locks_the_currency_even_with_no_entry_in_the_books(book, status):
    """A book edited by hand, or left by an older Till, can have a sent
    invoice whose posting is gone; it was still sent in this currency."""
    db, app, store, client, cid = book
    store.create_document("invoice", cid, "2026-11-01", _line(10000))
    _exec(db, f"UPDATE invoices SET status='{status}'")
    assert _sql(db, "SELECT COUNT(*) FROM journal") == [(0,)]
    with pytest.raises(Invalid, match="one currency per book"):
        store.save_company(currency="GBP")


def test_settings_shows_the_currency_read_only_once_it_is_locked(book):
    db, app, store, client, cid = book
    page = client.get("/settings").text
    assert re.search(r'<select[^>]*name="currency"', page) and LOCK_TEXT not in page
    inv = store.create_document("invoice", cid, "2026-11-01", _line())
    store.send_invoice(inv["id"])
    page = client.get("/settings").text
    assert not re.search(r'<select[^>]*name="currency"', page)
    assert not re.search(r'<input[^>]*name="currency"', page)  # the form doesn't send it at all
    locked = re.search(r'<div id="currency-locked".*?</div>', page, re.S)
    assert locked and "USD" in locked.group(0) and LOCK_TEXT in locked.group(0)
    # The rest of the form still saves.
    r = client.post("/settings", data={"name": "Renamed", "email": "", "phone": "", "address": "", "tax_id": ""})
    assert r.status_code == 303 and _sql(db, "SELECT name FROM company") == [("Renamed",)]


def test_a_fresh_book_can_switch_currency_and_its_drafts_and_quotes_follow(book):
    db, app, store, client, cid = book
    draft = store.create_document("invoice", cid, "2026-11-01", _line(10000))
    quote = store.create_document("quote", cid, "2026-11-01", _line(7500))
    gone = store.create_document("invoice", cid, "2026-11-01", _line(100))
    store.delete_draft(gone["id"])
    r = client.post("/settings", data={"name": "Shop", "currency": "GBP"})
    assert r.status_code == 303 and "saved=1" in r.headers["location"]
    assert _sql(db, "SELECT currency FROM company") == [("GBP",)]
    assert _sql(db, "SELECT DISTINCT currency FROM invoices WHERE status = 'draft'") == [("GBP",)]
    # Only drafts follow: a deleted one keeps the currency it was made in.
    assert store.get_invoice(gone["id"])["currency"] == "USD"
    assert 'data-currency="£"' in client.get(f"/invoices/{draft['id']}/edit").text
    assert "£75.00" in client.get(f"/invoices/{quote['id']}/print").text
    # And again, both ways round, while nothing is in the books.
    assert store.save_company(currency="eur")["currency"] == "EUR"
    assert _sql(db, "SELECT DISTINCT currency FROM invoices WHERE status = 'draft'") == [("EUR",)]
    # A converted quote's invoice is in the book's currency too.
    inv = store.convert_quote(quote["id"])
    assert inv["currency"] == "EUR"
    assert "another currency" not in client.get("/").text


def test_a_retainer_issues_in_the_book_currency_once_its_first_invoice_is_sent(book):
    """The reviewer's case: a USD retainer, then a switch to JPY."""
    db, app, store, client, cid = book
    store.create_recurring(cid, "monthly", "2026-10-01", _line(50000))
    (first,) = store.tick_recurring("2026-10-01")["created"]
    assert first["currency"] == "USD"
    store.send_invoice(first["id"])
    with pytest.raises(Invalid, match="one currency per book"):
        store.save_company(currency="JPY")
    (second,) = store.tick_recurring("2026-11-01")["created"]
    assert (second["currency"], second["total_cents"]) == ("USD", 50000)
    assert _sql(db, "SELECT DISTINCT currency FROM invoices") == [("USD",)]


def test_the_api_refuses_an_invoice_in_another_currency(book):
    db, app, store, client, cid = book
    body = {"customer_id": cid, "lines": [{"description": "y", "unit": "5"}]}
    r = client.post("/api/invoices", json={**body, "currency": "GBP"})
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert "GBP" in detail and "USD" in detail and "one currency per book" in detail
    assert _sql(db, "SELECT COUNT(*) FROM invoices") == [(0,)]  # no row, no number used
    assert client.post("/api/invoices", json={**body, "currency": "dollars"}).status_code == 400
    for same in ({"currency": "USD"}, {"currency": " usd "}, {"currency": None}, {}):
        r = client.post("/api/invoices", json={**body, **same})
        assert r.status_code == 200, (same, r.text)
        assert r.json()["currency"] == "USD"
    assert [n for (n,) in _sql(db, "SELECT number FROM invoices ORDER BY id")] == [
        "INV-0001", "INV-0002", "INV-0003", "INV-0004"
    ]


def test_upgrading_a_book_with_a_lower_case_currency_makes_it_upper_case(tmp_path):
    """The reviewer's case: a main-era book whose Settings saved 'usd'."""
    from test_corrections import OLD_SCHEMA

    db = tmp_path / "old.db"
    con = sqlite3.connect(db)
    con.executescript(OLD_SCHEMA)
    con.executescript(
        """
        INSERT INTO accounts (code, name, type) VALUES ('1100','Accounts Receivable','asset'), ('4000','Income','income');
        INSERT INTO company (id, name, currency) VALUES (1, 'Old Shop', ' usd');
        INSERT INTO customers (id, name, created_at) VALUES (1, 'Old Customer', '2025-01-01T09:00:00');
        INSERT INTO invoices (id, number, kind, customer_id, status, issue_date, due_date, subtotal_cents, total_cents, created_at)
            VALUES (1, 'INV-0001', 'invoice', 1, 'sent', '2025-01-01', '2025-01-15', 5000, 5000, '2025-01-01T09:00:00');
        INSERT INTO invoice_lines (invoice_id, position, description, qty_milli, unit_cents, tax_bps, line_cents, tax_cents)
            VALUES (1, 1, 'Old work', 1000, 5000, 0, 5000, 0);
        INSERT INTO journal (id, posted_at, memo, ref_type, ref_id) VALUES (1, '2025-01-01', 'Invoice INV-0001', 'invoice', 1);
        INSERT INTO journal_lines (journal_id, account_code, debit_cents, credit_cents) VALUES (1, '1100', 5000, 0), (1, '4000', 0, 5000);
        """
    )
    con.commit()
    con.close()
    app = create_app(db)
    store = app.state.store
    assert _sql(db, "PRAGMA user_version") == [(7,)]
    assert _sql(db, "SELECT currency FROM company") == [("USD",)]
    assert _sql(db, "SELECT currency FROM invoices") == [("USD",)]
    client = _client(app)
    for page in ("/", "/books", "/people"):
        assert "another currency" not in client.get(page).text, page
    assert "$50.00" in client.get("/invoices/1").text
    store.save_company(currency="USD")  # the owner saves Settings: still the same currency
    assert "another currency" not in client.get("/books").text


def test_upgrading_a_schema_5_book_upper_cases_its_documents_too(tmp_path):
    db = tmp_path / "five.db"
    app = create_app(db)
    store = app.state.store
    cid = store.create_customer("A")
    inv = store.create_document("invoice", cid, "2026-11-01", _line())
    store.send_invoice(inv["id"])
    store.close()
    _exec(db, "UPDATE invoices SET currency='usd'", "UPDATE company SET currency='USD'", "PRAGMA user_version = 5")
    app = create_app(db)
    assert _sql(db, "SELECT currency FROM invoices") == [("USD",)]
    assert _sql(db, "PRAGMA user_version") == [(7,)]
    assert "another currency" not in _client(app).get("/books").text


def test_a_legacy_book_in_two_currencies_upgrades_and_keeps_the_note(tmp_path):
    """Schema 5 let a book change currency after sending; leave its data as it is."""
    db = tmp_path / "mixed.db"
    app = create_app(db)
    store = app.state.store
    cid = store.create_customer("A")
    inv = store.create_document("invoice", cid, "2026-11-01", _line(10000))
    store.send_invoice(inv["id"])
    store.close()
    _exec(db, "UPDATE company SET currency='gbp'", "PRAGMA user_version = 5")
    app = create_app(db)
    store = app.state.store
    assert _sql(db, "SELECT currency FROM company") == [("GBP",)]
    assert _sql(db, "SELECT currency FROM invoices") == [("USD",)]
    client = _client(app)
    for page in ("/", "/books"):
        text = client.get(page).text
        assert "another currency (USD)" in text, page
    assert "$100.00" in client.get(f"/invoices/{inv['id']}").text
    assert client.get("/api/books/aging").json()["other_currencies"] == ["USD"]
    # It is locked like any other book with entries.
    with pytest.raises(Invalid, match="one currency per book"):
        store.save_company(currency="USD")


# --- 3. API edit details ------------------------------------------------------------


def _draft(store, cid):
    return store.create_document("invoice", cid, "2026-11-01", _line(10000), due_date="2026-11-15", notes="First notes")


@pytest.mark.parametrize("value", [None, "", "   "])
def test_an_api_edit_refuses_a_blank_issue_date(book, value):
    db, app, store, client, cid = book
    iid = _draft(store, cid)["id"]
    before = _sql(db, "SELECT * FROM invoices WHERE id=?", (iid,))
    r = client.post(f"/api/invoices/{iid}/edit", json={"issue_date": value, "notes": "Changed"})
    assert r.status_code == 400, r.text
    assert "Issue date" in r.json()["detail"]
    assert _sql(db, "SELECT * FROM invoices WHERE id=?", (iid,)) == before


def test_an_api_edit_without_lines_leaves_the_lines_exactly_as_they_are(book):
    """Lines an older Till saved, with a line break in the description (today's
    form makes it one line), must come through an edit of the notes untouched."""
    db, app, store, client, cid = book
    iid = _draft(store, cid)["id"]
    _exec(db, "UPDATE invoice_lines SET description = 'Kitchen tap' || char(10) || '  - washer and seat' WHERE invoice_id = %d" % iid)
    lines = _sql(db, "SELECT * FROM invoice_lines WHERE invoice_id=? ORDER BY id", (iid,))
    r = client.post(f"/api/invoices/{iid}/edit", json={"notes": "New notes"})
    assert r.status_code == 200, r.text
    assert _sql(db, "SELECT * FROM invoice_lines WHERE invoice_id=? ORDER BY id", (iid,)) == lines
    assert _sql(db, "SELECT notes, total_cents FROM invoices WHERE id=?", (iid,)) == [("New notes", 10000)]
    # Sending lines still replaces them, checked as usual.
    r = client.post(f"/api/invoices/{iid}/edit", json={"lines": [{"description": "New", "unit": "20"}]})
    assert r.status_code == 200 and _sql(db, "SELECT description FROM invoice_lines WHERE invoice_id=?", (iid,)) == [("New",)]


def test_an_api_edit_reads_and_writes_in_one_transaction(book, monkeypatch):
    """Another write to the draft can't land between the edit reading it and
    writing it back: if it could, the edit would put the old notes back."""
    import threading

    db, app, store, client, cid = book
    iid = _draft(store, cid)["id"]
    real_get = store.get_invoice
    other = {}

    def write_meanwhile():  # another edit of the notes alone, done properly
        with store.transaction():
            cur = real_get(iid)
            store.update_document(iid, cur["customer_id"], cur["issue_date"], None, due_date=cur["due_date"], notes="Written meanwhile")

    def get_then_someone_writes(invoice_id):
        inv = real_get(invoice_id)
        if "thread" not in other and threading.current_thread() is not threading.main_thread():
            other["thread"] = threading.Thread(target=write_meanwhile)
            other["thread"].start()
            other["thread"].join(timeout=1)  # it only gets in if nothing holds the book
        return inv

    monkeypatch.setattr(store, "get_invoice", get_then_someone_writes)
    r = client.post(f"/api/invoices/{iid}/edit", json={"issue_date": "2026-11-02"})
    assert r.status_code == 200, r.text
    other["thread"].join(timeout=10)
    assert not other["thread"].is_alive()
    # The other write waited for the edit, then landed on top of it.
    assert _sql(db, "SELECT issue_date, notes FROM invoices WHERE id=?", (iid,)) == [("2026-11-02", "Written meanwhile")]


# --- 4. send checks the whole half-finished posting, not only its total ---------------


def _taxed_draft(store, cid):
    inv = store.create_document("invoice", cid, "2026-11-01", _line(10000, tax=1000))
    assert (inv["subtotal_cents"], inv["tax_cents"], inv["total_cents"]) == (10000, 1000, 11000)
    return inv


def _half_sent(store, inv, lines):
    """What an older send left when it stopped after posting: the entry, and
    the invoice still a draft."""
    with store.transaction():
        store._post("2026-11-01", f"Invoice {inv['number']}", lines, "invoice", inv["id"])


def test_send_refuses_a_posting_whose_income_and_tax_split_differs(book):
    """The reviewer's case: the right total on receivables, but all of it booked as income."""
    db, app, store, client, cid = book
    inv = _taxed_draft(store, cid)
    _half_sent(store, inv, [("1100", 11000, 0), ("4000", 0, 11000)])
    journal = _sql(db, "SELECT * FROM journal_lines ORDER BY id")
    r = client.post(f"/api/invoices/{inv['id']}/send")
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert "already in the books" in detail and "void it and make a new one" in detail
    assert "Income (4000) credit: $110.00 in the books, $100.00 on the invoice" in detail
    assert "Tax Payable (2200) credit: $0.00 in the books, $10.00 on the invoice" in detail
    assert "Accounts Receivable" not in detail  # that part matches
    r = client.post(f"/invoices/{inv['id']}/send")
    assert r.status_code == 303 and "Income (4000)" in _error_of(r)
    assert _sql(db, "SELECT status FROM invoices WHERE id=?", (inv["id"],)) == [("draft",)]
    assert _sql(db, "SELECT * FROM journal_lines ORDER BY id") == journal
    assert store.trial_balance().get("2200", 0) == 0


@pytest.mark.parametrize(
    "lines, named",
    [
        ([("1100", 11000, 0), ("4000", 0, 10000), ("1000", 0, 1000)], "Cash (1000) credit: $10.00 in the books, $0.00 on the invoice"),
        ([("1100", 12000, 0), ("1100", 0, 1000), ("4000", 0, 10000), ("2200", 0, 1000)], "Accounts Receivable (1100) credit"),
        ([("1100", 10000, 0), ("4000", 0, 10000)], "Accounts Receivable (1100) debit: $100.00 in the books, $110.00 on the invoice"),
    ],
)
def test_send_refuses_any_other_difference_in_the_posting(book, lines, named):
    db, app, store, client, cid = book
    inv = _taxed_draft(store, cid)
    _half_sent(store, inv, lines)
    with pytest.raises(Invalid) as err:
        store.send_invoice(inv["id"])
    assert named in str(err.value)
    assert store.get_invoice(inv["id"])["status"] == "draft"


@pytest.mark.parametrize(
    "lines",
    [
        [("1100", 6000, 0), ("1100", 5000, 0), ("4000", 0, 10000), ("2200", 0, 1000)],  # receivables split
        [("2200", 0, 1000), ("4000", 0, 10000), ("1100", 11000, 0)],  # another order
    ],
)
def test_send_accepts_a_posting_that_matches_however_its_rows_are_laid_out(book, lines):
    db, app, store, client, cid = book
    inv = _taxed_draft(store, cid)
    _half_sent(store, inv, lines)
    journal = _sql(db, "SELECT * FROM journal_lines ORDER BY id")
    assert client.post(f"/api/invoices/{inv['id']}/send").json()["status"] == "sent"
    assert _sql(db, "SELECT * FROM journal_lines ORDER BY id") == journal  # nothing posted twice
    assert store.journal_is_balanced()
