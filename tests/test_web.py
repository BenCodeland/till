"""HTTP paths must actually send, pay, and brief — not just render."""

from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from till.web import create_app

LOCAL = "http://127.0.0.1:7740"


@pytest.fixture
def app_store(tmp_path):
    app = create_app(tmp_path / "till.db")
    store = app.state.store
    cid = store.create_customer(name="Maria Chen", email="maria@example.com", phone="555-0100")
    return app, store, cid


@pytest.fixture
def client(app_store):
    app, _, _ = app_store
    return TestClient(app, base_url=LOCAL)


def test_today_shows_overdue_and_job(app_store):
    app, store, cid = app_store
    yesterday = (date.today() - timedelta(days=2)).isoformat()
    inv = store.create_invoice(customer_id=cid, issue_date=yesterday, due_date=yesterday)
    store.add_line(inv["id"], "Callout", qty_milli=1000, unit_cents=18000, tax_bps=0)
    store.send_invoice(inv["id"], sent_at=yesterday)
    store.create_event(
        title="Hinge repair",
        start_at=f"{date.today().isoformat()}T09:00:00",
        end_at=f"{date.today().isoformat()}T11:00:00",
        customer_id=cid,
    )
    page = TestClient(app, base_url=LOCAL).get("/")
    assert page.status_code == 200
    assert "Maria Chen" in page.text
    assert "Hinge repair" in page.text
    assert "$180.00" in page.text


def test_form_create_send_and_pay_posts_journal(app_store):
    app, store, cid = app_store
    c = TestClient(app, base_url=LOCAL)
    created = c.post(
        "/invoices/new",
        data={
            "customer_id": str(cid),
            "issue_date": "2026-11-01",
            "due_date": "2026-11-15",
            "description": "Shop hours",
            "qty": "2",
            "unit": "80.00",
            "tax_bps": "0",
        },
        follow_redirects=False,
    )
    assert created.status_code == 303
    loc = created.headers["location"]
    invoice_id = int(loc.rsplit("/", 1)[-1])
    sent = c.post(f"/invoices/{invoice_id}/send", follow_redirects=False)
    assert sent.status_code == 303
    paid = c.post(
        f"/invoices/{invoice_id}/pay",
        data={"amount": "160.00", "paid_at": "2026-11-10", "method": "bank"},
        follow_redirects=False,
    )
    assert paid.status_code == 303
    inv = store.get_invoice(invoice_id)
    assert inv["status"] == "paid"
    assert inv["total_cents"] == 16000
    assert store.trial_balance()["1000"] == 16000
    assert store.trial_balance()["1100"] == 0
    print_page = c.get(f"/invoices/{invoice_id}/print")
    assert print_page.status_code == 200
    assert "INV-" in print_page.text
    assert "$160.00" in print_page.text


def test_api_invoice_and_brief(app_store):
    app, store, cid = app_store
    c = TestClient(app, base_url=LOCAL)
    r = c.post(
        "/api/invoices",
        json={
            "customer_id": cid,
            "issue_date": "2026-11-01",
            "due_date": "2026-11-08",
            "lines": [{"description": "Labour", "qty": 1, "unit_cents": 9000, "tax_bps": 0}],
        },
    )
    assert r.status_code == 200
    iid = r.json()["id"]
    assert c.post(f"/api/invoices/{iid}/send").json()["status"] == "sent"
    pay = c.post(f"/api/invoices/{iid}/pay", json={"amount_cents": 9000, "paid_at": "2026-11-09"})
    assert pay.json()["status"] == "paid"
    brief = c.get("/api/brief")
    assert brief.status_code == 200
    assert "Cash" in brief.text
    health = c.get("/api/health")
    assert health.json()["ok"] is True
    assert health.json()["balanced"] is True


def test_calendar_and_review_forms(app_store):
    app, store, cid = app_store
    c = TestClient(app, base_url=LOCAL)
    ev = c.post(
        "/calendar",
        data={
            "title": "Site visit",
            "day": "2026-11-20",
            "start_time": "14:00",
            "end_time": "15:30",
            "customer_id": str(cid),
            "location": "14 Pine",
        },
        follow_redirects=False,
    )
    assert ev.status_code == 303
    cal = c.get("/calendar?day=2026-11-20")
    assert "Site visit" in cal.text
    ics = c.get("/calendar.ics")
    assert "BEGIN:VEVENT" in ics.text
    rev = store.add_review("google", "Sam", 2, "Waited.", "2026-11-11T18:00:00")
    c.post(f"/reviews/{rev['id']}/draft", data={"body": "Sorry — coffee next visit."})
    assert store.get_review(rev["id"])["status"] == "drafted"
    page = c.get("/reviews")
    assert "coffee" in page.text


def test_chase_api_and_invoice_page_offer_mailto(app_store):
    app, store, cid = app_store
    yesterday = (date.today() - timedelta(days=4)).isoformat()
    inv = store.create_invoice(customer_id=cid, issue_date=yesterday, due_date=yesterday)
    store.add_line(inv["id"], "Labour", 1000, 12000, 0)
    store.send_invoice(inv["id"], sent_at=yesterday)
    c = TestClient(app, base_url=LOCAL)
    chase = c.get(f"/api/invoices/{inv['id']}/chase")
    assert chase.status_code == 200
    body = chase.json()
    assert body["balance_cents"] == 12000
    assert "mailto:maria@example.com" in body["mailto"]
    page = c.get(f"/invoices/{inv['id']}")
    assert "mailto:maria@example.com" in page.text
    assert "Chase" in page.text
    today = c.get("/")
    assert "Chase" in today.text


def test_retainer_form_ticks_a_draft_invoice(app_store):
    app, store, cid = app_store
    store.create_product("Shop hour", 8000, 0)
    c = TestClient(app, base_url=LOCAL)
    created = c.post(
        "/recurring",
        data={
            "customer_id": str(cid),
            "cadence": "monthly",
            "next_date": "2026-11-01",
            "description": "Monthly retainer",
            "qty": "4",
            "unit": "80.00",
            "tax_bps": "0",
        },
        follow_redirects=False,
    )
    assert created.status_code == 303
    recs = store.list_recurring()
    assert len(recs) == 1
    tick = c.post("/tick", follow_redirects=False)
    assert tick.status_code == 303
    drafts = store.list_invoices(status="draft")
    assert any(i["total_cents"] == 32000 for i in drafts)
    again = c.post("/api/tick", json={})
    assert again.status_code == 200
    assert again.json() == {"created": [], "failed": []}
