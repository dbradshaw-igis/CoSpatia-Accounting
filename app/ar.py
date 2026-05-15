"""Accounts receivable — customers, invoices, and payments received.

AR exists so the balance sheet shows what is owed to the business and so the
accountant can convert between cash and accrual. Every invoice and every
payment posts a balanced journal entry to the ledger automatically; AR is not a
separate set of books.

  Invoice posted   -> debit Accounts Receivable, credit the income accounts.
  Payment received -> debit the deposit (bank) account, credit Accounts
                      Receivable, and apply the cash to specific invoices.
"""
from datetime import datetime, timezone

from .ledger import PostingError, post_entry, void_entry


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --- account lookups -------------------------------------------------------

def _account_by_subtype(conn, company_id, subtype):
    return conn.execute(
        "SELECT * FROM accounts WHERE company_id = ? AND subtype = ? "
        "AND is_active = 1 ORDER BY account_number LIMIT 1",
        (company_id, subtype),
    ).fetchone()


def ar_account(conn, company_id):
    acct = _account_by_subtype(conn, company_id, "Accounts Receivable")
    if acct is None:
        raise PostingError(
            "This company has no Accounts Receivable account in its chart.")
    return acct


def bank_accounts(conn, company_id):
    return conn.execute(
        "SELECT * FROM accounts WHERE company_id = ? AND subtype = 'Bank' "
        "AND is_active = 1 ORDER BY account_number",
        (company_id,),
    ).fetchall()


def income_accounts(conn, company_id):
    return conn.execute(
        "SELECT * FROM accounts WHERE company_id = ? AND is_active = 1 "
        "AND type IN ('income','other_income') ORDER BY account_number",
        (company_id,),
    ).fetchall()


# --- customers -------------------------------------------------------------

def list_customers(conn, company_id):
    return conn.execute(
        "SELECT * FROM customers WHERE company_id = ? ORDER BY name",
        (company_id,),
    ).fetchall()


def get_customer(conn, customer_id):
    return conn.execute(
        "SELECT * FROM customers WHERE id = ?", (customer_id,)
    ).fetchone()


def create_customer(conn, company_id, name, email=None,
                    billing_address=None, terms_days=30):
    if not (name or "").strip():
        raise PostingError("A customer needs a name.")
    cur = conn.execute(
        """INSERT INTO customers
           (company_id, name, email, billing_address, terms_days)
           VALUES (?, ?, ?, ?, ?)""",
        (company_id, name.strip(), (email or "").strip() or None,
         (billing_address or "").strip() or None, int(terms_days or 30)),
    )
    conn.commit()
    return cur.lastrowid


# --- invoices --------------------------------------------------------------

def next_invoice_number(conn, company_id, year_month):
    """The next invoice number for a year-month, formatted YYYY-MM-NN.

    `year_month` is the first seven characters of the invoice date. Numbering
    restarts at 01 each month, and widens past two digits only if a month ever
    runs beyond 99 invoices.
    """
    prefix = f"{year_month}-"
    rows = conn.execute(
        "SELECT invoice_number FROM invoices "
        "WHERE company_id = ? AND invoice_number LIKE ?",
        (company_id, prefix + "%"),
    ).fetchall()
    highest = 0
    for r in rows:
        tail = r["invoice_number"][len(prefix):]
        if tail.isdigit():
            highest = max(highest, int(tail))
    return f"{prefix}{highest + 1:02d}"


def create_invoice(conn, company_id, customer_id, invoice_date, due_date,
                   invoice_number, lines, memo=None):
    """Create an invoice and post it. `lines` is a list of
    (description, income_account_id, amount_cents)."""
    customer = get_customer(conn, customer_id)
    if customer is None or customer["company_id"] != company_id:
        raise PostingError("Unknown customer.")

    real = [(d, int(a), int(amt)) for d, a, amt in lines if int(amt) != 0]
    if not real:
        raise PostingError("An invoice needs at least one line with an amount.")
    for _, _, amt in real:
        if amt < 0:
            raise PostingError("Invoice line amounts cannot be negative.")
    total = sum(amt for _, _, amt in real)
    ar = ar_account(conn, company_id)

    cur = conn.execute(
        """INSERT INTO invoices
           (company_id, customer_id, invoice_number, invoice_date, due_date,
            memo, status, created_at)
           VALUES (?, ?, ?, ?, ?, ?, 'open', ?)""",
        (company_id, customer_id, invoice_number.strip(), invoice_date,
         due_date, (memo or "").strip() or None, _now()),
    )
    invoice_id = cur.lastrowid
    conn.executemany(
        """INSERT INTO invoice_lines
           (invoice_id, description, income_account_id, amount_cents)
           VALUES (?, ?, ?, ?)""",
        [(invoice_id, d, a, amt) for d, a, amt in real],
    )

    # Debit AR for the total, credit each line's income account.
    entry_lines = [(ar["id"], total, 0)]
    entry_lines += [(a, 0, amt) for _, a, amt in real]
    entry_id = post_entry(
        conn, company_id, invoice_date, entry_lines,
        memo=f"Invoice {invoice_number.strip()} — {customer['name']}",
        reference=invoice_number.strip(), entry_type="invoice")
    conn.execute("UPDATE invoices SET journal_entry_id = ? WHERE id = ?",
                 (entry_id, invoice_id))
    conn.commit()
    return invoice_id


def _validated_lines(lines):
    real = [(d, int(a), int(amt)) for d, a, amt in lines if int(amt) != 0]
    if not real:
        raise PostingError("An invoice needs at least one line with an amount.")
    for _, _, amt in real:
        if amt < 0:
            raise PostingError("Invoice line amounts cannot be negative.")
    return real


def update_invoice(conn, invoice_id, customer_id, invoice_date, due_date,
                   invoice_number, lines, memo=None):
    """Edit an invoice. The old journal entry is voided and a fresh one is
    posted — the ledger is corrected, not rewritten, so the audit trail keeps
    the original. Editing is blocked once a payment has been applied, since
    that would leave the payment pointing at a changed invoice."""
    invoice = conn.execute(
        "SELECT * FROM invoices WHERE id = ?", (invoice_id,)
    ).fetchone()
    if invoice is None:
        raise PostingError("Unknown invoice.")
    if invoice["status"] == "void":
        raise PostingError("A void invoice cannot be edited.")
    if invoice_paid(conn, invoice_id) > 0:
        raise PostingError(
            "This invoice has a payment applied to it. Editing is blocked so "
            "the payment is not left pointing at different figures.")

    customer = get_customer(conn, customer_id)
    if customer is None or customer["company_id"] != invoice["company_id"]:
        raise PostingError("Unknown customer.")
    real = _validated_lines(lines)
    total = sum(amt for _, _, amt in real)
    company_id = invoice["company_id"]

    # Check the new date sits in an open period before changing anything, so a
    # locked-period rejection cannot leave the invoice without a posting.
    company = conn.execute(
        "SELECT locked_through FROM companies WHERE id = ?", (company_id,)
    ).fetchone()
    if company["locked_through"] and invoice_date <= company["locked_through"]:
        raise PostingError(
            f"The period through {company['locked_through']} is locked.")

    ar = ar_account(conn, company_id)
    if invoice["journal_entry_id"]:
        void_entry(conn, invoice["journal_entry_id"])
    conn.execute("DELETE FROM invoice_lines WHERE invoice_id = ?", (invoice_id,))
    conn.executemany(
        """INSERT INTO invoice_lines
           (invoice_id, description, income_account_id, amount_cents)
           VALUES (?, ?, ?, ?)""",
        [(invoice_id, d, a, amt) for d, a, amt in real],
    )
    entry_lines = [(ar["id"], total, 0)]
    entry_lines += [(a, 0, amt) for _, a, amt in real]
    entry_id = post_entry(
        conn, company_id, invoice_date, entry_lines,
        memo=f"Invoice {invoice_number.strip()} — {customer['name']}",
        reference=invoice_number.strip(), entry_type="invoice")
    conn.execute(
        """UPDATE invoices SET customer_id = ?, invoice_number = ?,
           invoice_date = ?, due_date = ?, memo = ?, journal_entry_id = ?,
           status = 'open' WHERE id = ?""",
        (customer_id, invoice_number.strip(), invoice_date, due_date,
         (memo or "").strip() or None, entry_id, invoice_id),
    )
    conn.commit()
    return invoice_id


def invoice_total(conn, invoice_id):
    row = conn.execute(
        "SELECT COALESCE(SUM(amount_cents), 0) AS t FROM invoice_lines "
        "WHERE invoice_id = ?", (invoice_id,),
    ).fetchone()
    return row["t"]


def invoice_paid(conn, invoice_id):
    row = conn.execute(
        "SELECT COALESCE(SUM(amount_cents), 0) AS p FROM payment_applications "
        "WHERE invoice_id = ?", (invoice_id,),
    ).fetchone()
    return row["p"]


def get_invoice(conn, invoice_id):
    invoice = conn.execute(
        "SELECT * FROM invoices WHERE id = ?", (invoice_id,)
    ).fetchone()
    if invoice is None:
        return None
    lines = conn.execute(
        """SELECT il.*, a.account_number, a.name AS account_name
           FROM invoice_lines il JOIN accounts a ON a.id = il.income_account_id
           WHERE il.invoice_id = ? ORDER BY il.id""",
        (invoice_id,),
    ).fetchall()
    customer = get_customer(conn, invoice["customer_id"])
    total = invoice_total(conn, invoice_id)
    paid = invoice_paid(conn, invoice_id)
    return {
        "invoice": invoice, "customer": customer, "lines": lines,
        "total": total, "paid": paid, "balance": total - paid,
    }


def list_invoices(conn, company_id):
    rows = conn.execute(
        """SELECT i.*, c.name AS customer_name
           FROM invoices i JOIN customers c ON c.id = i.customer_id
           WHERE i.company_id = ? ORDER BY i.invoice_date DESC, i.id DESC""",
        (company_id,),
    ).fetchall()
    out = []
    for r in rows:
        total = invoice_total(conn, r["id"])
        paid = invoice_paid(conn, r["id"])
        out.append({"invoice": r, "customer_name": r["customer_name"],
                    "total": total, "paid": paid, "balance": total - paid})
    return out


def open_invoices(conn, customer_id):
    """Open and partly-paid invoices for a customer, each with its balance."""
    rows = conn.execute(
        "SELECT * FROM invoices WHERE customer_id = ? "
        "AND status IN ('open','partial') ORDER BY invoice_date, id",
        (customer_id,),
    ).fetchall()
    out = []
    for r in rows:
        total = invoice_total(conn, r["id"])
        paid = invoice_paid(conn, r["id"])
        out.append({"invoice": r, "total": total, "paid": paid,
                    "balance": total - paid})
    return out


def _paid_as_of(conn, invoice_id, as_of):
    """Amount applied to an invoice by payments dated on or before `as_of`."""
    row = conn.execute(
        """SELECT COALESCE(SUM(pa.amount_cents), 0) AS p
           FROM payment_applications pa
           JOIN payments p ON p.id = pa.payment_id
           WHERE pa.invoice_id = ? AND p.payment_date <= ?""",
        (invoice_id, as_of),
    ).fetchone()
    return row["p"]


AGING_BUCKETS = ("current", "1-30", "31-60", "61-90", "90+")


def _bucket(days_overdue):
    if days_overdue <= 0:
        return "current"
    if days_overdue <= 30:
        return "1-30"
    if days_overdue <= 60:
        return "31-60"
    if days_overdue <= 90:
        return "61-90"
    return "90+"


def ar_aging(conn, company_id, as_of):
    """Open receivables as of a date, bucketed by how far past due they are.
    The grand total ties to the Accounts Receivable balance on the balance
    sheet for the same date."""
    from datetime import date as _date
    as_of_day = _date.fromisoformat(as_of)
    rows = conn.execute(
        "SELECT * FROM invoices WHERE company_id = ? AND status != 'void' "
        "AND invoice_date <= ? ORDER BY customer_id, invoice_date, id",
        (company_id, as_of),
    ).fetchall()
    items = []
    totals = {b: 0 for b in AGING_BUCKETS}
    for r in rows:
        balance = invoice_total(conn, r["id"]) - _paid_as_of(conn, r["id"], as_of)
        if balance <= 0:
            continue
        overdue = (as_of_day - _date.fromisoformat(r["due_date"])).days
        bucket = _bucket(overdue)
        totals[bucket] += balance
        customer = get_customer(conn, r["customer_id"])
        items.append({
            "invoice": r, "customer_name": customer["name"],
            "balance": balance, "bucket": bucket,
            "days_overdue": max(overdue, 0),
        })
    return {"as_of": as_of, "buckets": AGING_BUCKETS, "rows": items,
            "totals": totals, "grand_total": sum(totals.values())}


def _recompute_status(conn, invoice_id):
    invoice = conn.execute(
        "SELECT * FROM invoices WHERE id = ?", (invoice_id,)
    ).fetchone()
    if invoice["status"] == "void":
        return
    total = invoice_total(conn, invoice_id)
    paid = invoice_paid(conn, invoice_id)
    status = "open" if paid == 0 else "paid" if paid >= total else "partial"
    conn.execute("UPDATE invoices SET status = ? WHERE id = ?",
                 (status, invoice_id))


# --- payments --------------------------------------------------------------

def receive_payment(conn, company_id, customer_id, payment_date,
                    deposit_account_id, applications, reference=None,
                    memo=None):
    """Record a payment received and apply it to invoices. `applications` is a
    list of (invoice_id, amount_cents); the payment total is their sum."""
    customer = get_customer(conn, customer_id)
    if customer is None or customer["company_id"] != company_id:
        raise PostingError("Unknown customer.")

    real = [(int(i), int(a)) for i, a in applications if int(a) != 0]
    if not real:
        raise PostingError("Enter an amount against at least one invoice.")

    total = 0
    for invoice_id, amount in real:
        if amount < 0:
            raise PostingError("Payment amounts cannot be negative.")
        invoice = conn.execute(
            "SELECT * FROM invoices WHERE id = ?", (invoice_id,)
        ).fetchone()
        if invoice is None or invoice["customer_id"] != customer_id:
            raise PostingError("A selected invoice is not this customer's.")
        balance = invoice_total(conn, invoice_id) - invoice_paid(conn, invoice_id)
        if amount > balance:
            raise PostingError(
                f"Invoice {invoice['invoice_number']} has a balance of "
                f"{balance / 100:.2f}; cannot apply {amount / 100:.2f}.")
        total += amount

    ar = ar_account(conn, company_id)
    entry_id = post_entry(
        conn, company_id, payment_date,
        [(int(deposit_account_id), total, 0), (ar["id"], 0, total)],
        memo=memo or f"Payment received — {customer['name']}",
        reference=reference, entry_type="payment")

    cur = conn.execute(
        """INSERT INTO payments
           (company_id, customer_id, payment_date, amount_cents,
            deposit_account_id, reference, memo, journal_entry_id, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (company_id, customer_id, payment_date, total, int(deposit_account_id),
         (reference or "").strip() or None, (memo or "").strip() or None,
         entry_id, _now()),
    )
    payment_id = cur.lastrowid
    conn.executemany(
        "INSERT INTO payment_applications (payment_id, invoice_id, amount_cents)"
        " VALUES (?, ?, ?)",
        [(payment_id, i, a) for i, a in real],
    )
    for invoice_id, _ in real:
        _recompute_status(conn, invoice_id)
    conn.commit()
    return payment_id
