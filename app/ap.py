"""Accounts payable — vendors, bills, and bill payments.

AP carries the 1099 data, so vendor records are richer than customers. Every
bill and every payment posts a balanced journal entry to the ledger:

  Bill posted    -> debit the expense/asset accounts, credit Accounts Payable.
  Bill paid      -> debit Accounts Payable, credit the account it was paid from.

A bill payment records its method and date because 1099 totals are based on the
payment date, and payments made by credit card are reported by the card
processor, not the business — so they are excluded from the 1099 figures.
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


def ap_account(conn, company_id):
    acct = _account_by_subtype(conn, company_id, "Accounts Payable")
    if acct is None:
        raise PostingError(
            "This company has no Accounts Payable account in its chart.")
    return acct


def bill_line_accounts(conn, company_id):
    """Accounts a bill line can post to — expenses and the asset accounts a
    capital purchase would land in."""
    return conn.execute(
        "SELECT * FROM accounts WHERE company_id = ? AND is_active = 1 "
        "AND type IN ('expense','cogs','other_expense','asset') "
        "ORDER BY account_number",
        (company_id,),
    ).fetchall()


def payment_accounts(conn, company_id):
    """Accounts a bill can be paid from — bank and credit-card accounts."""
    return conn.execute(
        "SELECT * FROM accounts WHERE company_id = ? AND is_active = 1 "
        "AND subtype IN ('Bank','Credit Card') ORDER BY account_number",
        (company_id,),
    ).fetchall()


# --- vendors ---------------------------------------------------------------

def list_vendors(conn, company_id):
    return conn.execute(
        "SELECT * FROM vendors WHERE company_id = ? ORDER BY name",
        (company_id,),
    ).fetchall()


def get_vendor(conn, vendor_id):
    return conn.execute(
        "SELECT * FROM vendors WHERE id = ?", (vendor_id,)
    ).fetchone()


def create_vendor(conn, company_id, name, address=None, tin=None,
                  is_1099=False, box_1099=None,
                  default_expense_account_id=None, terms_days=30):
    if not (name or "").strip():
        raise PostingError("A vendor needs a name.")
    conn.execute(
        """INSERT INTO vendors
           (company_id, name, address, tin, is_1099, box_1099,
            default_expense_account_id, terms_days)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (company_id, name.strip(), (address or "").strip() or None,
         (tin or "").strip() or None, 1 if is_1099 else 0,
         (box_1099 or "").strip() or None,
         int(default_expense_account_id) if default_expense_account_id else None,
         int(terms_days or 30)),
    )
    conn.commit()
    return conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]


# --- bills -----------------------------------------------------------------

def create_bill(conn, company_id, vendor_id, bill_date, due_date,
                bill_number, lines, memo=None):
    """Create a bill and post it. `lines` is a list of
    (description, account_id, amount_cents)."""
    vendor = get_vendor(conn, vendor_id)
    if vendor is None or vendor["company_id"] != company_id:
        raise PostingError("Unknown vendor.")

    real = [(d, int(a), int(amt)) for d, a, amt in lines if int(amt) != 0]
    if not real:
        raise PostingError("A bill needs at least one line with an amount.")
    for _, _, amt in real:
        if amt < 0:
            raise PostingError("Bill line amounts cannot be negative.")
    total = sum(amt for _, _, amt in real)
    ap = ap_account(conn, company_id)
    number = (bill_number or "").strip() or None

    cur = conn.execute(
        """INSERT INTO bills
           (company_id, vendor_id, bill_number, bill_date, due_date, memo,
            status, created_at)
           VALUES (?, ?, ?, ?, ?, ?, 'open', ?)""",
        (company_id, vendor_id, number, bill_date, due_date,
         (memo or "").strip() or None, _now()),
    )
    bill_id = cur.lastrowid
    conn.executemany(
        "INSERT INTO bill_lines (bill_id, description, account_id, amount_cents)"
        " VALUES (?, ?, ?, ?)",
        [(bill_id, d, a, amt) for d, a, amt in real],
    )

    # Debit each line's expense/asset account, credit Accounts Payable.
    entry_lines = [(a, amt, 0) for _, a, amt in real]
    entry_lines.append((ap["id"], 0, total))
    label = number or "(no number)"
    entry_id = post_entry(
        conn, company_id, bill_date, entry_lines,
        memo=f"Bill {label} — {vendor['name']}",
        reference=number, entry_type="bill")
    conn.execute("UPDATE bills SET journal_entry_id = ? WHERE id = ?",
                 (entry_id, bill_id))
    conn.commit()
    return bill_id


def bill_total(conn, bill_id):
    return conn.execute(
        "SELECT COALESCE(SUM(amount_cents), 0) AS t FROM bill_lines "
        "WHERE bill_id = ?", (bill_id,),
    ).fetchone()["t"]


def bill_paid(conn, bill_id):
    return conn.execute(
        "SELECT COALESCE(SUM(amount_cents), 0) AS p "
        "FROM bill_payment_applications WHERE bill_id = ?", (bill_id,),
    ).fetchone()["p"]


def get_bill(conn, bill_id):
    bill = conn.execute(
        "SELECT * FROM bills WHERE id = ?", (bill_id,)
    ).fetchone()
    if bill is None:
        return None
    lines = conn.execute(
        """SELECT bl.*, a.account_number, a.name AS account_name
           FROM bill_lines bl JOIN accounts a ON a.id = bl.account_id
           WHERE bl.bill_id = ? ORDER BY bl.id""",
        (bill_id,),
    ).fetchall()
    total = bill_total(conn, bill_id)
    paid = bill_paid(conn, bill_id)
    return {
        "bill": bill, "vendor": get_vendor(conn, bill["vendor_id"]),
        "lines": lines, "total": total, "paid": paid,
        "balance": total - paid,
    }


def list_bills(conn, company_id):
    rows = conn.execute(
        """SELECT b.*, v.name AS vendor_name
           FROM bills b JOIN vendors v ON v.id = b.vendor_id
           WHERE b.company_id = ? ORDER BY b.bill_date DESC, b.id DESC""",
        (company_id,),
    ).fetchall()
    out = []
    for r in rows:
        total = bill_total(conn, r["id"])
        paid = bill_paid(conn, r["id"])
        out.append({"bill": r, "vendor_name": r["vendor_name"],
                    "total": total, "paid": paid, "balance": total - paid})
    return out


def open_bills(conn, vendor_id):
    rows = conn.execute(
        "SELECT * FROM bills WHERE vendor_id = ? "
        "AND status IN ('open','partial') ORDER BY bill_date, id",
        (vendor_id,),
    ).fetchall()
    out = []
    for r in rows:
        total = bill_total(conn, r["id"])
        paid = bill_paid(conn, r["id"])
        out.append({"bill": r, "total": total, "paid": paid,
                    "balance": total - paid})
    return out


def _recompute_status(conn, bill_id):
    bill = conn.execute("SELECT * FROM bills WHERE id = ?",
                        (bill_id,)).fetchone()
    if bill["status"] == "void":
        return
    total = bill_total(conn, bill_id)
    paid = bill_paid(conn, bill_id)
    status = "open" if paid == 0 else "paid" if paid >= total else "partial"
    conn.execute("UPDATE bills SET status = ? WHERE id = ?",
                 (status, bill_id))


# --- bill payments ---------------------------------------------------------

def pay_bills(conn, company_id, vendor_id, payment_date, paid_from_account_id,
              payment_method, applications, reference=None, memo=None):
    """Pay one or more of a vendor's bills. `applications` is a list of
    (bill_id, amount_cents); the payment total is their sum."""
    vendor = get_vendor(conn, vendor_id)
    if vendor is None or vendor["company_id"] != company_id:
        raise PostingError("Unknown vendor.")
    if payment_method not in ("check", "ach", "cash", "card"):
        raise PostingError("Unknown payment method.")

    real = [(int(b), int(a)) for b, a in applications if int(a) != 0]
    if not real:
        raise PostingError("Enter an amount against at least one bill.")

    total = 0
    for bill_id, amount in real:
        if amount < 0:
            raise PostingError("Payment amounts cannot be negative.")
        bill = conn.execute("SELECT * FROM bills WHERE id = ?",
                            (bill_id,)).fetchone()
        if bill is None or bill["vendor_id"] != vendor_id:
            raise PostingError("A selected bill is not this vendor's.")
        balance = bill_total(conn, bill_id) - bill_paid(conn, bill_id)
        if amount > balance:
            raise PostingError(
                f"Bill {bill['bill_number'] or bill_id} has a balance of "
                f"{balance / 100:.2f}; cannot apply {amount / 100:.2f}.")
        total += amount

    ap = ap_account(conn, company_id)
    entry_id = post_entry(
        conn, company_id, payment_date,
        [(ap["id"], total, 0), (int(paid_from_account_id), 0, total)],
        memo=memo or f"Payment to {vendor['name']}",
        reference=reference, entry_type="bill_payment")

    cur = conn.execute(
        """INSERT INTO bill_payments
           (company_id, vendor_id, payment_date, amount_cents,
            paid_from_account_id, payment_method, reference, memo,
            journal_entry_id, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (company_id, vendor_id, payment_date, total,
         int(paid_from_account_id), payment_method,
         (reference or "").strip() or None, (memo or "").strip() or None,
         entry_id, _now()),
    )
    payment_id = cur.lastrowid
    conn.executemany(
        "INSERT INTO bill_payment_applications (payment_id, bill_id, amount_cents)"
        " VALUES (?, ?, ?)",
        [(payment_id, b, a) for b, a in real],
    )
    for bill_id, _ in real:
        _recompute_status(conn, bill_id)
    conn.commit()
    return payment_id


# --- reports ---------------------------------------------------------------

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


def _paid_as_of(conn, bill_id, as_of):
    row = conn.execute(
        """SELECT COALESCE(SUM(bpa.amount_cents), 0) AS p
           FROM bill_payment_applications bpa
           JOIN bill_payments bp ON bp.id = bpa.payment_id
           WHERE bpa.bill_id = ? AND bp.payment_date <= ?""",
        (bill_id, as_of),
    ).fetchone()
    return row["p"]


def ap_aging(conn, company_id, as_of):
    """Open payables as of a date, bucketed by how far past due they are. The
    grand total ties to the Accounts Payable balance on the balance sheet."""
    from datetime import date as _date
    as_of_day = _date.fromisoformat(as_of)
    rows = conn.execute(
        "SELECT * FROM bills WHERE company_id = ? AND status != 'void' "
        "AND bill_date <= ? ORDER BY vendor_id, bill_date, id",
        (company_id, as_of),
    ).fetchall()
    items = []
    totals = {b: 0 for b in AGING_BUCKETS}
    for r in rows:
        balance = bill_total(conn, r["id"]) - _paid_as_of(conn, r["id"], as_of)
        if balance <= 0:
            continue
        overdue = (as_of_day - _date.fromisoformat(r["due_date"])).days
        bucket = _bucket(overdue)
        totals[bucket] += balance
        items.append({
            "bill": r, "vendor_name": get_vendor(conn, r["vendor_id"])["name"],
            "balance": balance, "bucket": bucket,
            "days_overdue": max(overdue, 0),
        })
    return {"as_of": as_of, "buckets": AGING_BUCKETS, "rows": items,
            "totals": totals, "grand_total": sum(totals.values())}


def vendor_1099_report(conn, company_id, year, threshold_cents):
    """1099 figures for a calendar year: total paid to each 1099-eligible
    vendor, by payment date, excluding credit-card payments. Flags vendors at
    or above the reporting threshold and any that are missing a TIN."""
    start, end = f"{int(year):04d}-01-01", f"{int(year):04d}-12-31"
    rows = []
    over_threshold = missing_tin = 0
    for vendor in list_vendors(conn, company_id):
        if not vendor["is_1099"]:
            continue
        paid = conn.execute(
            """SELECT COALESCE(SUM(amount_cents), 0) AS p FROM bill_payments
               WHERE vendor_id = ? AND payment_method != 'card'
                 AND payment_date >= ? AND payment_date <= ?""",
            (vendor["id"], start, end),
        ).fetchone()["p"]
        if paid == 0:
            continue
        is_over = paid >= threshold_cents
        no_tin = not (vendor["tin"] or "").strip()
        if is_over:
            over_threshold += 1
        if is_over and no_tin:
            missing_tin += 1
        rows.append({
            "vendor": vendor, "total": paid,
            "over_threshold": is_over, "missing_tin": no_tin,
        })
    rows.sort(key=lambda r: r["total"], reverse=True)
    return {
        "year": int(year), "threshold_cents": threshold_cents, "rows": rows,
        "over_threshold_count": over_threshold,
        "missing_tin_count": missing_tin,
    }
