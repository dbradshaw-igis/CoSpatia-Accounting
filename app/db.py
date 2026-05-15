import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "accounting.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS companies (
    id                    INTEGER PRIMARY KEY,
    legal_name            TEXT NOT NULL,
    dba                   TEXT,
    ein                   TEXT,
    entity_type           TEXT NOT NULL CHECK (entity_type IN
                              ('sole_prop','partnership','s_corp','c_corp')),
    tax_year_type         TEXT NOT NULL DEFAULT 'calendar'
                              CHECK (tax_year_type IN ('calendar','fiscal')),
    fiscal_year_end_month INTEGER NOT NULL DEFAULT 12,
    accounting_basis      TEXT NOT NULL DEFAULT 'accrual'
                              CHECK (accounting_basis IN ('cash','accrual')),
    state                 TEXT,
    locked_through        TEXT,
    created_at            TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS accounts (
    id                INTEGER PRIMARY KEY,
    company_id        INTEGER NOT NULL REFERENCES companies(id),
    account_number    TEXT NOT NULL,
    name              TEXT NOT NULL,
    type              TEXT NOT NULL CHECK (type IN
                          ('asset','liability','equity','income','cogs',
                           'expense','other_income','other_expense')),
    subtype           TEXT,
    tax_line          TEXT,
    parent_account_id INTEGER REFERENCES accounts(id),
    is_active         INTEGER NOT NULL DEFAULT 1,
    reconcilable      INTEGER NOT NULL DEFAULT 0,
    UNIQUE (company_id, account_number)
);

CREATE TABLE IF NOT EXISTS journal_entries (
    id             INTEGER PRIMARY KEY,
    company_id     INTEGER NOT NULL REFERENCES companies(id),
    entry_date     TEXT NOT NULL,
    entry_type     TEXT NOT NULL DEFAULT 'manual',
    reference      TEXT,
    memo           TEXT,
    created_by     TEXT NOT NULL DEFAULT 'owner',
    created_at     TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'posted'
                       CHECK (status IN ('posted','void')),
    reversal_of_id INTEGER REFERENCES journal_entries(id)
);

CREATE TABLE IF NOT EXISTS journal_lines (
    id           INTEGER PRIMARY KEY,
    entry_id     INTEGER NOT NULL REFERENCES journal_entries(id) ON DELETE CASCADE,
    account_id   INTEGER NOT NULL REFERENCES accounts(id),
    debit_cents  INTEGER NOT NULL DEFAULT 0,
    credit_cents INTEGER NOT NULL DEFAULT 0,
    line_memo    TEXT,
    CHECK (debit_cents >= 0 AND credit_cents >= 0),
    CHECK (NOT (debit_cents > 0 AND credit_cents > 0))
);

CREATE TABLE IF NOT EXISTS customers (
    id              INTEGER PRIMARY KEY,
    company_id      INTEGER NOT NULL REFERENCES companies(id),
    name            TEXT NOT NULL,
    email           TEXT,
    billing_address TEXT,
    terms_days      INTEGER NOT NULL DEFAULT 30,
    UNIQUE (company_id, name)
);

CREATE TABLE IF NOT EXISTS invoices (
    id               INTEGER PRIMARY KEY,
    company_id       INTEGER NOT NULL REFERENCES companies(id),
    customer_id      INTEGER NOT NULL REFERENCES customers(id),
    invoice_number   TEXT NOT NULL,
    invoice_date     TEXT NOT NULL,
    due_date         TEXT NOT NULL,
    memo             TEXT,
    status           TEXT NOT NULL DEFAULT 'open'
                         CHECK (status IN ('open','partial','paid','void')),
    journal_entry_id INTEGER REFERENCES journal_entries(id),
    created_at       TEXT NOT NULL,
    UNIQUE (company_id, invoice_number)
);

CREATE TABLE IF NOT EXISTS invoice_lines (
    id                INTEGER PRIMARY KEY,
    invoice_id        INTEGER NOT NULL REFERENCES invoices(id) ON DELETE CASCADE,
    description       TEXT,
    income_account_id INTEGER NOT NULL REFERENCES accounts(id),
    amount_cents      INTEGER NOT NULL CHECK (amount_cents > 0)
);

CREATE TABLE IF NOT EXISTS payments (
    id                 INTEGER PRIMARY KEY,
    company_id         INTEGER NOT NULL REFERENCES companies(id),
    customer_id        INTEGER NOT NULL REFERENCES customers(id),
    payment_date       TEXT NOT NULL,
    amount_cents       INTEGER NOT NULL CHECK (amount_cents > 0),
    deposit_account_id INTEGER NOT NULL REFERENCES accounts(id),
    reference          TEXT,
    memo               TEXT,
    journal_entry_id   INTEGER REFERENCES journal_entries(id),
    created_at         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS payment_applications (
    id           INTEGER PRIMARY KEY,
    payment_id   INTEGER NOT NULL REFERENCES payments(id) ON DELETE CASCADE,
    invoice_id   INTEGER NOT NULL REFERENCES invoices(id),
    amount_cents INTEGER NOT NULL CHECK (amount_cents > 0)
);

CREATE TABLE IF NOT EXISTS bank_import_batches (
    id         INTEGER PRIMARY KEY,
    company_id INTEGER NOT NULL REFERENCES companies(id),
    filename   TEXT,
    raw_csv    TEXT NOT NULL,
    committed  INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS bank_transactions (
    id               INTEGER PRIMARY KEY,
    company_id       INTEGER NOT NULL REFERENCES companies(id),
    bank_account_id  INTEGER NOT NULL REFERENCES accounts(id),
    batch_id         INTEGER REFERENCES bank_import_batches(id),
    txn_date         TEXT NOT NULL,
    description      TEXT,
    amount_cents     INTEGER NOT NULL,
    status           TEXT NOT NULL DEFAULT 'unreviewed'
                         CHECK (status IN ('unreviewed','posted','ignored')),
    journal_entry_id INTEGER REFERENCES journal_entries(id),
    created_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS vendors (
    id                         INTEGER PRIMARY KEY,
    company_id                 INTEGER NOT NULL REFERENCES companies(id),
    name                       TEXT NOT NULL,
    address                    TEXT,
    tin                        TEXT,
    is_1099                    INTEGER NOT NULL DEFAULT 0,
    box_1099                   TEXT,
    default_expense_account_id INTEGER REFERENCES accounts(id),
    terms_days                 INTEGER NOT NULL DEFAULT 30,
    UNIQUE (company_id, name)
);

CREATE TABLE IF NOT EXISTS bills (
    id               INTEGER PRIMARY KEY,
    company_id       INTEGER NOT NULL REFERENCES companies(id),
    vendor_id        INTEGER NOT NULL REFERENCES vendors(id),
    bill_number      TEXT,
    bill_date        TEXT NOT NULL,
    due_date         TEXT NOT NULL,
    memo             TEXT,
    status           TEXT NOT NULL DEFAULT 'open'
                         CHECK (status IN ('open','partial','paid','void')),
    journal_entry_id INTEGER REFERENCES journal_entries(id),
    created_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS bill_lines (
    id           INTEGER PRIMARY KEY,
    bill_id      INTEGER NOT NULL REFERENCES bills(id) ON DELETE CASCADE,
    description  TEXT,
    account_id   INTEGER NOT NULL REFERENCES accounts(id),
    amount_cents INTEGER NOT NULL CHECK (amount_cents > 0)
);

CREATE TABLE IF NOT EXISTS bill_payments (
    id                   INTEGER PRIMARY KEY,
    company_id           INTEGER NOT NULL REFERENCES companies(id),
    vendor_id            INTEGER NOT NULL REFERENCES vendors(id),
    payment_date         TEXT NOT NULL,
    amount_cents         INTEGER NOT NULL CHECK (amount_cents > 0),
    paid_from_account_id INTEGER NOT NULL REFERENCES accounts(id),
    payment_method       TEXT NOT NULL DEFAULT 'check'
                             CHECK (payment_method IN
                                    ('check','ach','cash','card')),
    reference            TEXT,
    memo                 TEXT,
    journal_entry_id     INTEGER REFERENCES journal_entries(id),
    created_at           TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS bill_payment_applications (
    id           INTEGER PRIMARY KEY,
    payment_id   INTEGER NOT NULL REFERENCES bill_payments(id) ON DELETE CASCADE,
    bill_id      INTEGER NOT NULL REFERENCES bills(id),
    amount_cents INTEGER NOT NULL CHECK (amount_cents > 0)
);

CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY,
    username      TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    display_name  TEXT,
    role          TEXT NOT NULL DEFAULT 'standard'
                      CHECK (role IN ('admin','standard')),
    active        INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_accounts_company ON accounts(company_id);
CREATE INDEX IF NOT EXISTS idx_entries_company  ON journal_entries(company_id);
CREATE INDEX IF NOT EXISTS idx_entries_date     ON journal_entries(entry_date);
CREATE INDEX IF NOT EXISTS idx_lines_entry      ON journal_lines(entry_id);
CREATE INDEX IF NOT EXISTS idx_lines_account    ON journal_lines(account_id);
CREATE INDEX IF NOT EXISTS idx_invoices_company ON invoices(company_id);
CREATE INDEX IF NOT EXISTS idx_invlines_invoice ON invoice_lines(invoice_id);
CREATE INDEX IF NOT EXISTS idx_payapp_invoice   ON payment_applications(invoice_id);
CREATE INDEX IF NOT EXISTS idx_banktxn_company  ON bank_transactions(company_id);
CREATE INDEX IF NOT EXISTS idx_bills_company    ON bills(company_id);
CREATE INDEX IF NOT EXISTS idx_billlines_bill   ON bill_lines(bill_id);
CREATE INDEX IF NOT EXISTS idx_billpay_company  ON bill_payments(company_id);
CREATE INDEX IF NOT EXISTS idx_billpayapp_bill  ON bill_payment_applications(bill_id);
"""


def get_conn(db_path=None):
    conn = sqlite3.connect(str(db_path or DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn):
    conn.executescript(SCHEMA)
    conn.commit()
