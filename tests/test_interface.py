"""Interface: currency, plain words, labels and accessibility, the invoice
form's live total, the invoice and print pages, the phone layout, and the
sample-data banner.

Every page is fetched through create_app on a real file database, the way
the browser sees it.
"""

import json
import re
import shutil
import subprocess
from datetime import date, timedelta
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from till.money import format_cents, line_cents, tax_cents
from till.seed import seed
from till.web import create_app

LOCAL = "http://127.0.0.1:7740"
TODAY = date.today().isoformat()
CSS = (Path(__file__).resolve().parent.parent / "till" / "static" / "till.css").read_text()


@pytest.fixture
def book(tmp_path):
    app = create_app(tmp_path / "till.db")
    store = app.state.store
    cid = store.create_customer(name="Pat Rivera", email="pat@example.com", address="4 Elm Row")
    client = TestClient(app, base_url=LOCAL, follow_redirects=False)
    return store, cid, client


@pytest.fixture
def seeded(tmp_path):
    app = create_app(tmp_path / "till.db")
    seed(app.state.store)
    return app.state.store, TestClient(app, base_url=LOCAL, follow_redirects=False)


def _sent(store, cid, lines, issue=TODAY, due=None):
    inv = store.create_document("invoice", cid, issue, lines, due_date=due or (date.today() + timedelta(days=14)).isoformat())
    store.send_invoice(inv["id"])
    return store.get_invoice(inv["id"])


def _line(desc="Labour", qty=1000, unit=18000, tax=0):
    return {"description": desc, "qty_milli": qty, "unit_cents": unit, "tax_bps": tax}


def _visible_text(html: str) -> str:
    html = re.sub(r"<script.*?</script>|<style.*?</style>", " ", html, flags=re.S)
    return re.sub(r"<[^>]+>", " ", html)


# --- 1. currency ------------------------------------------------------------


@pytest.mark.parametrize(
    "cents,code,text",
    [
        (123450, "USD", "$1,234.50"),
        (123450, "CAD", "$1,234.50"),
        (500, "AUD", "$5.00"),
        (500, "NZD", "$5.00"),
        (1200, "GBP", "£12.00"),
        (1200, "EUR", "€12.00"),
        (-500, "GBP", "-£5.00"),
        (1200, "CHF", "CHF 12.00"),
        (-1200, "SEK", "-SEK 12.00"),
        (1200, None, "$12.00"),
    ],
)
def test_format_cents_puts_the_right_symbol_in_the_right_place(cents, code, text):
    assert (format_cents(cents, code) if code is not None else format_cents(cents)) == text


def test_every_page_and_the_print_view_use_the_company_currency(book):
    store, cid, client = book
    store.save_company(currency="GBP")
    yesterday = (date.today() - timedelta(days=2)).isoformat()
    inv = _sent(store, cid, [_line(unit=18000)], issue=yesterday, due=yesterday)
    store.record_expense("City Hardware", 6320, "5100", TODAY)
    for path in ("/", "/invoices", f"/invoices/{inv['id']}", f"/invoices/{inv['id']}/print", "/books", "/people", f"/people/{cid}"):
        page = client.get(path).text
        assert "£180.00" in page, path
        assert "$" not in _visible_text(page), path


def test_settings_offers_a_currency_list_not_free_text(book):
    store, _, client = book
    page = client.get("/settings").text
    assert re.search(r'<select[^>]*name="currency"', page)
    assert not re.search(r'<input[^>]*name="currency"', page)
    for code in ("USD", "CAD", "AUD", "NZD", "GBP", "EUR"):
        assert f'value="{code}"' in page
    assert re.search(r'<option value="USD" selected', page)
    r = client.post("/settings", data={"currency": "EUR"})
    assert r.status_code == 303 and store.get_company()["currency"] == "EUR"
    assert re.search(r'<option value="EUR" selected', client.get("/settings").text)


def test_a_currency_outside_the_list_stays_selected(book):
    store, _, client = book
    store.save_company(currency="CHF")
    assert re.search(r'<option value="CHF" selected', client.get("/settings").text)


# --- 2. plain words ---------------------------------------------------------


def test_nav_says_settings_not_shop(book):
    _, _, client = book
    page = client.get("/").text
    nav = re.search(r"<nav.*?</nav>", page, re.S).group(0)
    assert ">Settings</a>" in nav and ">Shop</a>" not in nav


def test_basis_points_never_appear_on_screen(seeded):
    store, client = seeded
    for path in ("/", "/invoices", "/invoices/new", "/quotes/new", "/invoices/1", "/invoices/3", "/invoices/3/edit",
                 "/invoices/1/print", "/books", "/settings", "/jobs", "/people", "/people/1"):
        text = _visible_text(client.get(path).text).lower()
        assert "bps" not in text, path


def test_today_names_the_real_weekday(book):
    _, _, client = book
    page = client.get("/").text
    weekday = date.today().strftime("%A")
    assert weekday in page
    for other in ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"):
        if other != weekday:
            assert other not in page, other


def test_send_reads_mark_as_sent_and_says_till_does_not_email(book):
    store, cid, client = book
    inv = store.create_document("invoice", cid, TODAY, [_line()])
    page = client.get(f"/invoices/{inv['id']}").text
    assert re.search(r"<button[^>]*>\s*Mark as sent\s*</button>", page)
    assert not re.search(r"<button[^>]*>\s*Send\s*</button>", page)
    assert "Till doesn't email" in page
    assert f'href="/invoices/{inv["id"]}/print"' in page


def test_books_leads_with_a_plain_sentence_about_the_month(book):
    store, cid, client = book
    # The persona's job: 280.00 of work at 8.25% tax, and a 63.20 receipt.
    _sent(store, cid, [_line("Labour", 2500, 9500, 825), _line("Part", 1000, 4250, 825)])
    store.record_expense("City Hardware", 6320, "5100", TODAY)
    page = client.get("/books").text
    sentence = "This month you've made $216.80: you invoiced $280.00 (before sales tax) and spent $63.20 on expenses."
    assert sentence in page
    accountant = page.index("For your accountant")
    assert page.index(sentence) < accountant
    # The jargon lives below, with explanations.
    for word in ("balanced", "Accounts receivable", "5100 Materials"):
        assert page.index(word) > accountant, word


def test_books_says_down_when_the_month_lost_money(book):
    store, cid, client = book
    _sent(store, cid, [_line(unit=10000)])
    store.record_expense("Landlord", 25000, "5300", TODAY)
    page = client.get("/books").text
    assert "This month you're down $150.00: you invoiced $100.00 (before sales tax) and spent $250.00 on expenses." in page


def test_books_names_another_period_in_words(book):
    _, _, client = book
    page = client.get("/books?start=2026-10-01&end=2026-10-31").text
    assert "From 1 October 2026 to 31 October 2026 you" in page


def test_expense_picker_shows_category_names_not_codes(book):
    _, _, client = book
    page = client.get("/books").text
    select = re.search(r'<select[^>]*name="account_code".*?</select>', page, re.S).group(0)
    assert re.search(r'<option value="5100"[^>]*>\s*Materials\s*</option>', select)
    assert not re.search(r">\s*5\d00 ", select)


# --- 3. labels and accessibility ---------------------------------------------


class _Fields(HTMLParser):
    """Every form control, and whether a <label> names it."""

    def __init__(self):
        super().__init__()
        self.depth = 0
        self.fors = set()
        self.controls = []  # (tag, attrs, inside_label)

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "label":
            self.depth += 1
            if a.get("for"):
                self.fors.add(a["for"])
        elif tag in ("input", "select", "textarea"):
            self.controls.append((tag, a, self.depth > 0))

    def handle_endtag(self, tag):
        if tag == "label":
            self.depth -= 1

    def unlabeled(self):
        bad = []
        for tag, a, inside in self.controls:
            if tag == "input" and a.get("type") in ("hidden", "submit", "button"):
                continue
            if inside or a.get("aria-label") or (a.get("id") and a["id"] in self.fors):
                continue
            bad.append(f"{tag} name={a.get('name')}")
        return bad


def test_every_form_field_has_a_label(seeded):
    store, client = seeded
    paths = ["/", "/invoices", "/invoices?kind=quote", "/invoices/new", "/quotes/new", "/invoices/1", "/invoices/3",
             "/invoices/3/edit", "/invoices/4", "/people", "/people/1", "/jobs", "/books", "/calendar", "/reviews", "/settings"]
    for path in paths:
        parser = _Fields()
        parser.feed(client.get(path).text)
        assert parser.unlabeled() == [], path


def test_invoice_line_inputs_have_labels_that_name_the_line(book):
    _, _, client = book
    page = client.get("/invoices/new").text
    for what in ("Description", "Quantity", "Rate", "Tax %"):
        assert f"{what}, line 1" in page, what


def _hex(c):
    c = c.lstrip("#")
    return tuple(int(c[i : i + 2], 16) / 255 for i in (0, 2, 4))


def _luminance(c):
    def ch(v):
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4

    r, g, b = (ch(v) for v in _hex(c))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _contrast(a, b):
    la, lb = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def _var(name):
    return re.search(rf"{name}:\s*(#[0-9a-fA-F]{{6}})", CSS).group(1)


def test_focus_ring_is_visible_on_the_sidebar_and_on_paper():
    rule = re.search(r"\.cover a:focus-visible[^{]*\{([^}]*)\}", CSS)
    assert rule, "the sidebar needs its own focus style"
    colour = re.search(r"outline:[^;]*?(#[0-9a-fA-F]{6}|var\(--[a-z-]+\))", rule.group(1)).group(1)
    if colour.startswith("var("):
        colour = _var(colour[4:-1])
    assert _contrast(colour, _var("--cover")) >= 3.0
    assert _contrast(_var("--carbon"), _var("--paper")) >= 3.0


def test_errors_are_announced_and_saves_are_confirmed(book):
    store, cid, client = book
    page = client.get("/settings?error=Nope").text
    assert re.search(r'role="alert"[^>]*>\s*Nope', page) or re.search(r'class="error" role="alert">Nope', page)
    r = client.post("/settings", data={"name": "Dana's Plumbing", "email": "dana@example.com"})
    assert r.status_code == 303
    assert parse_qs(urlsplit(r.headers["location"]).query).get("saved") == ["1"]
    page = client.get(r.headers["location"]).text
    assert re.search(r'role="status"[^>]*>[^<]*Saved', page)
    assert 'role="status"' not in client.get("/settings").text


@pytest.mark.parametrize(
    "path,data",
    [
        ("/people/{cid}/edit", {"name": "Pat R"}),
        ("/books/expenses", {"vendor_name": "City Hardware", "amount": "63.20", "account_code": "5100"}),
        ("/invoices/{sent}/pay", {"amount": "10.00", "method": "card"}),
        ("/invoices/{draft}/edit", {"customer_id": "{cid}", "description": "Labour", "qty": "1", "unit": "5"}),
    ],
)
def test_other_saves_are_confirmed_too(book, path, data):
    store, cid, client = book
    sent = _sent(store, cid, [_line()])["id"]
    draft = store.create_document("invoice", cid, TODAY, [_line()])["id"]
    fill = lambda s: s.format(cid=cid, sent=sent, draft=draft)
    r = client.post(fill(path), data={k: fill(v) for k, v in data.items()})
    assert r.status_code == 303 and "error" not in r.headers["location"], r.headers["location"]
    assert "saved=" in r.headers["location"]
    assert 'role="status"' in client.get(r.headers["location"]).text


def test_saved_message_is_never_taken_from_the_url(book):
    _, _, client = book
    page = client.get("/settings?saved=<b>hacked</b>").text
    assert "hacked" not in page


# --- 4. the invoice form -----------------------------------------------------


def test_the_persona_invoice_totals_303_10(book):
    store, cid, client = book
    r = client.post(
        "/invoices/new",
        data={
            "customer_id": str(cid),
            "description": ["Labour", "Cartridge", ""],
            "qty": ["2.5", "1", "1"],
            "unit": ["95", "42.50", "95"],
            "tax_pct": ["8.25", "8.25", "8.25"],
        },
    )
    assert r.status_code == 303
    inv = store.get_invoice(int(r.headers["location"].rsplit("/", 1)[-1]))
    assert (inv["subtotal_cents"], inv["tax_cents"], inv["total_cents"]) == (28000, 2310, 30310)


def test_invoice_form_has_live_totals_and_line_buttons_with_a_no_js_fallback(book):
    store, _, client = book
    store.save_company(default_tax_bps=825)
    page = client.get("/invoices/new").text
    assert re.search(r'<output[^>]*id="subtotal"', page)
    assert re.search(r'<output[^>]*id="tax-total"', page)
    assert re.search(r'<output[^>]*id="grand-total"', page)
    assert re.search(r'<button[^>]*id="add-line"', page)
    assert re.search(r'<button[^>]*class="[^"]*remove-line', page)
    # Without JavaScript there are blank rows to fill in.
    rows = page.split('<template id="line-template">')[0]
    assert len(re.findall(r'name="description"', rows)) >= 3
    # Every new line starts at the default tax from Settings.
    taxes = re.findall(r'name="tax_pct"[^>]*value="([^"]*)"', page)
    assert taxes and all(t == "8.25" for t in taxes)


def test_settings_saves_default_tax_and_payment_instructions(book):
    store, _, client = book
    r = client.post("/settings", data={"default_tax_pct": "8.25", "payment_instructions": "Bank: 12-3456\nRef: invoice number"})
    assert r.status_code == 303 and "error" not in r.headers["location"]
    company = store.get_company()
    assert company["default_tax_bps"] == 825
    assert company["payment_instructions"] == "Bank: 12-3456\nRef: invoice number"
    page = client.get("/settings").text
    assert 'name="default_tax_pct"' in page and 'value="8.25"' in page
    assert "Bank: 12-3456" in page
    bad = client.post("/settings", data={"default_tax_pct": "abc"})
    assert "error" in bad.headers["location"] and store.get_company()["default_tax_bps"] == 825


def _totals_js(page: str) -> str:
    m = re.search(r"// totals:begin(.*?)// totals:end", page, re.S)
    assert m, "the rounding code should be marked so it can be checked"
    return m.group(1)


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_live_total_rounds_exactly_like_the_store(book):
    _, _, client = book
    js = _totals_js(client.get("/invoices/new").text)
    cases = [
        [("2.5", "95", "8.25"), ("1", "42.50", "8.25")],
        [("1", "0.05", "10")],
        [("0.333", "1.50", "7.5"), ("3", "33.33", "0")],
        [("1.005", "19.99", "12.5"), ("2", "1,234.56", "100")],
        [("7", "0.07", "0.5"), ("0.5", "0.01", "50")],
        [("1000000", "9999.99", "8.25")],
    ]
    script = js + "\nconst cases = " + json.dumps(cases) + ";\n" + (
        "console.log(JSON.stringify(cases.map(c => tillTotals(c.map(([q,u,t]) => ({qty:q, unit:u, tax:t}))))));"
    )
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    got = json.loads(out.stdout)

    def milli(s):
        w, _, f = s.replace(",", "").partition(".")
        return int(w or 0) * 1000 + int((f + "000")[:3])

    def cents(s):
        w, _, f = s.replace(",", "").partition(".")
        return int(w or 0) * 100 + int((f + "00")[:2])

    for case, result in zip(cases, got):
        sub = tax = 0
        for q, u, t in case:
            lc = line_cents(milli(q), cents(u))
            sub += lc
            tax += tax_cents(lc, cents(t))
        assert [int(result["subtotal"]), int(result["tax"]), int(result["total"])] == [sub, tax, sub + tax], case
    assert got[0]["total"] in (30310, "30310")


# --- 5. the invoice page -----------------------------------------------------


def test_invoice_page_lists_payments_and_remembers_the_last_method(book):
    store, cid, client = book
    inv = _sent(store, cid, [_line(unit=30310)])
    assert client.post(f"/invoices/{inv['id']}/pay", data={"amount": "150.00", "method": "card", "paid_at": TODAY}).status_code == 303
    page = client.get(f"/invoices/{inv['id']}").text
    assert "Payments received" in page
    table = re.search(r"Payments received.*?</table>", page, re.S).group(0)
    assert TODAY in table and "Card" in table and "$150.00" in table
    assert re.search(r'<option value="card" selected', page)
    other = _sent(store, cid, [_line()])
    assert re.search(r'<option value="card" selected', client.get(f"/invoices/{other['id']}").text)


def test_void_and_delete_sit_apart_under_fix_a_mistake(book):
    store, cid, client = book
    draft = store.create_document("invoice", cid, TODAY, [_line()])
    page = client.get(f"/invoices/{draft['id']}").text
    fix = page.index("Fix a mistake")
    assert page.index("Delete draft") > fix
    assert page.index("Mark as sent") < fix
    sent = _sent(store, cid, [_line()])
    page = client.get(f"/invoices/{sent['id']}").text
    fix = page.index("Fix a mistake")
    assert page.index("Record payment") < fix < page.index(f"/invoices/{sent['id']}/void")


def test_invoice_on_a_job_opens_the_form_prefilled(book):
    store, cid, client = book
    store.create_product("Shop hour", 9500, 0)
    store.save_company(default_tax_bps=825)
    job = store.create_job(customer_id=cid, title="Fix kitchen faucet")
    jobs = client.get("/jobs").text
    assert f'href="/invoices/new?job_id={job["id"]}"' in jobs
    before = len(store.list_invoices())
    # The old button posted; it now opens the form too, and writes nothing.
    r = client.post(f"/jobs/{job['id']}/invoice")
    assert r.status_code == 303 and r.headers["location"] == f"/invoices/new?job_id={job['id']}"
    assert len(store.list_invoices()) == before
    page = client.get(f"/invoices/new?job_id={job['id']}").text
    customers = re.search(r'<select name="customer_id".*?</select>', page, re.S).group(0)
    jobs_select = re.search(r'<select name="job_id".*?</select>', page, re.S).group(0)
    assert re.search(rf'<option value="{cid}" selected', customers)
    assert re.search(rf'<option value="{job["id"]}" selected', jobs_select)
    first = re.search(r'name="description"[^>]*value="([^"]*)"', page).group(1)
    assert "Fix kitchen faucet" in first
    assert re.search(r'name="unit"[^>]*value="95.00"', page)
    assert re.search(r'name="tax_pct"[^>]*value="8.25"', page)


# --- 6. the print view -------------------------------------------------------


def test_print_view_is_customer_ready(book):
    store, cid, client = book
    store.save_company(name="Dana's Plumbing", tax_id="", payment_instructions="Bank transfer to 12-3456\nPlease quote the invoice number")
    inv = _sent(store, cid, [_line("Labour", 2500, 9500, 825), _line("Part", 1000, 4250, 0)])
    page = client.get(f"/invoices/{inv['id']}/print").text
    printed = re.sub(r'<[^>]*class="[^"]*screen-only.*?</(p|div)>', "", page, flags=re.S)
    assert "prints as a PDF" not in printed
    assert re.search(r'class="[^"]*screen-only', page)
    assert re.search(r"@media print\s*\{[^}]*\.screen-only", CSS)
    assert "How to pay" in page and "Bank transfer to 12-3456" in page
    assert page.index("How to pay") > page.index("Balance due")
    assert "8.25%" in page
    assert "Tax ID" not in page
    store.save_company(tax_id="12-3456789")
    assert "Tax ID 12-3456789" in client.get(f"/invoices/{inv['id']}/print").text


def test_print_view_without_tax_has_no_tax_column(book):
    store, cid, client = book
    inv = _sent(store, cid, [_line()])
    page = client.get(f"/invoices/{inv['id']}/print").text
    assert "Tax %" not in page


# --- 7. the phone layout -----------------------------------------------------


def test_every_table_scrolls_inside_its_own_box(seeded):
    store, client = seeded
    for path in ("/", "/invoices", "/invoices/1", "/invoices/new", "/people", "/people/1", "/jobs", "/books", "/calendar"):
        page = client.get(path).text
        tables = len(re.findall(r"<table", page))
        wrapped = len(re.findall(r'<div class="table-wrap">\s*<table', page))
        assert tables == wrapped, path
    assert re.search(r"\.table-wrap\s*\{[^}]*overflow-x:\s*auto", CSS)


def test_phone_css_stacks_lines_and_moves_the_margin():
    phone = re.search(r"@media \(max-width: 600px\)\s*\{(.*?)\n\}", CSS, re.S)
    assert phone, "a phone-width block"
    body = phone.group(1)
    assert ".lines" in body and ".page" in body and ".cover nav" in body


# --- 8. the sample-data banner ------------------------------------------------


def test_sample_book_says_so_on_today(seeded):
    store, client = seeded
    assert store.get_company()["is_demo"] == 1
    page = client.get("/").text
    assert "This is sample data." in page
    assert re.search(r'<a href="/settings#start-fresh">Start your own book</a>', page)
    assert 'id="start-fresh"' in client.get("/settings").text


def test_a_real_book_has_no_sample_banner(book):
    store, _, client = book
    assert store.get_company()["is_demo"] == 0
    assert "sample data" not in client.get("/").text


def test_invoice_from_a_job_keeps_an_archived_customer(book):
    store, cid, client = book
    store.create_customer(name="Aaron First")  # would be picked silently if Pat were missing
    job = store.create_job(customer_id=cid, title="Leak")
    store.set_customer_archived(cid, True)
    page = client.get(f"/invoices/new?job_id={job['id']}").text
    select = re.search(r'<select name="customer_id".*?</select>', page, re.S).group(0)
    assert re.search(rf'<option value="{cid}" selected', select)


def test_upgrade_takes_the_default_tax_from_the_first_product(tmp_path):
    import sqlite3

    from till.store import Store

    db = tmp_path / "v3.db"
    store = Store(db)
    store.migrate()
    store.create_product("Shop hour", 9500, 825)
    store.close()
    con = sqlite3.connect(db)
    con.execute("ALTER TABLE company DROP COLUMN default_tax_bps")
    con.execute("ALTER TABLE company DROP COLUMN payment_instructions")
    con.execute("ALTER TABLE company DROP COLUMN is_demo")
    con.execute("PRAGMA user_version = 3")
    con.commit()
    con.close()
    again = Store(db)
    again.migrate()
    assert again.get_company()["default_tax_bps"] == 825
