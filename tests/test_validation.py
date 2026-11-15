"""Validate on the way in, and never answer a bad input with a 500.

Every case goes through the real HTTP routes into a real file database. The
battery comes from the security review's bad-input probe: each input must be
refused with 400 or 404, or redirected back (303) with an ?error= message, and
must leave the database byte-for-byte unchanged.
"""

import sqlite3
from datetime import date, timedelta
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from till.money import parse_money, parse_percent, parse_qty
from till.seed import seed
from till.store import Store
from till.web import create_app

LOCAL = "http://127.0.0.1:7740"
TODAY = date.today().isoformat()

# Seeded ids: customers 1-4; invoice 1 sent (open), 2 paid, 3 draft; 4 quote; job 1; review 1; recurring 1.
OPEN, DRAFT, QUOTE = 1, 3, 4


# --- helpers ----------------------------------------------------------------


def _dump(db) -> dict:
    con = sqlite3.connect(db)
    try:
        tables = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        return {t: con.execute(f"SELECT * FROM {t} ORDER BY rowid").fetchall() for t in tables}
    finally:
        con.close()


@pytest.fixture(scope="module")
def seeded_template(tmp_path_factory):
    path = tmp_path_factory.mktemp("seed") / "seed.db"
    store = Store(path)
    store.migrate()
    seed(store)
    store.close()
    return path


@pytest.fixture
def book(tmp_path, seeded_template):
    db = tmp_path / "till.db"
    src, dst = sqlite3.connect(seeded_template), sqlite3.connect(db)
    src.backup(dst)
    src.close()
    dst.close()
    app = create_app(db)
    client = TestClient(app, base_url=LOCAL, raise_server_exceptions=False, follow_redirects=False)
    return db, app.state.store, client


def _error_of(response) -> str:
    return parse_qs(urlsplit(response.headers["location"]).query).get("error", [""])[0]


# --- the battery ------------------------------------------------------------

F = dict
J = "json"

# (label, method, path, form-or-json, kind)  kind: "form" | "json" | "get"
BAD_INPUTS = [
    # HTML form routes
    ("invoice no customer", "/invoices/new", F(description="x", unit="1")),
    ("invoice customer abc", "/invoices/new", F(customer_id="abc", description="x", unit="1")),
    ("invoice customer 999", "/invoices/new", F(customer_id="999", description="x", unit="1")),
    ("invoice no lines", "/invoices/new", F(customer_id="1")),
    ("invoice unit abc", "/invoices/new", F(customer_id="1", description="x", unit="abc")),
    ("invoice unit -5", "/invoices/new", F(customer_id="1", description="x", unit="-5")),
    ("invoice unit 0.-9", "/invoices/new", F(customer_id="1", description="x", unit="0.-9")),
    ("invoice unit 1_000", "/invoices/new", F(customer_id="1", description="x", unit="1_000")),
    ("invoice unit 1. 5", "/invoices/new", F(customer_id="1", description="x", unit="1. 5")),
    ("invoice unit 30 nines", "/invoices/new", F(customer_id="1", description="x", unit="9" * 30)),
    ("invoice tax_bps inf", "/invoices/new", F(customer_id="1", description="x", unit="1", tax_bps="inf")),
    ("invoice tax_bps 1e30", "/invoices/new", F(customer_id="1", description="x", unit="1", tax_bps="1e30")),
    ("invoice tax_pct 101", "/invoices/new", F(customer_id="1", description="x", unit="1", tax_pct="101")),
    ("invoice tax_pct 8.255", "/invoices/new", F(customer_id="1", description="x", unit="1", tax_pct="8.255")),
    ("invoice qty abc", "/invoices/new", F(customer_id="1", description="x", qty="abc")),
    ("invoice qty -0.5", "/invoices/new", F(customer_id="1", description="x", qty="-0.5", unit="1")),
    ("invoice qty 0", "/invoices/new", F(customer_id="1", description="x", qty="0", unit="1")),
    ("invoice job 999", "/invoices/new", F(customer_id="1", description="x", unit="1", job_id="999")),
    ("invoice due 1", "/invoices/new", F(customer_id="1", description="x", unit="10", due_date="1")),
    ("invoice due garbage", "/invoices/new", F(customer_id="1", description="x", unit="10", due_date="garbage")),
    ("invoice issue 14/11/2026", "/invoices/new", F(customer_id="1", description="x", unit="10", issue_date="14/11/2026")),
    ("invoice due 2026-02-30", "/invoices/new", F(customer_id="1", description="x", unit="10", due_date="2026-02-30")),
    ("invoice due before issue", "/invoices/new", F(customer_id="1", description="x", unit="10", issue_date="2026-11-10", due_date="2026-11-01")),
    ("quote bad unit", "/quotes/new", F(customer_id="1", description="x", unit="abc")),
    ("quote no customer", "/quotes/new", F()),
    ("send 999", "/invoices/999/send", None),
    ("pay 999", "/invoices/999/pay", F(amount="1")),
    ("void 999", "/invoices/999/void", None),
    ("send abc", "/invoices/abc/send", None),
    ("pay amount abc", f"/invoices/{OPEN}/pay", F(amount="abc")),
    ("pay amount 1.999", f"/invoices/{OPEN}/pay", F(amount="1.999")),
    ("pay amount 1. 5", f"/invoices/{OPEN}/pay", F(amount="1. 5")),
    ("pay amount 12,50", f"/invoices/{OPEN}/pay", F(amount="12,50")),
    ("pay paid_at garbage", f"/invoices/{OPEN}/pay", F(amount="1", paid_at="garbage")),
    ("pay paid_at last tuesday", f"/invoices/{OPEN}/pay", F(amount="1", paid_at="last tuesday")),
    ("convert 999", "/quotes/999/convert", None),
    ("convert an invoice", f"/quotes/{DRAFT}/convert", None),
    ("recurring no customer", "/recurring", F(description="x", unit="1")),
    ("recurring cadence daily", "/recurring", F(customer_id="1", cadence="daily", unit="1")),
    ("recurring customer 999", "/recurring", F(customer_id="999", unit="1")),
    ("recurring next_date 0", "/recurring", F(customer_id="1", next_date="0", unit="1")),
    ("recurring unit abc", "/recurring", F(customer_id="1", unit="abc")),
    ("recurring tax_pct abc", "/recurring", F(customer_id="1", unit="1", tax_pct="abc")),
    ("recurring stop 999", "/recurring/999/stop", None),
    ("job no customer", "/jobs/new", F(title="x")),
    ("job customer 999", "/jobs/new", F(customer_id="999", title="x")),
    ("job start garbage", "/jobs/new", F(customer_id="1", title="x", scheduled_for="2026-11-14", start_time="zz")),
    ("job end before start", "/jobs/new", F(customer_id="1", title="x", scheduled_for="2026-11-14", start_time="15:00", end_time="13:00")),
    ("job day garbage", "/jobs/new", F(customer_id="1", title="x", scheduled_for="garbage")),
    ("job status 999", "/jobs/999/status", F(status="done")),
    ("job status hacked", "/jobs/1/status", F(status="hacked")),
    ("job invoice 999", "/jobs/999/invoice", None),
    ("expense amount abc", "/books/expenses", F(vendor_name="v", amount="abc")),
    ("expense amount -5", "/books/expenses", F(vendor_name="v", amount="-5")),
    ("expense amount 12.345", "/books/expenses", F(vendor_name="v", amount="12.345")),
    ("expense account 1000", "/books/expenses", F(vendor_name="v", amount="5", account_code="1000")),
    ("expense INT64 max", "/books/expenses", F(vendor_name="v", amount="92233720368547758.07")),
    ("expense 20 nines", "/books/expenses", F(vendor_name="v", amount="9" * 20)),
    ("expense over cap", "/books/expenses", F(vendor_name="v", amount="10000000000.01")),
    ("expense spent_on garbage", "/books/expenses", F(vendor_name="v", amount="5", spent_on="garbage")),
    ("expense spent_on 2026-13-45", "/books/expenses", F(vendor_name="v", amount="5", spent_on="2026-13-45")),
    ("calendar customer abc", "/calendar", F(title="x", customer_id="abc")),
    ("calendar customer 999", "/calendar", F(title="x", customer_id="999")),
    ("calendar day garbage", "/calendar", F(title="x", day="garbage")),
    ("calendar end before start", "/calendar", F(title="x", day="2026-11-20", start_time="15:00", end_time="13:00")),
    ("review rating abc", "/reviews", F(author="a", body="b", rating="abc")),
    ("review rating 99", "/reviews", F(author="a", body="b", rating="99")),
    ("review rating -3", "/reviews", F(author="a", body="b", rating="-3")),
    ("review rating 10**15", "/reviews", F(author="a", body="b", rating=str(10**15))),
    ("review draft 999", "/reviews/999/draft", F(body="x")),
    ("review replied 999", "/reviews/999/replied", None),
    ("deal value abc", "/pipeline", F(title="d", value="abc")),
    ("deal customer abc", "/pipeline", F(title="d", customer_id="abc")),
    ("deal value -500", "/pipeline", F(title="d", value="-500")),
    ("deal stage bogus", "/pipeline", F(title="d", stage="bogus")),
    ("settings currency blank", "/settings", F(name="n", currency="")),
    ("settings currency abc", "/settings", F(name="n", currency="dollars")),
    ("settings email junk", "/settings", F(name="n", email="not an email")),
    ("person blank name", "/people/new", F(name=" ")),
    ("person bcc email", "/people/new", F(name="Eve", email="eve@example.com?bcc=x@evil.example")),
    ("person spaced email", "/people/new", F(name="Eve", email="eve @example.com")),
    # JSON API routes
    ("api customer {}", "/api/customers", (J, {})),
    ("api customer blank name", "/api/customers", (J, {"name": " "})),
    ("api customer int name", "/api/customers", (J, {"name": 123})),
    ("api customer null name", "/api/customers", (J, {"name": None})),
    ("api customer list name", "/api/customers", (J, {"name": ["x"]})),
    ("api customer body list", "/api/customers", (J, [])),
    ("api customer bad email", "/api/customers", (J, {"name": "Eve", "email": "a@b.com&cc=x"})),
    ("api invoice {}", "/api/invoices", (J, {})),
    ("api invoice customer abc", "/api/invoices", (J, {"customer_id": "abc"})),
    ("api invoice customer 999", "/api/invoices", (J, {"customer_id": 999, "lines": [{"description": "x", "unit_cents": 1}]})),
    ("api invoice job 999", "/api/invoices", (J, {"customer_id": 1, "job_id": 999, "lines": [{"description": "x", "unit_cents": 1}]})),
    ("api invoice no lines", "/api/invoices", (J, {"customer_id": 1})),
    ("api invoice lines string", "/api/invoices", (J, {"customer_id": 1, "lines": "abc"})),
    ("api invoice line no description", "/api/invoices", (J, {"customer_id": 1, "lines": [{"qty_milli": 1000}]})),
    ("api invoice unit -500", "/api/invoices", (J, {"customer_id": 1, "lines": [{"description": "x", "unit_cents": -500}]})),
    ("api invoice unit float", "/api/invoices", (J, {"customer_id": 1, "lines": [{"description": "x", "qty_milli": 1000, "unit_cents": 80.75}]})),
    ("api invoice qty float", "/api/invoices", (J, {"customer_id": 1, "lines": [{"description": "x", "qty_milli": 1.9, "unit_cents": 8000}]})),
    ("api invoice unit string", "/api/invoices", (J, {"customer_id": 1, "lines": [{"description": "x", "unit_cents": "80.00"}]})),
    ("api invoice unit 2**62 x2", "/api/invoices", (J, {"customer_id": 1, "lines": [{"description": "x", "qty_milli": 1000, "unit_cents": 2**62}, {"description": "y", "qty_milli": 1000, "unit_cents": 2**62}]})),
    ("api invoice unit 10**30", "/api/invoices", (J, {"customer_id": 1, "lines": [{"description": "x", "unit_cents": 10**30}]})),
    ("api invoice tax -100", "/api/invoices", (J, {"customer_id": 1, "lines": [{"description": "x", "unit_cents": 100, "tax_bps": -100}]})),
    ("api invoice tax 10001", "/api/invoices", (J, {"customer_id": 1, "lines": [{"description": "x", "unit_cents": 100, "tax_bps": 10001}]})),
    ("api invoice tax_pct 8.255", "/api/invoices", (J, {"customer_id": 1, "lines": [{"description": "x", "unit_cents": 100, "tax_pct": "8.255"}]})),
    ("api invoice description null", "/api/invoices", (J, {"customer_id": 1, "lines": [{"description": None, "unit_cents": 100}]})),
    ("api quote due garbage", "/api/invoices", (J, {"kind": "quote", "customer_id": 1, "due_date": "garbage", "lines": [{"description": "x", "unit_cents": 100}]})),
    ("api invoice dates 1", "/api/invoices", (J, {"customer_id": 1, "issue_date": "1", "due_date": "1", "lines": [{"description": "x", "qty_milli": 1000, "unit_cents": 100}]})),
    ("api invoice due 11/30/2026", "/api/invoices", (J, {"customer_id": 1, "due_date": "11/30/2026", "lines": [{"description": "x", "unit_cents": 100}]})),
    ("api send 999", "/api/invoices/999/send", None),
    ("api pay 999", "/api/invoices/999/pay", (J, {"amount_cents": 1})),
    ("api pay amount abc", f"/api/invoices/{OPEN}/pay", (J, {"amount_cents": "abc"})),
    ("api pay amount float", f"/api/invoices/{OPEN}/pay", (J, {"amount_cents": 99.99})),
    ("api pay amount true", f"/api/invoices/{OPEN}/pay", (J, {"amount_cents": True})),
    ("api pay amount text abc", f"/api/invoices/{OPEN}/pay", (J, {"amount": "abc"})),
    ("api pay paid_at zz", f"/api/invoices/{OPEN}/pay", (J, {"amount_cents": 100, "paid_at": "zz"})),
    ("api pay method dict", f"/api/invoices/{OPEN}/pay", (J, {"amount_cents": 100, "method": {"a": 1}})),
    ("api expense {}", "/api/expenses", (J, {})),
    ("api expense -5", "/api/expenses", (J, {"vendor_name": "v", "amount_cents": -5})),
    ("api expense string cents", "/api/expenses", (J, {"vendor_name": "v", "amount_cents": "12.99"})),
    ("api expense float cents", "/api/expenses", (J, {"vendor_name": "v", "amount_cents": 12.99})),
    ("api expense income account", "/api/expenses", (J, {"vendor_name": "v", "amount_cents": 5, "account_code": "4000"})),
    ("api expense INT64 max", "/api/expenses", (J, {"vendor_name": "v", "amount_cents": 2**63 - 1})),
    ("api expense 2**63", "/api/expenses", (J, {"vendor_name": "v", "amount_cents": 2**63})),
    ("api expense spent_on T16:30", "/api/expenses", (J, {"vendor_name": "v", "amount_cents": 5, "spent_on": f"{TODAY}T16:30"})),
    ("api calendar {}", "/api/calendar", (J, {})),
    ("api calendar customer 999", "/api/calendar", (J, {"title": "t", "start_at": "2026-11-20T09:00", "end_at": "2026-11-20T10:00", "customer_id": 999})),
    ("api calendar int title", "/api/calendar", (J, {"title": 123, "start_at": "2026-11-20T09:00:00", "end_at": "2026-11-20T10:00:00"})),
    ("api calendar null title", "/api/calendar", (J, {"title": None, "start_at": "a", "end_at": "b"})),
    ("api calendar start Tzz", "/api/calendar", (J, {"title": "t", "start_at": f"{TODAY}Tzz", "end_at": "b"})),
    ("api calendar end before start", "/api/calendar", (J, {"title": "t", "start_at": "2026-11-20T10:00", "end_at": "2026-11-20T09:00"})),
    ("api review draft 999", "/api/reviews/999/draft", (J, {"body": "x"})),
    ("api review draft {}", "/api/reviews/1/draft", (J, {})),
    ("api review replied 999", "/api/reviews/999/replied", None),
    ("api job {}", "/api/jobs", (J, {})),
    ("api job customer 999", "/api/jobs", (J, {"customer_id": 999, "title": "t"})),
    ("api job status bogus", "/api/jobs", (J, {"customer_id": 1, "title": "t", "status": "bogus"})),
    ("api recurring {}", "/api/recurring", (J, {})),
    ("api recurring lines [{}]", "/api/recurring", (J, {"customer_id": 1, "next_date": "2020-01-01", "lines": [{}]})),
    ("api recurring lines string", "/api/recurring", (J, {"customer_id": 1, "lines": "abc"})),
    ("api recurring unit -100", "/api/recurring", (J, {"customer_id": 1, "next_date": "2020-01-01", "unit_cents": -100})),
    ("api recurring next_date zz", "/api/recurring", (J, {"customer_id": 1, "next_date": "zz", "unit_cents": 100})),
    ("api tick as_of int", "/api/tick", (J, {"as_of": 123})),
    ("api tick as_of garbage", "/api/tick", (J, {"as_of": "garbage"})),
    # GET routes
    ("get api invoice abc", "GET /api/invoices/abc", None),
    ("get api invoice 999", "GET /api/invoices/999", None),
    ("get api chase 999", "GET /api/invoices/999/chase", None),
    ("get api calendar garbage", "GET /api/calendar?day=garbage", None),
    ("get api pnl zz", "GET /api/books/pnl?start=zz", None),
    ("get invoice 999", "GET /invoices/999", None),
    ("get invoice abc", "GET /invoices/abc", None),
    ("get print 999", "GET /invoices/999/print", None),
    ("get person 999", "GET /people/999", None),
    ("get new invoice customer abc", "GET /invoices/new?customer_id=abc", None),
]

FORM_PAGES_THAT_RERENDER = ("/invoices/new", "/quotes/new")


@pytest.mark.parametrize("label,path,body", BAD_INPUTS, ids=[c[0] for c in BAD_INPUTS])
def test_bad_input_is_refused_cleanly_and_changes_nothing(book, label, path, body):
    db, _, client = book
    before = _dump(db)
    if path.startswith("GET "):
        r = client.get(path[4:])
    elif isinstance(body, tuple):
        r = client.post(path, json=body[1])
    else:
        r = client.post(path, data=body)
    assert r.status_code != 500, r.text
    if path.startswith("/api/") or path.startswith("GET /api/"):
        assert r.status_code in (400, 404), (r.status_code, r.text)
        assert "detail" in r.json()
    elif path.startswith("GET "):
        assert r.status_code in (400, 404), (r.status_code, r.text)
        assert r.headers["content-type"].startswith("text/html")
    elif path in FORM_PAGES_THAT_RERENDER:
        assert r.status_code == 400, (r.status_code, r.text)
        assert 'class="error"' in r.text
    else:
        assert r.status_code in (303, 404), (r.status_code, r.text)
        if r.status_code == 303:
            assert _error_of(r), r.headers["location"]
    assert "invalid literal" not in r.text and "Traceback" not in r.text
    if r.status_code == 303:
        assert "invalid literal" not in _error_of(r)
    assert _dump(db) == before
    for page in ("/", "/books", "/api/health", "/api/today", "/api/books/aging", "/invoices", "/calendar.ics"):
        assert client.get(page).status_code == 200, page


def test_converting_a_quote_twice_is_refused_with_a_message(book):
    db, _, client = book
    assert client.post(f"/quotes/{QUOTE}/convert").status_code == 303
    before = _dump(db)
    r = client.post(f"/quotes/{QUOTE}/convert")
    assert r.status_code == 303 and "converted" in _error_of(r)
    assert _dump(db) == before


# --- messages the owner can read --------------------------------------------


def test_bad_expense_amount_shows_a_plain_message_on_the_books_page(book):
    _, _, client = book
    r = client.post("/books/expenses", data={"vendor_name": "Hardware", "amount": "abc"})
    assert r.status_code == 303
    assert urlsplit(r.headers["location"]).path == "/books"
    page = client.get(r.headers["location"])
    assert page.status_code == 200
    assert "Amount must be a number like 12.50" in page.text
    assert "invalid literal" not in page.text


def test_decimal_comma_suggests_a_dot(book):
    _, _, client = book
    r = client.post(f"/invoices/{OPEN}/pay", data={"amount": "12,50"})
    assert urlsplit(r.headers["location"]).path == f"/invoices/{OPEN}"
    assert "12.50" in _error_of(r)
    assert "12.50" in client.get(r.headers["location"]).text


@pytest.mark.parametrize(
    "path,data,page",
    [
        ("/people/new", {"name": " "}, "/people"),
        ("/jobs/new", {"customer_id": "1", "title": "x", "scheduled_for": "zz"}, "/jobs"),
        ("/calendar", {"title": "x", "day": "zz"}, "/calendar"),
        ("/reviews", {"author": "a", "body": "b", "rating": "9"}, "/reviews"),
        ("/settings", {"name": "n", "currency": "dollars"}, "/settings"),
        ("/recurring", {"customer_id": "1", "unit": "abc"}, "/invoices"),
        ("/pipeline", {"title": "d", "value": "abc"}, "/people"),
        ("/books/expenses", {"vendor_name": "v", "amount": "-1"}, "/books"),
    ],
)
def test_each_form_page_shows_its_error(book, path, data, page):
    _, _, client = book
    r = client.post(path, data=data)
    assert r.status_code == 303
    loc = r.headers["location"]
    assert urlsplit(loc).path == page
    msg = _error_of(r)
    assert msg
    shown = client.get(loc)
    assert shown.status_code == 200
    assert 'class="error"' in shown.text


def test_failed_new_invoice_form_keeps_what_was_typed(book):
    db, store, client = book
    before = len(store.list_invoices())
    form = {
        "customer_id": "2",
        "issue_date": "2026-11-01",
        "due_date": "2026-11-15",
        "description": ["Callout fee", "Hinge parts"],
        "qty": ["1.5", "2"],
        "unit": ["120.00", "ten"],
        "tax_pct": ["8.25", "0"],
        "notes": "Back door, second visit",
    }
    r = client.post("/invoices/new", data=form)
    assert r.status_code == 400
    assert "Line 2 rate must be a number like 12.50" in r.text
    for typed in ("Callout fee", "Hinge parts", "1.5", "120.00", "ten", "8.25", "Back door, second visit", "2026-11-15"):
        assert typed in r.text, typed
    assert 'value="2" selected' in r.text or '<option value="2" selected' in r.text
    assert len(store.list_invoices()) == before


def test_unknown_page_is_an_html_404(book):
    _, _, client = book
    for path in ("/invoices/999", "/invoices/abc", "/people/999"):
        r = client.get(path)
        assert r.status_code == 404
        assert r.headers["content-type"].startswith("text/html")
        assert "<nav>" in r.text


@pytest.mark.parametrize("path", ["/calendar?day=garbage", "/calendar?day=2026-02-30", "/books?start=zz", "/books?start=2026-11-01&end=junk"])
def test_bad_query_falls_back_to_today(book, path):
    _, _, client = book
    r = client.get(path)
    assert r.status_code == 200


# --- tax as a percent -------------------------------------------------------


def test_tax_percent_on_the_invoice_form(book):
    _, store, client = book
    r = client.post(
        "/invoices/new",
        data={"customer_id": "1", "description": "Labour", "qty": "1", "unit": "100.00", "tax_pct": "8.25"},
    )
    assert r.status_code == 303
    inv = store.get_invoice(int(r.headers["location"].rsplit("/", 1)[-1]))
    assert inv["lines"][0]["tax_bps"] == 825
    assert inv["tax_cents"] == 825
    assert inv["total_cents"] == 10825


def test_old_tax_bps_form_field_still_works(book):
    _, store, client = book
    r = client.post("/invoices/new", data={"customer_id": "1", "description": "Labour", "unit": "100", "tax_bps": "500"})
    assert r.status_code == 303
    assert store.get_invoice(int(r.headers["location"].rsplit("/", 1)[-1]))["tax_cents"] == 500


def test_api_takes_tax_pct_or_tax_bps(book):
    _, _, client = book
    pct = client.post("/api/invoices", json={"customer_id": 1, "lines": [{"description": "a", "unit_cents": 10000, "tax_pct": 8.25}]})
    assert pct.status_code == 200, pct.text
    assert pct.json()["tax_cents"] == 825
    bps = client.post("/api/invoices", json={"customer_id": 1, "lines": [{"description": "a", "unit_cents": 10000, "tax_bps": 825}]})
    assert bps.json()["tax_cents"] == 825


def test_retainer_form_takes_tax_percent(book):
    _, store, client = book
    r = client.post("/recurring", data={"customer_id": "1", "next_date": "2026-11-01", "unit": "100", "tax_pct": "5"})
    assert r.status_code == 303 and "error" not in r.headers["location"]
    (mine,) = [r for r in store.list_recurring() if r["next_date"] == "2026-11-01"]
    assert mine["template"]["lines"][0]["tax_bps"] == 500


def test_forms_label_tax_as_a_percent(book):
    _, _, client = book
    for path in ("/invoices/new", "/quotes/new", "/invoices"):
        page = client.get(path).text
        assert 'name="tax_pct"' in page, path
        assert "Tax %" in page, path
        assert "Tax bps" not in page, path


# --- strict numbers ----------------------------------------------------------


@pytest.mark.parametrize(
    "text,cents",
    [("1234", 123400), ("1,234.50", 123450), ("$12.5", 1250), ("-3.20", -320), ("12.30 ", 1230), ("0.05", 5), ("80", 8000)],
)
def test_parse_money_accepts(text, cents):
    assert parse_money(text) == cents


@pytest.mark.parametrize("text", ["abc", "1. 5", "1.2.3", "1e5", "12,50", "1.999", "1_000", "１２", "٣", "12.-5", "0.-9", "-", ".", "inf", "nan", "1,23,4", ""])
def test_parse_money_rejects(text):
    with pytest.raises(ValueError):
        parse_money(text)


def test_parse_money_decimal_comma_message_suggests_a_dot():
    with pytest.raises(ValueError, match=r"12\.50"):
        parse_money("12,50")


@pytest.mark.parametrize("text,milli", [("2", 2000), ("2.5", 2500), ("0.25", 250), ("1.125", 1125), ("-0.5", -500), ("-1.5", -1500)])
def test_parse_qty_keeps_the_sign_and_three_decimals(text, milli):
    assert parse_qty(text) == milli


@pytest.mark.parametrize("text", ["1.-5", "1.2345", "abc", "1,5", "1e3"])
def test_parse_qty_rejects(text):
    with pytest.raises(ValueError):
        parse_qty(text)


@pytest.mark.parametrize("text,bps", [("8.25", 825), ("0", 0), ("100", 10000), ("5", 500), ("8.25%", 825), (" 7.5 ", 750)])
def test_parse_percent(text, bps):
    assert parse_percent(text) == bps


@pytest.mark.parametrize("text", ["100.01", "-1", "8.255", "abc", "1e2", "inf"])
def test_parse_percent_rejects(text):
    with pytest.raises(ValueError):
        parse_percent(text)


def test_negative_qty_on_the_form_is_refused_not_flipped(book):
    db, store, client = book
    r = client.post("/invoices/new", data={"customer_id": "1", "description": "Refund?", "qty": "-0.5", "unit": "100"})
    assert r.status_code == 400
    assert not [i for i in store.list_invoices() if i["total_cents"] == 5000]


# --- the store itself refuses bad data (form, API and CLI share it) ------------


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "till.db")
    s.migrate()
    return s


def test_store_refuses_bad_dates_and_order(store):
    cid = store.create_customer(name="Ada West")
    with pytest.raises(ValueError, match="date"):
        store.create_invoice(customer_id=cid, issue_date="2026-11-14", due_date="11/30/2026")
    with pytest.raises(ValueError):
        store.create_invoice(customer_id=cid, issue_date="2026-11-14", due_date="2026-11-01")
    with pytest.raises(ValueError, match="date"):
        store.record_expense("v", 100, "5000", "2026-13-45")
    with pytest.raises(ValueError):
        store.create_event("t", "2026-11-20T10:00", "2026-11-20T09:00")
    with pytest.raises(ValueError):
        store.create_event("t", "2026-11-20Tzz", "2026-11-20T09:00")
    with pytest.raises(ValueError):
        store.tick_recurring(as_of="garbage")
    assert store.list_invoices() == [] and store.list_expenses() == []


def test_store_refuses_missing_references_with_a_message(store):
    with pytest.raises(ValueError, match="no customer 999"):
        store.create_invoice(customer_id=999, issue_date="2026-11-14")
    with pytest.raises(ValueError, match="no customer 999"):
        store.create_job(customer_id=999, title="x")
    with pytest.raises(ValueError, match="no customer 999"):
        store.create_recurring(999, "monthly", "2026-11-01", [{"description": "x", "qty_milli": 1000, "unit_cents": 1}])
    cid = store.create_customer(name="A")
    with pytest.raises(ValueError, match="no job 999"):
        store.create_invoice(customer_id=cid, issue_date="2026-11-14", job_id=999)


def test_store_bounds_amounts_rates_and_ratings(store):
    cid = store.create_customer(name="A")
    with pytest.raises(ValueError):
        store.record_expense("v", 10**12 + 1, "5000", "2026-11-01")
    with pytest.raises(ValueError):
        store.record_expense("v", 12.99, "5000", "2026-11-01")
    with pytest.raises(ValueError):
        store.add_review("google", "a", 6, "b", "2026-11-01T10:00:00")
    with pytest.raises(ValueError):
        store.create_document("invoice", cid, "2026-11-01", [{"description": "x", "qty_milli": 1000, "unit_cents": 100, "tax_bps": 10001}])
    with pytest.raises(ValueError):
        store.create_document("invoice", cid, "2026-11-01", [{"description": "x", "qty_milli": 0, "unit_cents": 100}])
    with pytest.raises(ValueError):
        store.save_company(currency="usd1")
    assert store.save_company(currency="eur")["currency"] == "EUR"
    assert store.list_invoices() == [] and store.list_expenses() == [] and store.list_reviews() == []


def test_titles_lose_line_breaks(store):
    cid = store.create_customer(name="A")
    job = store.create_job(customer_id=cid, title="Hinge\r\nEND:VEVENT")
    assert "\n" not in job["title"] and "\r" not in job["title"]
    ev = store.create_event("Visit\nBEGIN:VEVENT", "2026-11-20T09:00", "2026-11-20T10:00")
    assert "\n" not in ev["title"]


def test_retainer_lines_are_checked_when_the_retainer_is_made(store):
    cid = store.create_customer(name="A")
    for bad in ([], "abc", [{}], [{"description": "x", "qty_milli": 1000, "unit_cents": -1}]):
        with pytest.raises(ValueError):
            store.create_recurring(cid, "monthly", "2026-11-01", bad)
    assert store.list_recurring() == []


# --- an old database with bad dates already in it ----------------------------


@pytest.mark.parametrize("bad", ["garbage", "11/30/2026", "0000", "2026-02-30", ""])
def test_a_bad_date_already_stored_does_not_take_pages_down(book, bad):
    db, store, client = book
    con = sqlite3.connect(db)
    con.execute("UPDATE invoices SET due_date=? WHERE id=?", (bad, OPEN))
    con.execute("UPDATE recurring SET next_date=?", (bad or "x",))
    con.commit()
    con.close()
    for page in ("/", "/books", "/api/books/aging", "/api/today", "/api/brief", f"/invoices/{OPEN}", "/invoices", "/people"):
        assert client.get(page).status_code == 200, page
    aging = client.get("/api/books/aging").json()
    assert aging["total_cents"] == store.trial_balance()["1100"]
    assert client.post("/tick").status_code == 303
