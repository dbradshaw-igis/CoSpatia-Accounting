"""Tax-year configuration (spec section 8.6).

Thresholds and similar figures change yearly. They live here as dated data, not
scattered through the code, so rolling the module to a new tax year is a review
of this file against current IRS forms — not a code change.
"""

TAX_YEARS = {
    2025: {
        # 1099-NEC reporting threshold for the 2025 tax year.
        "form_1099_nec_threshold_cents": 60000,    # $600
    },
    2026: {
        # Raised to $2,000 for payments made in 2026 (One Big Beautiful Bill
        # Act); inflation-indexed from 2027 — re-verify before rolling forward.
        "form_1099_nec_threshold_cents": 200000,   # $2,000
    },
}


def for_year(year):
    """Configuration for a tax year. For a year with no entry, the nearest
    earlier year is used; for a year before the earliest entry, the earliest."""
    year = int(year)
    if year in TAX_YEARS:
        return TAX_YEARS[year]
    earlier = [y for y in TAX_YEARS if y <= year]
    return TAX_YEARS[max(earlier)] if earlier else TAX_YEARS[min(TAX_YEARS)]


def form_1099_nec_threshold_cents(year):
    return for_year(year)["form_1099_nec_threshold_cents"]
