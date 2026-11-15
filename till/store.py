"""SQLite daybook. Invoices, journal, calendar, reviews."""

from __future__ import annotations

import calendar
import functools
import json
import os
import re
import sqlite3
import threading
import zoneinfo
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

from till.money import MAX_CENTS, Invalid, format_cents, line_cents, tax_cents

SCHEMA = """
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS company (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  name TEXT NOT NULL,
  email TEXT,
  phone TEXT,
  address TEXT,
  tax_id TEXT,
  currency TEXT NOT NULL DEFAULT 'USD'
);

CREATE TABLE IF NOT EXISTS customers (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  email TEXT,
  phone TEXT,
  address TEXT,
  notes TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS vendors (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  email TEXT,
  phone TEXT
);

CREATE TABLE IF NOT EXISTS products (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  unit_cents INTEGER NOT NULL,
  tax_bps INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS jobs (
  id INTEGER PRIMARY KEY,
  customer_id INTEGER NOT NULL REFERENCES customers(id),
  title TEXT NOT NULL,
  status TEXT NOT NULL,
  notes TEXT,
  scheduled_for TEXT
);

CREATE TABLE IF NOT EXISTS invoices (
  id INTEGER PRIMARY KEY,
  number TEXT NOT NULL UNIQUE,
  kind TEXT NOT NULL CHECK (kind IN ('invoice','quote')),
  customer_id INTEGER NOT NULL REFERENCES customers(id),
  status TEXT NOT NULL,
  issue_date TEXT NOT NULL,
  due_date TEXT,
  sent_at TEXT,
  notes TEXT,
  po_number TEXT,
  job_id INTEGER REFERENCES jobs(id),
  subtotal_cents INTEGER NOT NULL DEFAULT 0,
  tax_cents INTEGER NOT NULL DEFAULT 0,
  total_cents INTEGER NOT NULL DEFAULT 0,
  paid_cents INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS invoice_lines (
  id INTEGER PRIMARY KEY,
  invoice_id INTEGER NOT NULL REFERENCES invoices(id) ON DELETE CASCADE,
  position INTEGER NOT NULL,
  description TEXT NOT NULL,
  qty_milli INTEGER NOT NULL,
  unit_cents INTEGER NOT NULL,
  tax_bps INTEGER NOT NULL DEFAULT 0,
  line_cents INTEGER NOT NULL,
  tax_cents INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS payments (
  id INTEGER PRIMARY KEY,
  invoice_id INTEGER NOT NULL REFERENCES invoices(id),
  amount_cents INTEGER NOT NULL,
  paid_at TEXT NOT NULL,
  method TEXT NOT NULL,
  notes TEXT,
  journal_id INTEGER
);

CREATE TABLE IF NOT EXISTS accounts (
  code TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  type TEXT NOT NULL CHECK (type IN ('asset','liability','equity','income','expense'))
);

CREATE TABLE IF NOT EXISTS journal (
  id INTEGER PRIMARY KEY,
  posted_at TEXT NOT NULL,
  memo TEXT NOT NULL,
  ref_type TEXT,
  ref_id INTEGER
);

CREATE TABLE IF NOT EXISTS journal_lines (
  id INTEGER PRIMARY KEY,
  journal_id INTEGER NOT NULL REFERENCES journal(id),
  account_code TEXT NOT NULL REFERENCES accounts(code),
  debit_cents INTEGER NOT NULL DEFAULT 0,
  credit_cents INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS expenses (
  id INTEGER PRIMARY KEY,
  vendor_id INTEGER REFERENCES vendors(id),
  vendor_name TEXT NOT NULL,
  amount_cents INTEGER NOT NULL,
  account_code TEXT NOT NULL REFERENCES accounts(code),
  spent_on TEXT NOT NULL,
  memo TEXT,
  journal_id INTEGER
);

CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY,
  title TEXT NOT NULL,
  start_at TEXT NOT NULL,
  end_at TEXT NOT NULL,
  customer_id INTEGER REFERENCES customers(id),
  job_id INTEGER REFERENCES jobs(id),
  location TEXT,
  notes TEXT
);

CREATE TABLE IF NOT EXISTS reviews (
  id INTEGER PRIMARY KEY,
  source TEXT NOT NULL,
  author TEXT NOT NULL,
  rating INTEGER NOT NULL,
  body TEXT NOT NULL,
  received_at TEXT NOT NULL,
  reply_draft TEXT,
  replied_at TEXT,
  status TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS deals (
  id INTEGER PRIMARY KEY,
  customer_id INTEGER REFERENCES customers(id),
  title TEXT NOT NULL,
  stage TEXT NOT NULL,
  value_cents INTEGER NOT NULL DEFAULT 0,
  notes TEXT
);

CREATE TABLE IF NOT EXISTS activities (
  id INTEGER PRIMARY KEY,
  customer_id INTEGER REFERENCES customers(id),
  kind TEXT NOT NULL,
  body TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS recurring (
  id INTEGER PRIMARY KEY,
  customer_id INTEGER NOT NULL REFERENCES customers(id),
  cadence TEXT NOT NULL,
  next_date TEXT NOT NULL,
  template_json TEXT NOT NULL,
  active INTEGER NOT NULL DEFAULT 1
);
"""

ACCOUNTS = [
    ("1000", "Cash", "asset"),
    ("1100", "Accounts Receivable", "asset"),
    ("2000", "Accounts Payable", "liability"),
    ("2200", "Tax Payable", "liability"),
    ("3000", "Owner Equity", "equity"),
    ("4000", "Income", "income"),
    ("5000", "Operating Expenses", "expense"),
    ("5100", "Materials", "expense"),
    ("5200", "Software", "expense"),
    ("5300", "Rent", "expense"),
    ("5400", "Vehicle", "expense"),
]

OPEN_STATUSES = ("sent", "partial")
CURRENCY_LOCKED = "Till keeps one currency per book. To work in another currency, start a new book."
# PRAGMA user_version. 0 or 1: the original schema (SCHEMA above, as first
# shipped). 2: corrections (reversed payments and expenses, archived
# customers, deleted drafts). 3: retainer anchor day, indexes, and one
# posting per invoice. 4: the company's default tax, payment instructions
# and the sample-book flag. 5: each invoice and quote keeps its own currency.
# 6: currency codes upper-case and trimmed (older Settings saved 'usd').
# 7: no document without a currency (a blank one takes the book's).
# migrate() upgrades older files in place, all in one transaction: a step
# that fails leaves the file exactly as it was.
SCHEMA_VERSION = 7
MIGRATIONS = {
    2: [
        ("payments", "reversed_at", "TEXT"),
        ("payments", "reversal_journal_id", "INTEGER REFERENCES journal(id)"),
        ("expenses", "reversed_at", "TEXT"),
        ("expenses", "reversal_journal_id", "INTEGER REFERENCES journal(id)"),
        ("customers", "archived_at", "TEXT"),
        ("invoices", "deleted_at", "TEXT"),
    ],
    3: [
        ("recurring", "anchor_day", "INTEGER"),
    ],
    4: [
        ("company", "default_tax_bps", "INTEGER NOT NULL DEFAULT 0"),
        ("company", "payment_instructions", "TEXT"),
        ("company", "is_demo", "INTEGER NOT NULL DEFAULT 0"),
    ],
    5: [
        ("invoices", "currency", "TEXT"),
    ],
    6: [],
    7: [],
}
INDEXES = {
    "invoices_customer": "invoices(customer_id)",
    "invoices_kind_status": "invoices(kind, status)",
    "invoice_lines_invoice": "invoice_lines(invoice_id)",
    "payments_invoice": "payments(invoice_id)",
    "journal_ref": "journal(ref_type, ref_id)",
    "journal_lines_journal": "journal_lines(journal_id)",
    "journal_lines_account": "journal_lines(account_code)",
    "events_start": "events(start_at)",
    "jobs_customer": "jobs(customer_id)",
}


class MigrationError(RuntimeError):
    """The file can't be upgraded as it stands; the message says why and what to do."""


CADENCES = ("weekly", "monthly", "yearly")
DEFAULT_TERMS_DAYS = 14
JOB_STATUSES = ("scheduled", "in_progress", "done", "cancelled")
DEAL_STAGES = ("lead", "quoted", "won", "lost")
MAX_QTY_MILLI = 10**9  # a million units on one line


def _row(r: sqlite3.Row | None) -> dict | None:
    if r is None:
        return None
    return {k: r[k] for k in r.keys()}


def _writes(method):
    """Run a public write method as one transaction (see Store.transaction)."""

    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        with self.transaction():
            return method(self, *args, **kwargs)

    return wrapper


def _reads(method):
    """Run a public read method under the store lock, so it never sees another
    thread's half-finished transaction on the shared connection."""

    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)

    return wrapper


class Store:
    """All access to the book goes through one connection, guarded by one lock.

    FastAPI runs sync routes on a thread pool, so several threads share this
    connection. Every public method takes `_lock`; every public write runs in
    `transaction()`, so its checks and its writes happen together or not at all.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # isolation_level=None: the sqlite3 module never opens or commits a
        # transaction on its own; transaction() says BEGIN/COMMIT/ROLLBACK.
        self.db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.execute("PRAGMA busy_timeout = 5000")
        self._lock = threading.RLock()
        self._depth = 0  # how many transaction() blocks are open on this connection

    @contextmanager
    def transaction(self):
        """One all-or-nothing unit of work, holding the lock until it ends.

        The outermost block runs BEGIN IMMEDIATE, which also takes SQLite's
        write lock, so another process (`till tick` while the server runs)
        can't slip in between a check and its write. It commits on success and
        rolls back on any exception.

        Nested blocks join the outer transaction as a SAVEPOINT: nothing is
        committed until the outermost block ends, and if a nested block
        raises, only its own changes are undone before the error propagates.
        """
        with self._lock:
            savepoint = f"sp{self._depth}" if self._depth else None
            self.db.execute(f"SAVEPOINT {savepoint}" if savepoint else "BEGIN IMMEDIATE")
            self._depth += 1
            try:
                yield
            except BaseException:
                self._depth -= 1
                self._undo(savepoint)
                raise
            self._depth -= 1
            if not self.db.in_transaction:
                # SQLite itself rolled everything back (disk full, I/O error)
                # and an outer block swallowed the error. Don't pretend it saved.
                raise sqlite3.OperationalError("transaction was rolled back by SQLite")
            if savepoint:
                self.db.execute(f"RELEASE {savepoint}")
                return
            try:
                self.db.execute("COMMIT")
            except BaseException:
                self._undo(None)
                raise

    def _undo(self, savepoint: str | None) -> None:
        if not self.db.in_transaction:
            return  # SQLite already rolled the whole transaction back
        if savepoint:
            self.db.execute(f"ROLLBACK TO {savepoint}")
            self.db.execute(f"RELEASE {savepoint}")
        else:
            self.db.execute("ROLLBACK")

    def migrate(self) -> None:
        with self._lock:
            # The schema script sets journal_mode, which can't run inside a
            # transaction; every statement in it is idempotent.
            self.db.executescript(SCHEMA)
            with self.transaction():
                version = self.db.execute("PRAGMA user_version").fetchone()[0]
                if version > SCHEMA_VERSION:
                    raise RuntimeError(
                        f"{self.path} was written by a newer Till (schema {version}); this one knows {SCHEMA_VERSION}"
                    )
                for target in range(max(version, 1) + 1, SCHEMA_VERSION + 1):
                    for table, column, decl in MIGRATIONS[target]:
                        have = {r["name"] for r in self.db.execute(f"PRAGMA table_info({table})")}
                        if column not in have:
                            self.db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
                    if target == 3:
                        self._migrate_3()
                    if target == 4:
                        # New lines used to take their tax from the first
                        # product; keep that as the book's default tax.
                        self.db.execute(
                            """UPDATE company SET default_tax_bps = COALESCE(
                                 (SELECT tax_bps FROM products WHERE tax_bps BETWEEN 0 AND 10000 ORDER BY id LIMIT 1), 0)"""
                        )
                    if target == 5:
                        # Until now every document showed the company's
                        # currency, so that is what each one was written in.
                        self.db.execute(
                            """UPDATE invoices SET currency = COALESCE(
                                 (SELECT currency FROM company WHERE id=1), 'USD') WHERE currency IS NULL"""
                        )
                    if target == 6:
                        # One spelling per code, so 'usd' and 'USD' are the
                        # same currency. A book that already holds documents
                        # in two currencies keeps them as they are; its pages
                        # carry the "in another currency" note.
                        self.db.execute("UPDATE company SET currency = upper(trim(currency))")
                        self.db.execute(
                            "UPDATE invoices SET currency = upper(trim(currency)) WHERE currency IS NOT NULL"
                        )
                    if target == 7:
                        # A document with no currency was written in the
                        # book's; a book with none is in USD, the default.
                        self.db.execute("UPDATE company SET currency = 'USD' WHERE trim(currency) = ''")
                        self.db.execute(
                            """UPDATE invoices SET currency = (SELECT currency FROM company WHERE id=1)
                               WHERE currency IS NULL OR trim(currency) = ''"""
                        )
                        self.db.execute("UPDATE invoices SET currency = 'USD' WHERE currency IS NULL")
                    self.db.execute(f"PRAGMA user_version = {target}")
                for code, name, typ in ACCOUNTS:
                    self.db.execute(
                        "INSERT OR IGNORE INTO accounts (code, name, type) VALUES (?,?,?)",
                        (code, name, typ),
                    )
                self.db.execute(
                    "INSERT OR IGNORE INTO company (id, name, currency) VALUES (1, 'Your workshop', 'USD')"
                )

    def _migrate_3(self) -> None:
        # A retainer's anchor is the day of month it was started on. Older
        # rows only kept next_date, so that day is the best there is.
        self.db.execute(
            """UPDATE recurring SET anchor_day = CAST(substr(next_date, 9, 2) AS INTEGER)
               WHERE anchor_day IS NULL AND next_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]*'"""
        )
        for name, target in INDEXES.items():
            self.db.execute(f"CREATE INDEX IF NOT EXISTS {name} ON {target}")
        # Sending posts an invoice once ('invoice'); its void is a separate
        # 'invoice_void' entry. Refuse to build the guard over existing
        # doubles rather than drop or hide either entry.
        doubles = self.db.execute(
            """SELECT j.ref_id, COALESCE(i.number, '#' || j.ref_id) AS number, group_concat(j.id, ', ') AS ids
               FROM journal j LEFT JOIN invoices i ON i.id = j.ref_id
               WHERE j.ref_type = 'invoice'
               GROUP BY j.ref_id HAVING COUNT(*) > 1 ORDER BY j.ref_id"""
        ).fetchall()
        if doubles:
            listed = "; ".join(f"{d['number']} (journal entries {d['ids']})" for d in doubles)
            raise MigrationError(
                f"Can't upgrade {self.path}: these invoices were posted to the books more than once: {listed}. "
                "Nothing was changed. Keep a copy of the file, then remove the extra entries (and their "
                "journal_lines) so each invoice is posted once, and start Till again."
            )
        self.db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS journal_invoice_posted_once ON journal(ref_id) WHERE ref_type = 'invoice'"
        )

    def _company_currency(self) -> str:
        row = self.db.execute("SELECT currency FROM company WHERE id=1").fetchone()
        return row["currency"] if row else "USD"

    def _currency_locked(self) -> bool:
        """True once the book holds anything in its currency: a journal entry,
        or an invoice that has been sent (and maybe paid or voided). Till
        keeps one currency per book, so from then on it can't change."""
        row = self.db.execute(
            """SELECT EXISTS(SELECT 1 FROM journal)
                      OR EXISTS(SELECT 1 FROM invoices
                                WHERE kind='invoice' AND status IN ('sent', 'partial', 'paid', 'void'))"""
        ).fetchone()
        return bool(row[0])

    @_reads
    def currency_locked(self) -> bool:
        return self._currency_locked()

    def _refuse_other_currency(self, inv: dict) -> None:
        """A draft left in another currency by an older Till can't go into the
        books until it is saved again, which puts it in the book's."""
        book = self._company_currency()
        if inv["currency"] != book:
            raise Invalid(
                f"{inv['number']} is in {inv['currency']} but this book is in {book}. "
                f"Edit the draft to save it in {book} first."
            )

    def _same_currency(self, value) -> str:
        """`value` as a code, if it is the book's currency; else a refusal."""
        code, book = _currency(value), self._company_currency()
        if code != book:
            raise Invalid(
                f"This book is in {book}, so it can't hold a document in {code}. {CURRENCY_LOCKED}"
            )
        return code

    def _money(self, cents: int, currency: str | None = None) -> str:
        """An amount in `currency` (a document's own), else the company's."""
        return format_cents(cents, currency or self._company_currency())

    def close(self) -> None:
        with self._lock:
            self.db.close()

    def _ref(self, table: str, value, label: str) -> int:
        """The id of an existing row a write points at, or a refusal that says so."""
        if value is None or value == "":
            raise Invalid(f"Choose a {label}")
        if isinstance(value, str) and value.strip().isascii() and value.strip().isdigit():
            value = int(value.strip())
        if isinstance(value, bool) or not isinstance(value, int) or not 0 < value < 2**63:
            raise Invalid(f"no {label} {str(value)[:40]}")
        if not self.db.execute(f"SELECT 1 FROM {table} WHERE id=?", (value,)).fetchone():
            raise Invalid(f"no {label} {value}")
        return value

    # --- company / customers -------------------------------------------------

    @_reads
    def is_new_book(self) -> bool:
        """Nothing entered yet: no customers, invoices or quotes, and no postings."""
        row = self.db.execute(
            """SELECT EXISTS(SELECT 1 FROM customers) OR EXISTS(SELECT 1 FROM invoices)
                      OR EXISTS(SELECT 1 FROM journal)"""
        ).fetchone()
        return not row[0]

    @_reads
    def get_company(self) -> dict:
        row = self.db.execute("SELECT * FROM company WHERE id=1").fetchone()
        return _row(row)

    @_writes
    def save_company(self, **fields) -> dict:
        """Change the named fields; fields not passed keep their value."""
        checks = {
            "name": lambda v: _text(v, "Shop name", one_line=True, max_len=200),
            "email": lambda v: _email(v),
            "phone": lambda v: _text(v, "Phone", required=False, one_line=True, max_len=100),
            "address": lambda v: _text(v, "Address", required=False, max_len=1000),
            "tax_id": lambda v: _text(v, "Tax id", required=False, one_line=True, max_len=100),
            "currency": lambda v: _currency(v),
            "default_tax_bps": lambda v: _int(v, "Default tax", 0, 10_000),
            "payment_instructions": lambda v: _text(v, "Payment instructions", required=False, max_len=2000) or None,
            "is_demo": lambda v: _flag(v, "is_demo"),
        }
        unknown = set(fields) - set(checks)
        if unknown:
            raise Invalid(f"Unknown company field {sorted(unknown)[0]}")
        sets = {k: checks[k](v) for k, v in fields.items()}
        if "currency" in sets and sets["currency"] != self._company_currency():
            if self._currency_locked():
                raise Invalid(
                    f"This book already has entries in {self._company_currency()}, so its currency can't change. "
                    f"{CURRENCY_LOCKED}"
                )
            # Nothing has been sent yet: drafts follow the book. A quote
            # already accepted or voided keeps the currency it was given in.
            self.db.execute("UPDATE invoices SET currency=? WHERE status = 'draft'", (sets["currency"],))
        if sets:
            sql = ", ".join(f"{k}=?" for k in sets)
            self.db.execute(f"UPDATE company SET {sql} WHERE id=1", tuple(sets.values()))
        return self.get_company()

    @_writes
    def create_customer(
        self,
        name: str,
        email: str | None = None,
        phone: str | None = None,
        address: str | None = None,
        notes: str | None = None,
    ) -> int:
        cur = self.db.execute(
            """INSERT INTO customers (name, email, phone, address, notes, created_at)
               VALUES (?,?,?,?,?,?)""",
            (*_customer_fields(name, email, phone, address, notes), _now()),
        )
        return cur.lastrowid

    @_reads
    def get_customer(self, customer_id: int) -> dict:
        row = self.db.execute("SELECT * FROM customers WHERE id=?", (customer_id,)).fetchone()
        if not row:
            raise KeyError(f"customer {customer_id}")
        return _row(row)

    @_reads
    def list_customers(self, include_archived: bool = False) -> list[dict]:
        """Customers for lists and pickers; archived ones only when asked."""
        where = "" if include_archived else " WHERE archived_at IS NULL"
        rows = self.db.execute(f"SELECT * FROM customers{where} ORDER BY name").fetchall()
        return [_row(r) for r in rows]

    @_writes
    def update_customer(self, customer_id: int, **fields) -> dict:
        """Change the named fields (name, email, phone, address, notes) with the
        same checks as adding a customer; fields not passed keep their value."""
        current = self.get_customer(customer_id)
        allowed = ("name", "email", "phone", "address", "notes")
        unknown = set(fields) - set(allowed)
        if unknown:
            raise Invalid(f"Unknown customer field {sorted(unknown)[0]}")
        merged = {k: fields.get(k, current[k]) for k in allowed}
        values = _customer_fields(**merged)
        self.db.execute(
            "UPDATE customers SET name=?, email=?, phone=?, address=?, notes=? WHERE id=?",
            (*values, customer_id),
        )
        return self.get_customer(customer_id)

    @_writes
    def set_customer_archived(self, customer_id: int, archived: bool) -> dict:
        """Archive hides a customer from lists and pickers; their invoices,
        jobs and payments stay exactly as they are."""
        self.get_customer(customer_id)
        self.db.execute(
            "UPDATE customers SET archived_at=? WHERE id=?",
            (_now() if archived else None, customer_id),
        )
        return self.get_customer(customer_id)

    @_writes
    def add_activity(self, customer_id: int, kind: str, body: str) -> dict:
        cur = self.db.execute(
            "INSERT INTO activities (customer_id, kind, body, created_at) VALUES (?,?,?,?)",
            (
                self._ref("customers", customer_id, "customer"),
                _text(kind, "Kind", one_line=True, max_len=40),
                _text(body, "Note", max_len=5000),
                _now(),
            ),
        )
        return _row(self.db.execute("SELECT * FROM activities WHERE id=?", (cur.lastrowid,)).fetchone())

    # --- invoices ------------------------------------------------------------

    @_writes
    def create_invoice(
        self,
        customer_id: int,
        issue_date: str,
        due_date: str | None = None,
        notes: str = "",
        job_id: int | None = None,
        po_number: str | None = None,
    ) -> dict:
        return self._create_doc(
            "invoice", customer_id, issue_date, due_date, notes, job_id, po_number
        )

    @_writes
    def create_quote(
        self,
        customer_id: int,
        issue_date: str,
        due_date: str | None = None,
        notes: str = "",
        job_id: int | None = None,
    ) -> dict:
        return self._create_doc("quote", customer_id, issue_date, due_date, notes, job_id, None)

    @_writes
    def create_document(
        self,
        kind: str,
        customer_id: int,
        issue_date: str,
        lines: list[dict],
        due_date: str | None = None,
        notes: str = "",
        job_id: int | None = None,
        po_number: str | None = None,
        currency: str | None = None,
    ) -> dict:
        """Create an invoice or quote with its lines in one transaction.

        Each line is {description, qty_milli, unit_cents, tax_bps}. If any line
        fails, nothing is saved and no number is used up. `currency`, if
        given, must be the book's: a book keeps one currency.
        """
        if kind not in ("invoice", "quote"):
            raise Invalid("kind must be invoice or quote")
        if currency is not None:
            self._same_currency(currency)
        clean = _lines(lines)
        doc = self._create_doc(kind, customer_id, issue_date, due_date, notes, job_id, po_number)
        self._insert_lines(doc["id"], clean)
        return self.get_invoice(doc["id"])

    def _create_doc(self, kind, customer_id, issue_date, due_date, notes, job_id, po_number) -> dict:
        customer_id, issue_date, due_date, notes, job_id = self._doc_fields(
            customer_id, issue_date, due_date, notes, job_id
        )
        po_number = _text(po_number, "PO number", required=False, one_line=True, max_len=100) or None
        number = self._next_number(kind)
        cur = self.db.execute(
            """INSERT INTO invoices
               (number, kind, customer_id, status, issue_date, due_date, notes, po_number, job_id, created_at, currency)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                number,
                kind,
                customer_id,
                "draft",
                issue_date,
                due_date,
                notes,
                po_number,
                job_id,
                _now(),
                # A document is written in the book's currency. It keeps it:
                # the currency can only change while every document is a
                # draft or a quote, and those follow it (see save_company).
                self._company_currency(),
            ),
        )
        return self.get_invoice(cur.lastrowid)

    def _doc_fields(self, customer_id, issue_date, due_date, notes, job_id) -> tuple:
        """The checked header fields shared by creating and editing a document."""
        customer_id = self._ref("customers", customer_id, "customer")
        issue_date = _date(issue_date, "Issue date")
        due_date = _opt_date(due_date, "Due date")
        if due_date and due_date < issue_date:
            raise Invalid("Due date can't be before the issue date")
        notes = _text(notes, "Notes", required=False, max_len=5000)
        job_id = self._ref("jobs", job_id, "job") if job_id not in (None, "") else None
        return customer_id, issue_date, due_date, notes, job_id

    def _insert_lines(self, invoice_id: int, lines: list[dict]) -> None:
        pos = self.db.execute(
            "SELECT COALESCE(MAX(position),0) AS p FROM invoice_lines WHERE invoice_id=?",
            (invoice_id,),
        ).fetchone()["p"]
        for line in lines:
            pos += 1
            self.db.execute(
                """INSERT INTO invoice_lines
                   (invoice_id, position, description, qty_milli, unit_cents, tax_bps, line_cents, tax_cents)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (
                    invoice_id,
                    pos,
                    line["description"],
                    line["qty_milli"],
                    line["unit_cents"],
                    line["tax_bps"],
                    line["line_cents"],
                    line["tax_cents"],
                ),
            )
        self._recompute(invoice_id)

    def _next_number(self, kind: str) -> str:
        """One more than the highest number ever used for this kind.

        Invoices are never removed from the file (a deleted draft is only
        marked deleted), so a number once issued is never issued again, even
        when the newest draft is the one deleted."""
        prefix = "INV" if kind == "invoice" else "QUO"
        top = 0
        for (number,) in self.db.execute("SELECT number FROM invoices WHERE kind=?", (kind,)):
            tail = str(number).rsplit("-", 1)[-1]
            if tail.isascii() and tail.isdigit():
                top = max(top, int(tail))
        return f"{prefix}-{top + 1:04d}"

    @_writes
    def add_line(
        self,
        invoice_id: int,
        description: str,
        qty_milli: int,
        unit_cents: int,
        tax_bps: int = 0,
    ) -> dict:
        inv = self.get_invoice(invoice_id)
        if inv["status"] != "draft":
            raise Invalid("Only a draft can be changed")
        self._refuse_if_posted(inv)
        line = _line(
            {"description": description, "qty_milli": qty_milli, "unit_cents": unit_cents, "tax_bps": tax_bps}, 1
        )
        self._insert_lines(invoice_id, [line])
        return self.get_invoice(invoice_id)

    @_writes
    def update_document(
        self,
        invoice_id: int,
        customer_id: int,
        issue_date: str,
        lines: list[dict] | None,
        due_date: str | None = None,
        notes: str = "",
        job_id: int | None = None,
    ) -> dict:
        """Replace a draft's customer, dates, notes, job and its whole set of
        lines, all in one transaction. Only a draft: nothing has been posted.
        lines=None keeps the lines exactly as they are, unchecked and unwritten."""
        inv = self.get_invoice(invoice_id)
        if inv["status"] != "draft":
            raise Invalid(f"Only a draft can be edited; {inv['number']} is {inv['status']}")
        self._refuse_if_posted(inv)
        customer_id, issue_date, due_date, notes, job_id = self._doc_fields(
            customer_id, issue_date, due_date, notes, job_id
        )
        clean = None if lines is None else _lines(lines)
        # Saving a draft puts it in the book's currency (an older Till could
        # leave one in another).
        self.db.execute(
            "UPDATE invoices SET customer_id=?, issue_date=?, due_date=?, notes=?, job_id=?, currency=? WHERE id=?",
            (customer_id, issue_date, due_date, notes, job_id, self._company_currency(), invoice_id),
        )
        if clean is not None:
            self.db.execute("DELETE FROM invoice_lines WHERE invoice_id=?", (invoice_id,))
            self._insert_lines(invoice_id, clean)
        return self.get_invoice(invoice_id)

    @_writes
    def delete_draft(self, invoice_id: int) -> dict:
        """Delete a draft invoice or quote. The row stays, marked deleted and
        hidden from every list, so its number is never handed out again and
        the numbering keeps a record of why there is a gap."""
        inv = self.get_invoice(invoice_id)
        if inv["status"] == "deleted":
            raise Invalid(f"{inv['number']} was already deleted")
        if inv["status"] != "draft":
            hint = "; void it instead" if inv["status"] in OPEN_STATUSES else ""
            raise Invalid(f"Only a draft can be deleted; {inv['number']} is {inv['status']}{hint}")
        posted = self.db.execute(
            "SELECT 1 FROM journal WHERE ref_type LIKE 'invoice%' AND ref_id=?", (invoice_id,)
        ).fetchone()
        if posted:
            raise Invalid(f"{inv['number']} has entries in the books, so it can't be deleted")
        self.db.execute(
            "UPDATE invoices SET status='deleted', deleted_at=? WHERE id=?", (_now(), invoice_id)
        )
        return self.get_invoice(invoice_id)

    def _posted_ar(self, invoice_id: int) -> int | None:
        """What the invoice's own posting put on Accounts Receivable, or None
        if it was never posted. A draft can have one: older versions could
        stop halfway through a send, after the journal entry was saved."""
        rows = self.db.execute(
            """SELECT COALESCE(SUM(jl.debit_cents - jl.credit_cents), 0) AS ar
               FROM journal j LEFT JOIN journal_lines jl ON jl.journal_id = j.id AND jl.account_code = '1100'
               WHERE j.ref_type = 'invoice' AND j.ref_id = ?
               GROUP BY j.id""",
            (invoice_id,),
        ).fetchall()
        return sum(r["ar"] for r in rows) if rows else None

    def _posting_differences(self, inv: dict) -> list[str]:
        """How the invoice's own posting differs from what sending it would
        post now (receivables debited with the total, income credited with
        the subtotal, tax payable with the tax), one phrase per account and
        side. Empty if it matches; row order and how an amount is split
        across rows don't matter."""
        want = {("1100", "debit"): inv["total_cents"], ("4000", "credit"): inv["subtotal_cents"]}
        if inv["tax_cents"]:
            want[("2200", "credit")] = inv["tax_cents"]
        rows = self.db.execute(
            """SELECT jl.account_code AS code, COALESCE(a.name, jl.account_code) AS name,
                      SUM(jl.debit_cents) AS debit, SUM(jl.credit_cents) AS credit
               FROM journal j JOIN journal_lines jl ON jl.journal_id = j.id
               LEFT JOIN accounts a ON a.code = jl.account_code
               WHERE j.ref_type = 'invoice' AND j.ref_id = ?
               GROUP BY jl.account_code""",
            (inv["id"],),
        ).fetchall()
        have, names = {}, {"1100": "Accounts Receivable", "4000": "Income", "2200": "Tax Payable"}
        for r in rows:
            names[r["code"]] = r["name"]
            for side in ("debit", "credit"):
                if r[side]:
                    have[(r["code"], side)] = r[side]
        out = []
        for code, side in sorted(set(want) | set(have)):
            booked, expected = have.get((code, side), 0), want.get((code, side), 0)
            if booked != expected:
                out.append(
                    f"{names[code]} ({code}) {side}: {self._money(booked, inv['currency'])} in the books, "
                    f"{self._money(expected, inv['currency'])} on the invoice"
                )
        return out

    def _refuse_if_posted(self, inv: dict) -> None:
        """A draft already in the books can't change: the posting would no
        longer match it, and sending it would never post the difference."""
        if self._posted_ar(inv["id"]) is not None:
            raise Invalid(f"{inv['number']} is already in the books; void it and make a new one")

    def _recompute(self, invoice_id: int) -> None:
        row = self.db.execute(
            """SELECT COALESCE(SUM(line_cents),0) AS sub, COALESCE(SUM(tax_cents),0) AS tax
               FROM invoice_lines WHERE invoice_id=?""",
            (invoice_id,),
        ).fetchone()
        sub, tax = row["sub"], row["tax"]
        if sub + tax > MAX_CENTS:
            raise Invalid(f"The total is too large (the most Till takes is {format_cents(MAX_CENTS)})")
        self.db.execute(
            "UPDATE invoices SET subtotal_cents=?, tax_cents=?, total_cents=? WHERE id=?",
            (sub, tax, sub + tax, invoice_id),
        )

    @_reads
    def get_invoice(self, invoice_id: int) -> dict:
        row = self.db.execute(
            """SELECT i.*, c.name AS customer_name, c.email AS customer_email,
                      c.address AS customer_address, c.phone AS customer_phone
               FROM invoices i JOIN customers c ON c.id = i.customer_id
               WHERE i.id=?""",
            (invoice_id,),
        ).fetchone()
        if not row:
            raise KeyError(f"invoice {invoice_id}")
        inv = _row(row)
        lines = self.db.execute(
            "SELECT * FROM invoice_lines WHERE invoice_id=? ORDER BY position", (invoice_id,)
        ).fetchall()
        inv["lines"] = [_row(l) for l in lines]
        inv["balance_cents"] = inv["total_cents"] - inv["paid_cents"]
        inv["overdue"] = _is_overdue(inv)
        payments = self.db.execute(
            "SELECT * FROM payments WHERE invoice_id=? ORDER BY paid_at, id", (invoice_id,)
        ).fetchall()
        inv["payments"] = [_row(p) for p in payments]
        return inv

    @_reads
    def list_invoices(
        self, kind: str = "invoice", status: str | None = None, customer_id: int | None = None
    ) -> list[dict]:
        sql = """SELECT i.*, c.name AS customer_name, c.email AS customer_email
                 FROM invoices i JOIN customers c ON c.id = i.customer_id
                 WHERE i.kind=?"""
        args: list = [kind]
        if customer_id is not None:
            sql += " AND i.customer_id=?"
            args.append(customer_id)
        if status:
            sql += " AND i.status=?"
            args.append(status)
        else:
            sql += " AND i.status != 'deleted'"
        sql += " ORDER BY i.issue_date DESC, i.id DESC"
        out = []
        for r in self.db.execute(sql, args).fetchall():
            inv = _row(r)
            inv["balance_cents"] = inv["total_cents"] - inv["paid_cents"]
            inv["overdue"] = _is_overdue(inv)
            out.append(inv)
        return out

    @_reads
    def owed_by_customer(self) -> dict[int, int]:
        """What each customer owes on open invoices, in one query."""
        rows = self.db.execute(
            f"""SELECT customer_id, SUM(total_cents - paid_cents) AS owed
                FROM invoices
                WHERE kind='invoice' AND status IN ({",".join("?" * len(OPEN_STATUSES))})
                GROUP BY customer_id""",
            OPEN_STATUSES,
        ).fetchall()
        return {r["customer_id"]: r["owed"] for r in rows}

    @_reads
    def chase_text(self, invoice_id: int) -> dict:
        inv = self.get_invoice(invoice_id)
        if inv["kind"] != "invoice":
            raise Invalid("Only an invoice can be chased, not a quote")
        if inv["status"] not in OPEN_STATUSES:
            raise Invalid("Nothing to chase: the invoice isn't open")
        return chase_message(inv, self.get_company())

    @_writes
    def send_invoice(self, invoice_id: int, sent_at: str | None = None) -> dict:
        inv = self.get_invoice(invoice_id)
        when = _date(sent_at, "Sent date") if sent_at else date.today().isoformat()
        if inv["kind"] != "invoice":
            raise Invalid("Only an invoice can be sent, not a quote")
        if inv["status"] in ("sent", "partial", "paid"):
            return inv
        if inv["status"] != "draft":
            raise Invalid(f"A {inv['status']} invoice can't be sent")
        self._refuse_other_currency(inv)
        if inv["total_cents"] <= 0:
            raise Invalid("An empty invoice can't be sent")
        if self._posted_ar(invoice_id) is not None:
            # Posted by an older send that stopped halfway. Mark it sent only
            # if what the books hold is exactly what sending would post now.
            differences = self._posting_differences(inv)
            if differences:
                raise Invalid(
                    f"{inv['number']} is already in the books, but not as it is now "
                    f"({'; '.join(differences)}); void it and make a new one"
                )
            self.db.execute(
                "UPDATE invoices SET status='sent', sent_at=COALESCE(sent_at, ?) WHERE id=?",
                (when, invoice_id),
            )
            return self.get_invoice(invoice_id)
        # Accrual: the income belongs to the invoice's own date, not the day
        # Send was clicked (sent_at keeps that).
        issued = _parse_day(inv["issue_date"])
        if issued is None:
            raise Invalid(f"{inv['number']} has no readable issue date; edit the draft and set one first")
        lines = [
            ("1100", inv["total_cents"], 0),
            ("4000", 0, inv["subtotal_cents"]),
        ]
        if inv["tax_cents"]:
            lines.append(("2200", 0, inv["tax_cents"]))
        self._post(issued.isoformat(), f"Invoice {inv['number']}", lines, "invoice", invoice_id)
        self.db.execute(
            "UPDATE invoices SET status='sent', sent_at=? WHERE id=?",
            (when, invoice_id),
        )
        return self.get_invoice(invoice_id)

    @_writes
    def record_payment(
        self,
        invoice_id: int,
        amount_cents: int,
        paid_at: str,
        method: str = "bank",
        notes: str = "",
    ) -> dict:
        inv = self.get_invoice(invoice_id)
        amount_cents = _cents(amount_cents, "Payment")
        paid_at = _date(paid_at, "Payment date")
        method = _text(method, "Method", one_line=True, max_len=40)
        notes = _text(notes, "Notes", required=False, max_len=2000)
        if inv["kind"] != "invoice":
            raise Invalid("A quote can't be paid; convert it to an invoice first")
        if inv["status"] == "draft":
            raise Invalid("A draft invoice can't be paid; send it first")
        if inv["status"] not in OPEN_STATUSES:
            raise Invalid(f"A {inv['status']} invoice can't be paid")
        if amount_cents <= 0:
            raise Invalid("Payment must be more than zero")
        if amount_cents > inv["balance_cents"]:
            raise Invalid(f"Payment is more than the balance of {self._money(inv['balance_cents'], inv['currency'])}")
        jid = self._post(
            paid_at,
            f"Payment {inv['number']}",
            [("1000", amount_cents, 0), ("1100", 0, amount_cents)],
            "payment",
            invoice_id,
        )
        self.db.execute(
            """INSERT INTO payments (invoice_id, amount_cents, paid_at, method, notes, journal_id)
               VALUES (?,?,?,?,?,?)""",
            (invoice_id, amount_cents, paid_at, method, notes, jid),
        )
        paid = inv["paid_cents"] + amount_cents
        status = "paid" if paid >= inv["total_cents"] else "partial"
        self.db.execute(
            "UPDATE invoices SET paid_cents=?, status=? WHERE id=?",
            (paid, status, invoice_id),
        )
        return self.get_invoice(invoice_id)

    @_writes
    def reverse_payment(self, payment_id: int, invoice_id: int | None = None) -> dict:
        """Undo a payment recorded by mistake: post Dr 1100 / Cr 1000, mark the
        payment reversed (the row stays), and put the balance back on the
        invoice. The reversal is dated like the payment, so every report
        window that saw the payment also sees it undone.

        Give `invoice_id` to insist the payment belongs to that invoice."""
        p = self.db.execute("SELECT * FROM payments WHERE id=?", (payment_id,)).fetchone()
        if p is None or (invoice_id is not None and p["invoice_id"] != invoice_id):
            raise KeyError(f"payment {payment_id}")
        if p["reversed_at"]:
            raise Invalid("This payment was already reversed")
        inv = self.get_invoice(p["invoice_id"])
        amount = p["amount_cents"]
        when = (_parse_day(p["paid_at"]) or date.today()).isoformat()
        jid = self._post(
            when,
            f"Reverse payment {inv['number']}",
            [("1100", amount, 0), ("1000", 0, amount)],
            "payment_reversal",
            inv["id"],
        )
        self.db.execute(
            "UPDATE payments SET reversed_at=?, reversal_journal_id=? WHERE id=?",
            (_now(), jid, payment_id),
        )
        paid = inv["paid_cents"] - amount
        self.db.execute(
            "UPDATE invoices SET paid_cents=?, status=? WHERE id=?",
            (paid, "partial" if paid > 0 else "sent", inv["id"]),
        )
        return self.get_invoice(inv["id"])

    @_writes
    def void_invoice(self, invoice_id: int) -> dict:
        inv = self.get_invoice(invoice_id)
        if inv["status"] == "void":
            return inv
        if inv["status"] == "deleted":
            raise Invalid(f"{inv['number']} was deleted; there is nothing to void")
        if inv["paid_cents"]:
            raise Invalid("An invoice with payments can't be voided; reverse the payments first")
        journals = self.db.execute(
            "SELECT id FROM journal WHERE ref_type='invoice' AND ref_id=?",
            (invoice_id,),
        ).fetchall()
        for j in journals:
            lines = self.db.execute(
                "SELECT account_code, debit_cents, credit_cents FROM journal_lines WHERE journal_id=?",
                (j["id"],),
            ).fetchall()
            reverse = [(r["account_code"], r["credit_cents"], r["debit_cents"]) for r in lines]
            self._post(
                date.today().isoformat(),
                f"Void {inv['number']}",
                reverse,
                "invoice_void",
                invoice_id,
            )
        self.db.execute("UPDATE invoices SET status='void' WHERE id=?", (invoice_id,))
        return self.get_invoice(invoice_id)

    @_writes
    def convert_quote(self, quote_id: int) -> dict:
        quote = self.get_invoice(quote_id)
        if quote["kind"] != "quote":
            raise Invalid("That is an invoice, not a quote")
        if quote["status"] == "converted":
            raise Invalid("This quote was already converted to an invoice")
        if quote["status"] == "deleted":
            raise Invalid("This quote was deleted, so it can't be converted")
        if quote["status"] == "void":
            raise Invalid("This quote was voided, so it can't be converted")
        # The invoice is for the price the customer accepted, and it is
        # written in the book's currency: the quote has to be in it too.
        self._refuse_other_currency(quote)
        # The invoice is issued today on the quote's own terms (its due date
        # minus its issue date), not by the quote's expiry: a quote accepted
        # late must not turn into an invoice that is already overdue.
        today = date.today()
        issued, expires = _parse_day(quote["issue_date"]), _parse_day(quote["due_date"])
        terms = (expires - issued).days if issued and expires and expires >= issued else DEFAULT_TERMS_DAYS
        invoice = self.create_document(
            "invoice",
            customer_id=quote["customer_id"],
            issue_date=today.isoformat(),
            due_date=(today + timedelta(days=terms)).isoformat(),
            notes=quote["notes"] or "",
            job_id=quote["job_id"],
            lines=[
                {k: line[k] for k in ("description", "qty_milli", "unit_cents", "tax_bps")} for line in quote["lines"]
            ],
        )
        self.db.execute("UPDATE invoices SET status='converted' WHERE id=?", (quote_id,))
        return self.get_invoice(invoice["id"])

    @_writes
    def create_recurring(
        self,
        customer_id: int,
        cadence: str,
        next_date: str,
        lines: list[dict],
        notes: str = "",
    ) -> dict:
        customer_id = self._ref("customers", customer_id, "customer")
        if cadence not in CADENCES:
            raise Invalid("Cadence must be weekly, monthly, or yearly")
        next_date = _date(next_date, "Next date")
        notes = _text(notes, "Notes", required=False, max_len=5000)
        # Checked now, so a bad template can't fail later at tick time.
        stored = [
            {k: line[k] for k in ("description", "qty_milli", "unit_cents", "tax_bps")} for line in _lines(lines)
        ]
        template = json.dumps({"lines": stored, "notes": notes})
        # The anchor is the day it starts on: a retainer started on the 31st
        # issues on the 31st, or the last day of a shorter month.
        cur = self.db.execute(
            """INSERT INTO recurring (customer_id, cadence, next_date, template_json, active, anchor_day)
               VALUES (?,?,?,?,1,?)""",
            (customer_id, cadence, next_date, template, int(next_date[8:10])),
        )
        return self.get_recurring(cur.lastrowid)

    @_reads
    def get_recurring(self, rec_id: int) -> dict:
        row = self.db.execute(
            """SELECT r.*, c.name AS customer_name
               FROM recurring r JOIN customers c ON c.id = r.customer_id
               WHERE r.id=?""",
            (rec_id,),
        ).fetchone()
        if not row:
            raise KeyError(f"recurring {rec_id}")
        rec = _row(row)
        rec["template"] = json.loads(rec["template_json"])
        return rec

    @_reads
    def list_recurring(self, active_only: bool = False) -> list[dict]:
        sql = """SELECT r.*, c.name AS customer_name
                 FROM recurring r JOIN customers c ON c.id = r.customer_id"""
        if active_only:
            sql += " WHERE r.active=1"
        sql += " ORDER BY r.next_date, r.id"
        out = []
        for r in self.db.execute(sql).fetchall():
            rec = _row(r)
            rec["template"] = json.loads(rec["template_json"])
            out.append(rec)
        return out

    @_writes
    def set_recurring_active(self, rec_id: int, active: bool) -> dict:
        self.get_recurring(rec_id)
        self.db.execute("UPDATE recurring SET active=? WHERE id=?", (1 if active else 0, rec_id))
        return self.get_recurring(rec_id)

    def tick_recurring(self, as_of: str | None = None) -> dict:
        """Issue a draft invoice for every retainer period due by `as_of`.

        Returns {"created": [invoice, ...], "failed": [{recurring_id,
        customer_name, error}, ...]}.

        Each period is its own transaction: the invoice, its lines and the
        retainer's next_date move forward together. If one period fails, the
        periods before it stay issued, that one is not issued at all, and the
        next tick carries on from it without duplicates. A retainer that fails
        is reported in "failed" and the other retainers are still issued.
        """
        as_of = _date(as_of, "As-of date") if as_of is not None else date.today().isoformat()
        created: list[dict] = []
        failed: list[dict] = []
        with self._lock:
            due = self.db.execute(
                """SELECT r.id, c.name AS customer_name FROM recurring r
                   LEFT JOIN customers c ON c.id = r.customer_id
                   WHERE r.active=1 AND r.next_date <= ? ORDER BY r.id""",
                (as_of,),
            ).fetchall()
            for r in due:
                try:
                    for _ in range(12):  # at most a year of monthly issues per tick
                        inv = self._issue_next_period(r["id"], as_of)
                        if inv is None:
                            break
                        created.append(inv)
                except Exception as exc:  # one retainer's failure never stops the rest
                    failed.append(
                        {
                            "recurring_id": r["id"],
                            "customer_name": r["customer_name"],
                            "error": str(exc) if isinstance(exc, Invalid) else f"{type(exc).__name__}: {exc}",
                        }
                    )
        return {"created": created, "failed": failed}

    @_writes
    def _issue_next_period(self, rec_id: int, as_of: str) -> dict | None:
        # Re-read inside the transaction: another process may have issued it.
        rec = self.db.execute("SELECT * FROM recurring WHERE id=?", (rec_id,)).fetchone()
        if rec is None or not rec["active"] or rec["next_date"] > as_of:
            return None
        template = json.loads(rec["template_json"])
        day = _parse_day(rec["next_date"])
        if day is None:
            raise Invalid(f"Retainer {rec_id} has a bad next date ({rec['next_date']}); stop it and add it again")
        issue = day.isoformat()
        inv = self.create_document(
            "invoice",
            customer_id=rec["customer_id"],
            issue_date=issue,
            due_date=(day + timedelta(days=14)).isoformat(),
            notes=template.get("notes") or f"Retainer {rec_id}",
            lines=template.get("lines") or [],
        )
        self.db.execute(
            "UPDATE recurring SET next_date=? WHERE id=?",
            (_advance(day, rec["cadence"], rec["anchor_day"]).isoformat(), rec_id),
        )
        return inv

    # --- ledger --------------------------------------------------------------

    def _post(self, posted_at: str, memo: str, lines: list[tuple], ref_type: str, ref_id: int) -> int:
        debits = sum(d for _, d, _c in lines)
        credits = sum(c for _, _d, c in lines)
        if debits != credits:
            raise ValueError(f"unbalanced journal {debits} != {credits}")
        posted_at = _date(posted_at, "Posting date")
        cur = self.db.execute(
            "INSERT INTO journal (posted_at, memo, ref_type, ref_id) VALUES (?,?,?,?)",
            (posted_at, memo, ref_type, ref_id),
        )
        jid = cur.lastrowid
        for account, d, c in lines:
            if d == 0 and c == 0:
                continue
            self.db.execute(
                """INSERT INTO journal_lines (journal_id, account_code, debit_cents, credit_cents)
                   VALUES (?,?,?,?)""",
                (jid, account, d, c),
            )
        return jid

    @_reads
    def journal_is_balanced(self) -> bool:
        row = self.db.execute(
            "SELECT COALESCE(SUM(debit_cents),0) AS d, COALESCE(SUM(credit_cents),0) AS c FROM journal_lines"
        ).fetchone()
        return row["d"] == row["c"]

    @_reads
    def trial_balance(self) -> dict[str, int]:
        rows = self.db.execute(
            """SELECT a.code,
                      COALESCE(SUM(jl.debit_cents),0) - COALESCE(SUM(jl.credit_cents),0) AS net
               FROM accounts a
               LEFT JOIN journal_lines jl ON jl.account_code = a.code
               GROUP BY a.code"""
        ).fetchall()
        return {r["code"]: r["net"] for r in rows}

    @_writes
    def record_expense(
        self,
        vendor_name: str,
        amount_cents: int,
        account_code: str,
        spent_on: str,
        memo: str = "",
        vendor_id: int | None = None,
    ) -> dict:
        vendor_name = _text(vendor_name, "Vendor", one_line=True, max_len=200)
        amount_cents = _cents(amount_cents, "Amount")
        if amount_cents <= 0:
            raise Invalid("Amount must be more than zero")
        spent_on = _date(spent_on, "Date")
        memo = _text(memo, "Memo", required=False, one_line=True, max_len=500)
        if isinstance(account_code, int) and not isinstance(account_code, bool):
            account_code = str(account_code)
        if not isinstance(account_code, str):
            raise Invalid("Choose an expense account")
        acct = self.db.execute("SELECT type FROM accounts WHERE code=?", (account_code,)).fetchone()
        if not acct:
            raise Invalid(f"There is no account {account_code}")
        if acct["type"] != "expense":
            raise Invalid("An expense has to go to an expense account (5000 and up)")
        vendor_id = self._ref("vendors", vendor_id, "vendor") if vendor_id is not None else None
        jid = self._post(
            spent_on,
            memo or f"Expense {vendor_name}",
            [(account_code, amount_cents, 0), ("1000", 0, amount_cents)],
            "expense",
            0,
        )
        cur = self.db.execute(
            """INSERT INTO expenses
               (vendor_id, vendor_name, amount_cents, account_code, spent_on, memo, journal_id)
               VALUES (?,?,?,?,?,?,?)""",
            (vendor_id, vendor_name, amount_cents, account_code, spent_on, memo, jid),
        )
        return _row(self.db.execute("SELECT * FROM expenses WHERE id=?", (cur.lastrowid,)).fetchone())

    @_writes
    def reverse_expense(self, expense_id: int) -> dict:
        """Undo an expense recorded by mistake: post the opposite entry
        (Dr 1000 / Cr the expense account) dated like the expense, and mark it
        reversed. The row stays; the journal nets it to zero in every window."""
        e = self.db.execute("SELECT * FROM expenses WHERE id=?", (expense_id,)).fetchone()
        if e is None:
            raise KeyError(f"expense {expense_id}")
        if e["reversed_at"]:
            raise Invalid("This expense was already reversed")
        amount = e["amount_cents"]
        jid = self._post(
            (_parse_day(e["spent_on"]) or date.today()).isoformat(),
            f"Reverse expense {e['vendor_name']}",
            [("1000", amount, 0), (e["account_code"], 0, amount)],
            "expense_reversal",
            expense_id,
        )
        self.db.execute(
            "UPDATE expenses SET reversed_at=?, reversal_journal_id=? WHERE id=?",
            (_now(), jid, expense_id),
        )
        return _row(self.db.execute("SELECT * FROM expenses WHERE id=?", (expense_id,)).fetchone())

    @_reads
    def list_expenses(self) -> list[dict]:
        rows = self.db.execute(
            """SELECT e.*, a.name AS account_name
               FROM expenses e JOIN accounts a ON a.code = e.account_code
               ORDER BY e.spent_on DESC, e.id DESC"""
        ).fetchall()
        return [_row(r) for r in rows]

    @_reads
    def profit_and_loss(self, start: str, end: str) -> dict:
        start, end = _date(start, "Start date"), _date(end, "End date")
        income = self.db.execute(
            """SELECT COALESCE(SUM(jl.credit_cents - jl.debit_cents),0) AS n
               FROM journal_lines jl
               JOIN journal j ON j.id = jl.journal_id
               JOIN accounts a ON a.code = jl.account_code
               WHERE a.type='income' AND substr(j.posted_at,1,10) >= ? AND substr(j.posted_at,1,10) <= ?""",
            (start, end),
        ).fetchone()["n"]
        expense = self.db.execute(
            """SELECT COALESCE(SUM(jl.debit_cents - jl.credit_cents),0) AS n
               FROM journal_lines jl
               JOIN journal j ON j.id = jl.journal_id
               JOIN accounts a ON a.code = jl.account_code
               WHERE a.type='expense' AND substr(j.posted_at,1,10) >= ? AND substr(j.posted_at,1,10) <= ?""",
            (start, end),
        ).fetchone()["n"]
        by_account = self.db.execute(
            """SELECT a.code, a.name, a.type,
                      COALESCE(SUM(CASE WHEN a.type='income'
                         THEN jl.credit_cents - jl.debit_cents
                         ELSE jl.debit_cents - jl.credit_cents END),0) AS n
               FROM journal j
               JOIN journal_lines jl ON jl.journal_id = j.id
               JOIN accounts a ON a.code = jl.account_code
               WHERE a.type IN ('income','expense')
                 AND substr(j.posted_at,1,10) >= ? AND substr(j.posted_at,1,10) <= ?
               GROUP BY a.code
               HAVING n != 0
               ORDER BY a.code""",
            (start, end),
        ).fetchall()
        return {
            "start": start,
            "end": end,
            "income_cents": income,
            "expense_cents": expense,
            "net_cents": income - expense,
            "accounts": [_row(r) for r in by_account],
        }

    @_reads
    def ar_aging(self, as_of: str) -> dict:
        buckets = {
            "current_cents": 0,
            "days_1_30_cents": 0,
            "days_31_60_cents": 0,
            "days_61_90_cents": 0,
            "days_90_plus_cents": 0,
            "total_cents": 0,
            "invoices": [],
        }
        as_of_d = date.fromisoformat(_date(as_of, "As-of date"))
        for inv in self.list_invoices(kind="invoice"):
            if inv["status"] not in OPEN_STATUSES or inv["balance_cents"] <= 0:
                continue
            # A date that doesn't parse (an old database) counts as current,
            # so the invoice still adds up in the total instead of vanishing.
            due = _due_day(inv)
            days = (as_of_d - due).days if due else 0
            if days <= 0:
                key = "current_cents"
            elif days <= 30:
                key = "days_1_30_cents"
            elif days <= 60:
                key = "days_31_60_cents"
            elif days <= 90:
                key = "days_61_90_cents"
            else:
                key = "days_90_plus_cents"
            buckets[key] += inv["balance_cents"]
            buckets["total_cents"] += inv["balance_cents"]
            item = dict(inv)
            item["days_overdue"] = max(days, 0)
            buckets["invoices"].append(item)
        buckets["other_currencies"] = self.other_open_currencies()
        return buckets

    @_reads
    def other_open_currencies(self) -> list[str]:
        """Currencies of open invoices other than the company's. Totals add
        every open invoice up in the company's currency, so a page says so
        when these exist instead of quietly mixing them."""
        rows = self.db.execute(
            f"""SELECT DISTINCT currency FROM invoices
                WHERE kind='invoice' AND status IN ({",".join("?" * len(OPEN_STATUSES))})
                  AND total_cents > paid_cents AND currency IS NOT NULL AND currency != ?
                ORDER BY currency""",
            (*OPEN_STATUSES, self._company_currency()),
        ).fetchall()
        return [r["currency"] for r in rows]

    @_reads
    def list_accounts(self) -> list[dict]:
        return [_row(r) for r in self.db.execute("SELECT * FROM accounts ORDER BY code").fetchall()]

    @_reads
    def cash_cents(self) -> int:
        return self.trial_balance().get("1000", 0)

    # --- jobs / calendar -----------------------------------------------------

    @_writes
    def create_job(
        self,
        customer_id: int,
        title: str,
        status: str = "scheduled",
        notes: str = "",
        scheduled_for: str | None = None,
    ) -> dict:
        cur = self.db.execute(
            """INSERT INTO jobs (customer_id, title, status, notes, scheduled_for)
               VALUES (?,?,?,?,?)""",
            (
                self._ref("customers", customer_id, "customer"),
                _text(title, "Title", one_line=True, max_len=200),
                _job_status(status),
                _text(notes, "Notes", required=False, max_len=5000),
                _opt_date(scheduled_for, "Day"),
            ),
        )
        return self.get_job(cur.lastrowid)

    @_reads
    def get_job(self, job_id: int) -> dict:
        row = self.db.execute(
            """SELECT j.*, c.name AS customer_name
               FROM jobs j JOIN customers c ON c.id = j.customer_id
               WHERE j.id=?""",
            (job_id,),
        ).fetchone()
        if not row:
            raise KeyError(f"job {job_id}")
        return _row(row)

    @_reads
    def list_jobs(self, customer_id: int | None = None) -> list[dict]:
        where, args = ("WHERE j.customer_id=?", (customer_id,)) if customer_id is not None else ("", ())
        rows = self.db.execute(
            f"""SELECT j.*, c.name AS customer_name
               FROM jobs j JOIN customers c ON c.id = j.customer_id
               {where}
               ORDER BY COALESCE(j.scheduled_for,'9999') , j.id DESC""",
            args,
        ).fetchall()
        return [_row(r) for r in rows]

    @_writes
    def set_job_status(self, job_id: int, status: str) -> dict:
        self.get_job(job_id)
        self.db.execute("UPDATE jobs SET status=? WHERE id=?", (_job_status(status), job_id))
        return self.get_job(job_id)

    @_writes
    def invoice_from_job(self, job_id: int, qty_milli: int = 1000) -> dict:
        job = self.get_job(job_id)
        product = self.db.execute("SELECT * FROM products ORDER BY id LIMIT 1").fetchone()
        unit = product["unit_cents"] if product else 8000
        tax = product["tax_bps"] if product else 0
        name = product["name"] if product else "Shop hour"
        inv = self.create_invoice(
            customer_id=job["customer_id"],
            issue_date=date.today().isoformat(),
            due_date=(date.today() + timedelta(days=14)).isoformat(),
            notes=job["title"],
            job_id=job_id,
        )
        self.add_line(inv["id"], f"{name} — {job['title']}", qty_milli, unit, tax)
        return self.get_invoice(inv["id"])

    @_writes
    def create_event(
        self,
        title: str,
        start_at: str,
        end_at: str,
        customer_id: int | None = None,
        job_id: int | None = None,
        location: str = "",
        notes: str = "",
    ) -> dict:
        start_at = _datetime(start_at, "Start")
        end_at = _datetime(end_at, "End")
        if end_at < start_at:
            raise Invalid("End can't be before the start")
        cur = self.db.execute(
            """INSERT INTO events (title, start_at, end_at, customer_id, job_id, location, notes)
               VALUES (?,?,?,?,?,?,?)""",
            (
                _text(title, "Title", one_line=True, max_len=200),
                start_at,
                end_at,
                self._ref("customers", customer_id, "customer") if customer_id not in (None, "") else None,
                self._ref("jobs", job_id, "job") if job_id not in (None, "") else None,
                _text(location, "Location", required=False, one_line=True, max_len=300),
                _text(notes, "Notes", required=False, max_len=5000),
            ),
        )
        return self.get_event(cur.lastrowid)

    @_reads
    def get_event(self, event_id: int) -> dict:
        row = self.db.execute(
            """SELECT e.*, c.name AS customer_name
               FROM events e LEFT JOIN customers c ON c.id = e.customer_id
               WHERE e.id=?""",
            (event_id,),
        ).fetchone()
        if not row:
            raise KeyError(f"event {event_id}")
        return _row(row)

    @_reads
    def events_on(self, day: str) -> list[dict]:
        day = _date(day, "Day")
        rows = self.db.execute(
            """SELECT e.*, c.name AS customer_name
               FROM events e LEFT JOIN customers c ON c.id = e.customer_id
               WHERE substr(e.start_at,1,10)=?
               ORDER BY e.start_at""",
            (day,),
        ).fetchall()
        return [_row(r) for r in rows]

    @_reads
    def events_between(self, start: str, end: str) -> list[dict]:
        rows = self.db.execute(
            """SELECT e.*, c.name AS customer_name
               FROM events e LEFT JOIN customers c ON c.id = e.customer_id
               WHERE substr(e.start_at,1,10) >= ? AND substr(e.start_at,1,10) <= ?
               ORDER BY e.start_at""",
            (start, end),
        ).fetchall()
        return [_row(r) for r in rows]

    @_reads
    def ics(self, start: str, end: str) -> str:
        """The events from `start` to `end` as an RFC 5545 calendar.

        Times are floating local times (what the owner typed), with the
        system's zone named in X-WR-TIMEZONE when it can be found. Every
        text value is escaped, so no stored text can end an event or add a
        property, and long lines are folded at 75 octets.
        """
        lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Till//EN", "CALSCALE:GREGORIAN"]
        zone = _local_zone_name()
        if zone:
            lines.append(f"X-WR-TIMEZONE:{zone}")
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        for e in self.events_between(start, end):
            dtstart = _ics_stamp(e["start_at"])
            dtend = _ics_stamp(e["end_at"])
            lines += ["BEGIN:VEVENT", f"UID:{e['id']}@till.local", f"DTSTAMP:{stamp}", f"DTSTART:{dtstart}"]
            # Without a DTEND the event is an instant at DTSTART; an old row
            # whose end isn't after its start never says it ends earlier.
            if dtend > dtstart:
                lines.append(f"DTEND:{dtend}")
            lines.append(f"SUMMARY:{_ics_text(e['title'])}")
            if e.get("location"):
                lines.append(f"LOCATION:{_ics_text(e['location'])}")
            if e.get("notes"):
                lines.append(f"DESCRIPTION:{_ics_text(e['notes'])}")
            lines.append("END:VEVENT")
        lines.append("END:VCALENDAR")
        return "".join(_ics_fold(line) + "\r\n" for line in lines)

    # --- reviews / deals -----------------------------------------------------

    @_writes
    def add_review(
        self,
        source: str,
        author: str,
        rating: int,
        body: str,
        received_at: str,
    ) -> dict:
        cur = self.db.execute(
            """INSERT INTO reviews (source, author, rating, body, received_at, status)
               VALUES (?,?,?,?,?,'new')""",
            (
                _text(source, "Source", one_line=True, max_len=40),
                _text(author, "Author", one_line=True, max_len=200),
                _int(rating, "Stars", 1, 5),
                _text(body, "Review", max_len=10000),
                _date_or_datetime(received_at, "Received"),
            ),
        )
        return self.get_review(cur.lastrowid)

    @_reads
    def get_review(self, review_id: int) -> dict:
        row = self.db.execute("SELECT * FROM reviews WHERE id=?", (review_id,)).fetchone()
        if not row:
            raise KeyError(f"review {review_id}")
        return _row(row)

    @_reads
    def list_reviews(self, status: str | None = None) -> list[dict]:
        if status:
            rows = self.db.execute(
                "SELECT * FROM reviews WHERE status=? ORDER BY received_at DESC", (status,)
            ).fetchall()
        else:
            rows = self.db.execute("SELECT * FROM reviews ORDER BY received_at DESC").fetchall()
        return [_row(r) for r in rows]

    @_writes
    def draft_review_reply(self, review_id: int, body: str) -> dict:
        self.get_review(review_id)
        self.db.execute(
            "UPDATE reviews SET reply_draft=?, status='drafted' WHERE id=?",
            (_text(body, "Reply", max_len=10000), review_id),
        )
        return self.get_review(review_id)

    @_writes
    def mark_review_replied(self, review_id: int, replied_at: str | None = None) -> dict:
        self.get_review(review_id)
        when = _date_or_datetime(replied_at, "Replied") if replied_at else _now()
        self.db.execute(
            "UPDATE reviews SET status='replied', replied_at=? WHERE id=?",
            (when, review_id),
        )
        return self.get_review(review_id)

    @_writes
    def create_deal(
        self,
        title: str,
        stage: str = "lead",
        customer_id: int | None = None,
        value_cents: int = 0,
        notes: str = "",
    ) -> dict:
        if stage not in DEAL_STAGES:
            raise Invalid("Stage must be lead, quoted, won or lost")
        cur = self.db.execute(
            "INSERT INTO deals (customer_id, title, stage, value_cents, notes) VALUES (?,?,?,?,?)",
            (
                self._ref("customers", customer_id, "customer") if customer_id not in (None, "") else None,
                _text(title, "Title", one_line=True, max_len=200),
                stage,
                _cents(value_cents, "Value"),
                _text(notes, "Notes", required=False, max_len=5000),
            ),
        )
        return _row(self.db.execute("SELECT * FROM deals WHERE id=?", (cur.lastrowid,)).fetchone())

    @_reads
    def list_deals(self) -> list[dict]:
        rows = self.db.execute(
            """SELECT d.*, c.name AS customer_name
               FROM deals d LEFT JOIN customers c ON c.id = d.customer_id
               ORDER BY d.id DESC"""
        ).fetchall()
        return [_row(r) for r in rows]

    @_writes
    def set_deal_stage(self, deal_id: int, stage: str) -> dict:
        if not self.db.execute("SELECT 1 FROM deals WHERE id=?", (deal_id,)).fetchone():
            raise KeyError(f"deal {deal_id}")
        if stage not in DEAL_STAGES:
            raise Invalid("Stage must be lead, quoted, won or lost")
        self.db.execute("UPDATE deals SET stage=? WHERE id=?", (stage, deal_id))
        return _row(self.db.execute("SELECT * FROM deals WHERE id=?", (deal_id,)).fetchone())

    @_reads
    def last_payment_method(self) -> str | None:
        """How the most recent payment that still stands was made, if any."""
        row = self.db.execute(
            "SELECT method FROM payments WHERE reversed_at IS NULL ORDER BY paid_at DESC, id DESC LIMIT 1"
        ).fetchone()
        return row["method"] if row else None

    @_reads
    def shop_rate(self) -> dict | None:
        """The first product added: what a job is billed at by default."""
        return _row(self.db.execute("SELECT * FROM products ORDER BY id LIMIT 1").fetchone())

    @_reads
    def list_products(self) -> list[dict]:
        return [_row(r) for r in self.db.execute("SELECT * FROM products ORDER BY name").fetchall()]

    @_writes
    def create_product(self, name: str, unit_cents: int, tax_bps: int = 0) -> dict:
        cur = self.db.execute(
            "INSERT INTO products (name, unit_cents, tax_bps) VALUES (?,?,?)",
            (
                _text(name, "Product name", one_line=True, max_len=200),
                _cents(unit_cents, "Rate"),
                _int(tax_bps, "Tax", 0, 10_000),
            ),
        )
        return _row(self.db.execute("SELECT * FROM products WHERE id=?", (cur.lastrowid,)).fetchone())

    # --- brief ---------------------------------------------------------------

    @_reads
    def today_brief(self, today: str | None = None) -> dict:
        today = today or date.today().isoformat()
        invoices = self.list_invoices()
        overdue = [i for i in invoices if i["overdue"]]
        reviews = [r for r in self.list_reviews() if r["status"] in ("new", "drafted")]
        month_start = today[:8] + "01"
        pnl = self.profit_and_loss(month_start, today)
        drafts = [i for i in invoices if i["status"] == "draft"]
        return {
            "today": today,
            "cash_cents": self.cash_cents(),
            "overdue": overdue,
            "overdue_count": len(overdue),
            "overdue_cents": sum(i["balance_cents"] for i in overdue),
            "events": self.events_on(today),
            "reviews": reviews,
            "review_count": len(reviews),
            "drafts": drafts,
            "month_income_cents": pnl["income_cents"],
            "month_expense_cents": pnl["expense_cents"],
            "month_net_cents": pnl["net_cents"],
            "other_currencies": self.other_open_currencies(),
        }

    @_reads
    def brief_text(self, today: str | None = None) -> str:
        b = self.today_brief(today)
        company = self.get_company()["name"]
        money = self._money
        lines = [
            f"{company} — {b['today']}",
            f"Cash {money(b['cash_cents'])}",
            f"This month invoiced {money(b['month_income_cents'])} / spent {money(b['month_expense_cents'])}",
        ]
        if b["overdue"]:
            lines.append(f"{b['overdue_count']} overdue ({money(b['overdue_cents'])})")
            for inv in b["overdue"]:
                lines.append(f"  {inv['number']} {inv['customer_name']} {money(inv['balance_cents'], inv['currency'])}")
        else:
            lines.append("Nothing overdue.")
        if b["other_currencies"]:
            lines.append(f"Some open invoices are {mixed_note(b['other_currencies'])}.")
        if b["events"]:
            lines.append("Today:")
            for e in b["events"]:
                hh = e["start_at"][11:16] if len(e["start_at"]) >= 16 else ""
                who = f" · {e['customer_name']}" if e.get("customer_name") else ""
                lines.append(f"  {hh} {e['title']}{who}")
        else:
            lines.append("No jobs on the calendar today.")
        if b["reviews"]:
            lines.append(f"{b['review_count']} review(s) waiting")
            for r in b["reviews"]:
                lines.append(f"  {r['rating']}★ {r['author']}: {r['body'][:80]}")
        return "\n".join(lines)

    @_reads
    def dump_state(self) -> str:
        return json.dumps(
            {
                "company": self.get_company(),
                "cash_cents": self.cash_cents(),
                "invoices": self.list_invoices(),
                "balanced": self.journal_is_balanced(),
            },
            default=str,
        )


def chase_message(inv: dict, company: dict) -> dict:
    """The reminder email for an open invoice row (from get_invoice or
    list_invoices, which both carry customer_name and customer_email).

    Overdue, it is a chase that says how late the invoice is. Not yet due,
    it is a friendly reminder of the due date, with no mention of lateness.
    `overdue` says which, so a page can label it "Chase" or "Send a reminder".
    """
    shop = company.get("name") or "us"
    first = ((inv["customer_name"] or "").split() or ["there"])[0]
    money = format_cents(inv["balance_cents"], inv.get("currency") or company.get("currency") or "USD")
    due = _parse_day(inv.get("due_date"))  # the same date _is_overdue goes by
    days = (date.today() - due).days if due else 0
    overdue = _is_overdue(inv)
    what = f"invoice {inv['number']} from {shop} for {money}"
    if overdue:
        late = "1 day ago" if days == 1 else f"{days} days ago"
        middle = (
            f"Just a note that {what} was due on {_long_date(due)}, {late}, and is still open. "
            "Could you send payment when you have a moment? If it's already on its way, please ignore this."
        )
        subject = f"Overdue: {shop} invoice {inv['number']} ({money})"
    else:
        when = "today" if due == date.today() else (f"on {_long_date(due)}" if due else None)
        due_part = f" is due {when}" if when else " is still open"
        middle = f"A friendly reminder that {what}{due_part}."
        subject = f"Reminder: {shop} invoice {inv['number']} ({money})"
    body = f"Hi {first},\n\n{middle}\n\nThanks,\n{shop}"
    email = inv.get("customer_email") or ""
    # The address is percent-encoded too, so a stored address can never
    # add its own ?bcc= or &body= to the link.
    mailto = f"mailto:{quote(email, safe='@')}?subject={quote(subject, safe='')}&body={quote(body, safe='')}"
    return {
        "invoice_id": inv["id"],
        "number": inv["number"],
        "customer_name": inv["customer_name"],
        "customer_email": email,
        "subject": subject,
        "body": body,
        "mailto": mailto,
        "balance_cents": inv["balance_cents"],
        "days_overdue": max(days, 0) if overdue else 0,
        "overdue": overdue,
    }


def mixed_note(currencies: list[str]) -> str:
    """'in another currency (EUR, GBP)': the words a total carries when it
    adds up open invoices that aren't all in the company's currency."""
    return f"in another currency ({', '.join(currencies)}), and totals count them as if they weren't"


def _long_date(d: date) -> str:
    return f"{d.day} {calendar.month_name[d.month]} {d.year}"


def _now() -> str:
    return datetime.now().replace(microsecond=0).isoformat()


# --- checks on the way in -------------------------------------------------------
#
# Every public write runs its input through these before touching the book, so
# the web form, the JSON API and the CLI are all held to the same rules. Each
# refusal is an Invalid with a message meant for the owner.

_DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_DATETIME_RE = re.compile(r"([0-9]{4}-[0-9]{2}-[0-9]{2})T([0-9]{2}):([0-9]{2})(?::([0-9]{2}))?")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]+")
_CONTROL_KEEP_LINES = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]+")
# One @, a dot in the domain, and none of the characters that would let an
# address add headers or recipients to a mailto: link.
_EMAIL_BAD = r"\s@?&#/\\,;:<>\"'()\[\]%\x00-\x1f\x7f"
_EMAIL_RE = re.compile(f"[^{_EMAIL_BAD}]+@[^{_EMAIL_BAD}]+\\.[^{_EMAIL_BAD}]+")


def _text(value, label: str, *, required: bool = True, one_line: bool = False, max_len: int = 2000) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise Invalid(f"{label} must be text")
    value = (_CONTROL if one_line else _CONTROL_KEEP_LINES).sub(" ", value).strip()
    if required and not value:
        raise Invalid(f"{label} is required")
    if len(value) > max_len:
        raise Invalid(f"{label} is too long (at most {max_len} characters)")
    return value


def _whole(value, label: str) -> int:
    if value is None or value == "":
        raise Invalid(f"{label} is required")
    if isinstance(value, bool) or not isinstance(value, int):
        raise Invalid(f"{label} must be a whole number")
    return value


def _int(value, label: str, lo: int, hi: int) -> int:
    value = _whole(value, label)
    if not lo <= value <= hi:
        raise Invalid(f"{label} must be from {lo} to {hi}")
    return value


def _cents(value, label: str) -> int:
    """Whole cents, never negative, at most MAX_CENTS."""
    value = _whole(value, label)
    if value < 0:
        raise Invalid(f"{label} can't be negative")
    if value > MAX_CENTS:
        raise Invalid(f"{label} is too large (the most Till takes is {format_cents(MAX_CENTS)})")
    return value


def _date(value, label: str) -> str:
    if isinstance(value, str) and _DATE_RE.fullmatch(value.strip()):
        try:
            return date.fromisoformat(value.strip()).isoformat()
        except ValueError:
            pass
    raise Invalid(f"{label} must be a real date like 2026-11-14")


def _opt_date(value, label: str) -> str | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    return _date(value, label)


def _datetime(value, label: str) -> str:
    m = _DATETIME_RE.fullmatch(value.strip()) if isinstance(value, str) else None
    if m:
        try:
            day = date.fromisoformat(m[1])
            when = datetime(day.year, day.month, day.day, int(m[2]), int(m[3]), int(m[4] or 0))
            return when.isoformat()
        except ValueError:
            pass
    raise Invalid(f"{label} must be a date and time like 2026-11-14T09:00")


def _date_or_datetime(value, label: str) -> str:
    if isinstance(value, str) and "T" in value:
        return _datetime(value, label)
    return _date(value, label)


def _email(value) -> str | None:
    value = _text(value, "Email", required=False, max_len=254)
    if value and not _EMAIL_RE.fullmatch(value):
        raise Invalid("Email doesn't look like an address (like name@example.com)")
    return value or None


def _currency(value) -> str:
    if isinstance(value, str) and re.fullmatch(r"[A-Za-z]{3}", value.strip()):
        return value.strip().upper()
    raise Invalid("Currency must be a 3-letter code like USD")


def _flag(value, label: str) -> int:
    if isinstance(value, bool) or value in (0, 1):
        return int(value)
    raise Invalid(f"{label} must be true or false")


def _job_status(value) -> str:
    if value not in JOB_STATUSES:
        raise Invalid("Status must be scheduled, in_progress, done or cancelled")
    return value


def _customer_fields(name, email, phone, address, notes) -> tuple:
    return (
        _text(name, "Name", one_line=True, max_len=200),
        _email(email),
        _text(phone, "Phone", required=False, one_line=True, max_len=100) or None,
        _text(address, "Address", required=False, max_len=1000) or None,
        _text(notes, "Notes", required=False, max_len=5000) or None,
    )


def _line(raw, n: int) -> dict:
    """One checked invoice line, with its amounts worked out."""
    if not isinstance(raw, dict):
        raise Invalid(f"Line {n} needs a description, quantity and rate")
    description = _text(raw.get("description"), f"Line {n} description", one_line=True, max_len=500)
    qty = _whole(raw.get("qty_milli"), f"Line {n} quantity")
    if qty <= 0:
        raise Invalid(f"Line {n} quantity must be more than zero")
    if qty > MAX_QTY_MILLI:
        raise Invalid(f"Line {n} quantity is too large")
    unit = _cents(raw.get("unit_cents"), f"Line {n} rate")
    tax = raw.get("tax_bps")
    tax = _whole(0 if tax is None else tax, f"Line {n} tax")
    if not 0 <= tax <= 10_000:
        raise Invalid(f"Line {n} tax must be between 0% and 100%")
    lc = line_cents(qty, unit)
    if lc > MAX_CENTS:
        raise Invalid(f"Line {n} amount is too large (the most Till takes is {format_cents(MAX_CENTS)})")
    return {
        "description": description,
        "qty_milli": qty,
        "unit_cents": unit,
        "tax_bps": tax,
        "line_cents": lc,
        "tax_cents": tax_cents(lc, tax),
    }


def _lines(lines) -> list[dict]:
    if not isinstance(lines, list):
        raise Invalid("Lines must be a list")
    if not lines:
        raise Invalid("Add at least one line")
    if len(lines) > 500:
        raise Invalid("Too many lines (at most 500)")
    return [_line(raw, n) for n, raw in enumerate(lines, start=1)]


def _parse_day(value) -> date | None:
    """A stored date, or None if it is missing or unreadable (old databases)."""
    if not isinstance(value, str) or len(value) < 10 or not _DATE_RE.fullmatch(value[:10]):
        return None
    if len(value) > 10 and value[10] != "T":
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _due_day(inv: dict) -> date | None:
    return _parse_day(inv.get("due_date")) or _parse_day(inv.get("issue_date"))


def _is_overdue(inv: dict) -> bool:
    if inv.get("kind") and inv["kind"] != "invoice":
        return False
    if inv["status"] not in OPEN_STATUSES:
        return False
    if (inv["total_cents"] - inv["paid_cents"]) <= 0:
        return False
    due = _parse_day(inv.get("due_date"))
    if due is None:
        return False  # no due date, or one an old database stored badly
    return due < date.today()


def _ics_stamp(iso: str) -> str:
    raw = iso.replace("-", "").replace(":", "")
    if "T" in raw:
        return raw[:15]
    return raw[:8] + "T090000"


_ICS_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def _ics_text(value) -> str:
    """An RFC 5545 TEXT value: backslash, semicolon and comma escaped, each
    line break written as \\n, CR and other control characters dropped."""
    text = str(value or "").replace("\r", "")
    text = _ICS_CONTROL.sub("", text)
    text = text.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,")
    return text.replace("\n", "\\n")


def _ics_fold(line: str) -> str:
    """Fold a content line so no physical line is longer than 75 octets,
    never splitting a UTF-8 character. Continuations start with a space."""
    out, current, limit = [], b"", 75
    for ch in line:
        b = ch.encode("utf-8")
        if len(current) + len(b) > limit:
            out.append(current)
            current, limit = b"", 74  # the leading space takes one octet
        current += b
    out.append(current)
    return "\r\n ".join(part.decode("utf-8") for part in out)


def _local_zone_name() -> str | None:
    """The system's IANA time zone (like America/Denver), or None.

    TZ first, then the /etc/localtime symlink, then /etc/timezone. Only a
    name that looks like Area/City and exists in the zone database counts."""
    candidates = []
    tz = os.environ.get("TZ")
    if tz:
        candidates.append(tz.removeprefix(":"))
    else:
        try:
            target = os.readlink("/etc/localtime")
            if "zoneinfo/" in target:
                candidates.append(target.split("zoneinfo/", 1)[1])
        except OSError:
            pass
        try:
            candidates.append(Path("/etc/timezone").read_text(encoding="utf-8").strip())
        except OSError:
            pass
    for name in candidates:
        if re.fullmatch(r"[A-Za-z_]+(/[A-Za-z0-9_+-]+)+|UTC", name):
            try:
                zoneinfo.ZoneInfo(name)
                return name
            except (zoneinfo.ZoneInfoNotFoundError, ValueError, OSError):
                continue
    return None


def _advance(d: date, cadence: str, anchor_day: int | None) -> date:
    """The period after `d`. Monthly and yearly land on the anchor day, or
    the month's last day when it is shorter, so a retainer started on the
    31st issues on Feb 28 and then Mar 31 again (and a Feb 29 one comes back
    to the 29th in leap years). Each step starts from the anchor, never from
    the previous clamped date, so it can't drift."""
    if cadence == "weekly":
        return d + timedelta(days=7)
    anchor = anchor_day if isinstance(anchor_day, int) and 1 <= anchor_day <= 31 else d.day
    if cadence == "yearly":
        year, month = d.year + 1, d.month
    else:
        year, month = (d.year + 1, 1) if d.month == 12 else (d.year, d.month + 1)
    return date(year, month, min(anchor, calendar.monthrange(year, month)[1]))
