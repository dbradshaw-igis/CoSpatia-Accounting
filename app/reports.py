"""Reports — read-only views over the ledger. Every report is a pure function
of the posted journal lines plus a date or date range: the same inputs always
return the same numbers. Voided entries are excluded everywhere."""
import calendar
from datetime import date

from . import accounts as acct
from .ledger import PostingError


def _activity(conn, company_id, start=None, end=None):
    """Per-account debit/credit totals over a date range, posted entries only.

    The date and status filters live inside the SUM via CASE — putting them in
    a LEFT JOIN's ON clause would null the entry columns without dropping the
    line, and the SUM would still count it.
    """
    cond = "je.status = 'posted'"
    date_params = []
    if start:
        cond += " AND je.entry_date >= ?"
        date_params.append(start)
    if end:
        cond += " AND je.entry_date <= ?"
        date_params.append(end)
    sql = f"""
        SELECT a.id, a.account_number, a.name, a.type, a.subtype, a.tax_line,
               COALESCE(SUM(CASE WHEN {cond}
                            THEN jl.debit_cents  ELSE 0 END), 0) AS debits,
               COALESCE(SUM(CASE WHEN {cond}
                            THEN jl.credit_cents ELSE 0 END), 0) AS credits
        FROM accounts a
        LEFT JOIN journal_lines jl   ON jl.account_id = a.id
        LEFT JOIN journal_entries je ON je.id = jl.entry_id
        WHERE a.company_id = ?
        GROUP BY a.id
        ORDER BY a.account_number
    """
    return conn.execute(sql, date_params + date_params + [company_id]).fetchall()


def fiscal_year_bounds(company, as_of):
    """First and last day of the fiscal year that contains `as_of`."""
    y, m, _ = map(int, as_of.split("-"))
    end_month = (company["fiscal_year_end_month"]
                 if company["tax_year_type"] == "fiscal" else 12)
    fy_end_year = y if m <= end_month else y + 1
    fy_end = date(fy_end_year, end_month,
                  calendar.monthrange(fy_end_year, end_month)[1])
    start_month = end_month % 12 + 1
    start_year = fy_end_year if start_month == 1 else fy_end_year - 1
    return date(start_year, start_month, 1).isoformat(), fy_end.isoformat()


def fiscal_year(company, year):
    """First and last day of the fiscal year identified by `year` — the
    calendar year for a calendar filer, the year the fiscal year ends in
    otherwise."""
    end_month = (company["fiscal_year_end_month"]
                 if company["tax_year_type"] == "fiscal" else 12)
    return fiscal_year_bounds(company, f"{int(year):04d}-{end_month:02d}-15")


# --- trial balance ---------------------------------------------------------

def trial_balance(conn, company_id, as_of):
    rows, report = _activity(conn, company_id, end=as_of), []
    total_debit = total_credit = 0
    for r in rows:
        net = r["debits"] - r["credits"]
        debit = net if net > 0 else 0
        credit = -net if net < 0 else 0
        if debit == 0 and credit == 0:
            continue
        total_debit += debit
        total_credit += credit
        report.append({
            "account_id": r["id"], "account_number": r["account_number"],
            "name": r["name"], "type": r["type"], "tax_line": r["tax_line"],
            "debit": debit, "credit": credit,
        })
    return {
        "as_of": as_of, "rows": report,
        "total_debit": total_debit, "total_credit": total_credit,
        "balanced": total_debit == total_credit,
    }


# --- profit & loss ---------------------------------------------------------

def _section(rows, type_name):
    out, total = [], 0
    for r in rows:
        if r["type"] != type_name:
            continue
        amount = acct.natural_balance(type_name, r["debits"], r["credits"])
        if amount == 0:
            continue
        total += amount
        out.append({"account_number": r["account_number"],
                    "name": r["name"], "amount": amount})
    return out, total


def profit_and_loss(conn, company_id, start, end):
    rows = _activity(conn, company_id, start, end)
    income,        t_income      = _section(rows, "income")
    cogs,          t_cogs        = _section(rows, "cogs")
    expense,       t_expense     = _section(rows, "expense")
    other_income,  t_other_inc   = _section(rows, "other_income")
    other_expense, t_other_exp   = _section(rows, "other_expense")

    gross_profit = t_income - t_cogs
    operating_income = gross_profit - t_expense
    net_income = operating_income + t_other_inc - t_other_exp
    return {
        "start": start, "end": end,
        "income": income, "total_income": t_income,
        "cogs": cogs, "total_cogs": t_cogs, "gross_profit": gross_profit,
        "expense": expense, "total_expense": t_expense,
        "operating_income": operating_income,
        "other_income": other_income, "total_other_income": t_other_inc,
        "other_expense": other_expense, "total_other_expense": t_other_exp,
        "net_income": net_income,
    }


def _net_income(rows):
    """Net income implied by a set of activity rows: revenue less every cost."""
    total = 0
    for r in rows:
        nb = acct.natural_balance(r["type"], r["debits"], r["credits"])
        if r["type"] in ("income", "other_income"):
            total += nb
        elif r["type"] in ("cogs", "expense", "other_expense"):
            total -= nb
    return total


# --- balance sheet ---------------------------------------------------------

def balance_sheet(conn, company_id, as_of):
    rows = _activity(conn, company_id, end=as_of)
    assets,      t_assets = _section(rows, "asset")
    liabilities, t_liab   = _section(rows, "liability")
    equity,      t_equity = _section(rows, "equity")

    # Net income not yet closed to retained earnings. It belongs to equity, and
    # including it is what makes the sheet balance before the year-end close.
    net_income = _net_income(rows)
    total_equity = t_equity + net_income
    return {
        "as_of": as_of,
        "assets": assets, "total_assets": t_assets,
        "liabilities": liabilities, "total_liabilities": t_liab,
        "equity": equity, "equity_accounts_total": t_equity,
        "net_income": net_income, "total_equity": total_equity,
        "total_liabilities_equity": t_liab + total_equity,
        "balanced": t_assets == t_liab + total_equity,
    }


# --- general ledger detail -------------------------------------------------

def account_ledger(conn, account_id, start=None, end=None):
    account = conn.execute(
        "SELECT * FROM accounts WHERE id = ?", (account_id,)
    ).fetchone()
    if account is None:
        raise PostingError("No such account.")
    sign = 1 if acct.is_debit_normal(account["type"]) else -1

    opening = 0
    if start:
        prior = conn.execute(
            """SELECT COALESCE(SUM(jl.debit_cents - jl.credit_cents), 0) AS net
               FROM journal_lines jl
               JOIN journal_entries je ON je.id = jl.entry_id
               WHERE jl.account_id = ? AND je.status = 'posted'
                 AND je.entry_date < ?""",
            (account_id, start),
        ).fetchone()
        opening = prior["net"] * sign

    sql = """
        SELECT je.entry_date, je.entry_type, je.reference, je.memo,
               je.id AS entry_id, jl.debit_cents, jl.credit_cents, jl.line_memo
        FROM journal_lines jl
        JOIN journal_entries je ON je.id = jl.entry_id
        WHERE jl.account_id = ? AND je.status = 'posted'
    """
    params = [account_id]
    if start:
        sql += " AND je.entry_date >= ?"
        params.append(start)
    if end:
        sql += " AND je.entry_date <= ?"
        params.append(end)
    sql += " ORDER BY je.entry_date, je.id, jl.id"

    running, lines = opening, []
    for r in conn.execute(sql, params).fetchall():
        running += (r["debit_cents"] - r["credit_cents"]) * sign
        lines.append({
            "entry_date": r["entry_date"], "entry_type": r["entry_type"],
            "reference": r["reference"], "memo": r["line_memo"] or r["memo"],
            "entry_id": r["entry_id"], "debit": r["debit_cents"],
            "credit": r["credit_cents"], "balance": running,
        })
    return {"account": account, "opening": opening,
            "closing": running, "lines": lines}
