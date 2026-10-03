"""Executive Overview dashboard (Phase 8)."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from frontend.components.charts import bar_chart, empty_figure, line_chart
from frontend.components.kpis import render_kpis
from frontend.components.styles import COLORS, inject_global_styles, render_brand, render_status
from frontend.services.api_client import APIError, get_api_client

st.set_page_config(page_title="FinSight · Overview", layout="wide")
inject_global_styles()


def _payload(response: Any) -> Any:
    """Unwrap the API's standard {data: ...} response envelope."""
    if isinstance(response, dict) and "data" in response:
        return response["data"]
    return response


def _money(value: Any, suffix: str = "M") -> str:
    if value is None or pd.isna(value):
        return "—"
    return f"₹{float(value) / 1_000_000:,.1f}{suffix}"


def _number(value: Any) -> str:
    if value is None or pd.isna(value):
        return "—"
    return f"{int(value):,}"


def _pct(value: Any) -> str | None:
    if value is None or pd.isna(value):
        return None
    return f"{float(value):+.1f}%"


def _frame(value: Any) -> pd.DataFrame:
    if isinstance(value, list):
        return pd.DataFrame(value)
    if isinstance(value, dict):
        return pd.DataFrame([value])
    return pd.DataFrame()


def _load_dashboard(client) -> dict[str, Any]:
    """Fetch only the read-only endpoints required by the Overview page."""
    return {
        "pnl": _payload(client.get("/api/v1/pnl/summary")),
        "pnl_trend": _frame(_payload(client.get("/api/v1/pnl/trend", {"grain": "month"}))),
        "pnl_bu": _frame(_payload(client.get("/api/v1/pnl/breakdown", {"dimension": "business_unit"}))),
        "liquidity": _payload(client.get("/api/v1/liquidity/summary")),
        "exceptions": _frame(_payload(client.get("/api/v1/exceptions/summary"))),
        "exception_trend": _frame(_payload(client.get("/api/v1/exceptions/trend", {"grain": "month"}))),
        "anomalies": _payload(client.get("/api/v1/anomalies/summary")),
    }


@st.cache_data(ttl=60, show_spinner=False)
def load_dashboard() -> dict[str, Any]:
    return _load_dashboard(get_api_client())


render_brand()
header_left, header_right = st.columns([4, 1])
with header_left:
    st.title("Executive Overview")
    st.caption("Financial performance, liquidity and control health at a glance")
with header_right:
    try:
        health = get_api_client().health()
        render_status(bool(health.get("status") == "ok"), "API")
    except APIError:
        render_status(False, "API")

try:
    dashboard = load_dashboard()
except APIError as exc:
    st.error(f"Unable to load the executive dashboard: {exc}")
    st.info("Start the FinSight FastAPI service and refresh this page.")
    st.stop()

pnl = dashboard["pnl"] if isinstance(dashboard["pnl"], dict) else {}
liquidity = dashboard["liquidity"] if isinstance(dashboard["liquidity"], dict) else {}
exceptions = dashboard["exceptions"]
anomalies = dashboard["anomalies"] if isinstance(dashboard["anomalies"], dict) else {}

critical = int(exceptions.loc[exceptions["severity"].eq("CRITICAL"), "count"].sum()) if not exceptions.empty and {"severity", "count"}.issubset(exceptions.columns) else 0
exception_total = int(exceptions["count"].sum()) if not exceptions.empty and "count" in exceptions.columns else 0

trend = dashboard["pnl_trend"].copy()


def _growth(column: str) -> str | None:
    if trend.empty or column not in trend.columns:
        return None
    values = trend[column].dropna()
    return _pct(values.iloc[-1]) if not values.empty else None


render_kpis([
    {"label": "Revenue", "value": _money(pnl.get("revenue")), "delta": _growth("revenue_mom_pct")},
    {"label": "Net P&L", "value": _money(pnl.get("net_pnl")), "delta": _growth("net_pnl_mom_pct")},
    {"label": "Cash Position", "value": _money(liquidity.get("closing_cash")), "delta": None},
    {"label": "Exceptions", "value": _number(exception_total), "delta": None},
])
st.caption(f"{critical:,} critical", unsafe_allow_html=False)

st.divider()

trend_col, bu_col = st.columns(2, gap="large")
with trend_col:
    st.subheader("P&L Trend")
    if not trend.empty and "period" in trend.columns:
        keep = [c for c in ["period", "net_pnl", "budget_pnl"] if c in trend.columns]
        chart_df = trend[keep].copy()
        rename = {"net_pnl": "Actual P&L", "budget_pnl": "Budget P&L"}
        chart_df = chart_df.rename(columns=rename)
        for column in ["Actual P&L", "Budget P&L"]:
            if column in chart_df.columns:
                chart_df[column] = chart_df[column] / 1_000_000
        fig = line_chart(chart_df, "period", [c for c in rename.values() if c in chart_df.columns])
        fig.update_yaxes(title_text="P&L (₹M)")
        fig.update_xaxes(title_text=None)
        st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False})
    else:
        st.plotly_chart(empty_figure("No P&L trend available"), use_container_width=True, config={"displayModeBar": False})

with bu_col:
    st.subheader("Business Unit Performance")
    if not dashboard["pnl_bu"].empty and {"business_unit", "net_pnl"}.issubset(dashboard["pnl_bu"].columns):
        chart_df = dashboard["pnl_bu"][["business_unit", "net_pnl"]].copy().sort_values("net_pnl")
        chart_df["net_pnl"] = chart_df["net_pnl"] / 1_000_000
        fig = bar_chart(chart_df, "net_pnl", "business_unit")
        fig.update_xaxes(title_text="Net P&L (₹M)")
        fig.update_yaxes(title_text=None)
        st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False})
    else:
        st.plotly_chart(empty_figure("No business-unit P&L available"), use_container_width=True, config={"displayModeBar": False})

exception_col, risk_col = st.columns(2, gap="large")
with exception_col:
    st.subheader("Open Exception Trend")
    exception_trend = dashboard["exception_trend"].copy()
    if not exception_trend.empty and {"period", "open_exceptions"}.issubset(exception_trend.columns):
        fig = line_chart(exception_trend, "period", "open_exceptions")
        fig.update_traces(line={"color": COLORS["red"], "width": 2.2}, marker={"color": COLORS["red"], "size": 6})
        fig.update_yaxes(title_text="Open exceptions")
        fig.update_xaxes(title_text=None)
        st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False})
    else:
        st.plotly_chart(empty_figure("No exception trend available"), use_container_width=True, config={"displayModeBar": False})

with risk_col:
    st.subheader("AI Risk Summary")
    total = int(anomalies.get("anomalies", 0) or 0)
    high = int(anomalies.get("high", 0) or 0)
    medium = int(anomalies.get("medium", 0) or 0)
    other = max(total - high - medium, 0)

    st.metric("Anomalies detected", _number(total))
    risk_a, risk_b = st.columns(2)
    risk_a.metric("High risk", _number(high))
    risk_b.metric("Medium risk", _number(medium))

    st.markdown(
        f"""
        <div class="fs-risk-bars">
            <div class="fs-risk-row"><span>High</span><div class="fs-risk-track"><div class="fs-risk-fill fs-risk-high" style="width:{(high / total * 100) if total else 0:.1f}%"></div></div><strong>{high:,}</strong></div>
            <div class="fs-risk-row"><span>Medium</span><div class="fs-risk-track"><div class="fs-risk-fill fs-risk-medium" style="width:{(medium / total * 100) if total else 0:.1f}%"></div></div><strong>{medium:,}</strong></div>
            <div class="fs-risk-row"><span>Other</span><div class="fs-risk-track"><div class="fs-risk-fill fs-risk-other" style="width:{(other / total * 100) if total else 0:.1f}%"></div></div><strong>{other:,}</strong></div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    st.caption(f"Anomaly rate: {anomalies.get('anomaly_rate_pct', '—')}% · Model: {anomalies.get('model_version', '—')}")

st.caption(
    f"Reporting period: {pnl.get('first_date', '—')} → {pnl.get('last_date', '—')} · "
    f"Data as of: {liquidity.get('as_of', '—')}"
)
