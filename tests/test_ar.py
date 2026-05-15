"""Tests for accounts receivable — invoices and payments received post balanced
journal entries, and an invoice's status follows what has been paid."""
import pytest

from app import ar, db, ledger, reports


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
    customer_id = ar.create_customer(conn, cid, "Acme Corp", terms_days=30)
    return {"id": cid, "customer": customer_id,
            "income": acct(conn, cid, "4000"),
            "ar": acct(conn, cid, "1200"),
            "bank": acct(conn, cid, "1000")}


def test_create_customer(conn, company):
    customers = ar.list_customers(conn, company["id"])
    assert len(customers) == 1
    assert customers[0]["name"] == "Acme Corp"


def test_invoice_numbering_follows_year_month(conn, company):
    first = ar.next_invoice_number(conn, company["id"], "2026-05")
    assert first == "2026-05-01"
    ar.create_invoice(conn, company["id"], company["customer"],
                      "2026-05-04", "2026-06-03", first,
                      [("Work", company["income"], 100000)])
    assert ar.next_invoice_number(conn, company["id"], "2026-05") == "2026-05-02"
    # A new month restarts the sequence at 01.
    assert ar.next_invoice_number(conn, company["id"], "2026-06") == "2026-06-01"


def test_invoice_posts_to_ledger(conn, company):
    inv = ar.create_invoice(
        conn, company["id"], company["customer"], "2026-02-01", "2026-03-03",
        "INV-1001", [("Consulting", company["income"], 250000)])
    detail = ar.get_invoice(conn, inv)
    assert detail["total"] == 250000
    assert detail["invoice"]["status"] == "open"
    # AR debited, income credited.
    tb = reports.trial_balance(conn, company["id"], "2026-12-31")
    by_number = {r["account_number"]: r for r in tb["rows"]}
    assert by_number["1200"]["debit"] == 250000
    assert by_number["4000"]["credit"] == 250000


def test_invoice_needs_a_line(conn, company):
    with pytest.raises(ledger.PostingError, match="at least one line"):
        ar.create_invoice(conn, company["id"], company["customer"],
                          "2026-02-01", "2026-03-03", "INV-1001", [])


def test_full_payment_marks_invoice_paid(conn, company):
    inv = ar.create_invoice(
        conn, company["id"], company["customer"], "2026-02-01", "2026-03-03",
        "INV-1001", [("Consulting", company["income"], 250000)])
    ar.receive_payment(conn, company["id"], company["customer"],
                       "2026-03-01", company["bank"], [(inv, 250000)])
    detail = ar.get_invoice(conn, inv)
    assert detail["invoice"]["status"] == "paid"
    assert detail["balance"] == 0
    # Cash up, AR back to zero.
    tb = reports.trial_balance(conn, company["id"], "2026-12-31")
    by_number = {r["account_number"]: r for r in tb["rows"]}
    assert by_number["1000"]["debit"] == 250000
    assert "1200" not in by_number  # AR nets to zero


def test_partial_payment_marks_invoice_partial(conn, company):
    inv = ar.create_invoice(
        conn, company["id"], company["customer"], "2026-02-01", "2026-03-03",
        "INV-1001", [("Consulting", company["income"], 250000)])
    ar.receive_payment(conn, company["id"], company["customer"],
                       "2026-03-01", company["bank"], [(inv, 100000)])
    detail = ar.get_invoice(conn, inv)
    assert detail["invoice"]["status"] == "partial"
    assert detail["balance"] == 150000


def test_overpayment_is_rejected(conn, company):
    inv = ar.create_invoice(
        conn, company["id"], company["customer"], "2026-02-01", "2026-03-03",
        "INV-1001", [("Consulting", company["income"], 250000)])
    with pytest.raises(ledger.PostingError, match="balance"):
        ar.receive_payment(conn, company["id"], company["customer"],
                           "2026-03-01", company["bank"], [(inv, 300000)])


def test_payment_against_wrong_customer_rejected(conn, company):
    inv = ar.create_invoice(
        conn, company["id"], company["customer"], "2026-02-01", "2026-03-03",
        "INV-1001", [("Consulting", company["income"], 250000)])
    other = ar.create_customer(conn, company["id"], "Other Inc")
    with pytest.raises(ledger.PostingError, match="not this customer"):
        ar.receive_payment(conn, company["id"], other,
                           "2026-03-01", company["bank"], [(inv, 250000)])


def test_editing_an_invoice_reposts_it(conn, company):
    inv = ar.create_invoice(
        conn, company["id"], company["customer"], "2026-02-01", "2026-03-03",
        "INV-1001", [("Consulting", company["income"], 250000)])
    old_je = ar.get_invoice(conn, inv)["invoice"]["journal_entry_id"]

    ar.update_invoice(
        conn, inv, company["customer"], "2026-02-01", "2026-03-03",
        "INV-1001", [("Consulting", company["income"], 400000)])

    detail = ar.get_invoice(conn, inv)
    assert detail["total"] == 400000
    new_je = detail["invoice"]["journal_entry_id"]
    assert new_je != old_je
    # The original entry is voided, not deleted — the audit trail keeps it.
    old = conn.execute("SELECT status FROM journal_entries WHERE id = ?",
                       (old_je,)).fetchone()
    assert old["status"] == "void"
    # The trial balance shows the corrected amount only.
    tb = reports.trial_balance(conn, company["id"], "2026-12-31")
    by_number = {r["account_number"]: r for r in tb["rows"]}
    assert by_number["1200"]["debit"] == 400000
    assert tb["balanced"]


def test_editing_is_blocked_after_a_payment(conn, company):
    inv = ar.create_invoice(
        conn, company["id"], company["customer"], "2026-02-01", "2026-03-03",
        "INV-1001", [("Consulting", company["income"], 250000)])
    ar.receive_payment(conn, company["id"], company["customer"],
                       "2026-03-01", company["bank"], [(inv, 100000)])
    with pytest.raises(ledger.PostingError, match="payment applied"):
        ar.update_invoice(conn, inv, company["customer"], "2026-02-01",
                          "2026-03-03", "INV-1001",
                          [("Consulting", company["income"], 400000)])


def test_open_invoices_excludes_paid(conn, company):
    inv1 = ar.create_invoice(
        conn, company["id"], company["customer"], "2026-02-01", "2026-03-03",
        "INV-1001", [("Work", company["income"], 100000)])
    ar.create_invoice(
        conn, company["id"], company["customer"], "2026-02-05", "2026-03-07",
        "INV-1002", [("Work", company["income"], 200000)])
    ar.receive_payment(conn, company["id"], company["customer"],
                       "2026-03-01", company["bank"], [(inv1, 100000)])
    still_open = ar.open_invoices(conn, company["customer"])
    assert len(still_open) == 1
    assert still_open[0]["invoice"]["invoice_number"] == "INV-1002"
