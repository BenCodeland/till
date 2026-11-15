"""Calendar, jobs, reviews, and the Tuesday brief."""

from datetime import date, datetime, timedelta

from till.store import Store


def test_calendar_lists_events_on_a_day(tmp_path):
    store = Store(tmp_path / "till.db")
    store.migrate()
    cid = store.create_customer(name="Rafi Cole")
    job = store.create_job(customer_id=cid, title="Kitchen hinge", status="scheduled")
    store.create_event(
        title="Kitchen hinge — on site",
        start_at="2026-11-12T09:00:00",
        end_at="2026-11-12T11:00:00",
        customer_id=cid,
        job_id=job["id"],
        location="14 Pine Street",
    )
    store.create_event(
        title="Tomorrow",
        start_at="2026-11-13T09:00:00",
        end_at="2026-11-13T10:00:00",
    )
    day = store.events_on("2026-11-12")
    assert len(day) == 1
    assert day[0]["title"].startswith("Kitchen")
    assert day[0]["customer_name"] == "Rafi Cole"


def test_review_draft_reply_stays_until_marked(tmp_path):
    store = Store(tmp_path / "till.db")
    store.migrate()
    rev = store.add_review(
        source="google",
        author="Sam",
        rating=2,
        body="Waited an hour.",
        received_at="2026-11-11T18:00:00",
    )
    store.draft_review_reply(rev["id"], "Sorry about the wait — coffee's on us next time.")
    rev = store.get_review(rev["id"])
    assert rev["status"] == "drafted"
    assert "coffee" in rev["reply_draft"]
    store.mark_review_replied(rev["id"], replied_at="2026-11-12T09:00:00")
    rev = store.get_review(rev["id"])
    assert rev["status"] == "replied"


def test_today_brief_names_overdue_jobs_and_reviews(tmp_path):
    store = Store(tmp_path / "till.db")
    store.migrate()
    cid = store.create_customer(name="Rafi Cole")
    yesterday = (date.today() - timedelta(days=3)).isoformat()
    inv = store.create_invoice(customer_id=cid, issue_date=yesterday, due_date=yesterday)
    store.add_line(inv["id"], "Callout", qty_milli=1000, unit_cents=12000, tax_bps=0)
    store.send_invoice(inv["id"], sent_at=yesterday)
    now = datetime.now().replace(hour=10, minute=0, second=0, microsecond=0)
    store.create_event(
        title="Follow-up",
        start_at=now.isoformat(timespec="seconds"),
        end_at=(now.replace(hour=11)).isoformat(timespec="seconds"),
        customer_id=cid,
    )
    store.add_review(
        source="google",
        author="Sam",
        rating=5,
        body="Fixed it fast.",
        received_at=now.isoformat(timespec="seconds"),
    )
    brief = store.today_brief(today=date.today().isoformat())
    assert brief["overdue_count"] == 1
    assert brief["overdue_cents"] == 12000
    assert brief["review_count"] >= 1
    assert any("Follow-up" in e["title"] for e in brief["events"])
    text = store.brief_text(today=date.today().isoformat())
    assert "Rafi Cole" in text
    assert "overdue" in text.lower()
