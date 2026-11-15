"""Calendar export (RFC 5545) and the chase email.

Events and invoices are made over HTTP into create_app on a file database.
Rows an old database could hold (raw CR/LF in a title, an end before its
start, a crafted email) are written through a second SQLite connection,
since today's validation refuses them on the way in.
"""

import html
import re
import sqlite3
from datetime import date, datetime, timedelta
from urllib.parse import parse_qs, unquote, urlsplit

import pytest
from fastapi.testclient import TestClient

from till.web import create_app

LOCAL = "http://127.0.0.1:7740"
CRAFTED = "victim@example.com?bcc=attacker@evil.example"


@pytest.fixture
def book(tmp_path):
    db = tmp_path / "till.db"
    app = create_app(db)
    store = app.state.store
    store.save_company(name="Northridge Workshop")
    cid = store.create_customer(name="Maria Chen", email="maria@example.com")
    client = TestClient(app, base_url=LOCAL, raise_server_exceptions=False, follow_redirects=False)
    return db, store, cid, client


def _exec(db, sql, args=()):
    con = sqlite3.connect(db)
    con.execute(sql, args)
    con.commit()
    con.close()


# --- a tiny RFC 5545 reader ---------------------------------------------------


def _unfold(raw: bytes) -> list[str]:
    assert raw.endswith(b"\r\n")
    physical = raw[:-2].split(b"\r\n")
    for line in physical:
        assert len(line) <= 75, line  # folded at 75 octets
        assert b"\r" not in line and b"\n" not in line
        line.decode("utf-8")  # never split inside a character
    logical: list[bytes] = []
    for line in physical:
        if line[:1] in (b" ", b"\t"):
            logical[-1] += line[1:]
        else:
            logical.append(line)
    return [line.decode("utf-8") for line in logical]


def _unescape(value: str) -> str:
    return re.sub(r"\\([\\;,nN])", lambda m: "\n" if m[1] in "nN" else m[1], value)


def _events(raw: bytes) -> list[dict]:
    lines = _unfold(raw)
    assert lines[0] == "BEGIN:VCALENDAR" and lines[-1] == "END:VCALENDAR"
    events, current = [], None
    for line in lines:
        name, _, value = line.partition(":")
        if line == "BEGIN:VEVENT":
            assert current is None
            current = {}
        elif line == "END:VEVENT":
            events.append(current)
            current = None
        elif current is not None:
            assert name not in current, f"{name} twice in one event"
            current[name] = value
    assert current is None
    return events


def _at(days: int, hh: int) -> str:
    return (datetime.combine(date.today() + timedelta(days=days), datetime.min.time()) + timedelta(hours=hh)).isoformat()


# --- (a) .ics -----------------------------------------------------------------


def test_injected_line_breaks_stay_inside_one_escaped_property(book):
    db, store, cid, client = book
    notes = "Bring the ladder.\r\nEND:VEVENT\r\nBEGIN:VEVENT\r\nSUMMARY:Forged\r\nURL:http://evil.example"
    r = client.post(
        "/api/calendar",
        json={
            "title": "Fix door; side, back \\ front",
            "start_at": _at(1, 9),
            "end_at": _at(1, 10),
            "location": "14 Pine St, Unit 2; rear",
            "notes": notes,
        },
    )
    assert r.status_code == 200, r.text
    # An old database row with raw CR/LF in its title (validation now refuses this).
    forged = "Old job\r\nEND:VEVENT\r\nBEGIN:VEVENT\r\nSUMMARY:Forged\r\nBEGIN:VALARM"
    _exec(db, "INSERT INTO events (title, start_at, end_at) VALUES (?,?,?)", (forged, _at(2, 9), _at(2, 10)))
    raw = client.get("/calendar.ics").content
    events = _events(raw)
    assert len(events) == 2
    first, old = events
    assert _unescape(first["SUMMARY"]) == "Fix door; side, back \\ front"
    assert _unescape(first["LOCATION"]) == "14 Pine St, Unit 2; rear"
    assert _unescape(first["DESCRIPTION"]) == notes.replace("\r\n", "\n")
    assert _unescape(old["SUMMARY"]) == forged.replace("\r\n", "\n")
    assert "URL" not in first and "BEGIN" not in old
    for e in events:
        assert re.fullmatch(r"[0-9]{8}T[0-9]{6}Z", e["DTSTAMP"])
        assert e["UID"].endswith("@till.local")


def test_ics_never_ends_an_event_before_it_starts(book):
    db, store, cid, client = book
    _exec(db, "INSERT INTO events (title, start_at, end_at) VALUES ('Backwards', ?, ?)", (_at(1, 11), _at(1, 9)))
    _exec(db, "INSERT INTO events (title, start_at, end_at) VALUES ('Instant', ?, ?)", (_at(1, 12), _at(1, 12)))
    client.post("/api/calendar", json={"title": "Normal", "start_at": _at(1, 14), "end_at": _at(1, 15)})
    for e in _events(client.get("/calendar.ics").content):
        if "DTEND" in e:
            assert e["DTEND"] > e["DTSTART"], e
    normal = [e for e in _events(client.get("/calendar.ics").content) if e["SUMMARY"] == "Normal"][0]
    assert normal["DTEND"] > normal["DTSTART"]


def test_ics_folds_long_multibyte_lines_on_character_boundaries(book):
    db, store, cid, client = book
    title = "Réparation de la porte d'entrée — " + "é" * 120
    client.post("/api/calendar", json={"title": title, "start_at": _at(1, 9), "end_at": _at(1, 10)})
    (event,) = _events(client.get("/calendar.ics").content)
    assert _unescape(event["SUMMARY"]) == title


def test_ics_names_the_local_time_zone_when_it_is_known(book, monkeypatch):
    db, store, cid, client = book
    monkeypatch.setenv("TZ", "Europe/Lisbon")
    assert "X-WR-TIMEZONE:Europe/Lisbon" in _unfold(client.get("/calendar.ics").content)
    monkeypatch.setenv("TZ", ":Not a zone at all")
    lines = _unfold(client.get("/calendar.ics").content)
    assert not any("Not a zone" in line for line in lines)


# --- (b) mailto ---------------------------------------------------------------


def _overdue(client, cid, days_late=10, terms=14):
    issued = date.today() - timedelta(days=days_late + terms)
    inv = client.post(
        "/api/invoices",
        json={
            "customer_id": cid,
            "issue_date": issued.isoformat(),
            "due_date": (issued + timedelta(days=terms)).isoformat(),
            "lines": [{"description": "Labour", "qty_milli": 1000, "unit_cents": 24000}],
        },
    ).json()
    assert client.post(f"/api/invoices/{inv['id']}/send").status_code == 200
    return inv["id"]


def _check_mailto(href: str, email: str):
    assert href.startswith("mailto:")
    address, _, query = href[len("mailto:") :].partition("?")
    assert unquote(address) == email
    assert "?" not in address and "&" not in address and "=" not in address
    assert set(parse_qs(query)) == {"subject", "body"}
    assert "bcc" not in query.lower()


def test_a_crafted_email_in_an_old_book_cannot_add_a_bcc(book):
    db, store, cid, client = book
    # Today's validation refuses it on the way in...
    r = client.post(f"/api/customers/{cid}/edit", json={"email": CRAFTED})
    assert r.status_code == 400
    # ...but an old database may already hold it.
    iid = _overdue(client, cid)
    _exec(db, "UPDATE customers SET email=? WHERE id=?", (CRAFTED, cid))
    chase = client.get(f"/api/invoices/{iid}/chase").json()
    _check_mailto(chase["mailto"], CRAFTED)
    for page in (client.get("/").text, client.get(f"/invoices/{iid}").text):
        hrefs = [html.unescape(h) for h in re.findall(r'href="(mailto:[^"]*)"', page)]
        assert len(hrefs) == 1
        _check_mailto(hrefs[0], CRAFTED)


# --- (c) chase wording, and only when it's due -----------------------------------


def test_overdue_chase_reads_naturally(book):
    db, store, cid, client = book
    iid = _overdue(client, cid, days_late=10)
    chase = client.get(f"/api/invoices/{iid}/chase").json()
    body = chase["body"]
    assert body.startswith("Hi Maria,\n\n")
    assert "$240.00" in body and "INV-0001" in body
    assert "10 days ago" in body
    assert "send payment" in body
    assert " ." not in body and "when ." not in body
    assert chase["overdue"] is True and chase["days_overdue"] == 10
    one = _overdue(client, cid, days_late=1)
    assert "1 day ago" in client.get(f"/api/invoices/{one}/chase").json()["body"]
    assert "Chase" in client.get(f"/invoices/{iid}").text
    assert f'href="/invoices/{iid}"' in client.get("/").text


def test_an_invoice_not_yet_due_offers_a_friendly_reminder_not_a_chase(book):
    db, store, cid, client = book
    inv = client.post(
        "/api/invoices",
        json={
            "customer_id": cid,
            "issue_date": date.today().isoformat(),
            "due_date": (date.today() + timedelta(days=14)).isoformat(),
            "lines": [{"description": "Labour", "qty_milli": 1000, "unit_cents": 24000}],
        },
    ).json()
    client.post(f"/api/invoices/{inv['id']}/send")
    chase = client.get(f"/api/invoices/{inv['id']}/chase").json()
    assert chase["overdue"] is False and chase["days_overdue"] == 0
    assert "reminder" in chase["body"].lower()
    assert "ago" not in chase["body"] and "overdue" not in chase["body"].lower()
    page = client.get(f"/invoices/{inv['id']}").text
    assert "Send a reminder" in page and "Chase" not in page
    today = client.get("/").text
    assert "mailto:" not in today and "Chase" not in today
