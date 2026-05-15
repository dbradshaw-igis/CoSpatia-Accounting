import os
import secrets
from datetime import date, timedelta
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from . import accounts as acct
from . import (accountant_export, ap, ar, auth, banking, charts, db, export,
               ledger, reports, taxconfig)

BASE = Path(__file__).resolve().parent

# Login is turned off for now. Flip this to True to require a sign-in again —
# the user accounts, sessions, login screen, and My Account page are all still
# in place behind it, nothing was removed.
AUTH_ENABLED = False

app = FastAPI(title="CoSpatia Accounting Module")
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")

templates = Jinja2Templates(directory=BASE / "templates")
templates.env.filters["money"] = ledger.fmt
templates.env.globals["type_labels"] = acct.TYPE_LABELS
templates.env.globals["type_order"] = acct.TYPE_ORDER
templates.env.globals["auth_enabled"] = AUTH_ENABLED


@app.on_event("startup")
def startup():
    conn = db.get_conn()
    db.init_db(conn)
    conn.close()


def render(name, request, **ctx):
    conn = ctx.get("_conn")
    company = ledger.current_company(conn) if conn is not None else None
    return templates.TemplateResponse(name, {
        "request": request,
        "company": company,
        "current_user": request.session.get("display_name")
                        or request.session.get("username"),
        "current_role": request.session.get("role"),
        **ctx})


# --- authentication ---------------------------------------------------------

def _session_secret():
    """A stable signing key for session cookies. Read from the environment in
    production; otherwise generated once and kept in a gitignored file so
    sessions survive a restart."""
    from_env = os.environ.get("COSPATIA_SESSION_SECRET")
    if from_env:
        return from_env
    path = BASE.parent / ".session_secret"
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    secret = secrets.token_hex(32)
    path.write_text(secret, encoding="utf-8")
    return secret


PUBLIC_PATHS = ("/login", "/register")


@app.middleware("http")
async def require_login(request: Request, call_next):
    """Every page needs a signed-in user. The login and register pages and the
    static files are the only things reachable without a session. The session's
    user is re-checked against the database, so a deleted or disabled account
    cannot keep working on a stale cookie."""
    if not AUTH_ENABLED:
        return await call_next(request)
    path = request.url.path
    if path.startswith("/static") or path in PUBLIC_PATHS:
        return await call_next(request)
    user_id = request.session.get("user_id")
    if user_id:
        conn = db.get_conn()
        try:
            user = auth.get_user_by_id(conn, user_id)
        finally:
            conn.close()
        if user is not None and user["active"]:
            return await call_next(request)
        request.session.clear()
    return RedirectResponse("/login", status_code=303)


# Added last so it wraps require_login — the session is decoded before the
# login check reads it.
app.add_middleware(SessionMiddleware, secret_key=_session_secret(),
                   max_age=43200, same_site="lax", https_only=False)


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    conn = db.get_conn()
    try:
        company = ledger.current_company(conn)
        if company is None:
            return RedirectResponse("/setup", status_code=303)
        as_of = date.today().isoformat()
        fy_start, _ = reports.fiscal_year_bounds(company, as_of)
        return render("dashboard.html", request, _conn=conn,
                      tb=reports.trial_balance(conn, company["id"], as_of),
                      bs=reports.balance_sheet(conn, company["id"], as_of),
                      pnl=reports.profit_and_loss(
                          conn, company["id"], fy_start, as_of),
                      fy_start=fy_start, as_of=as_of,
                      entity_labels=acct.ENTITY_LABELS)
    finally:
        conn.close()


@app.get("/setup", response_class=HTMLResponse)
def setup_page(request: Request, error: str = ""):
    conn = db.get_conn()
    try:
        return render("setup.html", request, _conn=conn, error=error,
                      entity_labels=acct.ENTITY_LABELS)
    finally:
        conn.close()


@app.post("/setup")
async def setup_submit(request: Request):
    form = await request.form()
    conn = db.get_conn()
    try:
        try:
            ledger.create_company(
                conn,
                legal_name=form.get("legal_name", ""),
                entity_type=form.get("entity_type", ""),
                dba=form.get("dba", ""),
                ein=form.get("ein", ""),
                tax_year_type=form.get("tax_year_type", "calendar"),
                fiscal_year_end_month=form.get("fiscal_year_end_month", 12),
                accounting_basis=form.get("accounting_basis", "accrual"),
                state=form.get("state", ""),
            )
        except (ledger.PostingError, ValueError) as exc:
            return RedirectResponse(f"/setup?error={exc}", status_code=303)
        return RedirectResponse("/", status_code=303)
    finally:
        conn.close()


def _require_company(conn):
    company = ledger.current_company(conn)
    if company is None:
        return None
    return company


@app.get("/accounts", response_class=HTMLResponse)
def accounts_page(request: Request):
    conn = db.get_conn()
    try:
        company = _require_company(conn)
        if company is None:
            return RedirectResponse("/setup", status_code=303)
        rows = ledger.list_accounts(conn, company["id"])
        grouped = {t: [r for r in rows if r["type"] == t]
                   for t in acct.TYPE_ORDER}
        return render("accounts.html", request, _conn=conn, grouped=grouped)
    finally:
        conn.close()


@app.get("/journal", response_class=HTMLResponse)
def journal_page(request: Request, error: str = "", posted: str = ""):
    conn = db.get_conn()
    try:
        company = _require_company(conn)
        if company is None:
            return RedirectResponse("/setup", status_code=303)
        return render("journal.html", request, _conn=conn,
                      account_list=ledger.list_accounts(conn, company["id"]),
                      recent=ledger.recent_entries(conn, company["id"]),
                      today=date.today().isoformat(), rows=range(8),
                      entry_types=["manual", "invoice", "payment", "bill",
                                   "check", "deposit", "payroll",
                                   "depreciation", "adjusting", "opening"],
                      error=error, posted=posted)
    finally:
        conn.close()


@app.post("/journal")
async def post_journal(request: Request):
    form = await request.form()
    conn = db.get_conn()
    try:
        company = _require_company(conn)
        if company is None:
            return RedirectResponse("/setup", status_code=303)
        lines = []
        for i in range(8):
            account = form.get(f"account_{i}")
            if not account:
                continue
            lines.append((int(account),
                          ledger.to_cents(form.get(f"debit_{i}")),
                          ledger.to_cents(form.get(f"credit_{i}"))))
        try:
            ledger.post_entry(
                conn, company["id"],
                form.get("entry_date") or date.today().isoformat(), lines,
                memo=form.get("memo", ""),
                reference=form.get("reference", ""),
                entry_type=form.get("entry_type", "manual"),
            )
        except ledger.PostingError as exc:
            return RedirectResponse(f"/journal?error={exc}", status_code=303)
        return RedirectResponse("/journal?posted=1", status_code=303)
    finally:
        conn.close()


@app.post("/journal/{entry_id}/void")
def void_journal(entry_id: int):
    conn = db.get_conn()
    try:
        try:
            ledger.void_entry(conn, entry_id)
        except ledger.PostingError as exc:
            return RedirectResponse(f"/journal?error={exc}", status_code=303)
        return RedirectResponse("/journal?posted=1", status_code=303)
    finally:
        conn.close()


@app.get("/trial-balance", response_class=HTMLResponse)
def trial_balance_page(request: Request, as_of: str = ""):
    as_of = as_of or date.today().isoformat()
    conn = db.get_conn()
    try:
        company = _require_company(conn)
        if company is None:
            return RedirectResponse("/setup", status_code=303)
        return render("trial_balance.html", request, _conn=conn, as_of=as_of,
                      tb=reports.trial_balance(conn, company["id"], as_of))
    finally:
        conn.close()


@app.get("/trial-balance.csv")
def trial_balance_download():
    conn = db.get_conn()
    as_of = date.today().isoformat()
    try:
        company = _require_company(conn)
        if company is None:
            return RedirectResponse("/setup", status_code=303)
        try:
            body = export.trial_balance_csv(conn, company, as_of)
        except ledger.PostingError as exc:
            return Response(content=str(exc), status_code=409,
                            media_type="text/plain")
    finally:
        conn.close()
    return Response(content=body, media_type="text/csv", headers={
        "Content-Disposition":
            f'attachment; filename="trial-balance-{as_of}.csv"'})


@app.get("/pnl", response_class=HTMLResponse)
def pnl_page(request: Request, start: str = "", end: str = ""):
    conn = db.get_conn()
    try:
        company = _require_company(conn)
        if company is None:
            return RedirectResponse("/setup", status_code=303)
        end = end or date.today().isoformat()
        if not start:
            start, _ = reports.fiscal_year_bounds(company, end)
        return render("pnl.html", request, _conn=conn, start=start, end=end,
                      pnl=reports.profit_and_loss(
                          conn, company["id"], start, end))
    finally:
        conn.close()


@app.get("/balance-sheet", response_class=HTMLResponse)
def balance_sheet_page(request: Request, as_of: str = ""):
    as_of = as_of or date.today().isoformat()
    conn = db.get_conn()
    try:
        company = _require_company(conn)
        if company is None:
            return RedirectResponse("/setup", status_code=303)
        return render("balance_sheet.html", request, _conn=conn, as_of=as_of,
                      bs=reports.balance_sheet(conn, company["id"], as_of))
    finally:
        conn.close()


@app.get("/account/{account_id}", response_class=HTMLResponse)
def account_ledger_page(request: Request, account_id: int,
                        start: str = "", end: str = ""):
    conn = db.get_conn()
    try:
        company = _require_company(conn)
        if company is None:
            return RedirectResponse("/setup", status_code=303)
        result = reports.account_ledger(
            conn, account_id, start or None, end or None)
        return render("account_ledger.html", request, _conn=conn,
                      result=result, start=start, end=end)
    finally:
        conn.close()


# --- customers -------------------------------------------------------------

@app.get("/customers", response_class=HTMLResponse)
def customers_page(request: Request, error: str = "", saved: str = ""):
    conn = db.get_conn()
    try:
        company = _require_company(conn)
        if company is None:
            return RedirectResponse("/setup", status_code=303)
        return render("customers.html", request, _conn=conn,
                      customers=ar.list_customers(conn, company["id"]),
                      error=error, saved=saved)
    finally:
        conn.close()


@app.post("/customers")
async def create_customer(request: Request):
    form = await request.form()
    conn = db.get_conn()
    try:
        company = _require_company(conn)
        if company is None:
            return RedirectResponse("/setup", status_code=303)
        try:
            ar.create_customer(
                conn, company["id"], form.get("name", ""),
                email=form.get("email", ""),
                billing_address=form.get("billing_address", ""),
                terms_days=form.get("terms_days") or 30)
        except (ledger.PostingError, ValueError) as exc:
            return RedirectResponse(f"/customers?error={exc}", status_code=303)
        return RedirectResponse("/customers?saved=1", status_code=303)
    finally:
        conn.close()


# --- invoices --------------------------------------------------------------

@app.get("/invoices", response_class=HTMLResponse)
def invoices_page(request: Request):
    conn = db.get_conn()
    try:
        company = _require_company(conn)
        if company is None:
            return RedirectResponse("/setup", status_code=303)
        return render("invoices.html", request, _conn=conn,
                      invoices=ar.list_invoices(conn, company["id"]),
                      today=date.today().isoformat())
    finally:
        conn.close()


@app.get("/invoices/new", response_class=HTMLResponse)
def invoice_new_page(request: Request, error: str = ""):
    conn = db.get_conn()
    try:
        company = _require_company(conn)
        if company is None:
            return RedirectResponse("/setup", status_code=303)
        return render("invoice_new.html", request, _conn=conn,
                      customers=ar.list_customers(conn, company["id"]),
                      income=ar.income_accounts(conn, company["id"]),
                      number=ar.next_invoice_number(
                          conn, company["id"], date.today().strftime("%Y-%m")),
                      today=date.today().isoformat(), rows=range(6),
                      error=error)
    finally:
        conn.close()


@app.post("/invoices")
async def create_invoice(request: Request):
    form = await request.form()
    conn = db.get_conn()
    try:
        company = _require_company(conn)
        if company is None:
            return RedirectResponse("/setup", status_code=303)
        invoice_date = form.get("invoice_date") or date.today().isoformat()
        due_date = form.get("due_date") or ""
        customer_id = form.get("customer_id")
        if not customer_id:
            return RedirectResponse(
                "/invoices/new?error=Choose a customer.", status_code=303)
        if not due_date:
            customer = ar.get_customer(conn, int(customer_id))
            terms = customer["terms_days"] if customer else 30
            due_date = (date.fromisoformat(invoice_date)
                        + timedelta(days=terms)).isoformat()
        lines = []
        for i in range(6):
            account = form.get(f"account_{i}")
            if not account:
                continue
            lines.append((form.get(f"description_{i}", ""), int(account),
                          ledger.to_cents(form.get(f"amount_{i}"))))
        number = ar.next_invoice_number(
            conn, company["id"], invoice_date[:7])
        try:
            invoice_id = ar.create_invoice(
                conn, company["id"], int(customer_id), invoice_date, due_date,
                number, lines, memo=form.get("memo", ""))
        except ledger.PostingError as exc:
            return RedirectResponse(f"/invoices/new?error={exc}",
                                    status_code=303)
        return RedirectResponse(f"/invoices/{invoice_id}", status_code=303)
    finally:
        conn.close()


@app.get("/invoices/{invoice_id}", response_class=HTMLResponse)
def invoice_view(request: Request, invoice_id: int, error: str = ""):
    conn = db.get_conn()
    try:
        company = _require_company(conn)
        if company is None:
            return RedirectResponse("/setup", status_code=303)
        detail = ar.get_invoice(conn, invoice_id)
        if detail is None:
            return RedirectResponse("/invoices", status_code=303)
        editable = (detail["paid"] == 0
                    and detail["invoice"]["status"] != "void")
        return render("invoice_view.html", request, _conn=conn, d=detail,
                      editable=editable, error=error)
    finally:
        conn.close()


@app.get("/invoices/{invoice_id}/edit", response_class=HTMLResponse)
def invoice_edit_page(request: Request, invoice_id: int, error: str = ""):
    conn = db.get_conn()
    try:
        company = _require_company(conn)
        if company is None:
            return RedirectResponse("/setup", status_code=303)
        detail = ar.get_invoice(conn, invoice_id)
        if detail is None:
            return RedirectResponse("/invoices", status_code=303)
        if detail["paid"] != 0 or detail["invoice"]["status"] == "void":
            return RedirectResponse(
                f"/invoices/{invoice_id}?error=This invoice can no longer be "
                "edited.", status_code=303)
        row_count = max(6, len(detail["lines"]) + 2)
        return render("invoice_edit.html", request, _conn=conn, d=detail,
                      customers=ar.list_customers(conn, company["id"]),
                      income=ar.income_accounts(conn, company["id"]),
                      rows=range(row_count), error=error)
    finally:
        conn.close()


@app.post("/invoices/{invoice_id}/edit")
async def invoice_edit_submit(request: Request, invoice_id: int):
    form = await request.form()
    conn = db.get_conn()
    try:
        company = _require_company(conn)
        if company is None:
            return RedirectResponse("/setup", status_code=303)
        invoice_date = form.get("invoice_date") or date.today().isoformat()
        due_date = form.get("due_date") or ""
        customer_id = form.get("customer_id")
        if not customer_id:
            return RedirectResponse(
                f"/invoices/{invoice_id}/edit?error=Choose a customer.",
                status_code=303)
        if not due_date:
            customer = ar.get_customer(conn, int(customer_id))
            terms = customer["terms_days"] if customer else 30
            due_date = (date.fromisoformat(invoice_date)
                        + timedelta(days=terms)).isoformat()
        lines = []
        for i in range(20):
            account = form.get(f"account_{i}")
            if not account:
                continue
            lines.append((form.get(f"description_{i}", ""), int(account),
                          ledger.to_cents(form.get(f"amount_{i}"))))
        existing = ar.get_invoice(conn, invoice_id)
        if existing is None:
            return RedirectResponse("/invoices", status_code=303)
        try:
            ar.update_invoice(
                conn, invoice_id, int(customer_id), invoice_date, due_date,
                existing["invoice"]["invoice_number"], lines,
                memo=form.get("memo", ""))
        except ledger.PostingError as exc:
            return RedirectResponse(
                f"/invoices/{invoice_id}/edit?error={exc}", status_code=303)
        return RedirectResponse(f"/invoices/{invoice_id}", status_code=303)
    finally:
        conn.close()


# --- payments --------------------------------------------------------------

@app.get("/payments/new", response_class=HTMLResponse)
def payment_new_page(request: Request, customer_id: str = "", error: str = ""):
    conn = db.get_conn()
    try:
        company = _require_company(conn)
        if company is None:
            return RedirectResponse("/setup", status_code=303)
        customer = invoices = None
        if customer_id:
            customer = ar.get_customer(conn, int(customer_id))
            invoices = ar.open_invoices(conn, int(customer_id))
        return render("payment_new.html", request, _conn=conn,
                      customers=ar.list_customers(conn, company["id"]),
                      banks=ar.bank_accounts(conn, company["id"]),
                      customer=customer, open_invoices=invoices,
                      today=date.today().isoformat(), error=error)
    finally:
        conn.close()


@app.post("/payments")
async def create_payment(request: Request):
    form = await request.form()
    conn = db.get_conn()
    try:
        company = _require_company(conn)
        if company is None:
            return RedirectResponse("/setup", status_code=303)
        customer_id = form.get("customer_id")
        deposit_account_id = form.get("deposit_account_id")
        if not customer_id or not deposit_account_id:
            return RedirectResponse(
                "/payments/new?error=Choose a customer and a deposit account.",
                status_code=303)
        applications = []
        for invoice in ar.open_invoices(conn, int(customer_id)):
            value = form.get(f"apply_{invoice['invoice']['id']}")
            applications.append((invoice["invoice"]["id"],
                                 ledger.to_cents(value)))
        try:
            ar.receive_payment(
                conn, company["id"], int(customer_id),
                form.get("payment_date") or date.today().isoformat(),
                int(deposit_account_id), applications,
                reference=form.get("reference", ""),
                memo=form.get("memo", ""))
        except ledger.PostingError as exc:
            return RedirectResponse(
                f"/payments/new?customer_id={customer_id}&error={exc}",
                status_code=303)
        return RedirectResponse(f"/payments/new?customer_id={customer_id}"
                                "&error=Payment recorded.", status_code=303)
    finally:
        conn.close()


# --- bank import -----------------------------------------------------------

@app.get("/banking", response_class=HTMLResponse)
def banking_page(request: Request, error: str = "", note: str = ""):
    conn = db.get_conn()
    try:
        company = _require_company(conn)
        if company is None:
            return RedirectResponse("/setup", status_code=303)
        return render("bank_import.html", request, _conn=conn,
                      banks=ar.bank_accounts(conn, company["id"]),
                      unreviewed=banking.list_unreviewed(conn, company["id"]),
                      posted=banking.recent_posted(conn, company["id"]),
                      all_accounts=ledger.list_accounts(conn, company["id"]),
                      error=error, note=note)
    finally:
        conn.close()


@app.post("/banking/upload")
async def banking_upload(request: Request):
    form = await request.form()
    conn = db.get_conn()
    try:
        company = _require_company(conn)
        if company is None:
            return RedirectResponse("/setup", status_code=303)
        upload = form.get("file")
        if upload is None or not getattr(upload, "filename", ""):
            return RedirectResponse("/banking?error=Choose a CSV file.",
                                    status_code=303)
        raw = (await upload.read()).decode("utf-8-sig", errors="replace")
        try:
            batch_id = banking.create_batch(
                conn, company["id"], upload.filename, raw)
        except ledger.PostingError as exc:
            return RedirectResponse(f"/banking?error={exc}", status_code=303)
        return RedirectResponse(f"/banking/map/{batch_id}", status_code=303)
    finally:
        conn.close()


@app.get("/banking/map/{batch_id}", response_class=HTMLResponse)
def banking_map_page(request: Request, batch_id: int, error: str = ""):
    conn = db.get_conn()
    try:
        company = _require_company(conn)
        if company is None:
            return RedirectResponse("/setup", status_code=303)
        batch = banking.get_batch(conn, batch_id)
        if batch is None or batch["company_id"] != company["id"]:
            return RedirectResponse("/banking", status_code=303)
        headers, data = banking.parse_csv(batch["raw_csv"])
        mapping = banking.guess_mapping(headers)
        rows, parse_error = banking.preview(batch["raw_csv"], mapping)
        return render("bank_map.html", request, _conn=conn, batch=batch,
                      headers=headers, columns=list(enumerate(headers)),
                      mapping=mapping, sample=data[:8], preview_rows=rows,
                      banks=ar.bank_accounts(conn, company["id"]),
                      parse_error=parse_error, error=error)
    finally:
        conn.close()


@app.post("/banking/map/{batch_id}")
async def banking_commit(request: Request, batch_id: int):
    form = await request.form()
    conn = db.get_conn()
    try:
        company = _require_company(conn)
        if company is None:
            return RedirectResponse("/setup", status_code=303)

        def col(field):
            value = form.get(field)
            return int(value) if value not in (None, "", "-1") else None

        mapping = {
            "date_col": col("date_col"),
            "desc_col": col("desc_col"),
            "mode": form.get("mode", "single"),
            "amount_col": col("amount_col"),
            "deposit_col": col("deposit_col"),
            "withdrawal_col": col("withdrawal_col"),
        }
        bank_account_id = form.get("bank_account_id")
        if not bank_account_id:
            return RedirectResponse(
                f"/banking/map/{batch_id}?error=Choose the bank account.",
                status_code=303)
        if mapping["date_col"] is None:
            return RedirectResponse(
                f"/banking/map/{batch_id}?error=Choose the date column.",
                status_code=303)
        try:
            count = banking.commit_batch(
                conn, batch_id, int(bank_account_id), mapping)
        except ledger.PostingError as exc:
            return RedirectResponse(f"/banking/map/{batch_id}?error={exc}",
                                    status_code=303)
        return RedirectResponse(
            f"/banking?note={count} transactions imported — review them below.",
            status_code=303)
    finally:
        conn.close()


@app.post("/banking/txn/{txn_id}/post")
async def banking_post_txn(request: Request, txn_id: int):
    form = await request.form()
    conn = db.get_conn()
    try:
        offset = form.get("offset_account_id")
        if not offset:
            return RedirectResponse(
                "/banking?error=Choose the account for that transaction.",
                status_code=303)
        try:
            banking.post_transaction(conn, txn_id, int(offset))
        except ledger.PostingError as exc:
            return RedirectResponse(f"/banking?error={exc}", status_code=303)
        return RedirectResponse("/banking?note=Transaction posted.",
                                status_code=303)
    finally:
        conn.close()


@app.post("/banking/txn/{txn_id}/ignore")
def banking_ignore_txn(txn_id: int):
    conn = db.get_conn()
    try:
        try:
            banking.ignore_transaction(conn, txn_id)
        except ledger.PostingError as exc:
            return RedirectResponse(f"/banking?error={exc}", status_code=303)
        return RedirectResponse("/banking?note=Marked as already recorded.",
                                status_code=303)
    finally:
        conn.close()


# --- year-end accountant package -------------------------------------------

@app.get("/accountant-export", response_class=HTMLResponse)
def accountant_export_page(request: Request, year: str = "", error: str = ""):
    conn = db.get_conn()
    try:
        company = _require_company(conn)
        if company is None:
            return RedirectResponse("/setup", status_code=303)
        chosen = int(year) if year.strip() else date.today().year
        return render("accountant_export.html", request, _conn=conn,
                      year=chosen,
                      preflight=accountant_export.preflight(
                          conn, company, chosen),
                      error=error)
    finally:
        conn.close()


@app.get("/accountant-export/download")
def accountant_export_download(year: str = ""):
    conn = db.get_conn()
    try:
        company = _require_company(conn)
        if company is None:
            return RedirectResponse("/setup", status_code=303)
        chosen = int(year) if year.strip() else date.today().year
        try:
            body, filename = accountant_export.build_package(
                conn, company, chosen)
        except ledger.PostingError as exc:
            return RedirectResponse(
                f"/accountant-export?year={chosen}&error={exc}",
                status_code=303)
    finally:
        conn.close()
    return Response(content=body, media_type="application/zip", headers={
        "Content-Disposition": f'attachment; filename="{filename}"'})


# --- login, registration, users --------------------------------------------

def _sign_in(request, user):
    request.session["user_id"] = user["id"]
    request.session["username"] = user["username"]
    request.session["display_name"] = user["display_name"] or user["username"]
    request.session["role"] = user["role"]


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, error: str = ""):
    conn = db.get_conn()
    try:
        if auth.user_count(conn) == 0:
            return RedirectResponse("/register", status_code=303)
        return render("login.html", request, _conn=conn, error=error)
    finally:
        conn.close()


@app.post("/login")
async def login_submit(request: Request):
    form = await request.form()
    conn = db.get_conn()
    try:
        user = auth.authenticate(conn, form.get("username", ""),
                                 form.get("password", ""))
        if user is None:
            return RedirectResponse(
                "/login?error=Incorrect username or password.",
                status_code=303)
        _sign_in(request, user)
        return RedirectResponse("/", status_code=303)
    finally:
        conn.close()


@app.get("/register", response_class=HTMLResponse)
def register_page(request: Request, error: str = ""):
    conn = db.get_conn()
    try:
        if auth.user_count(conn) > 0:
            return RedirectResponse("/login", status_code=303)
        return render("register.html", request, _conn=conn, error=error)
    finally:
        conn.close()


@app.post("/register")
async def register_submit(request: Request):
    form = await request.form()
    conn = db.get_conn()
    try:
        if auth.user_count(conn) > 0:
            return RedirectResponse("/login", status_code=303)
        if form.get("password", "") != form.get("confirm", ""):
            return RedirectResponse(
                "/register?error=The passwords do not match.",
                status_code=303)
        username = form.get("username", "")
        try:
            auth.create_user(conn, username, form.get("password", ""),
                             display_name=form.get("display_name", ""),
                             role="admin")
        except auth.AuthError as exc:
            return RedirectResponse(f"/register?error={exc}", status_code=303)
        _sign_in(request, auth.get_user(conn, username))
        return RedirectResponse("/", status_code=303)
    finally:
        conn.close()


@app.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


@app.get("/account", response_class=HTMLResponse)
def account_page(request: Request, error: str = "", saved: str = ""):
    conn = db.get_conn()
    try:
        account = auth.get_user_by_id(conn, request.session["user_id"])
        return render("account.html", request, _conn=conn, account=account,
                      error=error, saved=saved)
    finally:
        conn.close()


@app.post("/account/profile")
async def account_profile(request: Request):
    form = await request.form()
    conn = db.get_conn()
    try:
        try:
            auth.change_profile(conn, request.session["user_id"],
                                form.get("current_password", ""),
                                form.get("username", ""),
                                form.get("display_name", ""))
        except auth.AuthError as exc:
            return RedirectResponse(f"/account?error={exc}", status_code=303)
        user = auth.get_user_by_id(conn, request.session["user_id"])
        request.session["username"] = user["username"]
        request.session["display_name"] = (user["display_name"]
                                            or user["username"])
        return RedirectResponse("/account?saved=Username and display name "
                                "updated.", status_code=303)
    finally:
        conn.close()


@app.post("/account/password")
async def account_password(request: Request):
    form = await request.form()
    conn = db.get_conn()
    try:
        if form.get("new_password", "") != form.get("confirm", ""):
            return RedirectResponse(
                "/account?error=The new passwords do not match.",
                status_code=303)
        try:
            auth.change_password(conn, request.session["user_id"],
                                 form.get("current_password", ""),
                                 form.get("new_password", ""))
        except auth.AuthError as exc:
            return RedirectResponse(f"/account?error={exc}", status_code=303)
        return RedirectResponse("/account?saved=Password changed.",
                                status_code=303)
    finally:
        conn.close()


# --- vendors ---------------------------------------------------------------

@app.get("/vendors", response_class=HTMLResponse)
def vendors_page(request: Request, error: str = "", saved: str = ""):
    conn = db.get_conn()
    try:
        company = _require_company(conn)
        if company is None:
            return RedirectResponse("/setup", status_code=303)
        return render("vendors.html", request, _conn=conn,
                      vendors=ap.list_vendors(conn, company["id"]),
                      expense_accounts=ap.bill_line_accounts(
                          conn, company["id"]),
                      error=error, saved=saved)
    finally:
        conn.close()


@app.post("/vendors")
async def create_vendor(request: Request):
    form = await request.form()
    conn = db.get_conn()
    try:
        company = _require_company(conn)
        if company is None:
            return RedirectResponse("/setup", status_code=303)
        default_account = form.get("default_expense_account_id") or None
        try:
            ap.create_vendor(
                conn, company["id"], form.get("name", ""),
                address=form.get("address", ""), tin=form.get("tin", ""),
                is_1099=bool(form.get("is_1099")),
                box_1099=form.get("box_1099", ""),
                default_expense_account_id=default_account,
                terms_days=form.get("terms_days") or 30)
        except (ledger.PostingError, ValueError) as exc:
            return RedirectResponse(f"/vendors?error={exc}", status_code=303)
        return RedirectResponse("/vendors?saved=1", status_code=303)
    finally:
        conn.close()


@app.get("/vendors/1099", response_class=HTMLResponse)
def vendor_1099_page(request: Request, year: str = ""):
    conn = db.get_conn()
    try:
        company = _require_company(conn)
        if company is None:
            return RedirectResponse("/setup", status_code=303)
        chosen = int(year) if year.strip() else date.today().year
        threshold = taxconfig.form_1099_nec_threshold_cents(chosen)
        return render("vendor_1099.html", request, _conn=conn, year=chosen,
                      report=ap.vendor_1099_report(
                          conn, company["id"], chosen, threshold))
    finally:
        conn.close()


# --- bills -----------------------------------------------------------------

@app.get("/bills", response_class=HTMLResponse)
def bills_page(request: Request):
    conn = db.get_conn()
    try:
        company = _require_company(conn)
        if company is None:
            return RedirectResponse("/setup", status_code=303)
        return render("bills.html", request, _conn=conn,
                      bills=ap.list_bills(conn, company["id"]),
                      today=date.today().isoformat())
    finally:
        conn.close()


@app.get("/bills/new", response_class=HTMLResponse)
def bill_new_page(request: Request, error: str = ""):
    conn = db.get_conn()
    try:
        company = _require_company(conn)
        if company is None:
            return RedirectResponse("/setup", status_code=303)
        return render("bill_new.html", request, _conn=conn,
                      vendors=ap.list_vendors(conn, company["id"]),
                      accounts=ap.bill_line_accounts(conn, company["id"]),
                      today=date.today().isoformat(), rows=range(6),
                      error=error)
    finally:
        conn.close()


@app.post("/bills")
async def create_bill(request: Request):
    form = await request.form()
    conn = db.get_conn()
    try:
        company = _require_company(conn)
        if company is None:
            return RedirectResponse("/setup", status_code=303)
        bill_date = form.get("bill_date") or date.today().isoformat()
        due_date = form.get("due_date") or ""
        vendor_id = form.get("vendor_id")
        if not vendor_id:
            return RedirectResponse("/bills/new?error=Choose a vendor.",
                                    status_code=303)
        if not due_date:
            vendor = ap.get_vendor(conn, int(vendor_id))
            terms = vendor["terms_days"] if vendor else 30
            due_date = (date.fromisoformat(bill_date)
                        + timedelta(days=terms)).isoformat()
        lines = []
        for i in range(6):
            account = form.get(f"account_{i}")
            if not account:
                continue
            lines.append((form.get(f"description_{i}", ""), int(account),
                          ledger.to_cents(form.get(f"amount_{i}"))))
        try:
            bill_id = ap.create_bill(
                conn, company["id"], int(vendor_id), bill_date, due_date,
                form.get("bill_number", ""), lines, memo=form.get("memo", ""))
        except ledger.PostingError as exc:
            return RedirectResponse(f"/bills/new?error={exc}", status_code=303)
        return RedirectResponse(f"/bills/{bill_id}", status_code=303)
    finally:
        conn.close()


@app.get("/bills/{bill_id}", response_class=HTMLResponse)
def bill_view(request: Request, bill_id: int):
    conn = db.get_conn()
    try:
        company = _require_company(conn)
        if company is None:
            return RedirectResponse("/setup", status_code=303)
        detail = ap.get_bill(conn, bill_id)
        if detail is None:
            return RedirectResponse("/bills", status_code=303)
        return render("bill_view.html", request, _conn=conn, d=detail)
    finally:
        conn.close()


@app.get("/ap-aging", response_class=HTMLResponse)
def ap_aging_page(request: Request, as_of: str = ""):
    as_of = as_of or date.today().isoformat()
    conn = db.get_conn()
    try:
        company = _require_company(conn)
        if company is None:
            return RedirectResponse("/setup", status_code=303)
        return render("ap_aging.html", request, _conn=conn, as_of=as_of,
                      aging=ap.ap_aging(conn, company["id"], as_of))
    finally:
        conn.close()


# --- bill payments ---------------------------------------------------------

@app.get("/bill-payments/new", response_class=HTMLResponse)
def bill_payment_new_page(request: Request, vendor_id: str = "",
                          error: str = ""):
    conn = db.get_conn()
    try:
        company = _require_company(conn)
        if company is None:
            return RedirectResponse("/setup", status_code=303)
        vendor = bills = None
        if vendor_id:
            vendor = ap.get_vendor(conn, int(vendor_id))
            bills = ap.open_bills(conn, int(vendor_id))
        return render("bill_payment_new.html", request, _conn=conn,
                      vendors=ap.list_vendors(conn, company["id"]),
                      sources=ap.payment_accounts(conn, company["id"]),
                      vendor=vendor, open_bills=bills,
                      today=date.today().isoformat(), error=error)
    finally:
        conn.close()


@app.post("/bill-payments")
async def create_bill_payment(request: Request):
    form = await request.form()
    conn = db.get_conn()
    try:
        company = _require_company(conn)
        if company is None:
            return RedirectResponse("/setup", status_code=303)
        vendor_id = form.get("vendor_id")
        paid_from = form.get("paid_from_account_id")
        if not vendor_id or not paid_from:
            return RedirectResponse(
                "/bill-payments/new?error=Choose a vendor and the account "
                "the payment came from.", status_code=303)
        applications = []
        for bill in ap.open_bills(conn, int(vendor_id)):
            value = form.get(f"apply_{bill['bill']['id']}")
            applications.append((bill["bill"]["id"],
                                 ledger.to_cents(value)))
        try:
            ap.pay_bills(
                conn, company["id"], int(vendor_id),
                form.get("payment_date") or date.today().isoformat(),
                int(paid_from), form.get("payment_method", "check"),
                applications, reference=form.get("reference", ""),
                memo=form.get("memo", ""))
        except ledger.PostingError as exc:
            return RedirectResponse(
                f"/bill-payments/new?vendor_id={vendor_id}&error={exc}",
                status_code=303)
        return RedirectResponse(f"/bill-payments/new?vendor_id={vendor_id}"
                                "&error=Payment recorded.", status_code=303)
    finally:
        conn.close()


@app.get("/users", response_class=HTMLResponse)
def users_page(request: Request, error: str = "", saved: str = ""):
    if request.session.get("role") != "admin":
        return RedirectResponse("/", status_code=303)
    conn = db.get_conn()
    try:
        return render("users.html", request, _conn=conn,
                      users=auth.list_users(conn), error=error, saved=saved)
    finally:
        conn.close()


@app.post("/users")
async def create_user_submit(request: Request):
    if request.session.get("role") != "admin":
        return RedirectResponse("/", status_code=303)
    form = await request.form()
    conn = db.get_conn()
    try:
        if form.get("password", "") != form.get("confirm", ""):
            return RedirectResponse(
                "/users?error=The passwords do not match.", status_code=303)
        try:
            auth.create_user(conn, form.get("username", ""),
                             form.get("password", ""),
                             display_name=form.get("display_name", ""),
                             role=form.get("role", "standard"))
        except auth.AuthError as exc:
            return RedirectResponse(f"/users?error={exc}", status_code=303)
        return RedirectResponse("/users?saved=1", status_code=303)
    finally:
        conn.close()
