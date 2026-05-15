# CoSpatia® Accounting Module

The accounting add-on for CoSpatia®. A full bookkeeping system whose defining
requirement is the export: every file it produces must be something a small
business's accountant can use directly to complete quarterly filings and the
annual state and federal returns without re-keying data.

This codebase is built against `CoSpatia_Accounting_Module_Spec.docx` in this
folder. That spec is the source of truth.

## What is built — Phase 1

The spec's build order (section 9.1) starts with the ledger, because nothing
else can be correct until the ledger is. Phase 1 is complete:

- **Company / entity profile.** Entity type (Sole Proprietor, Partnership,
  S Corporation, C Corporation) is the master switch — it loads the right chart
  of accounts and drives tax-line mapping.
- **Chart of accounts.** Eight account types, default chart per entity type,
  income and expense accounts pre-mapped to the entity's return.
- **General ledger.** Balanced journal entries; an unbalanced entry is rejected,
  never plugged. Posted entries are voided, never hard-deleted. A locked period
  rejects new postings.
- **Reports.** Trial Balance, Profit & Loss, Balance Sheet, and per-account
  General Ledger detail — all read-only views over the ledger.
- **Trial Balance CSV export.** The interchange file tax software imports;
  the export aborts rather than producing a file that does not tie out.

## What is not built yet

Phases 2–5 from the spec: accounts receivable and payable, bank reconciliation,
payroll data, fixed assets and depreciation, sales tax, the supporting report
set, the Year-End Accountant Package, the quarterly export, and onboarding
import. PDF and XLSX export, the cash/accrual reporting toggle (which only
diverges once AR/AP exists), the audit log, and multi-company switching also
follow. The schema already keys every record to a company so multi-company is a
later addition, not a migration.

## Running it

```
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python -m uvicorn app.main:app --host 127.0.0.1 --port 8770
```

Open http://127.0.0.1:8770/. The first screen sets up the company; pick the
entity type and the chart of accounts loads.

## Integrity rules (spec section 4)

Debits equal credits on every entry. The trial balance always nets to zero. The
balance sheet always balances. Money is stored as integer cents — rounding
happens only at display. Nothing posted is ever hard-deleted.

## Tax-year configuration

Tax-line numbers, the 1099 threshold, and wage bases change yearly. They belong
in dated configuration, re-verified against current IRS forms each tax year —
not hard-coded. The tax-line mapping currently in `app/charts.py` is
representative and follows the spec's section 5.2 table; verify it before any
export is used to prepare a real return.
