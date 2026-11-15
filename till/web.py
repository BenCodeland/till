"""Local HTTP UI and API. Binds to loopback by default."""

from __future__ import annotations

import calendar as _calendar
import os
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Annotated
from urllib.parse import urlencode, urlsplit

from fastapi import FastAPI, Query, Request
from fastapi import Path as PathParam
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from jinja2 import pass_context
from starlette.exceptions import HTTPException as StarletteHTTPException

from till.money import (
    CURRENCY_NAMES,
    Invalid,
    currency_prefix,
    format_cents,
    parse_money,
    parse_percent,
    parse_qty,
    percent_label,
    qty_label,
)
from till.store import CURRENCY_LOCKED, Store, chase_message, mixed_note

ROOT = Path(__file__).resolve().parent
PROJECT = ROOT.parent
DEFAULT_DB = Path(os.environ.get("TILL_DB", PROJECT / "data" / "till.db"))


def _real_url() -> str:
    from till.install import SettingError, app_url, configured_port

    try:
        return app_url(configured_port())
    except SettingError:
        return app_url(7740)


def real_db_path() -> Path:
    """Where the owner's own book is (TILL_DB, else data/till.db), read when
    asked, so the demo server can say where the real book lives."""
    return Path(os.environ.get("TILL_DB", PROJECT / "data" / "till.db")).resolve()


PKG_STATIC = ROOT / "static"
PKG_TEMPLATES = ROOT / "templates"

# Till only ever listens on loopback, so these are the only names a browser
# can legitimately use to reach it. Anything else in Host is DNS rebinding.
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "[::1]")
UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    # Only framing is restricted: the templates use an inline script and an
    # inline onsubmit handler, which a script-src policy would break.
    "Content-Security-Policy": "frame-ancestors 'none'",
}
# The payment method picker; a method saved through the API shows as typed.
PAYMENT_METHODS = {"bank": "Bank", "cash": "Cash", "card": "Card", "cheque": "Cheque"}
# Shown instead of any ValueError that wasn't written for a person (Invalid is),
# so Python's own wording ("invalid literal for int()") never reaches the page.
GENERIC_ERROR = "Something in that wasn't valid. Check the numbers and dates and try again."
NOT_FOUND = "There's nothing here. The link may be old, or the number typed wrong."
# Shown for anything that wasn't expected at all (a 500). Never the exception.
CRASHED = "Something went wrong. Nothing you typed caused it; try again, and if it keeps happening, run `till doctor`."
# A record id is a SQLite integer. A path id outside that range names
# nothing (404); a query id outside it is refused (400). Without the bound,
# the database driver raises OverflowError and the page is a 500.
MIN_ID, MAX_ID = -(2**63), 2**63 - 1
Id = Annotated[int, PathParam(ge=MIN_ID, le=MAX_ID)]
QueryId = Annotated[int | None, Query(ge=MIN_ID, le=MAX_ID)]
# ?saved=<key> after a form saves. Only these fixed words are ever shown, so a
# link can't put its own text on the page.
SAVED = {
    "1": "Saved.",
    "payment": "Payment recorded.",
    "expense": "Expense recorded.",
}


def create_app(
    db_path: str | Path | None = None,
    allowed_hosts: list[str] | tuple[str, ...] | None = None,
) -> FastAPI:
    """Build the app. `allowed_hosts` replaces the loopback names (tests pass
    "testserver" here); the default accepts only 127.0.0.1, localhost and [::1]."""
    store = Store(db_path or DEFAULT_DB)
    store.migrate()
    hosts = {h.lower() for h in (allowed_hosts if allowed_hosts is not None else LOOPBACK_HOSTS)}
    app = FastAPI(title="Till", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.store = store

    @app.middleware("http")
    async def request_guard(request: Request, call_next):
        refusal = _refuse(request, hosts)
        response = refusal or await call_next(request)
        for name, value in SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        return response
    templates = Jinja2Templates(directory=str(PKG_TEMPLATES))
    @pass_context
    def money(ctx, cents, currency=None):
        """Cents in `currency` (a document's own: `x | money(invoice.currency)`),
        else in the company's, which is what totals and summaries use."""
        company = ctx.get("company") or {}
        return format_cents(cents, currency or company.get("currency"))

    templates.env.filters["money"] = money
    templates.env.filters["long_date"] = _long_date
    templates.env.filters["weekday"] = _weekday
    templates.env.filters["qty"] = qty_label
    templates.env.filters["pct"] = percent_label
    templates.env.globals["mixed_note"] = mixed_note

    def render(request: Request, name: str, status_code: int = 200, **ctx):
        ctx.setdefault("company", store.get_company())
        ctx.setdefault("today", date.today().isoformat())
        ctx.setdefault("nav", _nav(request.url.path))
        # A form that failed redirects back here with ?error=; base.html shows it.
        ctx.setdefault("error", request.query_params.get("error"))
        ctx.setdefault("saved", SAVED.get(request.query_params.get("saved", "")))
        return templates.TemplateResponse(
            request=request, name=name, context=ctx, status_code=status_code
        )

    def message_page(request: Request, status_code: int, heading: str, message: str):
        return render(request, "message.html", status_code=status_code, heading=heading, message=message, error=None)

    # --- errors -----------------------------------------------------------------
    # The store refuses bad input with ValueError (Invalid carries a message
    # for the owner) and a missing record with KeyError. These turn them into
    # 400/404 on the API, and on the pages into a redirect back to the form
    # with ?error=, or a plain page. Nothing a user types should reach a 500.

    @app.exception_handler(ValueError)
    def on_invalid(request: Request, exc: ValueError):
        msg = _message(exc)
        if _is_api(request):
            return JSONResponse({"detail": msg}, status_code=400)
        if request.method in UNSAFE_METHODS:
            return RedirectResponse(_with_error(_back(request.url.path), msg), status_code=303)
        return message_page(request, 400, "That didn't work", msg)

    @app.exception_handler(KeyError)
    def on_missing(request: Request, exc: KeyError):
        if _is_api(request):
            return JSONResponse({"detail": "Not found"}, status_code=404)
        return message_page(request, 404, "Not found", NOT_FOUND)

    @app.exception_handler(RequestValidationError)
    def on_bad_request(request: Request, exc: RequestValidationError):
        # A path id that isn't a number (/invoices/abc) names nothing: 404.
        in_path = any(tuple(e.get("loc", ()))[:1] == ("path",) for e in exc.errors())
        if _is_api(request):
            if in_path:
                return JSONResponse({"detail": "Not found"}, status_code=404)
            return JSONResponse({"detail": _validation_message(exc)}, status_code=400)
        if in_path:
            return message_page(request, 404, "Not found", NOT_FOUND)
        return message_page(request, 400, "That didn't work", _validation_message(exc))

    @app.exception_handler(StarletteHTTPException)
    def on_http(request: Request, exc: StarletteHTTPException):
        if _is_api(request) or exc.status_code not in (404, 405):
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=exc.headers)
        if exc.status_code == 404:
            return message_page(request, 404, "Not found", NOT_FOUND)
        return message_page(request, exc.status_code, "That didn't work", "That page can't do that.")

    @app.exception_handler(Exception)
    def on_crash(request: Request, exc: Exception):
        # Anything else is a 500. Starlette runs this outside every middleware,
        # so the security headers are set here, and the exception's text never
        # reaches the page (it is still logged by the server).
        if _is_api(request):
            response = JSONResponse({"detail": "Something went wrong"}, status_code=500)
        else:
            try:
                response = message_page(request, 500, "Something went wrong", CRASHED)
            except Exception:  # the page itself needs the book, which may be what broke
                response = HTMLResponse(
                    "<!doctype html><title>Something went wrong</title><h1>Something went wrong</h1>",
                    status_code=500,
                )
        for name, value in SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        return response

    app.mount("/static", StaticFiles(directory=str(PKG_STATIC)), name="static")

    @app.get("/", response_class=HTMLResponse)
    def today(request: Request):
        brief = store.today_brief()
        company = store.get_company()
        for inv in brief["overdue"]:  # built from the rows already loaded: no query per invoice
            inv["chase"] = chase_message(inv, company)
        # A new, real book gets a short guide instead of an empty page.
        first_run = not company.get("is_demo") and store.is_new_book()
        return render(request, "today.html", brief=brief, first_run=first_run)

    @app.get("/invoices", response_class=HTMLResponse)
    def invoices(request: Request, kind: str = "invoice"):
        return render(
            request,
            "invoices.html",
            invoices=store.list_invoices(kind=kind),
            kind=kind,
            recurring=store.list_recurring(),
            customers=store.list_customers(),
        )

    def invoice_form(
        request: Request,
        kind: str,
        status_code: int = 200,
        error: str | None = None,
        editing: dict | None = None,
        **values,
    ):
        """The new-document form, or with `editing` the same form for a draft."""
        products = store.list_products()
        company = store.get_company()
        rate = store.shop_rate()
        # A new line starts at the shop rate and the default tax from Settings.
        blank_line = {
            "description": "",
            "qty": "1",
            "unit": f"{rate['unit_cents'] / 100:.2f}" if rate else "",
            "tax_pct": percent_label(company["default_tax_bps"]),
        }
        ctx = {
            "customer_id": None,
            "job_id": None,
            "issue_date": date.today().isoformat(),
            "due_date": (date.today() + timedelta(days=14)).isoformat(),
            "notes": "",
            "lines": [blank_line],
        }
        ctx.update({k: v for k, v in values.items() if v is not None})
        # Without JavaScript, "Add another line" can't add rows, so there are
        # always two blank ones to fill in. Blank rows are skipped on save.
        ctx["lines"] = [*ctx["lines"], blank_line, blank_line]
        customers = store.list_customers()
        if editing:
            action = f"/invoices/{editing['id']}/edit"
            heading = f"Edit {editing['number']}"
            if all(c["id"] != editing["customer_id"] for c in customers):
                # An archived customer stays selectable on their own drafts.
                customers = [store.get_customer(editing["customer_id"]), *customers]
        else:
            action = "/quotes/new" if kind == "quote" else "/invoices/new"
            heading = None
            cid = _as_id(ctx.get("customer_id"))
            if cid is not None and all(c["id"] != cid for c in customers):
                # A job's customer may have been archived since; without this
                # the browser would silently pick the first name in the list.
                try:
                    customers = [store.get_customer(cid), *customers]
                except KeyError:
                    pass
        return render(
            request,
            "invoice_form.html",
            status_code=status_code,
            customers=customers,
            products=products,
            jobs=store.list_jobs(),
            invoice=editing,
            kind=kind,
            action=action,
            heading=heading,
            error=error,
            blank_line=blank_line,
            # Saving puts a document in the book's currency; an older Till could
            # leave a draft in another, and the form says it will change.
            currency_prefix=currency_prefix(company["currency"]),
            currency_change=(editing["currency"], company["currency"])
            if editing and editing.get("currency") != company["currency"]
            else None,
            **ctx,
        )

    def typed_back(request: Request, form, kind: str, exc: ValueError, editing: dict | None = None):
        """The form again, with everything the user typed and what was wrong."""
        return invoice_form(
            request,
            kind,
            status_code=400,
            error=_message(exc),
            editing=editing,
            customer_id=_field(form, "customer_id"),
            job_id=_field(form, "job_id"),
            issue_date=_field(form, "issue_date"),
            due_date=_field(form, "due_date"),
            notes=_field(form, "notes"),
            # Trailing blank rows come back on their own (invoice_form adds
            # two); rows before the last filled one keep their place, so
            # "Line 3" in a message is still the third row.
            lines=_trim_blank_rows(_typed_lines(form)) or None,
        )

    @app.get("/invoices/new", response_class=HTMLResponse)
    def invoice_new_form(request: Request, customer_id: QueryId = None, job_id: QueryId = None):
        lines = None
        if job_id is not None:
            # From a job: its customer, and one line for the job at the shop
            # rate and default tax, for the owner to check before saving.
            job = store.get_job(job_id)
            customer_id = customer_id or job["customer_id"]
            rate = store.shop_rate()
            lines = [
                {
                    "description": f"{rate['name']} — {job['title']}" if rate else job["title"],
                    "qty": "1",
                    "unit": f"{rate['unit_cents'] / 100:.2f}" if rate else "",
                    "tax_pct": percent_label(store.get_company()["default_tax_bps"]),
                }
            ]
        return invoice_form(request, "invoice", customer_id=customer_id, job_id=job_id, lines=lines)

    @app.post("/invoices/new")
    async def invoice_new(request: Request):
        form = await request.form()
        try:
            inv = _save_new_invoice(store, form, kind="invoice")
        except ValueError as exc:
            return typed_back(request, form, "invoice", exc)
        return RedirectResponse(f"/invoices/{inv['id']}", status_code=303)

    @app.get("/invoices/{invoice_id}", response_class=HTMLResponse)
    def invoice_detail(request: Request, invoice_id: Id):
        inv = store.get_invoice(invoice_id)
        chase = None
        if inv["status"] in ("sent", "partial"):
            try:
                chase = store.chase_text(invoice_id)
            except ValueError:
                chase = None
        return render(
            request,
            "invoice.html",
            invoice=inv,
            chase=chase,
            last_method=store.last_payment_method() or "bank",
            methods=PAYMENT_METHODS,
        )

    @app.get("/invoices/{invoice_id}/edit", response_class=HTMLResponse)
    def invoice_edit_form(request: Request, invoice_id: Id):
        inv = store.get_invoice(invoice_id)
        if inv["status"] != "draft":
            raise Invalid(f"Only a draft can be edited; {inv['number']} is {inv['status']}")
        return invoice_form(
            request,
            inv["kind"],
            editing=inv,
            customer_id=inv["customer_id"],
            job_id=inv["job_id"],
            issue_date=inv["issue_date"],
            due_date=inv["due_date"] or "",
            notes=inv["notes"] or "",
            lines=[
                {
                    "description": line["description"],
                    "qty": qty_label(line["qty_milli"]),
                    "unit": f"{line['unit_cents'] / 100:.2f}",
                    "tax_pct": percent_label(line["tax_bps"]),
                }
                for line in inv["lines"]
            ]
            or None,
        )

    @app.post("/invoices/{invoice_id}/edit")
    async def invoice_edit(request: Request, invoice_id: Id):
        inv = store.get_invoice(invoice_id)
        if inv["status"] != "draft":
            raise Invalid(f"Only a draft can be edited; {inv['number']} is {inv['status']}")
        form = await request.form()
        try:
            lines = _lines_from_form(form)
            if not lines:
                raise Invalid("Add at least one line with a description")
            store.update_document(
                invoice_id,
                customer_id=_field(form, "customer_id") or None,
                issue_date=_field(form, "issue_date") or inv["issue_date"],
                due_date=_field(form, "due_date") or None,
                notes=_field(form, "notes"),
                job_id=_field(form, "job_id") or None,
                lines=lines,
            )
        except ValueError as exc:
            return typed_back(request, form, inv["kind"], exc, editing=inv)
        return RedirectResponse(f"/invoices/{invoice_id}?saved=1", status_code=303)

    @app.post("/invoices/{invoice_id}/delete")
    def invoice_delete(invoice_id: Id):
        inv = store.delete_draft(invoice_id)
        return RedirectResponse("/invoices?kind=quote" if inv["kind"] == "quote" else "/invoices", status_code=303)

    @app.post("/invoices/{invoice_id}/payments/{payment_id}/reverse")
    def payment_reverse(invoice_id: Id, payment_id: Id):
        store.reverse_payment(payment_id, invoice_id=invoice_id)
        return RedirectResponse(f"/invoices/{invoice_id}", status_code=303)

    @app.get("/invoices/{invoice_id}/print", response_class=HTMLResponse)
    def invoice_print(request: Request, invoice_id: Id):
        return render(request, "invoice_print.html", invoice=store.get_invoice(invoice_id))

    @app.post("/invoices/{invoice_id}/send")
    def invoice_send(invoice_id: Id):
        store.send_invoice(invoice_id)
        return RedirectResponse(f"/invoices/{invoice_id}", status_code=303)

    @app.post("/invoices/{invoice_id}/pay")
    async def invoice_pay(request: Request, invoice_id: Id):
        form = await request.form()
        store.record_payment(
            invoice_id,
            amount_cents=parse_money(_field(form, "amount"), "Payment"),
            paid_at=_field(form, "paid_at") or date.today().isoformat(),
            method=_field(form, "method") or "bank",
        )
        return RedirectResponse(f"/invoices/{invoice_id}?saved=payment", status_code=303)

    @app.post("/invoices/{invoice_id}/void")
    def invoice_void(invoice_id: Id):
        store.void_invoice(invoice_id)
        return RedirectResponse(f"/invoices/{invoice_id}", status_code=303)

    @app.get("/quotes/new", response_class=HTMLResponse)
    def quote_new_form(request: Request):
        return invoice_form(request, "quote")

    @app.post("/quotes/new")
    async def quote_new(request: Request):
        form = await request.form()
        try:
            inv = _save_new_invoice(store, form, kind="quote")
        except ValueError as exc:
            return typed_back(request, form, "quote", exc)
        return RedirectResponse(f"/invoices/{inv['id']}", status_code=303)

    @app.post("/quotes/{quote_id}/convert")
    def quote_convert(quote_id: Id):
        inv = store.convert_quote(quote_id)
        return RedirectResponse(f"/invoices/{inv['id']}", status_code=303)

    @app.post("/recurring")
    async def recurring_new(request: Request):
        form = await request.form()
        store.create_recurring(
            customer_id=_field(form, "customer_id") or None,
            cadence=_field(form, "cadence") or "monthly",
            next_date=_field(form, "next_date") or date.today().isoformat(),
            lines=[
                {
                    "description": _field(form, "description") or "Retainer",
                    "qty_milli": parse_qty(_field(form, "qty") or "1", "Qty"),
                    "unit_cents": parse_money(_field(form, "unit") or "0", "Rate"),
                    "tax_bps": _form_tax(form.get("tax_pct"), form.get("tax_bps"), "Tax"),
                }
            ],
        )
        return RedirectResponse("/invoices", status_code=303)

    @app.post("/recurring/{rec_id}/stop")
    def recurring_stop(rec_id: Id):
        store.set_recurring_active(rec_id, False)
        return RedirectResponse("/invoices", status_code=303)

    @app.post("/tick")
    def tick():
        result = store.tick_recurring()
        created, failed = result["created"], result["failed"]
        if failed:
            issued = f"Issued {len(created)} draft(s). " if created else ""
            problems = "; ".join(
                f"Retainer {f['recurring_id']} ({f['customer_name'] or 'no customer'}) wasn't issued: {f['error']}"
                for f in failed
            )
            return RedirectResponse(_with_error("/invoices", issued + problems), status_code=303)
        if len(created) == 1:
            return RedirectResponse(f"/invoices/{created[0]['id']}", status_code=303)
        return RedirectResponse("/invoices", status_code=303)

    @app.get("/people", response_class=HTMLResponse)
    def people(request: Request):
        owed = store.owed_by_customer()
        customers = [{**c, "owed_cents": owed.get(c["id"], 0)} for c in store.list_customers()]
        archived = [c for c in store.list_customers(include_archived=True) if c["archived_at"]]
        return render(request, "people.html", customers=customers, archived=archived, deals=store.list_deals())

    @app.post("/people/new")
    async def people_new(request: Request):
        form = await request.form()
        cid = store.create_customer(
            name=_field(form, "name"),
            email=_field(form, "email") or None,
            phone=_field(form, "phone") or None,
            address=_field(form, "address") or None,
            notes=_field(form, "notes") or None,
        )
        return RedirectResponse(f"/people/{cid}", status_code=303)

    @app.post("/people/{customer_id}/edit")
    async def people_edit(request: Request, customer_id: Id):
        form = await request.form()
        fields = ("name", "email", "phone", "address", "notes")
        store.update_customer(customer_id, **{k: _field(form, k) or None for k in fields if k in form})
        return RedirectResponse(f"/people/{customer_id}?saved=1", status_code=303)

    @app.post("/people/{customer_id}/archive")
    def people_archive(customer_id: Id):
        store.set_customer_archived(customer_id, True)
        return RedirectResponse(f"/people/{customer_id}", status_code=303)

    @app.post("/people/{customer_id}/restore")
    def people_restore(customer_id: Id):
        store.set_customer_archived(customer_id, False)
        return RedirectResponse(f"/people/{customer_id}", status_code=303)

    @app.get("/people/{customer_id}", response_class=HTMLResponse)
    def person(request: Request, customer_id: Id):
        customer = store.get_customer(customer_id)
        invoices = store.list_invoices(customer_id=customer_id)
        quotes = store.list_invoices(kind="quote", customer_id=customer_id)
        jobs = store.list_jobs(customer_id=customer_id)
        return render(
            request,
            "person.html",
            customer=customer,
            invoices=invoices,
            quotes=quotes,
            jobs=jobs,
        )

    @app.get("/jobs", response_class=HTMLResponse)
    def jobs(request: Request):
        return render(request, "jobs.html", jobs=store.list_jobs(), customers=store.list_customers())

    @app.post("/jobs/new")
    async def jobs_new(request: Request):
        form = await request.form()
        day = _field(form, "scheduled_for")
        start = _form_time(_field(form, "start_time"), "Start time") if day else ""
        # The job and its calendar event are saved together or not at all.
        with store.transaction():
            job = store.create_job(
                customer_id=_field(form, "customer_id") or None,
                title=_field(form, "title"),
                status="scheduled",
                notes=_field(form, "notes"),
                scheduled_for=day or None,
            )
            if day and start:
                end = _form_time(_field(form, "end_time") or start, "End time")
                store.create_event(
                    title=job["title"],
                    start_at=f"{day}T{start}",
                    end_at=f"{day}T{end}",
                    customer_id=job["customer_id"],
                    job_id=job["id"],
                    location=_field(form, "location"),
                )
        return RedirectResponse("/jobs", status_code=303)

    @app.post("/jobs/{job_id}/status")
    async def jobs_status(request: Request, job_id: Id):
        form = await request.form()
        store.set_job_status(job_id, _field(form, "status") or "scheduled")
        return RedirectResponse("/jobs", status_code=303)

    @app.post("/jobs/{job_id}/invoice")
    def jobs_invoice(job_id: Id):
        # Older pages posted here. Nothing is written: the owner gets the
        # new-invoice form filled in from the job, to check and save.
        store.get_job(job_id)
        return RedirectResponse(f"/invoices/new?job_id={job_id}", status_code=303)

    @app.get("/books", response_class=HTMLResponse)
    def books(request: Request, start: str | None = None, end: str | None = None):
        today = date.today()
        month_start, today_iso = today.replace(day=1).isoformat(), today.isoformat()
        start = _query_day(start, month_start)
        end = _query_day(end, today_iso)
        if (start, end) == (month_start, today_iso):
            period = "This month"
        elif start == end:
            period = f"On {_long_date(start)}"
        else:
            period = f"From {_long_date(start)} to {_long_date(end)}"
        return render(
            request,
            "books.html",
            period=period,
            pnl=store.profit_and_loss(start, end),
            aging=store.ar_aging(today.isoformat()),
            expenses=store.list_expenses(),
            accounts=store.list_accounts(),
            cash_cents=store.cash_cents(),
            balanced=store.journal_is_balanced(),
            start=start,
            end=end,
        )

    @app.post("/books/expenses")
    async def books_expense(request: Request):
        form = await request.form()
        store.record_expense(
            vendor_name=_field(form, "vendor_name"),
            amount_cents=parse_money(_field(form, "amount"), "Amount"),
            account_code=_field(form, "account_code") or "5000",
            spent_on=_field(form, "spent_on") or date.today().isoformat(),
            memo=_field(form, "memo"),
        )
        return RedirectResponse("/books?saved=expense", status_code=303)

    @app.post("/books/expenses/{expense_id}/reverse")
    def books_expense_reverse(expense_id: Id):
        store.reverse_expense(expense_id)
        return RedirectResponse("/books", status_code=303)

    @app.get("/calendar", response_class=HTMLResponse)
    def calendar(request: Request, day: str | None = None):
        day = _query_day(day, date.today().isoformat())
        d = date.fromisoformat(day)
        week_start = d - timedelta(days=d.weekday())
        days = [(week_start + timedelta(days=i)).isoformat() for i in range(7)]
        by_day = {x: store.events_on(x) for x in days}
        return render(
            request,
            "calendar.html",
            day=day,
            days=days,
            by_day=by_day,
            events=store.events_on(day),
            customers=store.list_customers(),
        )

    @app.post("/calendar")
    async def calendar_new(request: Request):
        form = await request.form()
        day = _field(form, "day") or date.today().isoformat()
        if _query_day(day, "") != day:
            raise Invalid("Day must be a real date like 2026-11-14")
        event = store.create_event(
            title=_field(form, "title"),
            start_at=f"{day}T{_form_time(_field(form, 'start_time') or '09:00', 'Start time')}",
            end_at=f"{day}T{_form_time(_field(form, 'end_time') or '10:00', 'End time')}",
            customer_id=_field(form, "customer_id") or None,
            location=_field(form, "location"),
            notes=_field(form, "notes"),
        )
        return RedirectResponse(f"/calendar?day={event['start_at'][:10]}", status_code=303)

    @app.get("/calendar.ics")
    def calendar_ics():
        start = date.today().isoformat()
        end = (date.today() + timedelta(days=60)).isoformat()
        body = store.ics(start, end)
        return Response(content=body, media_type="text/calendar")

    @app.get("/reviews", response_class=HTMLResponse)
    def reviews(request: Request):
        return render(request, "reviews.html", reviews=store.list_reviews())

    @app.post("/reviews")
    async def reviews_new(request: Request):
        form = await request.form()
        store.add_review(
            source=_field(form, "source") or "other",
            author=_field(form, "author") or "Anonymous",
            rating=_form_int(_field(form, "rating") or "5", "Stars"),
            body=_field(form, "body"),
            received_at=_now(),
        )
        return RedirectResponse("/reviews", status_code=303)

    @app.post("/reviews/{review_id}/draft")
    async def reviews_draft(request: Request, review_id: Id):
        form = await request.form()
        store.draft_review_reply(review_id, _field(form, "body"))
        return RedirectResponse("/reviews", status_code=303)

    @app.post("/reviews/{review_id}/replied")
    def reviews_replied(review_id: Id):
        store.mark_review_replied(review_id)
        return RedirectResponse("/reviews", status_code=303)

    @app.get("/settings", response_class=HTMLResponse)
    def settings(request: Request):
        company = store.get_company()
        currencies = dict(CURRENCY_NAMES)
        if company["currency"] not in currencies:
            currencies[company["currency"]] = company["currency"]  # set through the API; keep it
        here = store.path.resolve()
        return render(
            request,
            "settings.html",
            currencies=currencies,
            currency_locked=store.currency_locked(),
            currency_locked_text=CURRENCY_LOCKED,
            db_path=str(store.path),
            real_db=str(real_db_path()),
            real_url=_real_url(),
            is_real_file=here == real_db_path(),
        )

    @app.post("/settings")
    async def settings_save(request: Request):
        form = await request.form()
        # Only the fields the form sent change; the rest keep their value.
        fields = ("name", "email", "phone", "address", "tax_id", "currency", "payment_instructions")
        changes = {k: _field(form, k) for k in fields if k in form}
        if "default_tax_pct" in form:
            changes["default_tax_bps"] = _form_tax(form.get("default_tax_pct"), None, "Default tax")
        store.save_company(**changes)
        return RedirectResponse("/settings?saved=1", status_code=303)

    @app.post("/pipeline")
    async def pipeline_new(request: Request):
        form = await request.form()
        store.create_deal(
            title=_field(form, "title"),
            stage=_field(form, "stage") or "lead",
            customer_id=_field(form, "customer_id") or None,
            value_cents=parse_money(_field(form, "value") or "0", "Value"),
        )
        return RedirectResponse("/people", status_code=303)

    # --- JSON API, for scripts and (optionally) an AI assistant ---------------

    @app.get("/api/health")
    def api_health():
        return {
            "ok": True,
            "balanced": store.journal_is_balanced(),
            "company": store.get_company()["name"],
            "cash_cents": store.cash_cents(),
        }

    @app.get("/api/today")
    def api_today():
        return store.today_brief()

    @app.get("/api/brief", response_class=PlainTextResponse)
    def api_brief():
        return store.brief_text()

    @app.get("/api/invoices")
    def api_invoices(kind: str = "invoice", status: str | None = None):
        return store.list_invoices(kind=kind, status=status)

    @app.get("/api/invoices/{invoice_id}")
    def api_invoice(invoice_id: Id):
        return store.get_invoice(invoice_id)

    @app.post("/api/invoices")
    def api_invoice_create(payload: dict):
        # One transaction: if any line fails, no invoice and no number is used.
        return store.create_document(
            "quote" if payload.get("kind") == "quote" else "invoice",
            customer_id=payload.get("customer_id"),
            issue_date=payload.get("issue_date") or date.today().isoformat(),
            due_date=payload.get("due_date"),
            notes=payload.get("notes") or "",
            job_id=payload.get("job_id"),
            lines=_lines_from_json(payload.get("lines")),
            currency=payload.get("currency"),  # optional; if sent, it must be the book's
        )

    @app.post("/api/invoices/{invoice_id}/edit")
    def api_invoice_edit(invoice_id: Id, payload: dict):
        """Change a draft; only the fields sent change (the same names as
        create). An omitted field keeps its value, and null or "" clears
        due_date, notes or job_id (customer_id and issue_date can't be
        cleared). "lines", when sent, replaces every line; when omitted, the
        lines stay exactly as they are."""
        with store.transaction():  # read and write together: no edit in between
            current = store.get_invoice(invoice_id)
            lines = _lines_from_json(payload["lines"]) if "lines" in payload else None
            given = lambda key: payload[key] if key in payload else current[key]
            return store.update_document(
                invoice_id,
                customer_id=given("customer_id"),
                issue_date=given("issue_date"),
                due_date=given("due_date"),
                notes=given("notes") or "",
                job_id=given("job_id"),
                lines=lines,
            )

    @app.post("/api/invoices/{invoice_id}/delete")
    def api_invoice_delete(invoice_id: Id):
        return store.delete_draft(invoice_id)

    @app.post("/api/payments/{payment_id}/reverse")
    def api_payment_reverse(payment_id: Id):
        return store.reverse_payment(payment_id)

    @app.get("/api/invoices/{invoice_id}/chase")
    def api_invoice_chase(invoice_id: Id):
        return store.chase_text(invoice_id)

    @app.post("/api/invoices/{invoice_id}/send")
    def api_invoice_send(invoice_id: Id):
        return store.send_invoice(invoice_id)

    @app.post("/api/invoices/{invoice_id}/pay")
    def api_invoice_pay(invoice_id: Id, payload: dict):
        amount = payload.get("amount_cents")
        if amount is None:
            amount = parse_money(_num_text(payload.get("amount"), "amount"), "amount")
        return store.record_payment(
            invoice_id,
            amount,
            paid_at=payload.get("paid_at") or date.today().isoformat(),
            method=payload.get("method") or "bank",
            notes=payload.get("notes") or "",
        )

    @app.get("/api/customers")
    def api_customers(include_archived: bool = False):
        return store.list_customers(include_archived=include_archived)

    @app.post("/api/customers")
    def api_customer_create(payload: dict):
        cid = store.create_customer(
            name=payload.get("name"),
            email=payload.get("email"),
            phone=payload.get("phone"),
            address=payload.get("address"),
            notes=payload.get("notes"),
        )
        return store.get_customer(cid)

    @app.post("/api/customers/{customer_id}/edit")
    def api_customer_edit(customer_id: Id, payload: dict):
        fields = ("name", "email", "phone", "address", "notes")
        return store.update_customer(customer_id, **{k: payload[k] for k in fields if k in payload})

    @app.post("/api/customers/{customer_id}/archive")
    def api_customer_archive(customer_id: Id):
        return store.set_customer_archived(customer_id, True)

    @app.post("/api/customers/{customer_id}/restore")
    def api_customer_restore(customer_id: Id):
        return store.set_customer_archived(customer_id, False)

    @app.get("/api/books/pnl")
    def api_pnl(start: str | None = None, end: str | None = None):
        today = date.today()
        return store.profit_and_loss(start or today.replace(day=1).isoformat(), end or today.isoformat())

    @app.get("/api/books/aging")
    def api_aging():
        return store.ar_aging(date.today().isoformat())

    @app.post("/api/expenses")
    def api_expense(payload: dict):
        amount = payload.get("amount_cents")
        if amount is None and payload.get("amount") is not None:
            amount = parse_money(_num_text(payload.get("amount"), "amount"), "amount")
        return store.record_expense(
            vendor_name=payload.get("vendor_name"),
            amount_cents=amount,
            account_code=payload.get("account_code") or "5000",
            spent_on=payload.get("spent_on") or date.today().isoformat(),
            memo=payload.get("memo") or "",
        )

    @app.post("/api/expenses/{expense_id}/reverse")
    def api_expense_reverse(expense_id: Id):
        return store.reverse_expense(expense_id)

    @app.get("/api/calendar")
    def api_calendar(day: str | None = None):
        return store.events_on(day or date.today().isoformat())

    @app.post("/api/calendar")
    def api_calendar_new(payload: dict):
        return store.create_event(
            title=payload.get("title"),
            start_at=payload.get("start_at"),
            end_at=payload.get("end_at"),
            customer_id=payload.get("customer_id"),
            job_id=payload.get("job_id"),
            location=payload.get("location") or "",
            notes=payload.get("notes") or "",
        )

    @app.get("/api/reviews")
    def api_reviews(status: str | None = None):
        return store.list_reviews(status=status)

    @app.post("/api/reviews/{review_id}/draft")
    def api_review_draft(review_id: Id, payload: dict):
        return store.draft_review_reply(review_id, payload.get("body"))

    @app.post("/api/reviews/{review_id}/replied")
    def api_review_replied(review_id: Id):
        return store.mark_review_replied(review_id)

    @app.get("/api/jobs")
    def api_jobs():
        return store.list_jobs()

    @app.post("/api/jobs")
    def api_job_create(payload: dict):
        return store.create_job(
            customer_id=payload.get("customer_id"),
            title=payload.get("title"),
            status=payload.get("status") or "scheduled",
            notes=payload.get("notes") or "",
            scheduled_for=payload.get("scheduled_for"),
        )

    @app.get("/api/recurring")
    def api_recurring():
        return store.list_recurring()

    @app.post("/api/recurring")
    def api_recurring_create(payload: dict):
        lines = payload.get("lines")
        if lines is None:
            lines = [{**payload, "description": payload.get("description") or "Retainer"}]
        return store.create_recurring(
            customer_id=payload.get("customer_id"),
            cadence=payload.get("cadence") or "monthly",
            next_date=payload.get("next_date") or date.today().isoformat(),
            lines=_lines_from_json(lines),
            notes=payload.get("notes") or "",
        )

    @app.post("/api/tick")
    async def api_tick(request: Request):
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        return store.tick_recurring(as_of=payload.get("as_of"))

    return app


def _split_host(netloc: str) -> tuple[str, int | None] | None:
    """'127.0.0.1:7740' -> ('127.0.0.1', 7740); '[::1]' -> ('[::1]', None); junk -> None."""
    netloc = netloc.strip().lower()
    if not netloc:
        return None
    if netloc.startswith("["):
        end = netloc.find("]")
        if end < 0:
            return None
        host, rest = netloc[: end + 1], netloc[end + 1 :]
        if rest and not rest.startswith(":"):
            return None
        port = rest[1:] if rest else ""
    else:
        host, _, port = netloc.partition(":")
        if ":" in port:
            return None
    if port == "":
        return host, None
    if not port.isdigit():
        return None
    return host, int(port)


def _refuse(request: Request, hosts: set[str]) -> Response | None:
    """The response that refuses this request, or None to let it through.

    1. Host must be one of `hosts` (any port), or the request is DNS rebinding.
    2. A write carrying browser cross-site signals is refused: an Origin that
       isn't exactly this server, or a Sec-Fetch-Site other than same-origin
       or none. Requests with neither header (curl, scripts) pass.
    3. A write to /api with a body must declare it as JSON, so an HTML form
       (urlencoded, multipart or text/plain) can never reach the API.
    """
    host = _split_host(request.headers.get("host", ""))
    if host is None or host[0] not in hosts:
        return PlainTextResponse("Unknown host", status_code=400)
    if request.method not in UNSAFE_METHODS:
        return None
    origin = request.headers.get("origin")
    if origin is not None:
        parts = urlsplit(origin)
        theirs = _split_host(parts.netloc) if parts.scheme == "http" else None
        if theirs is None or (theirs[0], theirs[1] or 80) != (host[0], host[1] or 80):
            return PlainTextResponse("Cross-site request refused", status_code=403)
    fetch_site = request.headers.get("sec-fetch-site")
    if fetch_site is not None and fetch_site.lower() not in ("same-origin", "none"):
        return PlainTextResponse("Cross-site request refused", status_code=403)
    if request.url.path.startswith("/api/") and _has_body(request):
        media = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if media != "application/json":
            return PlainTextResponse("Send the body as application/json", status_code=415)
    return None


def _has_body(request: Request) -> bool:
    length = request.headers.get("content-length")
    if length is not None:
        return length.strip() not in ("", "0")
    return "transfer-encoding" in request.headers


def _save_new_invoice(store: Store, form, kind: str) -> dict:
    """Parse the whole form first, then save the document and its lines in
    one transaction, so a bad line leaves no draft and uses no number."""
    lines = _lines_from_form(form)
    if not lines:
        raise Invalid("Add at least one line with a description")
    return store.create_document(
        kind,
        customer_id=_field(form, "customer_id") or None,
        issue_date=_field(form, "issue_date") or date.today().isoformat(),
        due_date=_field(form, "due_date") or None,
        notes=_field(form, "notes"),
        job_id=_field(form, "job_id") or None,
        lines=lines,
    )


def _field(form, key: str) -> str:
    """A form field as trimmed text; missing (or a file upload) is ''."""
    value = form.get(key)
    return value.strip() if isinstance(value, str) else ""


def _as_id(value) -> int | None:
    """A record id typed as digits, if it fits a SQLite integer; else None."""
    text = str(value if value is not None else "").strip()
    if text.isascii() and text.isdigit() and len(text) <= 19 and int(text) <= MAX_ID:
        return int(text)
    return None


def _nth(values: list, i: int) -> str | None:
    if i >= len(values) or not isinstance(values[i], str):
        return None
    return values[i].strip()


def _form_int(text: str, label: str) -> int:
    s = text.strip()
    digits = s[1:] if s.startswith("-") else s
    if digits.isascii() and digits.isdigit() and len(digits) <= 18:
        return int(s)
    raise Invalid(f"{label} must be a whole number")


def _form_time(text: str, label: str) -> str:
    if re.fullmatch(r"([01][0-9]|2[0-3]):[0-5][0-9](:[0-5][0-9])?", text):
        return text
    raise Invalid(f"{label} must be a time like 09:30")


def _form_tax(pct, bps, label: str) -> int:
    """Tax in basis points from the new percent field (tax_pct, '8.25') or,
    for old clients, the basis-point field (tax_bps, '825')."""
    if isinstance(pct, str):
        return parse_percent(pct, label) if pct.strip() else 0
    if isinstance(bps, str):
        s = bps.strip()
        if not s:
            return 0
        if s.isascii() and s.isdigit() and int(s) <= 10_000:
            return int(s)
        raise Invalid(f"{label} must be a percent like 8.25")
    return 0


def _lines_from_form(form) -> list[dict]:
    descriptions = form.getlist("description")
    qtys, units = form.getlist("qty"), form.getlist("unit")
    pcts, bpss = form.getlist("tax_pct"), form.getlist("tax_bps")
    lines = []
    for i in range(len(descriptions)):
        desc = _nth(descriptions, i)
        if not desc:
            continue  # a blank row the user didn't fill in
        n = i + 1
        lines.append(
            {
                "description": desc,
                "qty_milli": parse_qty(_nth(qtys, i) or "1", f"Line {n} quantity"),
                "unit_cents": parse_money(_nth(units, i) or "0", f"Line {n} rate"),
                "tax_bps": _form_tax(_nth(pcts, i), _nth(bpss, i), f"Line {n} tax"),
            }
        )
    return lines


def _typed_lines(form) -> list[dict]:
    """The line rows exactly as typed, to put back into a form that failed."""
    descriptions = form.getlist("description")
    qtys, units = form.getlist("qty"), form.getlist("unit")
    pcts, bpss = form.getlist("tax_pct"), form.getlist("tax_bps")
    rows = []
    for i in range(len(descriptions)):
        tax = _nth(pcts, i)
        if tax is None:
            bps = _nth(bpss, i) or "0"
            tax = percent_label(int(bps)) if bps.isascii() and bps.isdigit() and int(bps) <= 10_000 else bps
        rows.append(
            {
                "description": _nth(descriptions, i) or "",
                "qty": _nth(qtys, i) or "",
                "unit": _nth(units, i) or "",
                "tax_pct": tax,
            }
        )
    return rows


def _trim_blank_rows(rows: list[dict]) -> list[dict]:
    while rows and not rows[-1]["description"]:
        rows = rows[:-1]
    return rows


def _num_text(value, label: str) -> str:
    """A JSON number or numeric string as text for the strict parsers."""
    if isinstance(value, bool) or value is None:
        raise Invalid(f"{label} must be a number")
    if isinstance(value, (int, float, str)):
        return str(value)
    raise Invalid(f"{label} must be a number")


def _lines_from_json(lines):
    """Convert API line shorthands (qty, unit, tax_pct) to the store's fields.
    Anything that isn't a list of objects goes through unchanged so the store
    refuses it with its own message."""
    if not isinstance(lines, list):
        return [] if lines is None else lines
    return [_line_from_json(line, n) for n, line in enumerate(lines, start=1)]


def _line_from_json(line, n: int):
    if not isinstance(line, dict):
        return line
    out = {"description": line.get("description")}
    if line.get("qty_milli") is not None:
        out["qty_milli"] = line.get("qty_milli")
    elif line.get("qty") is not None:
        out["qty_milli"] = parse_qty(_num_text(line.get("qty"), f"Line {n} qty"), f"Line {n} qty")
    else:
        out["qty_milli"] = 1000
    if line.get("unit_cents") is not None:
        out["unit_cents"] = line.get("unit_cents")
    elif line.get("unit") is not None:
        out["unit_cents"] = parse_money(_num_text(line.get("unit"), f"Line {n} unit"), f"Line {n} unit")
    else:
        out["unit_cents"] = 0
    if line.get("tax_pct") is not None:
        out["tax_bps"] = parse_percent(_num_text(line.get("tax_pct"), f"Line {n} tax_pct"), f"Line {n} tax")
    else:
        out["tax_bps"] = line.get("tax_bps")
    return out


def _query_day(value: str | None, default: str) -> str:
    """A ?day= / ?start= value if it is a real date, else the default."""
    if value and re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        try:
            return date.fromisoformat(value).isoformat()
        except ValueError:
            pass
    return default


def _message(exc: ValueError) -> str:
    return str(exc) if isinstance(exc, Invalid) and str(exc) else GENERIC_ERROR


def _validation_message(exc: RequestValidationError) -> str:
    for err in exc.errors():
        loc = tuple(err.get("loc", ()))
        if loc[:1] == ("body",):
            return "Send the body as a JSON object"
        if loc:
            return f"{loc[-1]} isn't valid"
    return GENERIC_ERROR


def _is_api(request: Request) -> bool:
    return request.url.path == "/api" or request.url.path.startswith("/api/")


# Where a failed form post goes back to, so the error shows on the page the
# owner was looking at. (The Referer header is off: Referrer-Policy no-referrer.)
_BACK = [
    (re.compile(r"/invoices/([0-9]+)(/.*)?"), r"/invoices/\1"),
    (re.compile(r"/quotes/([0-9]+)(/.*)?"), r"/invoices/\1"),
    (re.compile(r"/people/([0-9]+)(/.*)?"), r"/people/\1"),
    (re.compile(r"/books/expenses/([0-9]+)(/.*)?"), "/books"),
    (re.compile(r"/invoices(/.*)?|/recurring(/.*)?|/tick"), "/invoices"),
    (re.compile(r"/quotes(/.*)?"), "/invoices?kind=quote"),
    (re.compile(r"/people(/.*)?|/pipeline(/.*)?"), "/people"),
    (re.compile(r"/jobs(/.*)?"), "/jobs"),
    (re.compile(r"/books(/.*)?"), "/books"),
    (re.compile(r"/calendar(/.*)?"), "/calendar"),
    (re.compile(r"/reviews(/.*)?"), "/reviews"),
    (re.compile(r"/settings(/.*)?"), "/settings"),
]


def _back(path: str) -> str:
    for pattern, target in _BACK:
        m = pattern.fullmatch(path)
        if m:
            return m.expand(target)
    return "/"


def _with_error(url: str, message: str) -> str:
    return url + ("&" if "?" in url else "?") + urlencode({"error": message})


def _nav(path: str) -> str:
    for name, prefix in (
        ("invoices", "/invoices"),
        ("people", "/people"),
        ("jobs", "/jobs"),
        ("books", "/books"),
        ("calendar", "/calendar"),
        ("reviews", "/reviews"),
        ("settings", "/settings"),
    ):
        if path == prefix or path.startswith(prefix + "/"):
            return name
    return "today"


def _now() -> str:
    return datetime.now().replace(microsecond=0).isoformat()


def _long_date(value) -> str:
    """'2026-11-14' -> '14 November 2026'; anything unreadable comes back as it was."""
    try:
        d = date.fromisoformat(str(value)[:10])
    except ValueError:
        return str(value)
    return f"{d.day} {_calendar.month_name[d.month]} {d.year}"


def _weekday(value) -> str:
    try:
        return _calendar.day_name[date.fromisoformat(str(value)[:10]).weekday()]
    except ValueError:
        return ""
