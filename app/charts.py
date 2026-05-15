"""Default chart of accounts per entity type, with tax-line mapping.

A new company is usable on day one because it ships a full chart already mapped
to the right return. The income and expense mapping below follows the
representative table in the module spec (section 5.2); line numbers are data
and must be re-checked against current IRS forms each tax year.

Balance-sheet accounts ship unmapped on purpose — Schedule L line assignment
depends on the entity and on size thresholds, so it is left for the accountant
to set. Unmapped accounts are flagged on export, not lost.
"""

# Federal return each entity type files.
ENTITY_FORM = {
    "sole_prop": "Schedule C",
    "partnership": "Form 1065",
    "s_corp": "Form 1120-S",
    "c_corp": "Form 1120",
}

# category -> (Schedule C, Form 1065, Form 1120-S, Form 1120) line label.
# Order of the tuple matches ENTITY_ORDER below.
ENTITY_ORDER = ("sole_prop", "partnership", "s_corp", "c_corp")

TAX_LINES = {
    "gross_receipts":    ("Line 1",   "Line 1a", "Line 1a", "Line 1a"),
    "returns":           ("Line 2",   "Line 1b", "Line 1b", "Line 1b"),
    "cogs":              ("Line 4",   "Line 2",  "Line 2",  "Line 2"),
    "advertising":       ("Line 8",   "Line 20", "Line 19", "Line 22"),
    "car_truck":         ("Line 9",   "Line 20", "Line 19", "Line 22"),
    "contract_labor":    ("Line 11",  "Line 20", "Line 19", "Line 22"),
    "depreciation":      ("Line 13",  "Line 16a","Line 14", "Line 20"),
    "insurance":         ("Line 15",  "Line 20", "Line 19", "Line 22"),
    "interest":          ("Line 16b", "Line 15", "Line 13", "Line 18"),
    "legal_professional":("Line 17",  "Line 20", "Line 19", "Line 22"),
    "office":            ("Line 18",  "Line 20", "Line 19", "Line 22"),
    "rent":              ("Line 20",  "Line 13", "Line 11", "Line 16"),
    "repairs":           ("Line 21",  "Line 14", "Line 12", "Line 17"),
    "supplies":          ("Line 22",  "Line 20", "Line 19", "Line 22"),
    "taxes_licenses":    ("Line 23",  "Line 14", "Line 12", "Line 17"),
    "travel":            ("Line 24a", "Line 20", "Line 19", "Line 22"),
    "meals":             ("Line 24b", "Line 20", "Line 19", "Line 22"),
    "utilities":         ("Line 25",  "Line 20", "Line 19", "Line 22"),
    "wages":             ("Line 26",  "Line 9",  "Line 8",  "Line 13"),
}


def tax_line(category, entity_type):
    """Resolve a category to a printable tax-line label for one entity."""
    if category is None:
        return None
    line = TAX_LINES[category][ENTITY_ORDER.index(entity_type)]
    return f"{ENTITY_FORM[entity_type]}, {line}"


# Shared accounts. (number, name, type, subtype, reconcilable, tax_category)
SHARED_ACCOUNTS = [
    ("1000", "Operating Checking",          "asset", "Bank",                 True,  None),
    ("1010", "Savings",                     "asset", "Bank",                 True,  None),
    ("1100", "Undeposited Funds",           "asset", "Other Current Asset",  False, None),
    ("1200", "Accounts Receivable",         "asset", "Accounts Receivable",  False, None),
    ("1400", "Prepaid Expenses",            "asset", "Other Current Asset",  False, None),
    ("1500", "Equipment",                   "asset", "Fixed Asset",          False, None),
    ("1600", "Accumulated Depreciation",    "asset", "Accumulated Depreciation", False, None),

    ("2000", "Accounts Payable",            "liability", "Accounts Payable",       False, None),
    ("2100", "Credit Card Payable",         "liability", "Credit Card",            True,  None),
    ("2200", "Payroll Liabilities",         "liability", "Other Current Liability",False, None),
    ("2300", "Sales Tax Payable",           "liability", "Other Current Liability",False, None),
    ("2700", "Notes Payable",               "liability", "Long-Term Liability",    False, None),

    ("4000", "Sales & Services Revenue",    "income", "Income", False, "gross_receipts"),
    ("4100", "Returns & Allowances",        "income", "Income", False, "returns"),

    ("5000", "Cost of Goods Sold",          "cogs", "Cost of Goods Sold", False, "cogs"),

    ("6000", "Advertising",                 "expense", "Expense", False, "advertising"),
    ("6010", "Car & Truck",                 "expense", "Expense", False, "car_truck"),
    ("6020", "Contract Labor",              "expense", "Expense", False, "contract_labor"),
    ("6030", "Depreciation Expense",        "expense", "Expense", False, "depreciation"),
    ("6040", "Insurance",                   "expense", "Expense", False, "insurance"),
    ("6050", "Interest Expense",            "expense", "Expense", False, "interest"),
    ("6060", "Legal & Professional Fees",   "expense", "Expense", False, "legal_professional"),
    ("6070", "Office Expense",              "expense", "Expense", False, "office"),
    ("6080", "Rent",                        "expense", "Expense", False, "rent"),
    ("6090", "Repairs & Maintenance",       "expense", "Expense", False, "repairs"),
    ("6100", "Supplies",                    "expense", "Expense", False, "supplies"),
    ("6110", "Taxes & Licenses",            "expense", "Expense", False, "taxes_licenses"),
    ("6120", "Travel",                      "expense", "Expense", False, "travel"),
    ("6130", "Meals",                       "expense", "Expense", False, "meals"),
    ("6140", "Utilities",                   "expense", "Expense", False, "utilities"),
    ("6150", "Wages & Salaries",            "expense", "Expense", False, "wages"),

    ("7000", "Other Income",                "other_income",  "Other Income",  False, None),
    ("8000", "Other Expense",               "other_expense", "Other Expense", False, None),
]

# Equity section, which is where entity type matters most.
EQUITY_ACCOUNTS = {
    "sole_prop": [
        ("3000", "Owner's Capital",          "equity", "Owner Equity"),
        ("3100", "Owner's Draws",            "equity", "Owner Equity"),
        ("3900", "Opening Balance Equity",   "equity", "Owner Equity"),
    ],
    "partnership": [
        ("3000", "Partners' Capital",        "equity", "Partner Capital"),
        ("3100", "Partner Draws",            "equity", "Partner Capital"),
        ("3900", "Opening Balance Equity",   "equity", "Partner Capital"),
    ],
    "s_corp": [
        ("3000", "Common Stock",             "equity", "Capital Stock"),
        ("3100", "Additional Paid-In Capital","equity", "Paid-In Capital"),
        ("3200", "Retained Earnings",        "equity", "Retained Earnings"),
        ("3300", "Shareholder Distributions","equity", "Distributions"),
        ("3900", "Opening Balance Equity",   "equity", "Retained Earnings"),
    ],
    "c_corp": [
        ("3000", "Common Stock",             "equity", "Capital Stock"),
        ("3100", "Additional Paid-In Capital","equity", "Paid-In Capital"),
        ("3200", "Retained Earnings",        "equity", "Retained Earnings"),
        ("3900", "Opening Balance Equity",   "equity", "Retained Earnings"),
    ],
}


def default_chart(entity_type):
    """The full starter chart of accounts for one entity type, tax-lines
    resolved. Returns a list of dicts ready to insert."""
    rows = []
    for number, name, atype, subtype, reconcilable, category in SHARED_ACCOUNTS:
        rows.append({
            "account_number": number, "name": name, "type": atype,
            "subtype": subtype, "reconcilable": reconcilable,
            "tax_line": tax_line(category, entity_type),
        })
    for number, name, atype, subtype in EQUITY_ACCOUNTS[entity_type]:
        rows.append({
            "account_number": number, "name": name, "type": atype,
            "subtype": subtype, "reconcilable": False, "tax_line": None,
        })
    rows.sort(key=lambda r: r["account_number"])
    return rows
