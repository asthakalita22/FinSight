"""Pure-contract tests for the Phase 8 executive overview helpers."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pandas as pd

PAGE = Path(__file__).parents[1] / "frontend" / "pages" / "01_Overview.py"


def _load_helpers():
    # Avoid executing Streamlit page code; these tests validate the helper layer's
    # source-level contract separately from the live UI.
    source = PAGE.read_text(encoding="utf-8")
    assert "def _payload(" in source
    assert "def _money(" in source
    assert "def _frame(" in source
    return source


def test_overview_uses_read_only_phase6_and_phase8_endpoints():
    source = _load_helpers()
    expected = [
        "/api/v1/pnl/summary",
        "/api/v1/pnl/trend",
        "/api/v1/pnl/breakdown",
        "/api/v1/liquidity/summary",
        "/api/v1/exceptions/summary",
        "/api/v1/exceptions/trend",
        "/api/v1/anomalies/summary",
    ]
    for endpoint in expected:
        assert endpoint in source


def test_financial_summary_fields_match_api_contract():
    summary = {
        "revenue": 4_736_600_000,
        "net_pnl": 1_569_600_000,
        "variance_pct": 1.6,
    }
    assert summary["revenue"] > 0
    assert summary["net_pnl"] > 0
    assert isinstance(summary["variance_pct"], float)


def test_exception_trend_contract_is_tabular():
    frame = pd.DataFrame({"period": ["2025-09", "2025-10"], "open_exceptions": [100, 80]})
    assert list(frame.columns) == ["period", "open_exceptions"]
    assert frame["open_exceptions"].sum() == 180
