"""A Tuesday that already has work on it. Dates are relative to today.

The sample business goes into its own book (`till demo`, data/demo.db), never
into a book that holds someone's real data: see `book_kind`.
"""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from pathlib import Path

from till.store import Store

# Tables that hold the owner's records. A row in any of them means real data.
RECORD_TABLES = (
    "customers", "vendors", "products", "jobs", "invoices", "payments",
    "journal", "expenses", "events", "reviews", "deals", "activities", "recurring",
)
# The company row a brand-new book starts with (store.migrate).
BLANK_COMPANY_NAME = "Your workshop"
COMPANY_DETAILS = ("email", "phone", "address", "tax_id", "payment_instructions")


def book_kind(path: str | Path) -> str:
    """What is in the file at `path`, read without changing it.

    "missing": no file. "empty": a new book (or an empty file) with nothing
    in it yet. "demo": a sample book (`company.is_demo`). "real": a Till book
    with the owner's records or business details in it. "foreign": a file that
    isn't a Till book, or can't be read. Only "missing", "empty" and "demo"
    may ever receive sample data.
    """
    path = Path(path)
    if not path.exists():
        return "missing"
    if path.is_file() and path.stat().st_size == 0:
        return "empty"
    try:
        con = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    except sqlite3.Error:
        return "foreign"
    try:
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not tables:
            return "empty"
        if "company" not in tables:
            return "foreign"
        con.row_factory = sqlite3.Row
        company = con.execute("SELECT * FROM company WHERE id=1").fetchone()
        if company is not None and "is_demo" in company.keys() and company["is_demo"]:
            return "demo"
        for table in RECORD_TABLES:
            if table in tables and con.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone():
                return "real"
        if company is not None:
            if company["name"] != BLANK_COMPANY_NAME:
                return "real"
            if any(k in company.keys() and company[k] for k in COMPANY_DETAILS):
                return "real"
        return "empty"
    except sqlite3.Error:
        return "foreign"
    finally:
        con.close()


def remove_book(path: str | Path) -> None:
    """Delete a book file and the sidecars that belong to it (-wal, -shm and
    -journal), so a new book never inherits an old one's write-ahead log."""
    for side in ("", "-wal", "-shm", "-journal"):
        Path(f"{path}{side}").unlink(missing_ok=True)


def seed(store: Store) -> None:
    today = date.today()
    store.save_company(
        name="Northridge Workshop",
        email="shop@northridge.example",
        phone="555-0142",
        address="88 Northridge Lane",
        tax_id="XX-0000000",
        currency="USD",
        payment_instructions="Bank transfer to Northridge Workshop, account 00-0000-0000000-00.\nPlease use the invoice number as the reference.",
        # Today shows a "this is sample data" banner while this is set.
        is_demo=True,
    )
    store._post(
        today.isoformat(),
        "Opening cash",
        [("1000", 250000, 0), ("3000", 0, 250000)],
        "opening",
        0,
    )
    store.create_product("Shop hour", 8000, 0)
    store.create_product("Callout", 12000, 0)

    maria = store.create_customer(
        name="Maria Chen",
        email="maria@example.com",
        phone="555-0100",
        address="12 Oak Avenue",
    )
    rafi = store.create_customer(
        name="Rafi Cole",
        email="rafi@example.com",
        phone="555-0101",
        address="14 Pine Street",
    )
    ada = store.create_customer(
        name="Ada West",
        email="ada@example.com",
        address="9 Canal Walk",
    )
    cafe = store.create_customer(
        name="Pine Street Cafe",
        email="hello@pinestreet.example",
        address="2 Pine Street",
    )

    overdue = store.create_invoice(
        customer_id=rafi,
        issue_date=(today - timedelta(days=21)).isoformat(),
        due_date=(today - timedelta(days=7)).isoformat(),
        notes="Front door closer",
    )
    store.add_line(overdue["id"], "Callout", 1000, 12000, 0)
    store.add_line(overdue["id"], "Shop hour — closer swap", 1500, 8000, 0)
    store.send_invoice(overdue["id"], sent_at=(today - timedelta(days=21)).isoformat())

    paid = store.create_invoice(
        customer_id=ada,
        issue_date=(today - timedelta(days=10)).isoformat(),
        due_date=(today - timedelta(days=3)).isoformat(),
    )
    store.add_line(paid["id"], "Shop hour — cabinet catch", 2000, 8000, 0)
    store.send_invoice(paid["id"], sent_at=(today - timedelta(days=10)).isoformat())
    store.record_payment(paid["id"], 16000, (today - timedelta(days=4)).isoformat(), method="bank")

    draft = store.create_invoice(
        customer_id=cafe,
        issue_date=today.isoformat(),
        due_date=(today + timedelta(days=14)).isoformat(),
    )
    store.add_line(draft["id"], "Shop hour — espresso machine shelf", 3000, 8000, 0)

    quote = store.create_quote(
        customer_id=maria,
        issue_date=today.isoformat(),
        due_date=(today + timedelta(days=10)).isoformat(),
    )
    store.add_line(quote["id"], "Rebuild gearbox", 1000, 75000, 0)

    job = store.create_job(
        customer_id=maria,
        title="Kitchen hinge",
        status="scheduled",
        scheduled_for=today.isoformat(),
    )
    store.create_event(
        title="Kitchen hinge — on site",
        start_at=f"{today.isoformat()}T09:00:00",
        end_at=f"{today.isoformat()}T11:00:00",
        customer_id=maria,
        job_id=job["id"],
        location="12 Oak Avenue",
    )
    store.create_event(
        title="Cafe shelf measure",
        start_at=f"{(today + timedelta(days=1)).isoformat()}T14:00:00",
        end_at=f"{(today + timedelta(days=1)).isoformat()}T15:00:00",
        customer_id=cafe,
        location="2 Pine Street",
    )

    store.record_expense(
        vendor_name="City Hardware",
        amount_cents=3499,
        account_code="5100",
        spent_on=(today - timedelta(days=2)).isoformat(),
        memo="Blades",
    )
    store.record_expense(
        vendor_name="Landlord",
        amount_cents=120000,
        account_code="5300",
        spent_on=today.replace(day=1).isoformat(),
        memo="Shop rent",
    )

    store.add_review(
        source="google",
        author="Sam",
        rating=2,
        body="Waited an hour in the rain.",
        received_at=f"{(today - timedelta(days=1)).isoformat()}T18:00:00",
    )
    priya = store.add_review(
        source="google",
        author="Priya",
        rating=5,
        body="Fixed the catch the same afternoon.",
        received_at=f"{(today - timedelta(days=4)).isoformat()}T12:00:00",
    )
    store.mark_review_replied(priya["id"])

    store.create_deal("Cafe expansion shelves", "quoted", cafe, 240000)
    store.add_activity(rafi, "note", "Promised a reminder this week.")
    next_month = (today.replace(day=1) + timedelta(days=32)).replace(day=1)
    store.create_recurring(
        customer_id=ada,
        cadence="monthly",
        next_date=next_month.isoformat(),
        lines=[{"description": "Monthly shop hours", "qty_milli": 4000, "unit_cents": 8000, "tax_bps": 0}],
        notes="Ada retainer",
    )
