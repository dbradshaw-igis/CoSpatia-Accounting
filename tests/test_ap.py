"""Tests for accounts payable — bills and bill payments post balanced journal
entries, bill status follows what is paid, and the 1099 report counts the right
payments."""
import pytest

from app import ap, db, ledger, reports, taxconfig


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
    vendor = ap.create_vendor(conn, cid, "Acme Supplies", tin="12-3456789")
    return {"id": cid, "vendor": vendor,
            "expense": acct(conn, cid, "6000"),
            "ap": acct(conn, cid, "2000"),
            "bank": acct(conn, cid, "1000")}


def test_create_vendor(conn, company):
    vendors = ap.list_vendors(conn, company["id"])
    assert len(vendors) == 1
    assert vendors[0]["name"] == "Acme Supplies"


def test_bill_posts_to_ledger(conn, company):
    bill = ap.create_bill(conn, company["id"], company["vendor"],
                          "2026-02-01", "2026-03-03", "ACME-77",
                          [("Office paper", company["expense"], 45000)])
    detail = ap.get_bill(conn, bill)
    assert detail["total"] == 45000
    assert detail["bill"]["status"] == "open"
    # Expense debited, Accounts Payable credited.
    tb = reports.trial_balance(conn, company["id"], "2026-12-31")
    by_number = {r["account_number"]: r for r in tb["rows"]}
    assert by_number["6000"]["debit"] == 45000
    assert by_number["2000"]["credit"] == 45000


def test_bill_needs_a_line(conn, company):
    with pytest.raises(ledger.PostingError, match="at least one line"):
        ap.create_bill(conn, company["id"], company["vendor"],
                       "2026-02-01", "2026-03-03", "ACME-77", [])


def test_full_payment_marks_bill_paid(conn, company):
    bill = ap.create_bill(conn, company["id"], company["vendor"],
                          "2026-02-01", "2026-03-03", "ACME-77",
                          [("Supplies", company["expense"], 45000)])
    ap.pay_bills(conn, company["id"], company["vendor"], "2026-03-01",
                 company["bank"], "check", [(bill, 45000)])
    detail = ap.get_bill(conn, bill)
    assert detail["bill"]["status"] == "paid"
    assert detail["balance"] == 0
    tb = reports.trial_balance(conn, company["id"], "2026-12-31")
    by_number = {r["account_number"]: r for r in tb["rows"]}
    assert "2000" not in by_number  # AP nets to zero
    assert by_number["1000"]["credit"] == 45000  # cash paid out


def test_partial_payment_marks_bill_partial(conn, company):
    bill = ap.create_bill(conn, company["id"], company["vendor"],
                          "2026-02-01", "2026-03-03", "ACME-77",
                          [("Supplies", company["expense"], 45000)])
    ap.pay_bills(conn, company["id"], company["vendor"], "2026-03-01",
                 company["bank"], "ach", [(bill, 20000)])
    detail = ap.get_bill(conn, bill)
    assert detail["bill"]["status"] == "partial"
    assert detail["balance"] == 25000


def test_overpaying_a_bill_is_rejected(conn, company):
    bill = ap.create_bill(conn, company["id"], company["vendor"],
                          "2026-02-01", "2026-03-03", "ACME-77",
                          [("Supplies", company["expense"], 45000)])
    with pytest.raises(ledger.PostingError, match="balance"):
        ap.pay_bills(conn, company["id"], company["vendor"], "2026-03-01",
                     company["bank"], "check", [(bill, 50000)])


def test_ap_aging_ties_to_open_balance(conn, company):
    ap.create_bill(conn, company["id"], company["vendor"], "2026-01-05",
                   "2026-02-04", "B1", [("X", company["expense"], 30000)])
    ap.create_bill(conn, company["id"], company["vendor"], "2026-06-01",
                   "2026-07-01", "B2", [("Y", company["expense"], 20000)])
    aging = ap.ap_aging(conn, company["id"], "2026-12-31")
    assert aging["grand_total"] == 50000


def test_1099_report_counts_non_card_payments(conn, company):
    contractor = ap.create_vendor(conn, company["id"], "Jane Contractor",
                                  tin="98-7654321", is_1099=True,
                                  box_1099="1099-NEC")
    bill1 = ap.create_bill(conn, company["id"], contractor, "2026-03-01",
                           "2026-03-31", "JC-1",
                           [("Consulting", company["expense"], 300000)])
    bill2 = ap.create_bill(conn, company["id"], contractor, "2026-05-01",
                           "2026-05-31", "JC-2",
                           [("Consulting", company["expense"], 150000)])
    ap.pay_bills(conn, company["id"], contractor, "2026-04-01",
                 company["bank"], "check", [(bill1, 300000)])
    # A card payment must be left out — the card processor reports it.
    ap.pay_bills(conn, company["id"], contractor, "2026-06-01",
                 company["bank"], "card", [(bill2, 150000)])

    threshold = taxconfig.form_1099_nec_threshold_cents(2026)
    report = ap.vendor_1099_report(conn, company["id"], 2026, threshold)
    rows = {r["vendor"]["name"]: r for r in report["rows"]}
    assert rows["Jane Contractor"]["total"] == 300000  # card payment excluded
    assert rows["Jane Contractor"]["over_threshold"] is True
    # The non-1099 vendor from the fixture is not in the report.
    assert "Acme Supplies" not in rows


def test_1099_report_flags_a_missing_tin(conn, company):
    contractor = ap.create_vendor(conn, company["id"], "No-TIN LLC",
                                  is_1099=True)
    bill = ap.create_bill(conn, company["id"], contractor, "2026-03-01",
                          "2026-03-31", "NT-1",
                          [("Work", company["expense"], 500000)])
    ap.pay_bills(conn, company["id"], contractor, "2026-04-01",
                 company["bank"], "check", [(bill, 500000)])
    report = ap.vendor_1099_report(conn, company["id"], 2026,
                                   taxconfig.form_1099_nec_threshold_cents(2026))
    assert report["missing_tin_count"] == 1


def test_1099_threshold_is_dated_config():
    assert taxconfig.form_1099_nec_threshold_cents(2026) == 200000
    assert taxconfig.form_1099_nec_threshold_cents(2025) == 60000
    # A year past the table falls back to the latest defined year.
    assert taxconfig.form_1099_nec_threshold_cents(2030) == 200000
