"""Export formats. The Trial Balance CSV is the workhorse for tax prep — its
column layout (account number, name, type, tax line, debit, credit) is what
ProConnect, Lacerte, Drake, UltraTax, and Xero accept on a trial-balance
import. The other CSV exports here follow the same conventions and feed the
Year-End Accountant Package.

CSV conventions (spec section 7): ISO dates, no thousands separators, period as
the decimal, leading minus for negatives, UTF-8, one header row.
"""
import csv
import io
from datetime import datetime, timezone

from . import ap, ar, charts, reports, taxconfig
from .ledger import PostingError


def _money(cents):
    """Signed decimal string for an amount column, e.g. -12.34."""
    return f"{cents / 100:.2f}"


def _side(cents):
    """A debit or credit column value — blank when zero, since a trial-balance
    line or a journal line shows one side, not 0.00 in both."""
    return "" if cents == 0 else f"{cents / 100:.2f}"


def _writer():
    buf = io.StringIO()
    return buf, csv.writer(buf)


def _metadata(writer, company, as_of):
    for key, value in [
        ("Company", company["legal_name"]),
        ("EIN", company["ein"] or ""),
        ("Entity type", charts.ENTITY_FORM.get(company["entity_type"], "")),
        ("Accounting basis", company["accounting_basis"]),
        ("As of", as_of),
        ("Generated", datetime.now(timezone.utc).isoformat(timespec="seconds")),
    ]:
        writer.writerow([key, value])


# --- trial balance ---------------------------------------------------------

def trial_balance_csv(conn, company, as_of):
    """Trial Balance CSV as of `as_of`. Aborts rather than producing a file
    that does not tie out (spec section 7.3)."""
    tb = reports.trial_balance(conn, company["id"], as_of)
    if not tb["balanced"]:
        raise PostingError(
            "Trial balance does not tie out — export aborted. "
            f"Debits {_money(tb['total_debit'])} vs. "
            f"credits {_money(tb['total_credit'])}.")
    buf, w = _writer()
    w.writerow(["account_number", "account_name", "account_type",
                "tax_line", "debit", "credit"])
    for row in tb["rows"]:
        w.writerow([row["account_number"], row["name"], row["type"],
                    row["tax_line"] or "",
                    _side(row["debit"]), _side(row["credit"])])
    w.writerow(["", "Total", "", "",
                _side(tb["total_debit"]), _side(tb["total_credit"])])
    w.writerow([])
    _metadata(w, company, as_of)
    return buf.getvalue()


# --- general ledger --------------------------------------------------------

def general_ledger_csv(conn, company, start, end):
    """Every posted journal line in the date range, flat — the detail an
    accountant reviews when the trial balance is not enough."""
    rows = conn.execute(
        """SELECT je.entry_date, a.account_number, a.name AS account_name,
                  je.entry_type, je.reference, je.memo,
                  jl.debit_cents, jl.credit_cents
           FROM journal_lines jl
           JOIN journal_entries je ON je.id = jl.entry_id
           JOIN accounts a ON a.id = jl.account_id
           WHERE je.company_id = ? AND je.status = 'posted'
             AND je.entry_date >= ? AND je.entry_date <= ?
           ORDER BY je.entry_date, je.id, jl.id""",
        (company["id"], start, end),
    ).fetchall()
    buf, w = _writer()
    w.writerow(["date", "account_number", "account_name", "entry_type",
                "reference", "memo", "debit", "credit"])
    for r in rows:
        w.writerow([r["entry_date"], r["account_number"], r["account_name"],
                    r["entry_type"], r["reference"] or "", r["memo"] or "",
                    _side(r["debit_cents"]), _side(r["credit_cents"])])
    return buf.getvalue()


# --- financial statements --------------------------------------------------

def profit_and_loss_csv(conn, company, start, end):
    pnl = reports.profit_and_loss(conn, company["id"], start, end)
    buf, w = _writer()
    w.writerow(["section", "account_number", "account_name", "amount"])

    def block(label, items, total, total_label):
        for it in items:
            w.writerow([label, it["account_number"], it["name"],
                        _money(it["amount"])])
        w.writerow([label, "", total_label, _money(total)])

    block("Income", pnl["income"], pnl["total_income"], "Total Income")
    if pnl["cogs"]:
        block("Cost of Goods Sold", pnl["cogs"], pnl["total_cogs"],
              "Total Cost of Goods Sold")
    w.writerow(["", "", "Gross Profit", _money(pnl["gross_profit"])])
    block("Expenses", pnl["expense"], pnl["total_expense"], "Total Expenses")
    w.writerow(["", "", "Operating Income", _money(pnl["operating_income"])])
    if pnl["other_income"]:
        block("Other Income", pnl["other_income"],
              pnl["total_other_income"], "Total Other Income")
    if pnl["other_expense"]:
        block("Other Expenses", pnl["other_expense"],
              pnl["total_other_expense"], "Total Other Expenses")
    w.writerow(["", "", "Net Income", _money(pnl["net_income"])])
    return buf.getvalue()


def balance_sheet_csv(conn, company, as_of):
    bs = reports.balance_sheet(conn, company["id"], as_of)
    buf, w = _writer()
    w.writerow(["section", "account_number", "account_name", "amount"])
    for it in bs["assets"]:
        w.writerow(["Assets", it["account_number"], it["name"],
                    _money(it["amount"])])
    w.writerow(["Assets", "", "Total Assets", _money(bs["total_assets"])])
    for it in bs["liabilities"]:
        w.writerow(["Liabilities", it["account_number"], it["name"],
                    _money(it["amount"])])
    w.writerow(["Liabilities", "", "Total Liabilities",
                _money(bs["total_liabilities"])])
    for it in bs["equity"]:
        w.writerow(["Equity", it["account_number"], it["name"],
                    _money(it["amount"])])
    w.writerow(["Equity", "", "Net Income (not yet closed)",
                _money(bs["net_income"])])
    w.writerow(["Equity", "", "Total Equity", _money(bs["total_equity"])])
    w.writerow(["", "", "Total Liabilities & Equity",
                _money(bs["total_liabilities_equity"])])
    return buf.getvalue()


# --- subledger reports -----------------------------------------------------

def ar_aging_csv(conn, company, as_of):
    aging = ar.ar_aging(conn, company["id"], as_of)
    buf, w = _writer()
    w.writerow(["invoice_number", "customer", "invoice_date", "due_date",
                "days_overdue", "aging_bucket", "balance"])
    for it in aging["rows"]:
        inv = it["invoice"]
        w.writerow([inv["invoice_number"], it["customer_name"],
                    inv["invoice_date"], inv["due_date"],
                    it["days_overdue"], it["bucket"], _money(it["balance"])])
    w.writerow([])
    for bucket in aging["buckets"]:
        w.writerow(["", "", "", "", "", bucket,
                    _money(aging["totals"][bucket])])
    w.writerow(["", "", "", "", "", "Total", _money(aging["grand_total"])])
    return buf.getvalue()


def ap_aging_csv(conn, company, as_of):
    aging = ap.ap_aging(conn, company["id"], as_of)
    buf, w = _writer()
    w.writerow(["bill_number", "vendor", "bill_date", "due_date",
                "days_overdue", "aging_bucket", "balance"])
    for it in aging["rows"]:
        bill = it["bill"]
        w.writerow([bill["bill_number"] or "", it["vendor_name"],
                    bill["bill_date"], bill["due_date"],
                    it["days_overdue"], it["bucket"], _money(it["balance"])])
    w.writerow([])
    for bucket in aging["buckets"]:
        w.writerow(["", "", "", "", "", bucket,
                    _money(aging["totals"][bucket])])
    w.writerow(["", "", "", "", "", "Total", _money(aging["grand_total"])])
    return buf.getvalue()


def vendor_1099_csv(conn, company, year):
    """1099 vendor figures for a calendar year — the file an e-file service or
    the accountant uses to prepare the 1099-NEC forms."""
    threshold = taxconfig.form_1099_nec_threshold_cents(year)
    report = ap.vendor_1099_report(conn, company["id"], year, threshold)
    buf, w = _writer()
    w.writerow(["vendor", "tin", "address", "box", "amount_paid",
                "over_threshold", "missing_tin"])
    for r in report["rows"]:
        v = r["vendor"]
        w.writerow([v["name"], v["tin"] or "", v["address"] or "",
                    v["box_1099"] or "", _money(r["total"]),
                    "yes" if r["over_threshold"] else "no",
                    "yes" if r["missing_tin"] else "no"])
    return buf.getvalue()


def tax_line_mapping_csv(conn, company, as_of):
    """Every account, its balance, and the return line it maps to. Unmapped
    income and expense accounts that carry activity are listed first, since
    those are the ones that need resolving before the return is prepared."""
    rows = reports._activity(conn, company["id"], end=as_of)
    income_expense = ("income", "cogs", "expense",
                      "other_income", "other_expense")

    def sort_key(r):
        needs_mapping = (not r["tax_line"] and r["type"] in income_expense
                         and (r["debits"] or r["credits"]))
        return (0 if needs_mapping else 1, r["account_number"])

    buf, w = _writer()
    w.writerow(["account_number", "account_name", "account_type",
                "tax_line", "debit_balance", "credit_balance"])
    for r in sorted(rows, key=sort_key):
        net = r["debits"] - r["credits"]
        w.writerow([r["account_number"], r["name"], r["type"],
                    r["tax_line"] or "UNMAPPED",
                    _side(net if net > 0 else 0),
                    _side(-net if net < 0 else 0)])
    return buf.getvalue()
