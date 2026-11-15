"""The request guard: only loopback Hosts, no cross-site writes, JSON-only API bodies.

Every test goes through HTTP into create_app. "No state change" is checked by
reading every table of the database file through a separate connection.
"""

import sqlite3
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from till.web import create_app

LOCAL = "http://127.0.0.1:7740"
EVIL = "https://evil.example"


def _snapshot(db_path) -> dict:
    con = sqlite3.connect(db_path)
    try:
        tables = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        return {t: con.execute(f"SELECT * FROM {t} ORDER BY rowid").fetchall() for t in tables}
    finally:
        con.close()


@pytest.fixture
def book(tmp_path):
    """A small book with one target of every kind a write route can touch."""
    db = tmp_path / "till.db"
    app = create_app(db)
    store = app.state.store
    cid = store.create_customer(name="Maria Chen", email="maria@example.com")
    draft = store.create_invoice(customer_id=cid, issue_date="2026-11-01", due_date="2026-11-15")
    store.add_line(draft["id"], "Labour", 1000, 10000, 0)
    sent = store.create_invoice(customer_id=cid, issue_date="2026-11-01", due_date="2026-11-15")
    store.add_line(sent["id"], "Labour", 1000, 20000, 0)
    store.send_invoice(sent["id"], sent_at="2026-11-01")
    quote = store.create_quote(customer_id=cid, issue_date="2026-11-01", due_date="2026-11-15")
    store.add_line(quote["id"], "Quote work", 1000, 5000, 0)
    rec = store.create_recurring(
        customer_id=cid,
        cadence="monthly",
        next_date=(date.today() - timedelta(days=1)).isoformat(),
        lines=[{"description": "Retainer", "qty_milli": 1000, "unit_cents": 8000, "tax_bps": 0}],
    )
    job = store.create_job(customer_id=cid, title="Kitchen hinge")
    review = store.add_review("google", "Sam", 2, "Waited.", "2026-11-11T18:00:00")
    ids = {
        "cid": cid,
        "draft": draft["id"],
        "sent": sent["id"],
        "quote": quote["id"],
        "rec": rec["id"],
        "job": job["id"],
        "review": review["id"],
    }
    return app, db, ids


# (path template, form data or None, json body or None)
FORM_ROUTES = [
    ("/invoices/new", {"customer_id": "{cid}", "description": "Hinge", "qty": "1", "unit": "10.00", "tax_bps": "0"}),
    ("/invoices/{draft}/send", None),
    ("/invoices/{sent}/pay", {"amount": "50.00", "paid_at": "2026-11-10", "method": "bank"}),
    ("/invoices/{draft}/void", None),
    ("/quotes/new", {"customer_id": "{cid}", "description": "Quote", "qty": "1", "unit": "10.00", "tax_bps": "0"}),
    ("/quotes/{quote}/convert", None),
    ("/recurring", {"customer_id": "{cid}", "cadence": "monthly", "next_date": "2027-01-01", "unit": "10.00"}),
    ("/recurring/{rec}/stop", None),
    ("/tick", None),
    ("/people/new", {"name": "CSRF customer"}),
    ("/jobs/new", {"customer_id": "{cid}", "title": "CSRF job"}),
    ("/jobs/{job}/status", {"status": "done"}),
    # /jobs/{job}/invoice is left out: it now only opens the invoice form and writes nothing.
    ("/books/expenses", {"vendor_name": "CSRF", "amount": "999.99", "account_code": "5000"}),
    ("/calendar", {"title": "CSRF event", "day": "2026-11-20"}),
    ("/reviews", {"author": "CSRF", "rating": "1", "body": "x"}),
    ("/reviews/{review}/draft", {"body": "CSRF reply"}),
    ("/reviews/{review}/replied", None),
    ("/settings", {"name": "Pwned Workshop"}),
    ("/pipeline", {"title": "CSRF deal", "value": "10"}),
]

API_ROUTES = [
    ("/api/invoices", {"customer_id": "{cid}", "lines": [{"description": "x", "qty": 1, "unit_cents": 100}]}),
    ("/api/invoices/{draft}/send", None),
    ("/api/invoices/{sent}/pay", {"amount_cents": 100}),
    ("/api/customers", {"name": "CSRF"}),
    ("/api/expenses", {"vendor_name": "CSRF", "amount_cents": 100}),
    ("/api/calendar", {"title": "CSRF", "start_at": "2026-11-20T09:00:00", "end_at": "2026-11-20T10:00:00"}),
    ("/api/reviews/{review}/draft", {"body": "CSRF"}),
    ("/api/reviews/{review}/replied", None),
    ("/api/jobs", {"customer_id": "{cid}", "title": "CSRF"}),
    ("/api/recurring", {"customer_id": "{cid}", "unit_cents": 100}),
    ("/api/tick", {}),
]


def _fill(value, ids):
    if isinstance(value, str):
        return value.format(**ids)
    if isinstance(value, dict):
        return {k: _fill(v, ids) for k, v in value.items()}
    if isinstance(value, list):
        return [_fill(v, ids) for v in value]
    return value


def _fill_json(value, ids):
    out = _fill(value, ids)
    if isinstance(out, dict) and "customer_id" in out:
        out["customer_id"] = int(out["customer_id"])
    return out


def _post(client, path, form=None, body=None, headers=None):
    if body is not None:
        return client.post(path, json=body, headers=headers or {}, follow_redirects=False)
    return client.post(path, data=form, headers=headers or {}, follow_redirects=False)


@pytest.mark.parametrize("path,form", FORM_ROUTES, ids=[r[0] for r in FORM_ROUTES])
def test_form_route_with_foreign_origin_is_refused_and_changes_nothing(book, path, form):
    app, db, ids = book
    c = TestClient(app, base_url=LOCAL)
    url = path.format(**ids)
    data = _fill(form, ids) if form else None
    before = _snapshot(db)
    r = _post(c, url, form=data, headers={"Origin": EVIL})
    assert r.status_code == 403
    assert _snapshot(db) == before
    # The same request without an Origin (curl, Hermes) does change the book,
    # so the refusal above is what kept it unchanged.
    ok = _post(c, url, form=data)
    assert ok.status_code < 400, ok.text
    assert _snapshot(db) != before


@pytest.mark.parametrize("path,body", API_ROUTES, ids=[r[0] for r in API_ROUTES])
def test_api_route_with_foreign_origin_is_refused_and_changes_nothing(book, path, body):
    app, db, ids = book
    c = TestClient(app, base_url=LOCAL)
    url = path.format(**ids)
    payload = _fill_json(body, ids) if body is not None else None
    before = _snapshot(db)
    r = _post(c, url, body=payload, headers={"Origin": EVIL})
    assert r.status_code == 403
    assert _snapshot(db) == before
    ok = _post(c, url, body=payload)
    assert ok.status_code < 400, ok.text
    assert _snapshot(db) != before


@pytest.mark.parametrize(
    "headers",
    [
        {"Origin": "null"},
        {"Origin": "http://127.0.0.1:9999"},  # another program on this machine
        {"Origin": "https://127.0.0.1:7740"},  # same host and port, different scheme
        {"Origin": "http://127.0.0.1.evil.example:7740"},
        {"Origin": "http://localhost.evil.example"},
        {"Sec-Fetch-Site": "cross-site"},
        {"Sec-Fetch-Site": "same-site"},
        {"Origin": LOCAL, "Sec-Fetch-Site": "cross-site"},
    ],
    ids=["origin-null", "other-port", "other-scheme", "suffix-host", "localhost-suffix", "sfs-cross-site", "sfs-same-site", "local-origin-but-cross-site"],
)
def test_cross_site_signals_refuse_a_settings_change(book, headers):
    app, db, _ = book
    c = TestClient(app, base_url=LOCAL)
    before = _snapshot(db)
    r = c.post("/settings", data={"name": "Pwned Workshop"}, headers=headers, follow_redirects=False)
    assert r.status_code == 403
    assert _snapshot(db) == before
    assert app.state.store.get_company()["name"] != "Pwned Workshop"


@pytest.mark.parametrize(
    "base,headers",
    [
        (LOCAL, {"Origin": LOCAL, "Sec-Fetch-Site": "same-origin"}),
        ("http://localhost:7740", {"Origin": "http://localhost:7740", "Sec-Fetch-Site": "same-origin"}),
        ("http://[::1]:7740", {"Origin": "http://[::1]:7740"}),
        (LOCAL, {"Sec-Fetch-Site": "none"}),
    ],
    ids=["127-same-origin", "localhost-same-origin", "ipv6-origin", "sfs-none"],
)
def test_same_origin_form_post_still_works(book, base, headers):
    app, _, _ = book
    c = TestClient(app, base_url=base)
    r = c.post("/settings", data={"name": "Northridge"}, headers=headers, follow_redirects=False)
    assert r.status_code == 303
    assert app.state.store.get_company()["name"] == "Northridge"


def test_curl_style_request_without_origin_works(book):
    app, _, ids = book
    c = TestClient(app, base_url=LOCAL)
    r = c.post("/api/customers", json={"name": "From Hermes"})
    assert r.status_code == 200
    assert r.json()["name"] == "From Hermes"
    assert c.post(f"/api/invoices/{ids['draft']}/send").json()["status"] == "sent"


@pytest.mark.parametrize(
    "host",
    ["rebind.evil.example", "rebind.evil.example:7740", "127.0.0.1.evil.example", "testserver", "127.0.0.1:abc", ""],
)
def test_foreign_host_is_refused(book, host):
    app, _, _ = book
    c = TestClient(app, base_url=LOCAL)
    r = c.get("/api/invoices", headers={"Host": host})
    assert r.status_code == 400
    assert "Maria" not in r.text
    assert c.get("/", headers={"Host": host}).status_code == 400


@pytest.mark.parametrize("host", ["127.0.0.1:7740", "127.0.0.1", "localhost:7740", "localhost", "LOCALHOST:7740", "[::1]:7740", "[::1]"])
def test_loopback_hosts_are_served(book, host):
    app, _, _ = book
    c = TestClient(app, base_url=LOCAL)
    r = c.get("/api/invoices", headers={"Host": host})
    assert r.status_code == 200
    assert r.json()[0]["customer_name"] == "Maria Chen"


def test_testserver_host_only_when_asked_for(tmp_path):
    default = TestClient(create_app(tmp_path / "a.db"))
    assert default.get("/api/health").status_code == 400
    allowed = TestClient(create_app(tmp_path / "b.db", allowed_hosts=["testserver"]))
    assert allowed.get("/api/health").status_code == 200


def test_api_tick_with_text_plain_body_is_refused(book):
    """An HTML form with enctype=text/plain can produce a body that parses as JSON."""
    app, db, _ = book
    c = TestClient(app, base_url=LOCAL)
    before = _snapshot(db)
    r = c.post("/api/tick", content=b'{"as_of": "2027-12-31"}', headers={"Content-Type": "text/plain"})
    assert r.status_code == 415
    assert _snapshot(db) == before
    r = c.post("/api/customers", content=b'{"name": "x"}', headers={"Content-Type": "text/plain"})
    assert r.status_code == 415
    r = c.post("/api/customers", data={"name": "x"})
    assert r.status_code == 415
    assert _snapshot(db) == before


def test_chunked_text_plain_body_is_refused(book):
    """A body sent without Content-Length (chunked) still counts as a body."""
    app, db, _ = book
    c = TestClient(app, base_url=LOCAL)
    before = _snapshot(db)

    def chunks():
        yield b'{"as_of": '
        yield b'"2027-12-31"}'

    r = c.post("/api/tick", content=chunks(), headers={"Content-Type": "text/plain"})
    assert r.status_code == 415
    assert _snapshot(db) == before


def test_bodyless_api_post_and_json_post_pass(book):
    app, _, _ = book
    c = TestClient(app, base_url=LOCAL)
    assert c.post("/api/tick").status_code == 200
    r = c.post("/api/customers", content=b'{"name": "Charset"}', headers={"Content-Type": "application/json; charset=utf-8"})
    assert r.status_code == 200


def test_openapi_is_off_and_security_headers_are_set(book):
    app, _, _ = book
    c = TestClient(app, base_url=LOCAL)
    assert c.get("/openapi.json").status_code == 404
    for r in (c.get("/"), c.get("/api/health"), c.get("/static/till.css"), c.get("/api/x", headers={"Host": "evil.example"})):
        assert r.headers["x-content-type-options"] == "nosniff"
        assert r.headers["referrer-policy"] == "no-referrer"
        assert r.headers["content-security-policy"] == "frame-ancestors 'none'"
