"""Fixes from the second review: ids too big for SQLite, a catch-all 500
page, API edits that keep omitted fields, drafts already in the books,
guards whose tests couldn't fail, per-document currency, `till restore`
against a book held open, and the note on voids in Books.

Everything goes through create_app over TestClient on a real file database
(or the real CLI main()), and the books are read back through a separate
SQLite connection, so only committed data counts.
"""

import re
import sqlite3
import threading
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from till.web import GENERIC_ERROR, create_app

LOCAL = "http://127.0.0.1:7740"
BIG = "99999999999999999999"  # more than SQLite's 64-bit integers hold
HEADERS = {
    "x-content-type-options": "nosniff",
    "referrer-policy": "no-referrer",
    "content-security-policy": "frame-ancestors 'none'",
}


def _sql(db, query, args=()):
    con = sqlite3.connect(db)
    try:
        return con.execute(query, args).fetchall()
    finally:
        con.close()


def _error_of(response) -> str:
    return parse_qs(urlsplit(response.headers["location"]).query).get("error", [""])[0]


def _has_security_headers(response) -> bool:
    return all(response.headers.get(k) == v for k, v in HEADERS.items())


@pytest.fixture
def book(tmp_path):
    db = tmp_path / "till.db"
    app = create_app(db)
    store = app.state.store
    cid = store.create_customer(name="Maria Chen", email="maria@example.com")
    client = TestClient(app, base_url=LOCAL, raise_server_exceptions=False, follow_redirects=False)
    return db, app, store, client, cid


# --- 1. ids too big for SQLite, and the 500 page ---------------------------------


def _id_routes(app):
    """Every route with an id in its path, as (method, path with BIG ids)."""
    out = []
    for route in app.routes:
        if isinstance(route, APIRoute) and "{" in route.path:
            for method in sorted(route.methods):
                out.append((method, re.sub(r"\{[a-z_]+\}", BIG, route.path)))
    return out


def test_every_path_id_too_big_for_sqlite_is_a_404_with_headers(book):
    db, app, store, client, cid = book
    routes = _id_routes(app)
    assert len(routes) >= 30  # the walk found the routes, not an empty list
    for method, path in routes:
        r = client.request(method, path)
        assert r.status_code == 404, (method, path, r.status_code, r.text[:200])
        assert _has_security_headers(r), (method, path)
        if path.startswith("/api/"):
            assert r.json() == {"detail": "Not found"}, path
        else:
            assert "nothing here" in r.text, path


@pytest.mark.parametrize("query", [f"customer_id={BIG}", f"job_id={BIG}", f"customer_id={BIG}&job_id={BIG}"])
def test_a_query_id_too_big_for_sqlite_is_refused_not_a_500(book, query):
    db, app, store, client, cid = book
    r = client.get(f"/invoices/new?{query}")
    assert r.status_code == 400
    assert "isn&#39;t valid" in r.text and _has_security_headers(r)


def test_new_invoice_form_with_a_huge_customer_id_comes_back_with_a_message(book):
    db, app, store, client, cid = book
    r = client.post(
        "/invoices/new",
        data={"customer_id": BIG, "issue_date": "2026-11-01", "description": "x", "qty": "1", "unit": "1"},
    )
    assert r.status_code == 400
    assert "no customer" in r.text
    assert _sql(db, "SELECT COUNT(*) FROM invoices") == [(0,)]


@pytest.mark.parametrize("path", ["/", "/api/invoices"])
def test_an_unexpected_error_is_a_generic_500_with_headers_and_no_internals(book, monkeypatch, path):
    db, app, store, client, cid = book

    def broken(*args, **kwargs):
        raise RuntimeError("secret internals /home/someone/till.db")

    monkeypatch.setattr(store, "today_brief", broken)
    monkeypatch.setattr(store, "list_invoices", broken)
    r = client.get(path)
    assert r.status_code == 500
    assert _has_security_headers(r)
    assert "secret" not in r.text and "RuntimeError" not in r.text and "Traceback" not in r.text
    if path.startswith("/api/"):
        assert r.json() == {"detail": "Something went wrong"}
    else:
        assert "Something went wrong" in r.text and r.headers["content-type"].startswith("text/html")


# --- 2. an API edit changes only the fields it sends ------------------------------


def _draft_with_everything(store, cid):
    job = store.create_job(cid, "Boiler")
    return store.create_document(
        "invoice",
        cid,
        "2026-11-01",
        [{"description": "Labour", "qty_milli": 1000, "unit_cents": 10000, "tax_bps": 0}],
        due_date="2026-11-30",
        notes="Gate code 1234",
        job_id=job["id"],
    )


def _header(db, iid):
    return _sql(db, "SELECT customer_id, issue_date, due_date, notes, job_id, total_cents FROM invoices WHERE id=?", (iid,))[0]


def test_api_edit_sending_only_lines_keeps_every_other_field(book):
    db, app, store, client, cid = book
    inv = _draft_with_everything(store, cid)
    r = client.post(f"/api/invoices/{inv['id']}/edit", json={"lines": [{"description": "Labour", "qty": 2, "unit": "100.00"}]})
    assert r.status_code == 200, r.text
    assert _header(db, inv["id"]) == (cid, "2026-11-01", "2026-11-30", "Gate code 1234", inv["job_id"], 20000)


def test_api_edit_sending_only_a_note_keeps_the_lines(book):
    db, app, store, client, cid = book
    inv = _draft_with_everything(store, cid)
    r = client.post(f"/api/invoices/{inv['id']}/edit", json={"notes": "Side door"})
    assert r.status_code == 200, r.text
    assert _header(db, inv["id"]) == (cid, "2026-11-01", "2026-11-30", "Side door", inv["job_id"], 10000)
    assert _sql(db, "SELECT description, qty_milli, unit_cents FROM invoice_lines WHERE invoice_id=?", (inv["id"],)) == [
        ("Labour", 1000, 10000)
    ]


def test_api_edit_with_explicit_null_or_blank_clears_due_date_notes_and_job(book):
    db, app, store, client, cid = book
    inv = _draft_with_everything(store, cid)
    r = client.post(f"/api/invoices/{inv['id']}/edit", json={"due_date": None, "notes": "", "job_id": None})
    assert r.status_code == 200, r.text
    assert _header(db, inv["id"]) == (cid, "2026-11-01", None, "", None, 10000)
    inv2 = _draft_with_everything(store, cid)
    r = client.post(f"/api/invoices/{inv2['id']}/edit", json={"due_date": "", "notes": None, "job_id": ""})
    assert r.status_code == 200, r.text
    assert _header(db, inv2["id"]) == (cid, "2026-11-01", None, "", None, 10000)
    # Empty lines are still refused, and nothing changes.
    assert client.post(f"/api/invoices/{inv2['id']}/edit", json={"lines": []}).status_code == 400
    assert _header(db, inv2["id"])[5] == 10000


def test_the_html_edit_form_sends_every_field_and_a_blank_one_still_clears(book):
    db, app, store, client, cid = book
    inv = _draft_with_everything(store, cid)
    page = client.get(f"/invoices/{inv['id']}/edit").text
    for name in ("customer_id", "issue_date", "due_date", "notes", "job_id", "description", "qty", "unit", "tax_pct"):
        assert f'name="{name}"' in page, name
    r = client.post(
        f"/invoices/{inv['id']}/edit",
        data={"customer_id": str(cid), "issue_date": "2026-11-02", "due_date": "", "notes": "", "job_id": "",
              "description": "Labour", "qty": "3", "unit": "100.00", "tax_pct": "0"},
    )
    assert r.status_code == 303, r.text
    assert _header(db, inv["id"]) == (cid, "2026-11-02", None, "", None, 30000)


# --- 3. a draft that is already in the books ---------------------------------------


def _legacy_posted_draft(db, store, cid, cents=10000):
    """A draft whose income was posted, as the old code left it when a send
    stopped after the journal entry was committed: the rows inserted the way
    its _post wrote them, with the invoice still a draft."""
    inv = store.create_document(
        "invoice", cid, "2026-03-01", [{"description": "Labour", "qty_milli": 1000, "unit_cents": cents, "tax_bps": 0}]
    )
    con = sqlite3.connect(db)
    try:
        cur = con.execute(
            "INSERT INTO journal (posted_at, memo, ref_type, ref_id) VALUES ('2026-03-01', ?, 'invoice', ?)",
            (f"Invoice {inv['number']}", inv["id"]),
        )
        con.executemany(
            "INSERT INTO journal_lines (journal_id, account_code, debit_cents, credit_cents) VALUES (?,?,?,?)",
            [(cur.lastrowid, "1100", cents, 0), (cur.lastrowid, "4000", 0, cents)],
        )
        con.commit()
    finally:
        con.close()
    assert store.get_invoice(inv["id"])["status"] == "draft"
    return inv


def _ar(db):
    return _sql(db, "SELECT COALESCE(SUM(debit_cents - credit_cents), 0) FROM journal_lines WHERE account_code='1100'")[0][0]


def _lines_of(db, iid):
    return _sql(db, "SELECT description, qty_milli, unit_cents FROM invoice_lines WHERE invoice_id=? ORDER BY position", (iid,))


def test_a_draft_already_in_the_books_cant_be_edited_over_http(book):
    db, app, store, client, cid = book
    inv = _legacy_posted_draft(db, store, cid)
    form = {"customer_id": str(cid), "issue_date": "2026-03-01", "description": "Labour", "qty": "1", "unit": "250.00"}
    r = client.post(f"/invoices/{inv['id']}/edit", data=form)
    assert r.status_code == 400 and "already in the books; void it and make a new one" in r.text
    r = client.post(f"/api/invoices/{inv['id']}/edit", json={"lines": [{"description": "Labour", "unit": "250.00"}]})
    assert r.status_code == 400 and "already in the books" in r.json()["detail"]
    with pytest.raises(ValueError, match="already in the books"):
        store.add_line(inv["id"], "Extra", 1000, 15000)
    assert _lines_of(db, inv["id"]) == [("Labour", 1000, 10000)]
    assert _sql(db, "SELECT total_cents, status FROM invoices WHERE id=?", (inv["id"],)) == [(10000, "draft")]


def test_sending_a_posted_draft_whose_total_changed_is_refused(book):
    db, app, store, client, cid = book
    inv = _legacy_posted_draft(db, store, cid)
    # The old code let the draft be edited after the posting: 100.00 posted, 250.00 on the invoice.
    con = sqlite3.connect(db)
    con.execute("UPDATE invoice_lines SET unit_cents=25000, line_cents=25000 WHERE invoice_id=?", (inv["id"],))
    con.execute("UPDATE invoices SET subtotal_cents=25000, total_cents=25000 WHERE id=?", (inv["id"],))
    con.commit()
    con.close()
    journal_before = _sql(db, "SELECT * FROM journal_lines ORDER BY id")
    r = client.post(f"/api/invoices/{inv['id']}/send")
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert "already in the books" in detail
    assert "Accounts Receivable (1100) debit: $100.00 in the books, $250.00 on the invoice" in detail
    assert "Income (4000) credit: $100.00 in the books, $250.00 on the invoice" in detail
    r = client.post(f"/invoices/{inv['id']}/send")
    assert r.status_code == 303 and "void it and make a new one" in _error_of(r)
    assert _sql(db, "SELECT status FROM invoices WHERE id=?", (inv["id"],)) == [("draft",)]
    assert _sql(db, "SELECT * FROM journal_lines ORDER BY id") == journal_before
    # Voiding it takes the posting back out; AR is then exact.
    assert client.post(f"/invoices/{inv['id']}/void").status_code == 303
    assert _ar(db) == 0


def test_sending_a_posted_draft_that_still_matches_marks_it_sent_and_posts_nothing_more(book):
    db, app, store, client, cid = book
    inv = _legacy_posted_draft(db, store, cid)
    assert client.post(f"/invoices/{inv['id']}/send").status_code == 303
    assert _sql(db, "SELECT status FROM invoices WHERE id=?", (inv["id"],)) == [("sent",)]
    assert _sql(db, "SELECT COUNT(*) FROM journal WHERE ref_type='invoice' AND ref_id=?", (inv["id"],)) == [(1,)]
    assert client.post(f"/api/invoices/{inv['id']}/pay", json={"amount_cents": 10000, "paid_at": "2026-03-05"}).status_code == 200
    assert _ar(db) == 0 and store.get_invoice(inv["id"])["status"] == "paid"


# --- 4. guards whose tests could not fail -------------------------------------------


def _sent_over_http(client, cid, issue="2026-03-01", unit="100.00"):
    r = client.post(
        "/invoices/new",
        data={"customer_id": str(cid), "issue_date": issue, "due_date": issue, "description": "Labour", "qty": "1", "unit": unit},
    )
    assert r.status_code == 303, r.text
    iid = int(urlsplit(r.headers["location"]).path.rsplit("/", 1)[-1])
    assert client.post(f"/invoices/{iid}/send").status_code == 303
    return iid


def test_a_payment_reversal_is_dated_on_the_payment_not_today(book):
    db, app, store, client, cid = book
    iid = _sent_over_http(client, cid)
    r = client.post(f"/invoices/{iid}/pay", data={"amount": "100.00", "paid_at": "2026-03-05", "method": "bank"})
    assert r.status_code == 303
    (pid,) = _sql(db, "SELECT id FROM payments WHERE invoice_id=?", (iid,))[0]
    assert client.post(f"/invoices/{iid}/payments/{pid}/reverse").status_code == 303
    assert _sql(db, "SELECT posted_at FROM journal WHERE ref_type='payment_reversal' AND ref_id=?", (iid,)) == [("2026-03-05",)]
    # Cash in March nets to zero (the payment and its reversal are both in it),
    # and no later window sees a lone reversal.
    window = """SELECT COALESCE(SUM(jl.debit_cents - jl.credit_cents), 0) FROM journal_lines jl
                JOIN journal j ON j.id = jl.journal_id
                WHERE jl.account_code='1000' AND j.posted_at BETWEEN ? AND ?"""
    assert _sql(db, window, ("2026-03-01", "2026-03-31")) == [(0,)]
    assert _sql(db, window, ("2026-04-01", "2099-12-31")) == [(0,)]


def test_a_quantity_too_long_for_python_shows_the_generic_message(book):
    db, app, store, client, cid = book
    huge = "1" * 5000  # past Python's int() digit limit: a plain ValueError, not Invalid
    r = client.post("/invoices/new", data={"customer_id": str(cid), "description": "x", "qty": huge, "unit": "1"})
    assert r.status_code == 400
    assert GENERIC_ERROR.replace("'", "&#39;") in r.text
    assert "4300" not in r.text and "Exceeds" not in r.text and "limit" not in r.text
    r = client.post("/api/invoices", json={"customer_id": cid, "lines": [{"description": "x", "qty": huge, "unit": "1"}]})
    assert r.status_code == 400 and r.json() == {"detail": GENERIC_ERROR}
    assert _sql(db, "SELECT COUNT(*) FROM invoices") == [(0,)]


def test_a_plain_value_error_never_shows_its_text(book, monkeypatch):
    db, app, store, client, cid = book

    def broken(*args, **kwargs):
        raise ValueError("secret internals")

    monkeypatch.setattr(store, "list_invoices", broken)
    monkeypatch.setattr(store, "save_company", broken)
    page = client.get("/invoices")
    assert page.status_code == 400 and "secret" not in page.text
    assert GENERIC_ERROR.replace("'", "&#39;") in page.text
    api = client.get("/api/invoices")
    assert api.status_code == 400 and api.json() == {"detail": GENERIC_ERROR}
    form = client.post("/settings", data={"name": "Shop"})
    assert form.status_code == 303 and _error_of(form) == GENERIC_ERROR


def test_a_draft_with_journal_entries_cant_be_deleted(book):
    db, app, store, client, cid = book
    inv = _legacy_posted_draft(db, store, cid)
    r = client.post(f"/invoices/{inv['id']}/delete")
    assert r.status_code == 303 and "has entries in the books" in _error_of(r)
    r = client.post(f"/api/invoices/{inv['id']}/delete")
    assert r.status_code == 400 and "has entries in the books" in r.json()["detail"]
    assert _sql(db, "SELECT status, deleted_at FROM invoices WHERE id=?", (inv["id"],)) == [("draft", None)]
    assert _ar(db) == 10000


def test_an_invoice_with_payments_cant_be_voided(book):
    db, app, store, client, cid = book
    iid = _sent_over_http(client, cid)
    assert client.post(f"/api/invoices/{iid}/pay", json={"amount_cents": 4000, "paid_at": "2026-03-05"}).status_code == 200
    journal_before = _sql(db, "SELECT * FROM journal_lines ORDER BY id")
    r = client.post(f"/invoices/{iid}/void")
    assert r.status_code == 303 and "reverse the payments first" in _error_of(r)
    assert _sql(db, "SELECT status, paid_cents FROM invoices WHERE id=?", (iid,)) == [("partial", 4000)]
    assert _sql(db, "SELECT * FROM journal_lines ORDER BY id") == journal_before
    assert _ar(db) == 6000


# --- 5. each invoice and quote keeps its own currency ---------------------------------
# A book keeps one currency once anything is sent (test_final_fixes.py), but
# an older Till let it change afterwards. These books are left as they are, so
# each document still shows its own currency and totals carry a note.


def _legacy_switch(db, code):
    """Change the company's currency the way an older Till's Settings did,
    with documents already sent in the old one."""
    con = sqlite3.connect(db)
    try:
        con.execute("UPDATE company SET currency=?", (code,))
        con.commit()
    finally:
        con.close()


def test_an_invoice_sent_in_dollars_still_shows_dollars_after_switching_to_pounds(book):
    db, app, store, client, cid = book
    iid = _sent_over_http(client, cid, issue="2026-11-01")
    assert "$100.00" in client.get(f"/invoices/{iid}/print").text
    assert "one currency per book" in _error_of(client.post("/settings", data={"currency": "GBP"}))
    _legacy_switch(db, "GBP")
    for page in (f"/invoices/{iid}/print", f"/invoices/{iid}", "/invoices", f"/people/{cid}"):
        text = client.get(page).text
        assert "$100.00" in text, page
        assert "£100.00" not in text, page
    # Today's overdue list and Books' aging rows show the invoice in dollars
    # (their totals are the company's, with a note: see the next tests).
    for page in ("/", "/books"):
        assert "$100.00" in client.get(page).text, page
    assert client.get(f"/api/invoices/{iid}").json()["currency"] == "USD"
    assert [i["currency"] for i in client.get("/api/invoices").json()] == ["USD"]
    chase = client.get(f"/api/invoices/{iid}/chase").json()
    assert "$100.00" in chase["subject"] and "$100.00" in chase["body"]
    over = client.post(f"/api/invoices/{iid}/pay", json={"amount_cents": 20000, "paid_at": "2026-11-02"})
    assert over.status_code == 400 and "$100.00" in over.json()["detail"]
    assert "$100.00" in client.get("/api/brief").text
    # A new invoice takes the company's currency at the time it is made.
    new = _sent_over_http(client, cid, issue="2026-11-02", unit="50.00")
    assert client.get(f"/api/invoices/{new}").json()["currency"] == "GBP"
    assert "£50.00" in client.get(f"/invoices/{new}/print").text
    assert 'data-currency="£"' in client.get("/invoices/new").text


def test_a_draft_quote_keeps_its_currency_until_it_is_saved_again(book):
    db, app, store, client, cid = book
    quote = store.create_document("quote", cid, "2026-11-01", [{"description": "x", "qty_milli": 1000, "unit_cents": 7500, "tax_bps": 0}])
    _legacy_switch(db, "EUR")
    assert "$75.00" in client.get(f"/invoices/{quote['id']}/print").text
    # The edit form is in the book's currency and says the save will change it.
    page = client.get(f"/invoices/{quote['id']}/edit").text
    assert 'data-currency="€"' in page and "This draft is in USD. Saving it puts it in EUR" in page
    # Converting it as it stands would turn the customer's dollar price into euros: refused.
    r = client.post(f"/quotes/{quote['id']}/convert")
    assert r.status_code == 303 and "is in USD but this book is in EUR" in _error_of(r)
    assert store.get_invoice(quote["id"])["status"] == "draft"


def test_summaries_say_so_when_an_open_invoice_is_in_another_currency(book):
    db, app, store, client, cid = book
    note = "in another currency"
    _sent_over_http(client, cid, issue="2026-11-01")
    assert note not in client.get("/").text and note not in client.get("/books").text
    _legacy_switch(db, "GBP")
    for page in ("/", "/books"):
        text = client.get(page).text
        assert note in text and "USD" in text, page
    assert client.get("/api/books/aging").json()["other_currencies"] == ["USD"]
    assert client.get("/api/today").json()["other_currencies"] == ["USD"]
    assert "USD" in client.get("/api/brief").text


# --- 6. `till restore` refuses while something holds the book open -----------------


def _free_port() -> int:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _names(db):
    return [n for (n,) in _sql(db, "SELECT name FROM customers ORDER BY id")]


def test_restore_refuses_while_another_connection_holds_the_book(book, tmp_path, monkeypatch, capsys):
    from till.cli import main

    db, app, store, client, cid = book
    monkeypatch.setenv("TILL_DB", str(db))
    monkeypatch.setenv("TILL_PORT", str(_free_port()))  # nothing listening: only the lock can tell
    saved = tmp_path / "saved.db"
    assert main(["backup", str(saved)]) == 0
    store.create_customer(name="Keep me")
    holder = sqlite3.connect(db, isolation_level=None)
    worker = threading.Thread(target=lambda: None)
    worker.start()
    try:
        holder.execute("BEGIN IMMEDIATE")
        holder.execute("INSERT INTO vendors (name) VALUES ('mid-write')")
        # In a thread: without the check, restore would wait on the lock for
        # as long as it is held (SQLite's backup retries a busy target).
        result = []
        worker = threading.Thread(target=lambda: result.append(main(["restore", str(saved)])), daemon=True)
        worker.start()
        worker.join(timeout=10)
        assert not worker.is_alive(), "restore went ahead and waited on the held book"
        assert result == [1]
        err = capsys.readouterr().err
        assert "in use" in err and "--force" in err
    finally:
        holder.execute("ROLLBACK")
        holder.close()
        worker.join(timeout=30)
    assert _names(db) == ["Maria Chen", "Keep me"]
    assert not (db.parent / "backups").exists()  # refused before anything was written
    # Once nothing has it open, restore goes ahead.
    store.close()
    assert main(["restore", str(saved)]) == 0
    assert _names(db) == ["Maria Chen"]


# --- 7. Books explains why a void lands in the month it is made ----------------------


def test_books_explains_that_a_void_lands_on_the_day_it_is_made(book):
    db, app, store, client, cid = book
    from datetime import date, timedelta

    last_month = (date.today().replace(day=1) - timedelta(days=1)).replace(day=1).isoformat()
    iid = _sent_over_http(client, cid, issue=last_month)
    assert client.post(f"/invoices/{iid}/void").status_code == 303
    page = client.get("/books").text
    # The case the note is for: this month's income is below zero.
    assert "you invoiced -$100.00" in page
    accountant = page.split('id="accountant-heading"', 1)[1]
    assert "A void is recorded on the day you void the invoice, not on the invoice&#39;s own date" in accountant or (
        "A void is recorded on the day you void the invoice, not on the invoice's own date" in accountant
    )
