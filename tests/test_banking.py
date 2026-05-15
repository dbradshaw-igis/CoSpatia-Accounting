"""Tests for the bank import — parsing a bank CSV, importing transactions for
review, and posting them as deposits and checks."""
import pytest

from app import banking, db, ledger, reports

SINGLE_AMOUNT_CSV = """Date,Description,Amount
01/15/2026,Client deposit,"1,500.00"
01/20/2026,Office supplies,-85.50
01/22/2026,Service revenue,$420.00
"""

SPLIT_COLUMN_CSV = """Date,Description,Deposits,Withdrawals
2026-02-01,Customer payment,2000.00,
2026-02-03,Utility bill,,(140.25)
"""


@pytest.fixture
def conn():
    c = db.get_conn(":memory:")
    db.init_db(c)
    yield c
    c.close()


def acct(conn, company_id, number):
    return conn.execute(
        "SELECT id FROM accounts WHERE company_id = ? AND account_number = ?",
        (company_id, number),
    ).fetchone()["id"]


@pytest.fixture
def company(conn):
    cid = ledger.create_company(conn, "Test Co", "s_corp")
    return {"id": cid, "bank": acct(conn, cid, "1000"),
            "income": acct(conn, cid, "4000"),
            "expense": acct(conn, cid, "6070")}


def test_parse_amount_handles_bank_formats():
    assert banking.parse_amount("1,500.00") == 150000
    assert banking.parse_amount("$420.00") == 42000
    assert banking.parse_amount("-85.50") == -8550
    assert banking.parse_amount("(140.25)") == -14025
    assert banking.parse_amount("") is None


def test_parse_date_handles_common_layouts():
    assert banking.parse_date("01/15/2026") == "2026-01-15"
    assert banking.parse_date("2026-02-01") == "2026-02-01"


def test_guess_mapping_detects_single_amount():
    headers, _ = banking.parse_csv(SINGLE_AMOUNT_CSV)
    mapping = banking.guess_mapping(headers)
    assert mapping["mode"] == "single"
    assert mapping["date_col"] == 0
    assert mapping["amount_col"] == 2


def test_guess_mapping_detects_split_columns():
    headers, _ = banking.parse_csv(SPLIT_COLUMN_CSV)
    mapping = banking.guess_mapping(headers)
    assert mapping["mode"] == "split"
    assert mapping["deposit_col"] == 2
    assert mapping["withdrawal_col"] == 3


def test_import_single_amount_file(conn, company):
    batch = banking.create_batch(conn, company["id"], "bank.csv",
                                 SINGLE_AMOUNT_CSV)
    headers, _ = banking.parse_csv(SINGLE_AMOUNT_CSV)
    count = banking.commit_batch(conn, batch, company["bank"],
                                 banking.guess_mapping(headers))
    assert count == 3
    txns = banking.list_unreviewed(conn, company["id"])
    assert {t["amount_cents"] for t in txns} == {150000, -8550, 42000}


def test_import_split_column_file(conn, company):
    batch = banking.create_batch(conn, company["id"], "bank.csv",
                                 SPLIT_COLUMN_CSV)
    headers, _ = banking.parse_csv(SPLIT_COLUMN_CSV)
    count = banking.commit_batch(conn, batch, company["bank"],
                                 banking.guess_mapping(headers))
    assert count == 2
    txns = banking.list_unreviewed(conn, company["id"])
    assert {t["amount_cents"] for t in txns} == {200000, -14025}


def test_a_batch_imports_only_once(conn, company):
    batch = banking.create_batch(conn, company["id"], "b.csv",
                                 SINGLE_AMOUNT_CSV)
    headers, _ = banking.parse_csv(SINGLE_AMOUNT_CSV)
    mapping = banking.guess_mapping(headers)
    banking.commit_batch(conn, batch, company["bank"], mapping)
    with pytest.raises(ledger.PostingError, match="already been imported"):
        banking.commit_batch(conn, batch, company["bank"], mapping)


def test_posting_deposits_and_checks_hits_the_ledger(conn, company):
    batch = banking.create_batch(conn, company["id"], "b.csv",
                                 SINGLE_AMOUNT_CSV)
    headers, _ = banking.parse_csv(SINGLE_AMOUNT_CSV)
    banking.commit_batch(conn, batch, company["bank"],
                         banking.guess_mapping(headers))

    for txn in banking.list_unreviewed(conn, company["id"]):
        offset = (company["income"] if txn["amount_cents"] >= 0
                  else company["expense"])
        banking.post_transaction(conn, txn["id"], offset)

    tb = reports.trial_balance(conn, company["id"], "2026-12-31")
    by_number = {r["account_number"]: r for r in tb["rows"]}
    # Deposits 1500 + 420 less the 85.50 check = 1834.50 in the bank.
    assert by_number["1000"]["debit"] == 183450
    assert by_number["4000"]["credit"] == 192000
    assert by_number["6070"]["debit"] == 8550
    assert tb["balanced"]


def test_ignored_transaction_does_not_post(conn, company):
    batch = banking.create_batch(conn, company["id"], "b.csv",
                                 SINGLE_AMOUNT_CSV)
    headers, _ = banking.parse_csv(SINGLE_AMOUNT_CSV)
    banking.commit_batch(conn, batch, company["bank"],
                         banking.guess_mapping(headers))
    txn = banking.list_unreviewed(conn, company["id"])[0]
    banking.ignore_transaction(conn, txn["id"])
    with pytest.raises(ledger.PostingError, match="already been reviewed"):
        banking.post_transaction(conn, txn["id"], company["income"])
    remaining = banking.list_unreviewed(conn, company["id"])
    assert txn["id"] not in {t["id"] for t in remaining}
