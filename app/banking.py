"""Bank import — load deposits and checks from a bank download file.

Bank CSV files differ from bank to bank, so the import is two steps: upload the
file, then confirm which columns hold the date, the description, and the
amount. An imported row is not a posted transaction yet — it sits for review
until someone assigns the account the other side of it belongs to. Only then
does it post a balanced journal entry.

  Deposit posts -> debit the bank account, credit the chosen account.
  Check posts   -> debit the chosen account, credit the bank account.

Stored amount sign: a deposit is positive, a check (withdrawal) is negative.
"""
import csv
import io
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from .ledger import PostingError, post_entry

DATE_FORMATS = (
    "%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%m-%d-%Y", "%Y/%m/%d",
    "%d-%b-%Y", "%b %d, %Y", "%m/%d/%Y %H:%M:%S", "%m/%d/%Y %H:%M",
)


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --- parsing ---------------------------------------------------------------

def parse_date(value):
    s = (value or "").strip()
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    raise PostingError(f"Could not read the date '{value}'.")


def parse_amount(value):
    """Parse a money string to integer cents. Returns None for a blank cell.
    Parentheses and a leading minus both mean negative."""
    s = (value or "").strip()
    if not s:
        return None
    negative = False
    if s.startswith("(") and s.endswith(")"):
        negative, s = True, s[1:-1]
    s = s.replace("$", "").replace(",", "").strip()
    if s.startswith("-"):
        negative, s = True, s[1:]
    if not s:
        return None
    try:
        cents = int((Decimal(s) * 100).quantize(Decimal("1")))
    except InvalidOperation:
        raise PostingError(f"Could not read the amount '{value}'.")
    return -cents if negative else cents


def parse_csv(raw_csv):
    """Return (headers, data_rows) from raw CSV text."""
    reader = csv.reader(io.StringIO(raw_csv))
    rows = [r for r in reader if any((c or "").strip() for c in r)]
    if not rows:
        raise PostingError("That file has no rows.")
    headers = [h.strip() for h in rows[0]]
    return headers, rows[1:]


def _find(headers, *needles):
    for i, h in enumerate(headers):
        low = h.lower()
        if any(n in low for n in needles):
            return i
    return None


def guess_mapping(headers):
    """Best guess at which column is which, for the confirm screen."""
    amount_col = _find(headers, "amount")
    deposit_col = _find(headers, "deposit", "credit")
    withdrawal_col = _find(headers, "withdraw", "debit", "payment")
    mode = "single" if amount_col is not None else (
        "split" if deposit_col is not None or withdrawal_col is not None
        else "single")
    return {
        "date_col": _find(headers, "date"),
        "desc_col": _find(headers, "description", "memo", "payee",
                          "name", "transaction", "detail"),
        "mode": mode,
        "amount_col": amount_col,
        "deposit_col": deposit_col,
        "withdrawal_col": withdrawal_col,
    }


def _cell(row, index):
    if index is None or index < 0 or index >= len(row):
        return ""
    return row[index]


def apply_mapping(row, mapping):
    """Turn one CSV row into (date, description, amount_cents), or None to skip
    a row that carries no amount."""
    if mapping["mode"] == "split":
        deposit = parse_amount(_cell(row, mapping["deposit_col"])) or 0
        withdrawal = parse_amount(_cell(row, mapping["withdrawal_col"])) or 0
        amount = deposit - abs(withdrawal)
    else:
        amount = parse_amount(_cell(row, mapping["amount_col"]))
    if not amount:
        return None
    return (parse_date(_cell(row, mapping["date_col"])),
            _cell(row, mapping["desc_col"]).strip(), amount)


def preview(raw_csv, mapping, limit=12):
    """Parsed rows for the confirm screen, plus any error encountered."""
    _, data = parse_csv(raw_csv)
    out = []
    try:
        for row in data[:limit]:
            parsed = apply_mapping(row, mapping)
            if parsed:
                out.append({"date": parsed[0], "description": parsed[1],
                            "amount_cents": parsed[2]})
        return out, None
    except PostingError as exc:
        return out, str(exc)


# --- import batches --------------------------------------------------------

def create_batch(conn, company_id, filename, raw_csv):
    if not (raw_csv or "").strip():
        raise PostingError("That file was empty.")
    cur = conn.execute(
        "INSERT INTO bank_import_batches "
        "(company_id, filename, raw_csv, created_at) VALUES (?, ?, ?, ?)",
        (company_id, filename, raw_csv, _now()),
    )
    conn.commit()
    return cur.lastrowid


def get_batch(conn, batch_id):
    return conn.execute(
        "SELECT * FROM bank_import_batches WHERE id = ?", (batch_id,)
    ).fetchone()


def commit_batch(conn, batch_id, bank_account_id, mapping):
    """Parse every row of a batch under the confirmed mapping and store the
    transactions for review."""
    batch = get_batch(conn, batch_id)
    if batch is None:
        raise PostingError("No such import batch.")
    if batch["committed"]:
        raise PostingError("That file has already been imported.")

    _, data = parse_csv(batch["raw_csv"])
    transactions = []
    for row in data:
        parsed = apply_mapping(row, mapping)
        if parsed:
            transactions.append(parsed)
    if not transactions:
        raise PostingError(
            "No transactions were found — check the column choices.")

    conn.executemany(
        """INSERT INTO bank_transactions
           (company_id, bank_account_id, batch_id, txn_date, description,
            amount_cents, status, created_at)
           VALUES (?, ?, ?, ?, ?, ?, 'unreviewed', ?)""",
        [(batch["company_id"], int(bank_account_id), batch_id,
          d, desc, amt, _now()) for d, desc, amt in transactions],
    )
    conn.execute("UPDATE bank_import_batches SET committed = 1 WHERE id = ?",
                 (batch_id,))
    conn.commit()
    return len(transactions)


# --- review and posting ----------------------------------------------------

def list_unreviewed(conn, company_id):
    return conn.execute(
        """SELECT bt.*, a.name AS bank_account_name
           FROM bank_transactions bt
           JOIN accounts a ON a.id = bt.bank_account_id
           WHERE bt.company_id = ? AND bt.status = 'unreviewed'
           ORDER BY bt.txn_date, bt.id""",
        (company_id,),
    ).fetchall()


def recent_posted(conn, company_id, limit=15):
    return conn.execute(
        """SELECT bt.*, a.name AS bank_account_name
           FROM bank_transactions bt
           JOIN accounts a ON a.id = bt.bank_account_id
           WHERE bt.company_id = ? AND bt.status = 'posted'
           ORDER BY bt.id DESC LIMIT ?""",
        (company_id, limit),
    ).fetchall()


def post_transaction(conn, txn_id, offset_account_id):
    """Categorize an imported transaction and post its journal entry."""
    txn = conn.execute(
        "SELECT * FROM bank_transactions WHERE id = ?", (txn_id,)
    ).fetchone()
    if txn is None:
        raise PostingError("No such bank transaction.")
    if txn["status"] != "unreviewed":
        raise PostingError("That transaction has already been reviewed.")

    bank = txn["bank_account_id"]
    offset = int(offset_account_id)
    amount = abs(txn["amount_cents"])
    if txn["amount_cents"] >= 0:                     # deposit
        lines, entry_type = [(bank, amount, 0), (offset, 0, amount)], "deposit"
    else:                                            # check / withdrawal
        lines, entry_type = [(offset, amount, 0), (bank, 0, amount)], "check"

    entry_id = post_entry(
        conn, txn["company_id"], txn["txn_date"], lines,
        memo=txn["description"], entry_type=entry_type)
    conn.execute(
        "UPDATE bank_transactions SET status = 'posted', journal_entry_id = ? "
        "WHERE id = ?", (entry_id, txn_id))
    conn.commit()
    return entry_id


def ignore_transaction(conn, txn_id):
    """Mark a transaction as already recorded elsewhere, so it is not posted a
    second time — the simple guard against double-counting a deposit that was
    also entered as a customer payment."""
    txn = conn.execute(
        "SELECT * FROM bank_transactions WHERE id = ?", (txn_id,)
    ).fetchone()
    if txn is None:
        raise PostingError("No such bank transaction.")
    if txn["status"] != "unreviewed":
        raise PostingError("That transaction has already been reviewed.")
    conn.execute(
        "UPDATE bank_transactions SET status = 'ignored' WHERE id = ?",
        (txn_id,))
    conn.commit()
