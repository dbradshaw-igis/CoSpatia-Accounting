"""Tests for the reports — trial balance, P&L, balance sheet — and the Trial
Balance CSV. Covers the spec acceptance criteria: the trial balance nets to
zero, the balance sheet balances for every entity type, and voided entries do
not appear in any report."""
import pytest

from app import db, export, ledger, reports


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


def seed_activity(conn, entity_type):
    """A company with an owner contribution, revenue, and an expense."""
    cid = ledger.create_company(conn, "Test Co", entity_type)
    cash = acct(conn, cid, "1000")
    equity = acct(conn, cid, "3000")
    revenue = acct(conn, cid, "4000")
    expense = acct(conn, cid, "6000")
    ledger.post_entry(conn, cid, "2026-01-02",
                      [(cash, 1000000, 0), (equity, 0, 1000000)],
                      entry_type="opening", memo="Owner contribution")
    ledger.post_entry(conn, cid, "2026-03-15",
                      [(cash, 500000, 0), (revenue, 0, 500000)],
                      memo="Services invoiced and paid")
    ledger.post_entry(conn, cid, "2026-04-10",
                      [(expense, 80000, 0), (cash, 0, 80000)],
                      memo="Advertising")
    return cid


def test_trial_balance_ties_out(conn):
    cid = seed_activity(conn, "sole_prop")
    tb = reports.trial_balance(conn, cid, "2026-12-31")
    assert tb["balanced"]
    assert tb["total_debit"] == tb["total_credit"]


def test_trial_balance_respects_as_of_date(conn):
    cid = seed_activity(conn, "sole_prop")
    # Before the expense posts, only the contribution and revenue count.
    early = reports.trial_balance(conn, cid, "2026-03-31")
    assert early["balanced"]
    assert early["total_debit"] == 1500000
    full = reports.trial_balance(conn, cid, "2026-12-31")
    assert full["total_debit"] == 1500000  # cash 1420000 + expense 80000


def test_pnl_computes_net_income(conn):
    cid = seed_activity(conn, "sole_prop")
    pnl = reports.profit_and_loss(conn, cid, "2026-01-01", "2026-12-31")
    assert pnl["total_income"] == 500000
    assert pnl["total_expense"] == 80000
    assert pnl["net_income"] == 420000


def test_pnl_respects_date_range(conn):
    cid = seed_activity(conn, "sole_prop")
    # Q1 has the revenue but not the April expense.
    q1 = reports.profit_and_loss(conn, cid, "2026-01-01", "2026-03-31")
    assert q1["total_income"] == 500000
    assert q1["total_expense"] == 0
    assert q1["net_income"] == 500000


@pytest.mark.parametrize("entity",
                         ["sole_prop", "partnership", "s_corp", "c_corp"])
def test_balance_sheet_balances_for_every_entity_type(conn, entity):
    cid = seed_activity(conn, entity)
    bs = reports.balance_sheet(conn, cid, "2026-12-31")
    assert bs["balanced"]
    assert bs["total_assets"] == bs["total_liabilities_equity"]
    # Assets: cash 1420000. Equity: contribution 1000000 + net income 420000.
    assert bs["total_assets"] == 1420000
    assert bs["net_income"] == 420000


def test_void_excluded_from_every_report(conn):
    cid = ledger.create_company(conn, "Test Co", "sole_prop")
    cash = acct(conn, cid, "1000")
    revenue = acct(conn, cid, "4000")
    good = ledger.post_entry(conn, cid, "2026-01-10",
                             [(cash, 300000, 0), (revenue, 0, 300000)])
    bad = ledger.post_entry(conn, cid, "2026-01-11",
                            [(cash, 999900, 0), (revenue, 0, 999900)])
    ledger.void_entry(conn, bad)

    tb = reports.trial_balance(conn, cid, "2026-12-31")
    assert tb["total_debit"] == 300000
    pnl = reports.profit_and_loss(conn, cid, "2026-01-01", "2026-12-31")
    assert pnl["total_income"] == 300000
    ledg = reports.account_ledger(conn, cash)
    assert ledg["closing"] == 300000
    assert len(ledg["lines"]) == 1  # only the un-voided entry


def test_account_ledger_running_balance(conn):
    cid = seed_activity(conn, "sole_prop")
    cash = acct(conn, cid, "1000")
    result = reports.account_ledger(conn, cash)
    assert result["closing"] == 1420000
    assert result["lines"][-1]["balance"] == 1420000


def test_trial_balance_csv_exports_and_ties_out(conn):
    cid = seed_activity(conn, "sole_prop")
    company = ledger.get_company(conn, cid)
    body = export.trial_balance_csv(conn, company, "2026-12-31")
    lines = body.splitlines()
    assert lines[0] == "account_number,account_name,account_type,tax_line,debit,credit"
    assert any(line.startswith(",Total,") for line in lines)
    assert "Test Co" in body  # metadata block


def test_csv_export_aborts_when_out_of_balance(conn, monkeypatch):
    cid = seed_activity(conn, "sole_prop")
    company = ledger.get_company(conn, cid)
    monkeypatch.setattr(
        reports, "trial_balance",
        lambda *a, **k: {"balanced": False, "total_debit": 1,
                         "total_credit": 2, "rows": []},
    )
    with pytest.raises(ledger.PostingError, match="does not tie out"):
        export.trial_balance_csv(conn, company, "2026-12-31")
