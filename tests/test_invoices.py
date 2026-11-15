"""Invoice lifecycle must go through send/pay, not a status flag."""

from datetime import date, timedelta

import pytest

from till.store import Store


def _workshop(tmp_path):
    store = Store(tmp_path / "till.db")
    store.migrate()
    cid = store.create_customer(name="Maria Chen", email="maria@example.com")
    return store, cid


def test_draft_invoice_totals_lines_and_tax(tmp_path):
    store, cid = _workshop(tmp_path)
    inv = store.create_invoice(customer_id=cid, issue_date="2026-11-01", due_date="2026-11-15")
    store.add_line(inv["id"], "Shop hours", qty_milli=2500, unit_cents=8000, tax_bps=500)
    store.add_line(inv["id"], "Parts", qty_milli=1000, unit_cents=4500, tax_bps=500)
    inv = store.get_invoice(inv["id"])
    # 2.5 * 80.00 = 200.00; parts 45.00; subtotal 245.00; 5% tax 12.25
    assert inv["subtotal_cents"] == 24500
    assert inv["tax_cents"] == 1225
    assert inv["total_cents"] == 25725
    assert inv["status"] == "draft"
    assert inv["number"].startswith("INV-")


def test_send_posts_receivable_and_income_once(tmp_path):
    store, cid = _workshop(tmp_path)
    inv = store.create_invoice(customer_id=cid, issue_date="2026-11-01", due_date="2026-11-15")
    store.add_line(inv["id"], "Labour", qty_milli=1000, unit_cents=10000, tax_bps=0)
    store.send_invoice(inv["id"], sent_at="2026-11-02")
    store.send_invoice(inv["id"], sent_at="2026-11-03")  # idempotent
    inv = store.get_invoice(inv["id"])
    assert inv["status"] == "sent"
    tb = store.trial_balance()
    assert tb["1100"] == 10000  # AR debit
    assert tb["4000"] == -10000  # income credit (credit shown negative in TB net)
    assert tb["1000"] == 0
    assert store.journal_is_balanced()


def test_payment_clears_ar_and_marks_paid(tmp_path):
    store, cid = _workshop(tmp_path)
    inv = store.create_invoice(customer_id=cid, issue_date="2026-11-01", due_date="2026-11-15")
    store.add_line(inv["id"], "Labour", qty_milli=1000, unit_cents=20000, tax_bps=0)
    store.send_invoice(inv["id"], sent_at="2026-11-02")
    store.record_payment(inv["id"], amount_cents=20000, paid_at="2026-11-10", method="bank")
    inv = store.get_invoice(inv["id"])
    assert inv["status"] == "paid"
    assert inv["paid_cents"] == 20000
    assert inv["balance_cents"] == 0
    tb = store.trial_balance()
    assert tb["1100"] == 0
    assert tb["1000"] == 20000
    assert tb["4000"] == -20000
    assert store.journal_is_balanced()


def test_partial_payment_leaves_balance(tmp_path):
    store, cid = _workshop(tmp_path)
    inv = store.create_invoice(customer_id=cid, issue_date="2026-11-01", due_date="2026-11-15")
    store.add_line(inv["id"], "Labour", qty_milli=1000, unit_cents=20000, tax_bps=0)
    store.send_invoice(inv["id"], sent_at="2026-11-02")
    store.record_payment(inv["id"], amount_cents=5000, paid_at="2026-11-10", method="cash")
    inv = store.get_invoice(inv["id"])
    assert inv["status"] == "partial"
    assert inv["balance_cents"] == 15000
    tb = store.trial_balance()
    assert tb["1100"] == 15000
    assert tb["1000"] == 5000


def test_cannot_pay_a_draft(tmp_path):
    store, cid = _workshop(tmp_path)
    inv = store.create_invoice(customer_id=cid, issue_date="2026-11-01", due_date="2026-11-15")
    store.add_line(inv["id"], "Labour", qty_milli=1000, unit_cents=10000, tax_bps=0)
    with pytest.raises(ValueError, match="draft"):
        store.record_payment(inv["id"], amount_cents=10000, paid_at="2026-11-10", method="cash")


def test_overdue_is_derived_from_due_date_and_balance(tmp_path):
    store, cid = _workshop(tmp_path)
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    last_week = (date.today() - timedelta(days=7)).isoformat()
    inv = store.create_invoice(customer_id=cid, issue_date=last_week, due_date=yesterday)
    store.add_line(inv["id"], "Labour", qty_milli=1000, unit_cents=9000, tax_bps=0)
    store.send_invoice(inv["id"], sent_at=last_week)
    inv = store.get_invoice(inv["id"])
    assert inv["overdue"] is True
    assert inv["status"] == "sent"


def test_quote_converts_to_a_new_invoice(tmp_path):
    store, cid = _workshop(tmp_path)
    quote = store.create_quote(customer_id=cid, issue_date="2026-11-01", due_date="2026-11-15")
    store.add_line(quote["id"], "Rebuild gearbox", qty_milli=1000, unit_cents=75000, tax_bps=0)
    invoice = store.convert_quote(quote["id"])
    quote = store.get_invoice(quote["id"])
    assert quote["kind"] == "quote"
    assert quote["status"] == "converted"
    assert invoice["kind"] == "invoice"
    assert invoice["status"] == "draft"
    assert invoice["total_cents"] == 75000
    assert invoice["id"] != quote["id"]


def test_chase_text_names_the_invoice_and_refuses_a_draft(tmp_path):
    store, cid = _workshop(tmp_path)
    yesterday = (date.today() - timedelta(days=5)).isoformat()
    inv = store.create_invoice(customer_id=cid, issue_date=yesterday, due_date=yesterday)
    store.add_line(inv["id"], "Labour", qty_milli=1000, unit_cents=24000, tax_bps=0)
    with pytest.raises(ValueError, match="chase"):
        store.chase_text(inv["id"])
    store.send_invoice(inv["id"], sent_at=yesterday)
    chase = store.chase_text(inv["id"])
    assert "INV-" in chase["subject"]
    assert "Maria" in chase["body"]
    assert "$240.00" in chase["body"]
    assert "mailto:maria@example.com" in chase["mailto"]
    assert chase["days_overdue"] >= 5
    assert chase["balance_cents"] == 24000


def test_monthly_retainer_ticks_a_draft_then_stays_put(tmp_path):
    store, cid = _workshop(tmp_path)
    rec = store.create_recurring(
        customer_id=cid,
        cadence="monthly",
        next_date="2026-11-01",
        lines=[{"description": "Monthly retainer", "qty_milli": 4000, "unit_cents": 8000, "tax_bps": 0}],
    )
    first = store.tick_recurring(as_of="2026-11-01")["created"]
    assert len(first) == 1
    inv = first[0]
    assert inv["status"] == "draft"
    assert inv["total_cents"] == 32000
    assert inv["customer_id"] == cid
    assert "Monthly retainer" in inv["lines"][0]["description"]
    again = store.tick_recurring(as_of="2026-11-01")
    assert again == {"created": [], "failed": []}
    rec = store.get_recurring(rec["id"])
    assert rec["next_date"] == "2026-12-01"
    later = store.tick_recurring(as_of="2026-12-01")["created"]
    assert len(later) == 1
    assert later[0]["id"] != inv["id"]


def test_tick_caps_a_years_long_backfill(tmp_path):
    store, cid = _workshop(tmp_path)
    rec = store.create_recurring(
        customer_id=cid,
        cadence="monthly",
        next_date="2020-01-01",
        lines=[{"description": "Old retainer", "qty_milli": 1000, "unit_cents": 8000, "tax_bps": 0}],
    )
    created = store.tick_recurring(as_of="2026-11-01")["created"]
    assert len(created) == 12
    rec = store.get_recurring(rec["id"])
    assert rec["next_date"] == "2021-01-01"


def test_void_reverses_journal_and_blocks_pay(tmp_path):
    store, cid = _workshop(tmp_path)
    inv = store.create_invoice(customer_id=cid, issue_date="2026-11-01", due_date="2026-11-15")
    store.add_line(inv["id"], "Labour", qty_milli=1000, unit_cents=10000, tax_bps=0)
    store.send_invoice(inv["id"], sent_at="2026-11-02")
    store.void_invoice(inv["id"])
    inv = store.get_invoice(inv["id"])
    assert inv["status"] == "void"
    tb = store.trial_balance()
    assert tb["1100"] == 0
    assert tb["4000"] == 0
    with pytest.raises(ValueError):
        store.record_payment(inv["id"], amount_cents=10000, paid_at="2026-11-10", method="cash")
