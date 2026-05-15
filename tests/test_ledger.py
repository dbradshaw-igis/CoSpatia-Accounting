"""Tests for the ledger engine — company setup, the default chart, and the
double-entry integrity rules from spec section 4."""
import pytest

from app import charts, db, ledger


@pytest.fixture
def conn():
    c = db.get_conn(":memory:")
    db.init_db(c)
    yield c
    c.close()


def company(conn, entity_type="sole_prop", **kw):
    return ledger.create_company(conn, "Test Co", entity_type, **kw)


def acct(conn, company_id, number):
    return conn.execute(
        "SELECT id FROM accounts WHERE company_id = ? AND account_number = ?",
        (company_id, number),
    ).fetchone()["id"]


def test_company_creation_loads_chart(conn):
    cid = company(conn)
    rows = ledger.list_accounts(conn, cid)
    assert len(rows) == len(charts.default_chart("sole_prop"))


def test_unknown_entity_type_rejected(conn):
    with pytest.raises(ledger.PostingError):
        ledger.create_company(conn, "Bad Co", "llc")


def test_chart_tax_lines_differ_by_entity(conn):
    sole = company(conn, "sole_prop")
    scorp = company(conn, "s_corp")

    def advertising_tax_line(cid):
        return conn.execute(
            "SELECT tax_line FROM accounts WHERE company_id = ? "
            "AND account_number = '6000'", (cid,),
        ).fetchone()["tax_line"]

    assert advertising_tax_line(sole) == "Schedule C, Line 8"
    assert advertising_tax_line(scorp) == "Form 1120-S, Line 19"


def test_entity_specific_equity_accounts(conn):
    scorp = company(conn, "s_corp")
    names = {a["name"] for a in ledger.list_accounts(conn, scorp)
             if a["type"] == "equity"}
    assert "Common Stock" in names
    assert "Shareholder Distributions" in names


def test_to_cents_parses_money():
    assert ledger.to_cents("1,234.56") == 123456
    assert ledger.to_cents("$80") == 8000
    assert ledger.to_cents("") == 0


def test_to_cents_rejects_garbage():
    with pytest.raises(ledger.PostingError):
        ledger.to_cents("not money")


def test_balanced_entry_posts(conn):
    cid = company(conn)
    cash, rev = acct(conn, cid, "1000"), acct(conn, cid, "4000")
    entry_id = ledger.post_entry(
        conn, cid, "2026-01-15",
        [(cash, 150000, 0), (rev, 0, 150000)],
        memo="Invoice paid", reference="INV-1001",
    )
    assert entry_id == 1


def test_unbalanced_entry_is_rejected(conn):
    cid = company(conn)
    cash, rev = acct(conn, cid, "1000"), acct(conn, cid, "4000")
    with pytest.raises(ledger.PostingError, match="out of balance"):
        ledger.post_entry(conn, cid, "2026-01-15",
                          [(cash, 150000, 0), (rev, 0, 100000)])


def test_one_sided_line_is_rejected(conn):
    cid = company(conn)
    cash, rev = acct(conn, cid, "1000"), acct(conn, cid, "4000")
    with pytest.raises(ledger.PostingError, match="one-sided"):
        ledger.post_entry(conn, cid, "2026-01-15",
                          [(cash, 150000, 50000), (rev, 0, 100000)])


def test_single_line_entry_is_rejected(conn):
    cid = company(conn)
    cash = acct(conn, cid, "1000")
    with pytest.raises(ledger.PostingError, match="at least two"):
        ledger.post_entry(conn, cid, "2026-01-15", [(cash, 150000, 0)])


def test_locked_period_rejects_posting(conn):
    cid = company(conn)
    conn.execute("UPDATE companies SET locked_through = '2026-03-31' "
                 "WHERE id = ?", (cid,))
    conn.commit()
    cash, rev = acct(conn, cid, "1000"), acct(conn, cid, "4000")
    with pytest.raises(ledger.PostingError, match="locked"):
        ledger.post_entry(conn, cid, "2026-02-01",
                          [(cash, 1000, 0), (rev, 0, 1000)])
    # A date in the open period still posts.
    assert ledger.post_entry(conn, cid, "2026-04-01",
                             [(cash, 1000, 0), (rev, 0, 1000)])


def test_void_keeps_the_row(conn):
    cid = company(conn)
    cash, rev = acct(conn, cid, "1000"), acct(conn, cid, "4000")
    eid = ledger.post_entry(conn, cid, "2026-01-15",
                            [(cash, 1000, 0), (rev, 0, 1000)])
    ledger.void_entry(conn, eid)
    row = conn.execute("SELECT status FROM journal_entries WHERE id = ?",
                       (eid,)).fetchone()
    assert row["status"] == "void"
    with pytest.raises(ledger.PostingError, match="already void"):
        ledger.void_entry(conn, eid)
