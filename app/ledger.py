"""The general ledger — the source of truth. Every financial event resolves to
a balanced journal entry here. Reports and exports are views over this; they
never store numbers of their own.

Integrity rules enforced in this module (spec section 4):
  - Debits equal credits on every entry, checked at save.
  - Money is integer cents. Rounding happens at display, never here.
  - Posted entries are never hard-deleted. They are voided, with the row kept.
  - A locked period rejects new postings and voids.
"""
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from . import charts


class PostingError(ValueError):
    """Raised when an operation would violate a ledger integrity rule."""


# --- money ----------------------------------------------------------------

def to_cents(value):
    """Parse a user-entered dollar amount into integer cents. Blank is zero."""
    s = str(value or "").strip().replace(",", "").replace("$", "")
    if not s:
        return 0
    try:
        return int((Decimal(s) * 100).quantize(Decimal("1")))
    except InvalidOperation:
        raise PostingError(f"'{value}' is not a valid amount.")


def fmt(cents):
    """Integer cents to a dollar string for display, parentheses for negative."""
    d = Decimal(cents) / 100
    return f"({abs(d):,.2f})" if d < 0 else f"{d:,.2f}"


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --- companies -------------------------------------------------------------

def list_companies(conn):
    return conn.execute("SELECT * FROM companies ORDER BY id").fetchall()


def get_company(conn, company_id):
    return conn.execute(
        "SELECT * FROM companies WHERE id = ?", (company_id,)
    ).fetchone()


def current_company(conn):
    """The active company. Phase 1 operates on one set of books; the schema
    already keys everything to a company so multi-company is a later add."""
    return conn.execute(
        "SELECT * FROM companies ORDER BY id LIMIT 1"
    ).fetchone()


def create_company(conn, legal_name, entity_type, dba=None, ein=None,
                    tax_year_type="calendar", fiscal_year_end_month=12,
                    accounting_basis="accrual", state=None):
    """Create a company and load the default chart of accounts for its entity
    type. Entity type is the master switch — it drives the tax-line mapping."""
    if entity_type not in charts.ENTITY_FORM:
        raise PostingError(f"Unknown entity type '{entity_type}'.")
    cur = conn.execute(
        """INSERT INTO companies
           (legal_name, dba, ein, entity_type, tax_year_type,
            fiscal_year_end_month, accounting_basis, state, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (legal_name.strip(), (dba or "").strip() or None,
         (ein or "").strip() or None, entity_type, tax_year_type,
         int(fiscal_year_end_month), accounting_basis,
         (state or "").strip() or None, _now()),
    )
    company_id = cur.lastrowid
    conn.executemany(
        """INSERT INTO accounts
           (company_id, account_number, name, type, subtype,
            tax_line, reconcilable)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        [(company_id, a["account_number"], a["name"], a["type"],
          a["subtype"], a["tax_line"], int(a["reconcilable"]))
         for a in charts.default_chart(entity_type)],
    )
    conn.commit()
    return company_id


# --- accounts --------------------------------------------------------------

def list_accounts(conn, company_id, include_inactive=False):
    sql = "SELECT * FROM accounts WHERE company_id = ?"
    if not include_inactive:
        sql += " AND is_active = 1"
    sql += " ORDER BY account_number"
    return conn.execute(sql, (company_id,)).fetchall()


def get_account(conn, account_id):
    return conn.execute(
        "SELECT * FROM accounts WHERE id = ?", (account_id,)
    ).fetchone()


# --- posting ---------------------------------------------------------------

def _check_open_period(company, entry_date):
    locked = company["locked_through"]
    if locked and entry_date <= locked:
        raise PostingError(
            f"The period through {locked} is locked. Post corrections as an "
            f"adjusting entry in an open period."
        )


def post_entry(conn, company_id, entry_date, lines, memo=None,
               reference=None, entry_type="manual", created_by="owner"):
    """Post a balanced journal entry. `lines` is a list of
    (account_id, debit_cents, credit_cents[, line_memo]); zero lines are
    dropped. An unbalanced entry is rejected, never plugged."""
    company = get_company(conn, company_id)
    if company is None:
        raise PostingError("No such company.")
    _check_open_period(company, entry_date)

    real = []
    for line in lines:
        account_id, debit, credit = int(line[0]), int(line[1]), int(line[2])
        line_memo = line[3] if len(line) > 3 else None
        if debit == 0 and credit == 0:
            continue
        if debit < 0 or credit < 0:
            raise PostingError("Amounts cannot be negative.")
        if debit > 0 and credit > 0:
            raise PostingError("A line is one-sided — debit or credit, not both.")
        real.append((account_id, debit, credit, line_memo))

    if len(real) < 2:
        raise PostingError("An entry needs at least two lines.")

    total_debit = sum(d for _, d, _, _ in real)
    total_credit = sum(c for _, _, c, _ in real)
    if total_debit != total_credit:
        raise PostingError(
            f"Entry is out of balance: debits {fmt(total_debit)} "
            f"vs. credits {fmt(total_credit)}. It was not saved."
        )
    if total_debit == 0:
        raise PostingError("An entry cannot be for zero.")

    cur = conn.execute(
        """INSERT INTO journal_entries
           (company_id, entry_date, entry_type, reference, memo,
            created_by, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (company_id, entry_date, entry_type, reference or None,
         memo or None, created_by, _now()),
    )
    entry_id = cur.lastrowid
    conn.executemany(
        """INSERT INTO journal_lines
           (entry_id, account_id, debit_cents, credit_cents, line_memo)
           VALUES (?, ?, ?, ?, ?)""",
        [(entry_id, a, d, c, m) for a, d, c, m in real],
    )
    conn.commit()
    return entry_id


def void_entry(conn, entry_id):
    """Void a posted entry. The row is kept and marked void so the audit trail
    stays intact — nothing is hard-deleted."""
    entry = conn.execute(
        "SELECT * FROM journal_entries WHERE id = ?", (entry_id,)
    ).fetchone()
    if entry is None:
        raise PostingError("No such entry.")
    if entry["status"] == "void":
        raise PostingError("Entry is already void.")
    company = get_company(conn, entry["company_id"])
    _check_open_period(company, entry["entry_date"])
    conn.execute(
        "UPDATE journal_entries SET status = 'void' WHERE id = ?", (entry_id,)
    )
    conn.commit()


def entry_with_lines(conn, entry_id):
    entry = conn.execute(
        "SELECT * FROM journal_entries WHERE id = ?", (entry_id,)
    ).fetchone()
    if entry is None:
        return None
    lines = conn.execute(
        """SELECT jl.*, a.account_number, a.name AS account_name
           FROM journal_lines jl JOIN accounts a ON a.id = jl.account_id
           WHERE jl.entry_id = ? ORDER BY jl.id""",
        (entry_id,),
    ).fetchall()
    return {"entry": entry, "lines": lines}


def recent_entries(conn, company_id, limit=25):
    rows = conn.execute(
        """SELECT id FROM journal_entries WHERE company_id = ?
           ORDER BY entry_date DESC, id DESC LIMIT ?""",
        (company_id, limit),
    ).fetchall()
    return [entry_with_lines(conn, r["id"]) for r in rows]
