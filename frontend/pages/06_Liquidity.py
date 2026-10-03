"""Liquidity and treasury dashboard — Phase 10."""
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

from frontend.components.kpis import render_kpis
from frontend.components.styles import COLORS, inject_global_styles, render_brand
from frontend.components.tables import render_table
from frontend.services.api_client import APIError, get_api_client

st.set_page_config(page_title="FinSight · Liquidity", layout="wide")
inject_global_styles()


def _payload(response: Any) -> Any:
    return response.get("data", response) if isinstance(response, dict) else response


def _frame(value: Any) -> pd.DataFrame:
    if isinstance(value, list):
        return pd.DataFrame(value)
    if isinstance(value, dict):
        return pd.DataFrame([value])
    return pd.DataFrame()


def _money(value: Any) -> str:
    if value is None or pd.isna(value):
        return "—"
    amount = float(value)
    if abs(amount) >= 1_000_000_000:
        return f"₹{amount / 1_000_000_000:,.2f}B"
    return f"₹{amount / 1_000_000:,.1f}M"


@st.cache_data(ttl=60, show_spinner=False)
def load_summary() -> dict[str, Any]:
    return _payload(get_api_client().get("/api/v1/liquidity/summary"))


@st.cache_data(ttl=60, show_spinner=False)
def load_monthly() -> pd.DataFrame:
    return _frame(_payload(get_api_client().get("/api/v1/liquidity/monthly")))


@st.cache_data(ttl=60, show_spinner=False)
def load_daily() -> pd.DataFrame:
    return _frame(_payload(get_api_client().get("/api/v1/liquidity/daily")))


@st.cache_data(ttl=60, show_spinner=False)
def load_business_units() -> pd.DataFrame:
    return _frame(_payload(get_api_client().get("/api/v1/liquidity/business-units")))


render_brand()
st.title("Liquidity")
st.caption("Cash position, cash flows and funding requirements")

try:
    summary = load_summary()
    monthly = load_monthly()
    daily = load_daily()
    by_bu = load_business_units()
except APIError as exc:
    st.error(f"Unable to load liquidity data: {exc}")
    st.info("Make sure the FinSight FastAPI service is running and refresh the page.")
    st.stop()

summary = summary if isinstance(summary, dict) else {}

# Keep these mapped to the backend's canonical fields, with conservative fallbacks.
opening = summary.get("opening_cash", summary.get("opening_cash_inr"))
closing = summary.get("closing_cash", summary.get("closing_cash_inr"))
inflow = summary.get("cash_inflow", summary.get("inflows"))
outflow = summary.get("cash_outflow", summary.get("outflows"))
net = summary.get("net_cash_flow")
if net is None and inflow is not None and outflow is not None:
    net = float(inflow) - float(outflow)
funding = summary.get("peak_funding_requirement", summary.get("funding_requirement", summary.get("funding", summary.get("peak_funding"))))

render_kpis([
    {"label": "Opening Cash", "value": _money(opening), "delta": None},
    {"label": "Closing Cash", "value": _money(closing), "delta": None},
    {"label": "Inflows", "value": _money(inflow), "delta": None},
    {"label": "Outflows", "value": _money(outflow), "delta": None},
    {"label": "Net Cash Flow", "value": _money(net), "delta": None},
])

st.caption(f"Funding requirement / peak funding: {_money(funding)}")
st.divider()

flow_col, bu_col = st.columns(2, gap="large")
with flow_col:
    st.subheader("Monthly Cash Flow")
    frame = monthly if not monthly.empty else daily
    if not frame.empty:
        x_col = "month" if "month" in frame.columns else "date" if "date" in frame.columns else None
        candidates = [("cash_inflow", COLORS["green"], "Inflows"), ("cash_outflow", COLORS["red"], "Outflows")]
        available = [(c, color, label) for c, color, label in candidates if c in frame.columns]
        if x_col and available:
            fig = go.Figure()
            for c, color, label in available:
                fig.add_trace(go.Scatter(x=frame[x_col], y=frame[c] / 1_000_000, mode="lines+markers", name=label,
                                         line={"color": color, "width": 2}, marker={"color": color, "size": 5}))
            fig.update_layout(height=330, paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                              margin={"l": 8, "r": 8, "t": 12, "b": 42},
                              font={"color": COLORS["black"], "family": "Arial, sans-serif"}, legend={"orientation": "h", "y": 1.02, "x": 0})
            fig.update_xaxes(title_text=None, gridcolor=COLORS["border"])
            fig.update_yaxes(title_text="₹M", gridcolor=COLORS["border"], zeroline=False)
            st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False})
        else:
            st.info("Cash-flow fields are not available.")
    else:
        st.info("No liquidity trend data is available.")

with bu_col:
    st.subheader("Liquidity by Business Unit")
    if not by_bu.empty:
        name_col = "business_unit" if "business_unit" in by_bu.columns else None
        value_candidates = [c for c in ["net_cash_flow", "closing_cash", "cash_inflow"] if c in by_bu.columns]
        value_col = value_candidates[0] if value_candidates else None
        if name_col and value_col:
            plot = by_bu[[name_col, value_col]].sort_values(value_col)
            fig = go.Figure(go.Bar(x=plot[value_col] / 1_000_000, y=plot[name_col], orientation="h", marker_color=COLORS["yellow"]))
            fig.update_layout(height=330, paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                              margin={"l": 8, "r": 8, "t": 12, "b": 42},
                              font={"color": COLORS["black"], "family": "Arial, sans-serif"}, showlegend=False)
            fig.update_xaxes(title_text="₹M", gridcolor=COLORS["border"], zeroline=False)
            fig.update_yaxes(title_text=None, showgrid=False)
            st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False})
        else:
            st.info("Business-unit liquidity fields are not available.")
    else:
        st.info("No business-unit liquidity data is available.")

st.divider()
st.subheader("Liquidity Detail")
st.caption("Read-only reporting view of the latest available cash-flow records.")

if not daily.empty:
    display = daily.copy()
    rename = {
        "date": "Date", "month": "Month", "opening_cash": "Opening Cash", "cash_inflow": "Inflows",
        "cash_outflow": "Outflows", "closing_cash": "Closing Cash", "funding": "Funding",
        "net_cash_flow": "Net Cash Flow", "business_unit": "Business Unit",
    }
    display = display[[c for c in rename if c in display.columns]].rename(columns=rename)
    for column in ["Opening Cash", "Inflows", "Outflows", "Closing Cash", "Funding", "Net Cash Flow"]:
        if column in display.columns:
            display[column] = display[column].map(lambda x: _money(x) if pd.notna(x) else "—")
    render_table(display, height=420)
    st.download_button("Download liquidity CSV", data=display.to_csv(index=False).encode("utf-8"),
                       file_name="finsight_liquidity.csv", mime="text/csv")
else:
    st.info("No liquidity detail records are available.")
