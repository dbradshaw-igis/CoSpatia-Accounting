"""Tests for the Year-End Accountant Package — the pre-flight check and the
dated ZIP it produces."""
import io
import zipfile

import pytest

from app import accountant_export, ar, banking, db, ledger


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
    """A company with a clean year of activity: contribution, sales, expense."""
    cid = ledger.create_company(conn, "InteractiveGIS, Inc.", "s_corp",
                                ein="54-1234567")
    cash = acct(conn, cid, "1000")
    equity = acct(conn, cid, "3000")
    revenue = acct(conn, cid, "4000")
    expense = acct(conn, cid, "6000")
    ledger.post_entry(conn, cid, "2026-01-02",
                      [(cash, 1000000, 0), (equity, 0, 1000000)],
                      entry_type="opening")
    ledger.post_entry(conn, cid, "2026-04-01",
                      [(cash, 600000, 0), (revenue, 0, 600000)])
    ledger.post_entry(conn, cid, "2026-05-01",
                      [(expense, 90000, 0), (cash, 0, 90000)])
    return ledger.get_company(conn, cid)


def test_preflight_passes_for_clean_books(conn, company):
    pf = accountant_export.preflight(conn, company, 2026)
    assert pf["start"] == "2026-01-01"
    assert pf["end"] == "2026-12-31"
    assert not pf["has_blocker"]
    statuses = {c["name"]: c["status"] for c in pf["checks"]}
    assert statuses["Trial balance"] == "ok"
    assert statuses["Tax-line mapping"] == "ok"


def test_preflight_flags_unmapped_account_with_activity(conn, company):
    # Strip the tax line off a revenue account that has activity.
    conn.execute("UPDATE accounts SET tax_line = NULL WHERE company_id = ? "
                 "AND account_number = '4000'", (company["id"],))
    conn.commit()
    pf = accountant_export.preflight(conn, company, 2026)
    mapping = next(c for c in pf["checks"] if c["name"] == "Tax-line mapping")
    assert mapping["status"] == "warning"
    assert "4000" in mapping["detail"]
    assert not pf["has_blocker"]  # a warning, not a blocker


def test_preflight_flags_uncategorized_bank_transactions(conn, company):
    batch = banking.create_batch(
        conn, company["id"], "bank.csv",
        "Date,Description,Amount\n03/01/2026,Deposit,500.00\n")
    headers, _ = banking.parse_csv(
        "Date,Description,Amount\n03/01/2026,Deposit,500.00\n")
    banking.commit_batch(conn, batch, acct(conn, company["id"], "1000"),
                         banking.guess_mapping(headers))
    pf = accountant_export.preflight(conn, company, 2026)
    bank_check = next(c for c in pf["checks"]
                      if c["name"] == "Imported bank transactions")
    assert bank_check["status"] == "warning"


def test_build_package_produces_a_zip(conn, company):
    body, filename = accountant_export.build_package(conn, company, 2026)
    assert filename.startswith("interactivegis-inc-accountant-package-FY2026-")
    assert filename.endswith(".zip")

    archive = zipfile.ZipFile(io.BytesIO(body))
    names = set(archive.namelist())
    assert "00-README.txt" in names
    for expected in accountant_export.PACKAGE_FILES:
        assert expected in names

    readme = archive.read("00-README.txt").decode()
    assert "InteractiveGIS, Inc." in readme
    assert "54-1234567" in readme
    assert "FY2026" in readme

    # The trial balance inside the package ties out.
    tb = archive.read("01-trial-balance.csv").decode()
    assert "account_number,account_name" in tb
    assert ",Total," in tb


def test_build_package_blocks_on_a_failing_preflight(conn, company,
                                                     monkeypatch):
    monkeypatch.setattr(
        accountant_export, "preflight",
        lambda *a, **k: {"year": 2026, "start": "2026-01-01",
                         "end": "2026-12-31", "checks": [], "has_blocker": True,
                         "has_warning": False},
    )
    with pytest.raises(ledger.PostingError, match="blocker"):
        accountant_export.build_package(conn, company, 2026)
