"""Corrections: edit or delete a draft, edit or archive a customer, reverse a
payment or an expense. Nothing deletes a posted journal entry; every
correction leaves the journal balanced and AR equal to the open balances.

Every correction is made through HTTP into create_app on a real file
database. The books are then checked through a separate SQLite connection,
so only committed data counts.
"""

import sqlite3
from datetime import date, timedelta
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from till.store import Store
from till.web import create_app

LOCAL = "http://127.0.0.1:7740"

# The schema as the first release shipped it (`git show main:till/store.py`),
# so the upgrade test opens a database exactly as the old code left it.
OLD_SCHEMA = '''
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS company (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  name TEXT NOT NULL,
  email TEXT,
  phone TEXT,
  address TEXT,
  tax_id TEXT,
  currency TEXT NOT NULL DEFAULT 'USD'
);

CREATE TABLE IF NOT EXISTS customers (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  email TEXT,
  phone TEXT,
  address TEXT,
  notes TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS vendors (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  email TEXT,
  phone TEXT
);

CREATE TABLE IF NOT EXISTS products (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  unit_cents INTEGER NOT NULL,
  tax_bps INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS jobs (
  id INTEGER PRIMARY KEY,
  customer_id INTEGER NOT NULL REFERENCES customers(id),
  title TEXT NOT NULL,
  status TEXT NOT NULL,
  notes TEXT,
  scheduled_for TEXT
);

CREATE TABLE IF NOT EXISTS invoices (
  id INTEGER PRIMARY KEY,
  number TEXT NOT NULL UNIQUE,
  kind TEXT NOT NULL CHECK (kind IN ('invoice','quote')),
  customer_id INTEGER NOT NULL REFERENCES customers(id),
  status TEXT NOT NULL,
  issue_date TEXT NOT NULL,
  due_date TEXT,
  sent_at TEXT,
  notes TEXT,
  po_number TEXT,
  job_id INTEGER REFERENCES jobs(id),
  subtotal_cents INTEGER NOT NULL DEFAULT 0,
  tax_cents INTEGER NOT NULL DEFAULT 0,
  total_cents INTEGER NOT NULL DEFAULT 0,
  paid_cents INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS invoice_lines (
  id INTEGER PRIMARY KEY,
  invoice_id INTEGER NOT NULL REFERENCES invoices(id) ON DELETE CASCADE,
  position INTEGER NOT NULL,
  description TEXT NOT NULL,
  qty_milli INTEGER NOT NULL,
  unit_cents INTEGER NOT NULL,
  tax_bps INTEGER NOT NULL DEFAULT 0,
  line_cents INTEGER NOT NULL,
  tax_cents INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS payments (
  id INTEGER PRIMARY KEY,
  invoice_id INTEGER NOT NULL REFERENCES invoices(id),
  amount_cents INTEGER NOT NULL,
  paid_at TEXT NOT NULL,
  method TEXT NOT NULL,
  notes TEXT,
  journal_id INTEGER
);

CREATE TABLE IF NOT EXISTS accounts (
  code TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  type TEXT NOT NULL CHECK (type IN ('asset','liability','equity','income','expense'))
);

CREATE TABLE IF NOT EXISTS journal (
  id INTEGER PRIMARY KEY,
  posted_at TEXT NOT NULL,
  memo TEXT NOT NULL,
  ref_type TEXT,
  ref_id INTEGER
);

CREATE TABLE IF NOT EXISTS journal_lines (
  id INTEGER PRIMARY KEY,
  journal_id INTEGER NOT NULL REFERENCES journal(id),
  account_code TEXT NOT NULL REFERENCES accounts(code),
  debit_cents INTEGER NOT NULL DEFAULT 0,
  credit_cents INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS expenses (
  id INTEGER PRIMARY KEY,
  vendor_id INTEGER REFERENCES vendors(id),
  vendor_name TEXT NOT NULL,
  amount_cents INTEGER NOT NULL,
  account_code TEXT NOT NULL REFERENCES accounts(code),
  spent_on TEXT NOT NULL,
  memo TEXT,
  journal_id INTEGER
);

CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY,
  title TEXT NOT NULL,
  start_at TEXT NOT NULL,
  end_at TEXT NOT NULL,
  customer_id INTEGER REFERENCES customers(id),
  job_id INTEGER REFERENCES jobs(id),
  location TEXT,
  notes TEXT
);

CREATE TABLE IF NOT EXISTS reviews (
  id INTEGER PRIMARY KEY,
  source TEXT NOT NULL,
  author TEXT NOT NULL,
  rating INTEGER NOT NULL,
  body TEXT NOT NULL,
  received_at TEXT NOT NULL,
  reply_draft TEXT,
  replied_at TEXT,
  status TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS deals (
  id INTEGER PRIMARY KEY,
  customer_id INTEGER REFERENCES customers(id),
  title TEXT NOT NULL,
  stage TEXT NOT NULL,
  value_cents INTEGER NOT NULL DEFAULT 0,
  notes TEXT
);

CREATE TABLE IF NOT EXISTS activities (
  id INTEGER PRIMARY KEY,
  customer_id INTEGER REFERENCES customers(id),
  kind TEXT NOT NULL,
  body TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS recurring (
  id INTEGER PRIMARY KEY,
  customer_id INTEGER NOT NULL REFERENCES customers(id),
  cadence TEXT NOT NULL,
  next_date TEXT NOT NULL,
  template_json TEXT NOT NULL,
  active INTEGER NOT NULL DEFAULT 1
);
'''


# --- helpers ----------------------------------------------------------------


def _sql(db, query, args=()):
    con = sqlite3.connect(db)
    try:
        return con.execute(query, args).fetchall()
    finally:
        con.close()


def _dump(db) -> dict:
    con = sqlite3.connect(db)
    try:
        tables = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        return {t: con.execute(f"SELECT * FROM {t} ORDER BY rowid").fetchall() for t in tables}
    finally:
        con.close()


def _assert_books_exact(db, client):
    """Journal balanced, and AR (1100) equal to the open invoices' balances,
    to the aging report, and to what /api/health reports."""
    debit, credit = _sql(db, "SELECT COALESCE(SUM(debit_cents),0), COALESCE(SUM(credit_cents),0) FROM journal_lines")[0]
    assert debit == credit
    ar = _sql(
        db,
        "SELECT COALESCE(SUM(debit_cents),0) - COALESCE(SUM(credit_cents),0) FROM journal_lines WHERE account_code='1100'",
    )[0][0]
    open_balances = sum(
        i["balance_cents"]
        for status in ("sent", "partial")
        for i in client.get(f"/api/invoices?status={status}").json()
    )
    assert ar == open_balances
    assert client.get("/api/books/aging").json()["total_cents"] == ar
    assert client.get("/api/health").json()["balanced"] is True
    unbalanced = _sql(
        db,
        """SELECT journal_id FROM journal_lines GROUP BY journal_id
           HAVING SUM(debit_cents) != SUM(credit_cents)""",
    )
    assert unbalanced == []


def _error_of(response) -> str:
    return parse_qs(urlsplit(response.headers["location"]).query).get("error", [""])[0]


def _id_from(response) -> int:
    return int(urlsplit(response.headers["location"]).path.rsplit("/", 1)[-1])


@pytest.fixture
def book(tmp_path):
    db = tmp_path / "till.db"
    app = create_app(db)
    store = app.state.store
    maria = store.create_customer(name="Maria Chen", email="maria@example.com")
    rafi = store.create_customer(name="Rafi Cole", email="rafi@example.com")
    client = TestClient(app, base_url=LOCAL, raise_server_exceptions=False, follow_redirects=False)
    return db, store, client, maria, rafi


def _new_draft(client, cid, *, kind="invoice", unit="100.00", qty="1"):
    r = client.post(
        "/quotes/new" if kind == "quote" else "/invoices/new",
        data={
            "customer_id": str(cid),
            "issue_date": "2026-11-01",
            "due_date": "2026-11-15",
            "description": "Labour",
            "qty": qty,
            "unit": unit,
            "tax_pct": "0",
        },
    )
    assert r.status_code == 303, r.text
    return _id_from(r)


def _sent(client, cid, unit="100.00"):
    iid = _new_draft(client, cid, unit=unit)
    assert client.post(f"/invoices/{iid}/send").status_code == 303
    return iid


# --- edit a draft -----------------------------------------------------------


def test_edit_a_draft_replaces_customer_dates_notes_and_every_line(book):
    db, store, client, maria, rafi = book
    iid = _new_draft(client, maria)
    number = store.get_invoice(iid)["number"]
    r = client.post(
        f"/invoices/{iid}/edit",
        data={
            "customer_id": str(rafi),
            "issue_date": "2026-11-03",
            "due_date": "2026-11-20",
            "notes": "Corrected",
            "description": ["Callout", "Hinge"],
            "qty": ["1", "2.5"],
            "unit": ["120.00", "8.00"],
            "tax_pct": ["8.25", "0"],
        },
    )
    assert r.status_code == 303 and urlsplit(r.headers["location"]).path == f"/invoices/{iid}"
    assert "error" not in r.headers["location"]
    inv = store.get_invoice(iid)
    assert inv["number"] == number and inv["status"] == "draft"
    assert (inv["customer_id"], inv["issue_date"], inv["due_date"], inv["notes"]) == (rafi, "2026-11-03", "2026-11-20", "Corrected")
    assert [(l["description"], l["qty_milli"], l["unit_cents"], l["tax_bps"]) for l in inv["lines"]] == [
        ("Callout", 1000, 12000, 825),
        ("Hinge", 2500, 800, 0),
    ]
    assert [l["position"] for l in inv["lines"]] == [1, 2]
    assert inv["subtotal_cents"] == 14000 and inv["tax_cents"] == 990 and inv["total_cents"] == 14990
    assert _sql(db, "SELECT COUNT(*) FROM journal")[0][0] == 0
    _assert_books_exact(db, client)


def test_the_edit_form_is_prefilled_with_the_draft(book):
    _, store, client, maria, rafi = book
    iid = _new_draft(client, rafi, unit="42.50", qty="1.5")
    page = client.get(f"/invoices/{iid}/edit")
    assert page.status_code == 200
    assert f'action="/invoices/{iid}/edit"' in page.text
    for shown in ("Labour", "1.5", "42.50", "2026-11-01", "2026-11-15", f'<option value="{rafi}" selected'):
        assert shown in page.text, shown
    assert f'href="/invoices/{iid}/edit"' in client.get(f"/invoices/{iid}").text


def test_a_failed_edit_changes_nothing_and_keeps_what_was_typed(book):
    db, _, client, maria, _ = book
    iid = _new_draft(client, maria)
    before = _dump(db)
    r = client.post(
        f"/invoices/{iid}/edit",
        data={"customer_id": str(maria), "issue_date": "2026-11-01", "description": ["Good", "Bad"], "qty": ["1", "1"], "unit": ["5", "five"]},
    )
    assert r.status_code == 400
    assert "Line 2 rate" in r.text and "five" in r.text and "Good" in r.text
    assert _dump(db) == before


def test_only_a_draft_can_be_edited(book):
    db, _, client, maria, _ = book
    iid = _sent(client, maria)
    before = _dump(db)
    r = client.post(f"/invoices/{iid}/edit", data={"customer_id": str(maria), "issue_date": "2026-11-01", "description": "X", "unit": "1"})
    assert r.status_code in (303, 400)
    assert _dump(db) == before
    assert client.get(f"/invoices/{iid}/edit").status_code == 400
    assert f'href="/invoices/{iid}/edit"' not in client.get(f"/invoices/{iid}").text
    assert client.post("/invoices/999/edit", data={"customer_id": str(maria)}).status_code == 404


def test_api_edit_of_a_draft(book):
    db, store, client, maria, rafi = book
    iid = _new_draft(client, maria)
    r = client.post(
        f"/api/invoices/{iid}/edit",
        json={"customer_id": rafi, "issue_date": "2026-11-02", "lines": [{"description": "New", "qty_milli": 2000, "unit_cents": 500}]},
    )
    assert r.status_code == 200, r.text
    assert r.json()["customer_id"] == rafi and r.json()["total_cents"] == 1000
    bad = client.post(f"/api/invoices/{iid}/edit", json={"customer_id": rafi, "issue_date": "2026-11-02", "lines": []})
    assert bad.status_code == 400
    assert store.get_invoice(iid)["total_cents"] == 1000


# --- delete a draft -----------------------------------------------------------


def test_deleting_a_draft_hides_it_and_its_number_is_never_reused(book):
    db, store, client, maria, _ = book
    first = _new_draft(client, maria)
    latest = _new_draft(client, maria)
    assert store.get_invoice(latest)["number"] == "INV-0002"
    r = client.post(f"/invoices/{latest}/delete")
    assert r.status_code == 303 and "error" not in r.headers["location"]
    # Deleting the newest draft must not free INV-0002 for the next invoice.
    nxt = _new_draft(client, maria)
    assert store.get_invoice(nxt)["number"] == "INV-0003"
    client.post(f"/invoices/{first}/delete")
    assert store.get_invoice(_new_draft(client, maria))["number"] == "INV-0004"
    numbers = [i["number"] for i in client.get("/api/invoices").json()]
    assert "INV-0002" not in numbers and "INV-0001" not in numbers
    assert "INV-0002" not in client.get("/invoices").text
    assert "INV-0002" not in client.get("/").text
    page = client.get(f"/invoices/{latest}")
    assert page.status_code == 200 and "deleted" in page.text
    assert f"/invoices/{latest}/send" not in page.text
    # Every number ever issued is still in the file, so the sequence has no unexplained gap.
    assert [r[0] for r in _sql(db, "SELECT number FROM invoices WHERE kind='invoice' ORDER BY id")] == [
        "INV-0001", "INV-0002", "INV-0003", "INV-0004"
    ]
    _assert_books_exact(db, client)


def test_only_a_draft_can_be_deleted_and_a_deleted_draft_stays_dead(book):
    db, _, client, maria, _ = book
    sent = _sent(client, maria)
    before = _dump(db)
    r = client.post(f"/invoices/{sent}/delete")
    assert r.status_code == 303 and _error_of(r)
    assert _dump(db) == before
    draft = _new_draft(client, maria)
    client.post(f"/invoices/{draft}/delete")
    before = _dump(db)
    for path in (f"/invoices/{draft}/send", f"/invoices/{draft}/void", f"/invoices/{draft}/delete"):
        r = client.post(path)
        assert r.status_code == 303 and _error_of(r), path
    assert client.post(f"/invoices/{draft}/edit", data={"customer_id": str(maria), "description": "x", "unit": "1"}).status_code in (303, 400)
    assert _dump(db) == before
    assert client.post("/invoices/999/delete").status_code == 404


def test_a_draft_quote_can_be_edited_and_deleted(book):
    db, store, client, maria, _ = book
    qid = _new_draft(client, maria, kind="quote")
    assert store.get_invoice(qid)["number"] == "QUO-0001"
    r = client.post(f"/invoices/{qid}/edit", data={"customer_id": str(maria), "issue_date": "2026-11-01", "description": "Gearbox", "unit": "750"})
    assert r.status_code == 303
    assert store.get_invoice(qid)["total_cents"] == 75000
    assert client.post(f"/invoices/{qid}/delete").status_code == 303
    assert client.get("/api/invoices?kind=quote").json() == []
    assert client.post(f"/quotes/{qid}/convert").status_code == 303
    assert store.list_invoices() == []
    assert store.get_invoice(_new_draft(client, maria, kind="quote"))["number"] == "QUO-0002"


def test_draft_offers_delete_and_sent_offers_void_as_plain_forms(book):
    _, _, client, maria, _ = book
    draft = _new_draft(client, maria)
    page = client.get(f"/invoices/{draft}").text
    assert f'action="/invoices/{draft}/delete"' in page
    assert f'action="/invoices/{draft}/void"' not in page  # one path for drafts: delete
    sent = _sent(client, maria)
    page = client.get(f"/invoices/{sent}").text
    assert f'action="/invoices/{sent}/void"' in page
    assert "confirm(" not in page and "onsubmit" not in page
    assert "<details" in page


# --- customers ------------------------------------------------------------------


def test_edit_a_customer_with_the_same_checks_as_adding_one(book):
    db, store, client, maria, _ = book
    r = client.post(
        f"/people/{maria}/edit",
        data={"name": "Maria Chen-Ortiz", "email": "mco@example.com", "phone": "555-0199", "address": "12 Oak Ave", "notes": "Gate code 4411"},
    )
    assert r.status_code == 303 and urlsplit(r.headers["location"]).path == f"/people/{maria}"
    assert "error" not in r.headers["location"]
    c = store.get_customer(maria)
    assert (c["name"], c["email"], c["phone"], c["address"], c["notes"]) == ("Maria Chen-Ortiz", "mco@example.com", "555-0199", "12 Oak Ave", "Gate code 4411")
    before = _dump(db)
    for bad in ({"name": " "}, {"name": "M", "email": "m@example.com?bcc=x@evil.example"}):
        r = client.post(f"/people/{maria}/edit", data=bad)
        assert r.status_code == 303 and urlsplit(r.headers["location"]).path == f"/people/{maria}" and _error_of(r)
    assert _dump(db) == before
    assert client.post("/people/999/edit", data={"name": "x"}).status_code == 404
    page = client.get(f"/people/{maria}").text
    assert f'action="/people/{maria}/edit"' in page and "Gate code 4411" in page
    api = client.post(f"/api/customers/{maria}/edit", json={"name": "Maria C", "email": None})
    assert api.status_code == 200 and api.json()["name"] == "Maria C" and api.json()["email"] is None


def test_archiving_a_customer_hides_them_from_pickers_and_keeps_their_history(book):
    db, store, client, maria, rafi = book
    iid = _sent(client, rafi)
    r = client.post(f"/people/{rafi}/archive")
    assert r.status_code == 303 and "error" not in r.headers["location"]
    for path in ("/invoices/new", "/jobs", "/calendar", "/invoices"):
        page = client.get(path).text
        assert f'<option value="{rafi}"' not in page, path
        assert f'<option value="{maria}"' in page, path
    people = client.get("/people").text
    assert f'href="/people/{rafi}"' in people  # still reachable, under Archived
    assert [c["id"] for c in client.get("/api/customers").json()] == [maria]
    person = client.get(f"/people/{rafi}")
    assert person.status_code == 200 and "Archived" in person.text
    assert store.get_invoice(iid)["customer_name"] == "Rafi Cole"
    assert client.get(f"/invoices/{iid}").status_code == 200
    _assert_books_exact(db, client)
    assert client.post(f"/people/{rafi}/restore").status_code == 303
    assert f'<option value="{rafi}"' in client.get("/invoices/new").text
    assert client.post("/people/999/archive").status_code == 404


# --- reverse a payment ------------------------------------------------------------


def test_reversing_a_payment_restores_the_balance_and_status(book):
    db, store, client, maria, _ = book
    iid = _sent(client, maria, unit="100.00")
    client.post(f"/invoices/{iid}/pay", data={"amount": "60.00", "paid_at": "2026-11-05"})
    client.post(f"/invoices/{iid}/pay", data={"amount": "40.00", "paid_at": "2026-11-06"})
    inv = store.get_invoice(iid)
    assert inv["status"] == "paid"
    first, second = inv["payments"]
    page = client.get(f"/invoices/{iid}").text
    assert f'action="/invoices/{iid}/payments/{second["id"]}/reverse"' in page
    _assert_books_exact(db, client)

    r = client.post(f"/invoices/{iid}/payments/{second['id']}/reverse")
    assert r.status_code == 303 and "error" not in r.headers["location"]
    inv = store.get_invoice(iid)
    assert (inv["status"], inv["paid_cents"], inv["balance_cents"]) == ("partial", 6000, 4000)
    _assert_books_exact(db, client)

    client.post(f"/invoices/{iid}/payments/{first['id']}/reverse")
    inv = store.get_invoice(iid)
    assert (inv["status"], inv["paid_cents"], inv["balance_cents"]) == ("sent", 0, 10000)
    _assert_books_exact(db, client)
    # The payment rows stay, marked reversed; the journal has the original and its reversal.
    rows = _sql(db, "SELECT amount_cents, reversed_at IS NOT NULL FROM payments WHERE invoice_id=? ORDER BY id", (iid,))
    assert rows == [(6000, 1), (4000, 1)]
    cash = _sql(db, "SELECT COALESCE(SUM(debit_cents - credit_cents),0) FROM journal_lines WHERE account_code='1000'")[0][0]
    assert cash == 0
    assert _sql(db, "SELECT COUNT(*) FROM journal WHERE ref_type='payment'")[0][0] == 2
    reversal = _sql(
        db,
        """SELECT jl.account_code, jl.debit_cents, jl.credit_cents FROM journal j JOIN journal_lines jl ON jl.journal_id=j.id
           WHERE j.ref_type='payment_reversal' ORDER BY j.id, jl.id LIMIT 2""",
    )
    assert reversal == [("1100", 4000, 0), ("1000", 0, 4000)]
    page = client.get(f"/invoices/{iid}").text
    assert "reversed" in page and f"/payments/{first['id']}/reverse" not in page

    before = _dump(db)
    again = client.post(f"/invoices/{iid}/payments/{first['id']}/reverse")
    assert again.status_code == 303 and "already" in _error_of(again)
    assert _dump(db) == before
    # With the payments reversed, the invoice can now be voided.
    assert client.post(f"/invoices/{iid}/void").status_code == 303
    assert store.get_invoice(iid)["status"] == "void"
    _assert_books_exact(db, client)


def test_a_payment_reversed_through_the_wrong_invoice_is_not_found(book):
    db, store, client, maria, _ = book
    a = _sent(client, maria)
    b = _sent(client, maria)
    client.post(f"/invoices/{a}/pay", data={"amount": "10"})
    pid = store.get_invoice(a)["payments"][0]["id"]
    before = _dump(db)
    assert client.post(f"/invoices/{b}/payments/{pid}/reverse").status_code == 404
    assert client.post(f"/invoices/{a}/payments/999/reverse").status_code == 404
    assert client.post("/api/payments/999/reverse").status_code == 404
    assert _dump(db) == before
    r = client.post(f"/api/payments/{pid}/reverse")
    assert r.status_code == 200 and r.json()["status"] == "sent"
    _assert_books_exact(db, client)


# --- reverse an expense -------------------------------------------------------------


def test_reversing_an_expense_nets_it_out_and_strikes_it_through(book):
    db, store, client, _, _ = book
    today = date.today().isoformat()
    client.post("/books/expenses", data={"vendor_name": "City Hardware", "amount": "3499", "account_code": "5100", "spent_on": today})
    client.post("/books/expenses", data={"vendor_name": "Paper", "amount": "12.00", "account_code": "5000", "spent_on": today})
    typo = [e for e in store.list_expenses() if e["vendor_name"] == "City Hardware"][0]
    books = client.get("/books").text
    assert f'action="/books/expenses/{typo["id"]}/reverse"' in books
    assert "$3,511.00" in books  # expenses this month before the correction

    r = client.post(f"/books/expenses/{typo['id']}/reverse")
    assert r.status_code == 303 and urlsplit(r.headers["location"]).path == "/books" and "error" not in r.headers["location"]
    pnl = client.get("/api/books/pnl").json()
    assert pnl["expense_cents"] == 1200
    assert store.trial_balance()["5100"] == 0
    assert store.trial_balance()["1000"] == -1200
    assert _sql(db, "SELECT amount_cents, reversed_at IS NOT NULL FROM expenses ORDER BY id") == [(349900, 1), (1200, 0)]
    books = client.get("/books").text
    assert "$12.00" in books and "$3,511.00" not in books
    assert 'class="reversed"' in books
    assert f'action="/books/expenses/{typo["id"]}/reverse"' not in books
    _assert_books_exact(db, client)

    before = _dump(db)
    again = client.post(f"/books/expenses/{typo['id']}/reverse")
    assert again.status_code == 303 and "already" in _error_of(again)
    assert client.post("/books/expenses/999/reverse").status_code == 404
    assert _dump(db) == before
    paper = [e for e in store.list_expenses() if e["vendor_name"] == "Paper"][0]
    assert client.post(f"/api/expenses/{paper['id']}/reverse").json()["reversed_at"]
    assert client.get("/api/books/pnl").json()["expense_cents"] == 0
    _assert_books_exact(db, client)


def test_an_earlier_month_expense_reversal_lands_in_that_month(book):
    """The reversal is dated like the entry it undoes, so every report window nets it out."""
    db, store, client, _, _ = book
    client.post("/books/expenses", data={"vendor_name": "Rent", "amount": "1200", "account_code": "5300", "spent_on": "2026-10-01"})
    eid = store.list_expenses()[0]["id"]
    client.post(f"/books/expenses/{eid}/reverse")
    assert client.get("/api/books/pnl?start=2026-10-01&end=2026-10-31").json()["expense_cents"] == 0
    assert client.get("/api/books/pnl?start=2026-11-01&end=2026-11-30").json()["expense_cents"] == 0


# --- all of it, in order ----------------------------------------------------------------


def test_every_correction_keeps_the_books_exact(book):
    db, store, client, maria, rafi = book
    steps = []
    a = _sent(client, maria, unit="250.00")
    steps.append("send a")
    client.post(f"/invoices/{a}/pay", data={"amount": "100.00"})
    steps.append("pay a")
    b = _new_draft(client, rafi, unit="80.00")
    client.post(f"/invoices/{b}/edit", data={"customer_id": str(maria), "issue_date": "2026-11-01", "description": ["x", "y"], "unit": ["10", "20"], "tax_pct": ["5", "5"]})
    steps.append("edit b")
    client.post(f"/invoices/{b}/send")
    client.post(f"/invoices/{b}/pay", data={"amount": "31.50"})
    steps.append("pay b in full")
    pb = store.get_invoice(b)["payments"][0]["id"]
    client.post(f"/invoices/{b}/payments/{pb}/reverse")
    steps.append("reverse b payment")
    c = _new_draft(client, rafi)
    client.post(f"/invoices/{c}/delete")
    steps.append("delete c")
    client.post("/books/expenses", data={"vendor_name": "V", "amount": "45.00"})
    client.post(f"/books/expenses/{store.list_expenses()[0]['id']}/reverse")
    steps.append("reverse expense")
    client.post(f"/people/{rafi}/edit", data={"name": "Rafi Cole Jr"})
    client.post(f"/people/{rafi}/archive")
    steps.append("edit + archive rafi")
    pa = store.get_invoice(a)["payments"][0]["id"]
    client.post(f"/invoices/{a}/payments/{pa}/reverse")
    client.post(f"/invoices/{a}/void")
    steps.append("reverse a payment, void a")
    for _ in steps:
        _assert_books_exact(db, client)
    assert store.get_invoice(a)["status"] == "void"
    assert store.get_invoice(b)["status"] == "sent" and store.get_invoice(b)["balance_cents"] == 3150
    _assert_books_exact(db, client)


# --- schema upgrade ---------------------------------------------------------------------


def test_migrate_upgrades_an_old_database_in_place(tmp_path):
    db = tmp_path / "old.db"
    con = sqlite3.connect(db)
    con.executescript(OLD_SCHEMA)
    con.executescript(
        """
        INSERT INTO accounts (code, name, type) VALUES ('1000','Cash','asset'), ('1100','Accounts Receivable','asset'),
            ('4000','Income','income'), ('5000','Operating Expenses','expense');
        INSERT INTO company (id, name, currency) VALUES (1, 'Old Shop', 'EUR');
        INSERT INTO customers (id, name, email, created_at) VALUES (1, 'Old Customer', 'old@example.com', '2025-01-01T09:00:00');
        INSERT INTO invoices (id, number, kind, customer_id, status, issue_date, due_date, subtotal_cents, tax_cents, total_cents, paid_cents, created_at)
            VALUES (1, 'INV-0007', 'invoice', 1, 'paid', '2025-01-01', '2025-01-15', 5000, 0, 5000, 5000, '2025-01-01T09:00:00');
        INSERT INTO invoice_lines (invoice_id, position, description, qty_milli, unit_cents, tax_bps, line_cents, tax_cents)
            VALUES (1, 1, 'Old work', 1000, 5000, 0, 5000, 0);
        INSERT INTO journal (id, posted_at, memo, ref_type, ref_id) VALUES (1, '2025-01-01', 'Invoice INV-0007', 'invoice', 1),
            (2, '2025-01-10', 'Payment INV-0007', 'payment', 1), (3, '2025-01-12', 'Expense', 'expense', 0);
        INSERT INTO journal_lines (journal_id, account_code, debit_cents, credit_cents) VALUES
            (1, '1100', 5000, 0), (1, '4000', 0, 5000), (2, '1000', 5000, 0), (2, '1100', 0, 5000), (3, '5000', 700, 0), (3, '1000', 0, 700);
        INSERT INTO payments (id, invoice_id, amount_cents, paid_at, method, journal_id) VALUES (1, 1, 5000, '2025-01-10', 'bank', 2);
        INSERT INTO expenses (id, vendor_name, amount_cents, account_code, spent_on, journal_id) VALUES (1, 'Old vendor', 700, '5000', '2025-01-12', 3);
        INSERT INTO recurring (id, customer_id, cadence, next_date, template_json, active)
            VALUES (1, 1, 'monthly', '2027-01-31', '{"lines": [{"description": "Retainer", "qty_milli": 1000, "unit_cents": 100, "tax_bps": 0}], "notes": ""}', 1);
        """
    )
    con.commit()
    assert con.execute("PRAGMA user_version").fetchone()[0] == 0
    con.close()
    before = _dump(db)

    app = create_app(db)
    store = app.state.store
    cols = lambda t: {r[1] for r in _sql(db, f"PRAGMA table_info({t})")}
    assert {"reversed_at", "reversal_journal_id"} <= cols("payments")
    assert {"reversed_at", "reversal_journal_id"} <= cols("expenses")
    assert "archived_at" in cols("customers")
    assert "deleted_at" in cols("invoices")
    assert _sql(db, "PRAGMA user_version")[0][0] == 7
    # Version 5: each invoice keeps its own currency, filled in from the
    # company's (what every document showed until then).
    assert _sql(db, "SELECT number, currency FROM invoices") == [("INV-0007", "EUR")]
    # Version 4: the company's default tax, payment instructions and sample flag.
    company = _sql(db, "SELECT default_tax_bps, payment_instructions, is_demo FROM company WHERE id=1")
    assert company == [(0, None, 0)]
    # Version 3: the retainer's anchor day comes from its next date, and the
    # indexes and the one-posting-per-invoice guard exist.
    assert _sql(db, "SELECT anchor_day FROM recurring WHERE id=1") == [(31,)]
    indexes = {r[0] for r in _sql(db, "SELECT name FROM sqlite_master WHERE type='index'")}
    assert {"invoice_lines_invoice", "journal_ref", "invoices_customer", "journal_invoice_posted_once"} <= indexes
    # Old rows are all still there, unchanged in every column they had
    # (migrate may add rows, like the accounts the old file lacked).
    for table, rows in before.items():
        if not rows:
            continue
        now = {r[: len(rows[0])] for r in _sql(db, f"SELECT * FROM {table} ORDER BY rowid")}
        assert set(rows) <= now, table
    # And the old data works with the new features.
    client = TestClient(app, base_url=LOCAL, raise_server_exceptions=False, follow_redirects=False)
    assert client.get("/").status_code == 200 and client.get("/books").status_code == 200
    assert client.post("/invoices/1/payments/1/reverse").status_code == 303
    assert store.get_invoice(1)["status"] == "sent"
    assert client.post("/books/expenses/1/reverse").status_code == 303
    assert store.trial_balance()["1000"] == 0
    assert store.get_invoice(_new_draft(client, 1))["number"] == "INV-0008"
    _assert_books_exact(db, client)
    # Opening it again is a no-op.
    store.close()
    again = Store(db)
    again.migrate()
    assert _sql(db, "PRAGMA user_version")[0][0] == 7
    assert again.get_invoice(1)["status"] == "sent"
    # The upgraded retainer keeps its 31st.
    made = again.tick_recurring(as_of="2027-03-31")["created"]
    assert [i["issue_date"] for i in made] == ["2027-01-31", "2027-02-28", "2027-03-31"]


def test_upgrade_refuses_an_old_book_with_an_invoice_posted_twice(tmp_path):
    db = tmp_path / "old.db"
    con = sqlite3.connect(db)
    con.executescript(OLD_SCHEMA)
    con.executescript(
        """
        INSERT INTO accounts (code, name, type) VALUES ('1100','Accounts Receivable','asset'), ('4000','Income','income');
        INSERT INTO company (id, name, currency) VALUES (1, 'Old Shop', 'USD');
        INSERT INTO customers (id, name, created_at) VALUES (1, 'Old Customer', '2025-01-01T09:00:00');
        INSERT INTO invoices (id, number, kind, customer_id, status, issue_date, total_cents, subtotal_cents, created_at)
            VALUES (1, 'INV-0004', 'invoice', 1, 'sent', '2025-01-01', 5000, 5000, '2025-01-01T09:00:00');
        INSERT INTO journal (id, posted_at, memo, ref_type, ref_id) VALUES
            (1, '2025-01-01', 'Invoice INV-0004', 'invoice', 1), (2, '2025-01-02', 'Invoice INV-0004', 'invoice', 1);
        INSERT INTO journal_lines (journal_id, account_code, debit_cents, credit_cents) VALUES
            (1, '1100', 5000, 0), (1, '4000', 0, 5000), (2, '1100', 5000, 0), (2, '4000', 0, 5000);
        """
    )
    con.commit()
    con.close()
    before = _dump(db)
    with pytest.raises(RuntimeError) as err:
        create_app(db)
    msg = str(err.value)
    assert "INV-0004" in msg and "1, 2" in msg and "Nothing was changed" in msg
    # Nothing was changed: no columns, no version, both entries still there.
    assert _sql(db, "PRAGMA user_version")[0][0] == 0
    assert "reversed_at" not in {r[1] for r in _sql(db, "PRAGMA table_info(payments)")}
    assert _dump(db) == before


def test_a_new_database_starts_at_schema_version_7(tmp_path):
    db = tmp_path / "new.db"
    create_app(db)
    assert _sql(db, "PRAGMA user_version")[0][0] == 7
    assert "currency" in {r[1] for r in _sql(db, "PRAGMA table_info(invoices)")}
    assert {"default_tax_bps", "payment_instructions", "is_demo"} <= {r[1] for r in _sql(db, "PRAGMA table_info(company)")}
    assert "reversed_at" in {r[1] for r in _sql(db, "PRAGMA table_info(payments)")}
