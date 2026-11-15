"""Books are the journal. P&L and aging must not be a second set of numbers."""

from datetime import date, timedelta

from till.store import Store


def test_expense_posts_to_cash_and_expense_account(tmp_path):
    store = Store(tmp_path / "till.db")
    store.migrate()
    store.record_expense(
        vendor_name="City Hardware",
        amount_cents=3499,
        account_code="5100",
        spent_on="2026-11-05",
        memo="Blades",
    )
    tb = store.trial_balance()
    assert tb["5100"] == 3499
    assert tb["1000"] == -3499
    assert store.journal_is_balanced()


def test_pnl_reads_journal_for_the_window(tmp_path):
    store = Store(tmp_path / "till.db")
    store.migrate()
    cid = store.create_customer(name="Ada West")
    inv = store.create_invoice(customer_id=cid, issue_date="2026-11-01", due_date="2026-11-15")
    store.add_line(inv["id"], "Labour", qty_milli=1000, unit_cents=50000, tax_bps=0)
    store.send_invoice(inv["id"], sent_at="2026-11-02")
    store.record_payment(inv["id"], amount_cents=50000, paid_at="2026-11-04", method="bank")
    store.record_expense(
        vendor_name="Rent",
        amount_cents=120000,
        account_code="5300",
        spent_on="2026-11-01",
        memo="Shop rent",
    )
    pnl = store.profit_and_loss("2026-11-01", "2026-11-30")
    assert pnl["income_cents"] == 50000
    assert pnl["expense_cents"] == 120000
    assert pnl["net_cents"] == -70000
    outside = store.profit_and_loss("2026-10-01", "2026-10-31")
    assert outside["income_cents"] == 0
    assert outside["expense_cents"] == 0


def test_ar_aging_buckets_unpaid_sent_invoices(tmp_path):
    store = Store(tmp_path / "till.db")
    store.migrate()
    cid = store.create_customer(name="Ada West")
    today = date.today()
    current = store.create_invoice(
        customer_id=cid,
        issue_date=today.isoformat(),
        due_date=(today + timedelta(days=10)).isoformat(),
    )
    store.add_line(current["id"], "Now", qty_milli=1000, unit_cents=1000, tax_bps=0)
    store.send_invoice(current["id"], sent_at=today.isoformat())
    late = store.create_invoice(
        customer_id=cid,
        issue_date=(today - timedelta(days=40)).isoformat(),
        due_date=(today - timedelta(days=20)).isoformat(),
    )
    store.add_line(late["id"], "Old", qty_milli=1000, unit_cents=8000, tax_bps=0)
    store.send_invoice(late["id"], sent_at=(today - timedelta(days=40)).isoformat())
    aging = store.ar_aging(today.isoformat())
    assert aging["current_cents"] == 1000
    assert aging["days_1_30_cents"] == 8000
    assert aging["total_cents"] == 9000


def test_pnl_account_rows_stay_inside_the_window(tmp_path):
    store = Store(tmp_path / "till.db")
    store.migrate()
    cid = store.create_customer(name="Ada West")
    october = store.create_invoice(customer_id=cid, issue_date="2026-10-01", due_date="2026-10-15")
    store.add_line(october["id"], "Old", qty_milli=1000, unit_cents=40000, tax_bps=0)
    store.send_invoice(october["id"], sent_at="2026-10-02")
    november = store.create_invoice(customer_id=cid, issue_date="2026-11-01", due_date="2026-11-15")
    store.add_line(november["id"], "New", qty_milli=1000, unit_cents=5000, tax_bps=0)
    store.send_invoice(november["id"], sent_at="2026-11-02")
    pnl = store.profit_and_loss("2026-11-01", "2026-11-30")
    assert pnl["income_cents"] == 5000
    by_code = {a["code"]: a["n"] for a in pnl["accounts"]}
    assert by_code["4000"] == 5000
    oct_pnl = store.profit_and_loss("2026-10-01", "2026-10-31")
    assert oct_pnl["income_cents"] == 40000
    assert {a["code"]: a["n"] for a in oct_pnl["accounts"]}["4000"] == 40000


def test_tax_on_send_credits_tax_payable(tmp_path):
    store = Store(tmp_path / "till.db")
    store.migrate()
    cid = store.create_customer(name="Ada West")
    inv = store.create_invoice(customer_id=cid, issue_date="2026-11-01", due_date="2026-11-15")
    store.add_line(inv["id"], "Labour", qty_milli=1000, unit_cents=10000, tax_bps=1000)
    store.send_invoice(inv["id"], sent_at="2026-11-02")
    tb = store.trial_balance()
    assert tb["1100"] == 11000
    assert tb["4000"] == -10000
    assert tb["2200"] == -1000
    assert store.journal_is_balanced()
