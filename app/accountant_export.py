"""The Year-End Accountant Package — the headline export.

One action produces a single dated ZIP for a chosen fiscal year. Before it
builds, a pre-flight check runs: the user sees any exceptions and can fix them
or proceed. The goal (spec section 7.4) is that an accountant receiving the
package has no follow-up questions about completeness.

The package contents grow as the module's later phases ship. Today it carries
the general ledger and the reports derived from it; accounts payable, payroll,
fixed assets, sales tax, bank reconciliation, and the cash-flow statement plug
into the same package as those phases land.
"""
import io
import re
import zipfile
from datetime import datetime, timezone

from . import charts, export, reports
from .accounts import ENTITY_LABELS
from .ledger import PostingError

# filename -> one-line description for the README index.
PACKAGE_FILES = {
    "01-trial-balance.csv":
        "Adjusted trial balance as of year-end, with the tax-line column.",
    "02-general-ledger.csv":
        "Every posted transaction for the year, account by account.",
    "03-profit-and-loss.csv":
        "Income and expense for the year, down to net income.",
    "04-balance-sheet.csv":
        "Assets, liabilities, and equity as of year-end.",
    "05-accounts-receivable-aging.csv":
        "Open invoices at year-end, bucketed by how far past due.",
    "06-tax-line-mapping.csv":
        "Each account mapped to its return line; unmapped accounts flagged.",
}


def preflight(conn, company, year):
    """Run the pre-flight checks for a fiscal year. Each check is ok, a
    warning the user can proceed past, a blocker that stops the build, or an
    informational note."""
    start, end = reports.fiscal_year(company, year)
    checks = []

    tb = reports.trial_balance(conn, company["id"], end)
    if tb["balanced"]:
        checks.append({"name": "Trial balance", "status": "ok",
                       "detail": "Debits equal credits as of year-end."})
    else:
        checks.append({"name": "Trial balance", "status": "blocker",
                       "detail": "The trial balance does not tie out. This is "
                                 "a data-integrity problem and must be fixed "
                                 "before the package can be built."})

    activity = reports._activity(conn, company["id"], start, end)
    income_expense = ("income", "cogs", "expense",
                      "other_income", "other_expense")
    unmapped = [r for r in activity
                if r["type"] in income_expense and not r["tax_line"]
                and (r["debits"] or r["credits"])]
    if unmapped:
        names = ", ".join(f"{r['account_number']} {r['name']}"
                          for r in unmapped)
        checks.append({"name": "Tax-line mapping", "status": "warning",
                       "detail": f"{len(unmapped)} income or expense account(s) "
                                 f"with activity are not mapped to a return "
                                 f"line: {names}."})
    else:
        checks.append({"name": "Tax-line mapping", "status": "ok",
                       "detail": "Every income and expense account with "
                                 "activity is mapped to a return line."})

    pending = conn.execute(
        "SELECT COUNT(*) AS n FROM bank_transactions "
        "WHERE company_id = ? AND status = 'unreviewed'",
        (company["id"],),
    ).fetchone()["n"]
    if pending:
        checks.append({"name": "Imported bank transactions",
                       "status": "warning",
                       "detail": f"{pending} imported bank transaction(s) "
                                 f"have not been categorized and posted."})
    else:
        checks.append({"name": "Imported bank transactions", "status": "ok",
                       "detail": "No imported transactions are waiting for "
                                 "review."})

    checks.append({"name": "Bank reconciliation", "status": "info",
                   "detail": "Statement reconciliation is not yet part of the "
                             "module. Confirm cash balances against bank "
                             "statements before filing."})

    return {
        "year": int(year), "start": start, "end": end, "checks": checks,
        "has_blocker": any(c["status"] == "blocker" for c in checks),
        "has_warning": any(c["status"] == "warning" for c in checks),
    }


def _slug(name):
    return re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-") or "company"


def _readme(company, pf, generated):
    form = charts.ENTITY_FORM.get(company["entity_type"], "")
    entity = ENTITY_LABELS.get(company["entity_type"], "")
    lines = [
        "ACCOUNTANT PACKAGE",
        "=" * 64,
        "",
        f"Company:           {company['legal_name']}",
    ]
    if company["dba"]:
        lines.append(f"Doing business as: {company['dba']}")
    lines += [
        f"EIN:               {company['ein'] or '(not set)'}",
        f"Entity type:       {entity} (files {form})",
        f"Fiscal year:       FY{pf['year']}  ({pf['start']} to {pf['end']})",
        f"Accounting basis:  {company['accounting_basis']}",
        f"Generated:         {generated.isoformat(timespec='seconds')}",
        "",
        "FILES IN THIS PACKAGE",
        "-" * 64,
        f"{'00-README.txt':<38}This file.",
    ]
    for name, description in PACKAGE_FILES.items():
        lines.append(f"{name:<38}{description}")
    lines += ["", "PRE-FLIGHT CHECK", "-" * 64]
    for check in pf["checks"]:
        lines.append(f"[{check['status'].upper():<8}] {check['name']}")
        lines.append(f"           {check['detail']}")
    lines += [
        "",
        "NOT YET INCLUDED",
        "-" * 64,
        "The accounting module is being built in phases. This package covers",
        "the general ledger and the reports derived from it. Still to come as",
        "later phases ship: accounts payable and the 1099 vendor report, bank",
        "and credit-card reconciliation reports, the depreciation schedule,",
        "payroll summaries and payroll-tax liability, the sales-tax liability",
        "summary, loan and owner-equity schedules, the statement of cash",
        "flows, and PDF and Excel copies of each report.",
        "",
    ]
    return "\n".join(lines)


def build_package(conn, company, year):
    """Build the dated ZIP for a fiscal year. Returns (zip_bytes, filename).
    A failing pre-flight blocker stops the build."""
    pf = preflight(conn, company, year)
    if pf["has_blocker"]:
        raise PostingError(
            "The package cannot be built while a pre-flight check is failing. "
            "Resolve the blocker shown above first.")

    start, end = pf["start"], pf["end"]
    generated = datetime.now(timezone.utc)
    files = {
        "01-trial-balance.csv":
            export.trial_balance_csv(conn, company, end),
        "02-general-ledger.csv":
            export.general_ledger_csv(conn, company, start, end),
        "03-profit-and-loss.csv":
            export.profit_and_loss_csv(conn, company, start, end),
        "04-balance-sheet.csv":
            export.balance_sheet_csv(conn, company, end),
        "05-accounts-receivable-aging.csv":
            export.ar_aging_csv(conn, company, end),
        "06-tax-line-mapping.csv":
            export.tax_line_mapping_csv(conn, company, end),
    }

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("00-README.txt", _readme(company, pf, generated))
        for name, body in files.items():
            archive.writestr(name, body)

    filename = (f"{_slug(company['legal_name'])}-accountant-package-"
                f"FY{pf['year']}-{generated.date().isoformat()}.zip")
    return buffer.getvalue(), filename
