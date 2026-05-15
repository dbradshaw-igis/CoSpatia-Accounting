"""Account-type metadata. Type determines which financial statement an account
lands on and the sign convention the ledger uses for it."""

# The eight account types, in the order a chart of accounts is read.
TYPE_ORDER = (
    "asset", "liability", "equity",
    "income", "cogs", "expense", "other_income", "other_expense",
)

TYPE_LABELS = {
    "asset": "Assets",
    "liability": "Liabilities",
    "equity": "Equity",
    "income": "Income",
    "cogs": "Cost of Goods Sold",
    "expense": "Expenses",
    "other_income": "Other Income",
    "other_expense": "Other Expenses",
}

# Accounts whose balance grows on the debit side. Everything else is
# credit-normal. This is the single source of the sign convention.
DEBIT_NORMAL = ("asset", "cogs", "expense", "other_expense")

ENTITY_LABELS = {
    "sole_prop": "Sole Proprietor / Single-Member LLC",
    "partnership": "Partnership / Multi-Member LLC",
    "s_corp": "S Corporation",
    "c_corp": "C Corporation",
}


def is_debit_normal(account_type):
    return account_type in DEBIT_NORMAL


def natural_balance(account_type, debit_cents, credit_cents):
    """The amount as it reads on a financial statement: positive when the
    account carries its normal balance. A debit-normal account with a debit
    balance is positive; a credit-normal account with a credit balance is
    positive."""
    net = debit_cents - credit_cents
    return net if is_debit_normal(account_type) else -net
