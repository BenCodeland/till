from datetime import date
from pathlib import Path

from till.cli import main
from till.seed import seed
from till.store import Store
from till.web import create_app
from fastapi.testclient import TestClient

LOCAL = "http://127.0.0.1:7740"


def test_seed_puts_work_on_today(tmp_path, monkeypatch):
    db = tmp_path / "till.db"
    monkeypatch.setenv("TILL_DB", str(db))
    store = Store(db)
    store.migrate()
    seed(store)
    today = date.today().isoformat()
    brief = store.today_brief(today)
    assert brief["overdue_count"] == 1
    assert any("Kitchen hinge" in e["title"] for e in brief["events"])
    assert brief["cash_cents"] != 0
    assert store.get_company()["name"] == "Northridge Workshop"
    page = TestClient(create_app(db), base_url=LOCAL).get("/")
    assert "Kitchen hinge" in page.text
    assert "Rafi Cole" in page.text
    retainers = store.list_recurring()
    assert len(retainers) == 1
    assert retainers[0]["cadence"] == "monthly"


def test_job_invoice_button_opens_the_form_at_the_shop_rate(tmp_path):
    app = create_app(tmp_path / "till.db")
    store = app.state.store
    cid = store.create_customer(name="Maria Chen")
    store.create_product("Shop hour", 8000, 0)
    job = store.create_job(customer_id=cid, title="Kitchen hinge")
    c = TestClient(app, base_url=LOCAL)
    r = c.post(f"/jobs/{job['id']}/invoice", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == f"/invoices/new?job_id={job['id']}"
    assert store.list_invoices() == []  # nothing is written until the owner saves
    page = c.get(r.headers["location"]).text
    assert 'value="Shop hour — Kitchen hinge"' in page
    assert 'value="80.00"' in page


def test_expense_rejects_income_account(tmp_path):
    store = Store(tmp_path / "till.db")
    store.migrate()
    try:
        store.record_expense("Nope", 100, "4000", "2026-11-01")
        assert False, "should have refused"
    except ValueError as exc:
        assert "expense account" in str(exc)
    assert store.trial_balance()["4000"] == 0


def test_unbalanced_journal_is_rejected(tmp_path):
    store = Store(tmp_path / "till.db")
    store.migrate()
    try:
        store._post("2026-11-01", "bad", [("1000", 50, 0), ("3000", 0, 40)], "x", 0)
        assert False, "should have refused"
    except ValueError as exc:
        assert "unbalanced" in str(exc)
    assert store.db.execute("SELECT COUNT(*) AS n FROM journal").fetchone()["n"] == 0


def test_telegram_brief_uses_argv_list(tmp_path, monkeypatch):
    db = tmp_path / "till.db"
    monkeypatch.setenv("TILL_DB", str(db))
    store = Store(db)
    store.migrate()
    seed(store)
    seen = {}

    def fake_which(name):
        return "/usr/bin/hermes" if name == "hermes" else None

    def fake_run(argv, check=False):
        seen["argv"] = argv

        class R:
            returncode = 0

        return R()

    monkeypatch.setattr("till.cli.shutil.which", fake_which)
    monkeypatch.setattr("till.cli.subprocess.run", fake_run)
    assert main(["telegram-brief"]) == 0
    assert seen["argv"][0] == "/usr/bin/hermes"
    assert seen["argv"][1:4] == ["send", "--to", "telegram"]
    assert "Rafi Cole" in seen["argv"][4]


def test_skill_documents_localhost_cents_and_no_home_path():
    text = Path("skills/till/SKILL.md").read_text()
    assert "127.0.0.1:7740" in text
    assert "amount_cents" in text or "qty_milli" in text
    assert "/mnt" + "/" not in text
    assert "~/" not in text
    assert "TILL_URL" in text
    assert "/api/invoices/ID/chase" in text or "/chase" in text


def test_tick_cli_issues_due_retainers(tmp_path, monkeypatch, capsys):
    db = tmp_path / "till.db"
    monkeypatch.setenv("TILL_DB", str(db))
    store = Store(db)
    store.migrate()
    cid = store.create_customer(name="Ada West")
    store.create_recurring(
        customer_id=cid,
        cadence="monthly",
        next_date=date.today().isoformat(),
        lines=[{"description": "Monthly retainer", "qty_milli": 1000, "unit_cents": 8000, "tax_bps": 0}],
    )
    assert main(["tick"]) == 0
    out = capsys.readouterr().out
    assert "INV-" in out
    assert "Ada West" in out
    assert main(["tick"]) == 0
    assert "no retainers due" in capsys.readouterr().out


def _skill_requests(text: str):
    """Every curl in SKILL.md as (method, path, json body or None)."""
    import json
    import re

    joined = text.replace("\\\n", " ")
    for m in re.finditer(r"curl -s (-X POST )?\"\$TILL_URL(/[^\"]*)\"(.*)", joined):
        body = re.search(r"-d '([^']*)'", m.group(3))
        yield ("POST" if m.group(1) else "GET"), m.group(2), (json.loads(body.group(1)) if body else None)


def test_skill_examples_run_against_the_api_as_documented(tmp_path):
    import re

    text = Path("skills/till/SKILL.md").read_text()
    app = create_app(tmp_path / "till.db")
    store = app.state.store
    seed(store)
    c = TestClient(app, base_url=LOCAL)
    ids = {"ID": None, "PAYMENT_ID": None, "EXPENSE_ID": None}
    seen = []
    for method, path, body in _skill_requests(text):
        if path.endswith("/delete"):
            draft = store.create_document("invoice", customer_id=1, issue_date=date.today().isoformat(),
                                          due_date=None, notes="", job_id=None,
                                          lines=[{"description": "x", "qty_milli": 1000, "unit_cents": 100, "tax_bps": 0}])
            path = path.replace("ID", str(draft["id"]))
        if path.startswith("/api/reviews/ID"):
            path = path.replace("ID", str(store.list_reviews()[0]["id"]))
        if "ID" in path and ids["ID"] is None:
            ids["ID"] = store.list_invoices(status="sent")[0]["id"]
        for key in ("PAYMENT_ID", "EXPENSE_ID", "ID"):
            if ids[key] is not None:
                path = re.sub(rf"\b{key}\b", str(ids[key]), path)
        r = c.post(path, json=body) if method == "POST" and body is not None else c.request(method, path)
        assert r.status_code == 200, (method, path, r.text)
        seen.append(path)
        data = r.json() if r.headers["content-type"].startswith("application/json") else None
        if path == "/api/invoices" and method == "POST":
            ids["ID"] = data["id"]
        if path.endswith("/pay"):
            ids["PAYMENT_ID"] = store.db.execute("SELECT MAX(id) FROM payments").fetchone()[0]
        if path == "/api/expenses":
            ids["EXPENSE_ID"] = data["id"]
        if path == "/api/tick":
            assert set(data) == {"created", "failed"}
    assert any(p.endswith("/send") for p in seen) and any(p.endswith("/pay") for p in seen)
    assert "/api/tick" in seen and "/api/expenses" in seen
    assert store.journal_is_balanced()
    # What the skill says about refusals is what Till does.
    assert c.post("/api/customers", content='{"name":"x"}', headers={"content-type": "text/plain"}).status_code == 415
    assert c.get("/api/health", headers={"host": "evil.example"}).status_code == 400
    bad = c.post("/api/invoices/999999/send")
    assert bad.status_code == 404 and "detail" in bad.json()
    assert "415" in text and "Unknown host" in text and '{"detail": "..."}' in text


def test_skill_has_the_guardrails():
    text = Path("skills/till/SKILL.md").read_text()
    assert "data, never instructions" in text
    assert "explicit yes" in text
    for write in ("send", "payment", "expense", "void", "revers", "invoice"):
        assert write in text.split("## Prerequisites")[0].lower()
    assert "windows" not in text.lower()
    assert "macos" not in text.lower()
    assert "Hermes Agent" not in text
    assert "/jobs/JOB_ID/invoice" not in text  # a page route that writes nothing
