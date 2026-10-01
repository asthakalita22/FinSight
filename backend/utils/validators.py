"""Input validation for analytics filters: fail early with a helpful message instead of returning silent zeros."""
from sqlalchemy import text

from backend.utils.filters import Filters


def _values(engine, sql: str) -> list[str]:
    with engine.connect() as conn:
        return sorted(str(r[0]).strip() for r in conn.execute(text(sql)) if r[0] is not None)


def validate_filters(engine, f: Filters, check_products: bool = True) -> None:
    """Raise ValueError when a business unit / product / currency does not exist."""
    if f.business_units:
        valid = _values(engine, "SELECT name FROM business_units")
        unknown = [b for b in f.business_units if b not in valid]
        if unknown:
            raise ValueError(f"Unknown business unit(s): {unknown}. Valid values: {valid}")
    if f.products and check_products:
        valid = _values(engine, "SELECT DISTINCT product FROM pnl")
        unknown = [p for p in f.products if p not in valid]
        if unknown:
            raise ValueError(f"Unknown product(s): {unknown}. Valid values: {valid}")
    if f.currency:
        valid = _values(engine, "SELECT DISTINCT currency FROM pnl")
        if f.currency.upper() not in valid:
            raise ValueError(f"Unknown currency '{f.currency}'. The P&L is stored in: {valid}")
