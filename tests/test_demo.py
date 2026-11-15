"""Sample data lives in its own book: `till demo` never touches the real one,
and `till seed` refuses to pour demo money into a book that has real data."""

import socket
from pathlib import Path

import pytest
import uvicorn
from fastapi.testclient import TestClient

from till.cli import main
from till.store import Store
from till.web import PROJECT, create_app

LOCAL = "http://127.0.0.1:7740"


@pytest.fixture
def books(tmp_path, monkeypatch):
    real = tmp_path / "till.db"
    demo = tmp_path / "demo.db"
    monkeypatch.setenv("TILL_DB", str(real))
    monkeypatch.setenv("TILL_DEMO_DB", str(demo))
    # A free port, so a demo already running on 7749 (as the README's
    # `till demo` leaves one) can't make these tests fail.
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        monkeypatch.setenv("TILL_DEMO_PORT", str(s.getsockname()[1]))
    served = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: served.append((app, kw)))
    return real, demo, served


def _real_book(path: Path) -> Store:
    store = Store(path)
    store.migrate()
    store.create_customer(name="Dana Real")
    return store


def _names(path: Path) -> list[str]:
    store = Store(path)
    try:
        return [c["name"] for c in store.list_customers(include_archived=True)]
    finally:
        store.close()


def test_demo_seeds_its_own_book_and_serves_it_on_the_demo_port(books, monkeypatch, capsys):
    real, demo, served = books
    monkeypatch.delenv("TILL_DEMO_PORT")
    # 7749 is the default; whether something else holds it here doesn't matter.
    monkeypatch.setattr("till.cli._port_in_use", lambda port: False)
    assert main(["demo"]) == 0
    assert not real.exists(), "the real book must not even be created"
    assert "Rafi Cole" in _names(demo)
    (app, kw), = served
    assert kw["host"] == "127.0.0.1"
    assert kw["port"] == 7749
    out = capsys.readouterr().out
    assert "http://127.0.0.1:7749/" in out
    page = TestClient(app, base_url="http://127.0.0.1:7749").get("/")
    assert "Northridge Workshop" in page.text
    assert "This is sample data" in page.text


def test_demo_port_and_book_come_from_the_environment(books, monkeypatch, capsys):
    real, demo, served = books
    monkeypatch.setenv("TILL_DEMO_PORT", "7795")
    assert main(["demo"]) == 0
    assert served[0][1]["port"] == 7795
    assert "http://127.0.0.1:7795/" in capsys.readouterr().out


def test_demo_default_book_is_data_demo_db(monkeypatch):
    from till import cli

    monkeypatch.delenv("TILL_DEMO_DB", raising=False)
    assert cli._demo_db_path() == PROJECT / "data" / "demo.db"


def test_demo_reuses_an_existing_demo_book(books):
    real, demo, served = books
    assert main(["demo"]) == 0
    store = Store(demo)
    store.create_customer(name="Added While Trying")
    store.close()
    assert main(["demo"]) == 0
    assert "Added While Trying" in _names(demo)
    assert main(["demo", "--fresh"]) == 0
    assert "Added While Trying" not in _names(demo)
    assert "Rafi Cole" in _names(demo)


def test_demo_refuses_to_seed_over_a_real_book(books, capsys):
    real, demo, served = books
    _real_book(demo).close()
    assert main(["demo"]) == 1
    assert served == []
    assert _names(demo) == ["Dana Real"]
    assert "sample" in capsys.readouterr().err.lower()


def test_demo_refuses_when_it_would_share_the_real_books_file(books, monkeypatch):
    real, demo, served = books
    monkeypatch.setenv("TILL_DEMO_DB", str(real))
    assert main(["demo"]) == 1
    assert served == []
    assert not real.exists()


def test_seed_refuses_a_book_with_real_data_even_with_force(books, capsys):
    real, demo, served = books
    _real_book(real).close()
    assert main(["seed"]) == 1
    assert main(["seed", "--force"]) == 1
    assert _names(real) == ["Dana Real"]
    err = capsys.readouterr().err
    assert "till backup" in err
    assert "till demo" in err


def test_seed_refuses_a_book_whose_only_change_is_the_business_details(books):
    real, demo, served = books
    store = Store(real)
    store.migrate()
    store.save_company(name="Dana's Plumbing")
    store.close()
    assert main(["seed", "--force"]) == 1
    store = Store(real)
    assert store.get_company()["name"] == "Dana's Plumbing"
    store.close()


def test_seed_refuses_a_file_that_is_not_a_till_book(books):
    real, demo, served = books
    real.write_bytes(b"not a database at all, but somebody's file")
    assert main(["seed", "--force"]) == 1
    assert real.read_bytes() == b"not a database at all, but somebody's file"


def test_seed_fills_an_empty_book_and_force_rebuilds_only_a_demo_book(books, capsys):
    real, demo, served = books
    Store(real).migrate()  # a fresh, empty book
    assert main(["seed"]) == 0
    assert "Rafi Cole" in _names(real)
    store = Store(real)
    store.create_customer(name="Added While Trying")
    store.close()
    assert main(["seed"]) == 0  # already a sample book: left as it is
    assert "Added While Trying" in _names(real)
    assert main(["seed", "--force"]) == 0
    assert "Added While Trying" not in _names(real)
    assert "Rafi Cole" in _names(real)
    store = Store(real)
    assert store.get_company()["is_demo"] == 1
    store.close()


def test_a_fresh_real_book_starts_empty_with_a_first_run_guide(tmp_path):
    app = create_app(tmp_path / "till.db")
    c = TestClient(app, base_url=LOCAL)
    page = c.get("/").text
    assert "This is sample data" not in page
    assert "Getting started" in page
    assert 'href="/settings"' in page and "business name" in page
    assert "Add a customer" in page
    assert "New invoice" in page
    store = app.state.store
    assert store.list_customers() == []
    assert store.list_invoices() == []
    cid = store.create_customer(name="First Customer")
    store.create_document("invoice", customer_id=cid, issue_date="2026-11-14", due_date=None,
                          notes="", job_id=None,
                          lines=[{"description": "Work", "qty_milli": 1000, "unit_cents": 5000, "tax_bps": 0}])
    assert "Getting started" not in c.get("/").text


def test_the_demo_book_shows_the_sample_banner_and_no_guide(books):
    real, demo, served = books
    assert main(["demo"]) == 0
    page = TestClient(served[0][0], base_url=LOCAL).get("/").text
    assert "This is sample data" in page
    assert "Getting started" not in page


def test_settings_tells_a_demo_user_how_to_start_their_own_book(books):
    real, demo, served = books
    assert main(["demo"]) == 0
    page = TestClient(served[0][0], base_url=LOCAL).get("/settings").text
    assert "Start your own book" in page
    assert "till serve" in page
    assert str(real) in page  # where their own book will be
    assert "till demo" in page


def test_demo_on_a_busy_port_refuses_before_touching_the_book(books, capsys):
    real, demo, served = books
    assert main(["demo"]) == 0
    store = Store(demo)
    store.create_customer(name="Added While Trying")
    store.close()
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        s.listen()
        port = s.getsockname()[1]
        assert main(["demo", "--fresh", "--port", str(port)]) == 1
    assert "Added While Trying" in _names(demo)
    assert len(served) == 1
