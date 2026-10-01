"""Filters shared by the analytics services (P&L and liquidity)."""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date

from backend.utils.calculations import period_bounds


@dataclass(frozen=True)
class Filters:
    """Every field is optional; None means "no restriction".

    start_date / end_date  inclusive date range (a month or a quarter is just a date range: see for_period)
    business_units         business-unit NAMES, e.g. ("Equities", "Treasury")
    products               product names, e.g. ("Equity",)      [P&L only]
    currency               reporting-currency code of the P&L rows (the P&L is stored in INR)   [P&L only]
    """
    start_date: date | None = None
    end_date: date | None = None
    business_units: tuple[str, ...] | None = None
    products: tuple[str, ...] | None = None
    currency: str | None = None

    def __post_init__(self):
        if self.start_date and self.end_date and self.start_date > self.end_date:
            raise ValueError(f"start_date {self.start_date} is after end_date {self.end_date}")
        for name in ("business_units", "products"):
            value = getattr(self, name)
            if isinstance(value, str):                       # tolerate a single string
                object.__setattr__(self, name, (value,))
            elif value is not None:
                object.__setattr__(self, name, tuple(value))

    @classmethod
    def for_period(cls, month: str | None = None, quarter: str | None = None, **kwargs) -> "Filters":
        start, end = period_bounds(month=month, quarter=quarter)
        return cls(start_date=start, end_date=end, **kwargs)

    def with_(self, **changes) -> "Filters":
        return replace(self, **changes)


def build_where(f: Filters, date_col: str, bu_name_col: str = "bu.name", product_col: str | None = None,
                currency_col: str | None = None) -> tuple[str, dict]:
    """Return (' WHERE ...' or '', params) using bound parameters only (no string interpolation of values)."""
    clauses, params = [], {}
    if f.start_date:
        clauses.append(f"{date_col} >= :start_date")
        params["start_date"] = f.start_date
    if f.end_date:
        clauses.append(f"{date_col} <= :end_date")
        params["end_date"] = f.end_date
    if f.business_units:
        clauses.append(f"{bu_name_col} = ANY(:business_units)")
        params["business_units"] = list(f.business_units)
    if f.products and product_col:
        clauses.append(f"{product_col} = ANY(:products)")
        params["products"] = list(f.products)
    if f.currency and currency_col:
        clauses.append(f"{currency_col} = :currency")
        params["currency"] = f.currency.upper()
    return (" WHERE " + " AND ".join(clauses)) if clauses else "", params
