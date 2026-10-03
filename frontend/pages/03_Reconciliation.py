"""Reconciliation dashboard — Phase 9 financial controls UI."""
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

from frontend.components.charts import line_chart
from frontend.components.kpis import render_kpis
from frontend.components.styles import COLORS, inject_global_styles, render_brand
from frontend.components.tables import render_table
from frontend.services.api_client import APIError, get_api_client

st.set_page_config(page_title="FinSight · Reconciliation", layout="wide")
inject_global_styles()


def _payload(response: Any) -> Any:
    if isinstance(response, dict) and "data" in response:
        return response["data"]
    return response


def _frame(value: Any) -> pd.DataFrame:
    if isinstance(value, list):
        return pd.DataFrame(value)
    if isinstance(value, dict):
        return pd.DataFrame([value])
    return pd.DataFrame()


def _number(value: Any) -> str:
    if value is None or pd.isna(value):
        return "—"
    return f"{int(value):,}"


def _pct(value: Any) -> str:
    if value is None or pd.isna(value):
        return "—"
    return f"{float(value):.2f}%"


def _money(value: Any) -> str:
    if value is None or pd.isna(value):
        return "—"
    return f"₹{float(value) / 1_000_000:,.1f}M"


STATUS_LABELS = {
    "AMOUNT_MISMATCH": "Amount mismatch",
    "CURRENCY_MISMATCH": "Currency mismatch",
    "BUSINESS_UNIT_MISMATCH": "Business-unit mismatch",
    "DATE_MISMATCH": "Date mismatch",
    "MISSING_IN_LEDGER": "Missing in ledger",
    "MISSING_IN_SOURCE": "Missing in source",
    "DUPLICATE": "Duplicate",
    "INVALID": "Invalid",
    "MATCHED": "Matched",
}


@st.cache_data(ttl=60, show_spinner=False)
def load_summary() -> dict[str, Any]:
    client = get_api_client()
    return _payload(client.get("/api/v1/reconciliation/summary"))


@st.cache_data(ttl=60, show_spinner=False)
def load_daily() -> pd.DataFrame:
    return _frame(_payload(get_api_client().get("/api/v1/reconciliation/daily")))


@st.cache_data(ttl=60, show_spinner=False)
def load_business_units() -> pd.DataFrame:
    return _frame(_payload(get_api_client().get("/api/v1/reconciliation/by-business-unit")))


@st.cache_data(ttl=60, show_spinner=False)
def load_details(statuses: tuple[str, ...], limit: int) -> pd.DataFrame:
    params: dict[str, Any] = {"limit": limit}
    if statuses:
        params["status"] = list(statuses)
    return _frame(_payload(get_api_client().get("/api/v1/reconciliation/details", params)))


render_brand()
st.title("Reconciliation")
st.caption("Source-system transactions vs finance ledger")

try:
    summary = load_summary()
    daily = load_daily()
    by_bu = load_business_units()
except APIError as exc:
    st.error(f"Unable to load reconciliation data: {exc}")
    st.info("Make sure the FinSight FastAPI service is running and refresh the page.")
    st.stop()

summary = summary if isinstance(summary, dict) else {}

st.markdown('<div class="fs-section">Control health</div>', unsafe_allow_html=True)
render_kpis([
    {"label": "Match Rate", "value": _pct(summary.get("match_rate")), "delta": None},
    {"label": "Mismatched", "value": _number(summary.get("mismatched")), "delta": None},
    {"label": "Missing", "value": _number(summary.get("missing")), "delta": None},
    {"label": "Duplicates", "value": _number(summary.get("duplicates")), "delta": None},
    {"label": "Invalid", "value": _number(summary.get("invalid")), "delta": None},
])

st.caption(f"Total reconciliation records: {_number(summary.get('total_records'))}")
st.divider()

chart_col, bu_col = st.columns(2, gap="large")
with chart_col:
    st.subheader("Monthly Reconciliation")
    if not daily.empty and "date" in daily.columns:
        series_candidates = [c for c in ["matched", "mismatched", "missing", "duplicates", "invalid"] if c in daily.columns]
        if series_candidates:
            chart_df = daily[["date", *series_candidates]].copy()
            chart_df["date"] = pd.to_datetime(chart_df["date"], errors="coerce")
            chart_df = chart_df.dropna(subset=["date"])
            chart_df["period"] = chart_df["date"].dt.to_period("M").dt.to_timestamp()
            chart_df = chart_df.groupby("period", as_index=False)[series_candidates].sum()
            chart_df = chart_df.rename(columns={"period": "date"})
            rename = {c: c.replace("_", " ").title() for c in series_candidates}
            chart_df = chart_df.rename(columns=rename)
            fig = line_chart(chart_df, "date", list(rename.values()))
            fig.update_yaxes(title_text="Records")
            fig.update_xaxes(title_text=None)
            st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False})
        else:
            st.info("Daily reconciliation breakdown is not available.")
    else:
        st.info("No daily reconciliation data is available.")

with bu_col:
    st.subheader("Business Unit Control Breaks")
    if not by_bu.empty and {"business_unit", "breaks"}.issubset(by_bu.columns):
        display = by_bu[["business_unit", "breaks"]].copy().sort_values("breaks")
        fig = go.Figure(go.Bar(
            x=display["breaks"],
            y=display["business_unit"],
            orientation="h",
            marker_color=COLORS["red"],
        ))
        fig.update_layout(
            height=330,
            paper_bgcolor="rgba(0,0,0,0)",
            plot_bgcolor="rgba(0,0,0,0)",
            margin={"l": 8, "r": 8, "t": 12, "b": 42},
            font={"color": COLORS["black"], "family": "Arial, sans-serif"},
            showlegend=False,
        )
        fig.update_xaxes(title_text="Control breaks", gridcolor=COLORS["border"], zeroline=False)
        fig.update_yaxes(title_text=None, showgrid=False)
        st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False})
    else:
        st.info("Business-unit reconciliation data is not available.")

st.divider()
st.subheader("Reconciliation Detail")
st.caption("Use the status filter to focus the control-break population. The table is read-only.")

status_options = [
    "AMOUNT_MISMATCH",
    "CURRENCY_MISMATCH",
    "BUSINESS_UNIT_MISMATCH",
    "DATE_MISMATCH",
    "MISSING_IN_LEDGER",
    "MISSING_IN_SOURCE",
    "DUPLICATE",
    "INVALID",
    "MATCHED",
]
filter_col, limit_col = st.columns([3, 1])
with filter_col:
    selected_status_labels = st.multiselect("Status", [STATUS_LABELS[s] for s in status_options], default=[STATUS_LABELS[s] for s in status_options if s != "MATCHED"])
selected_statuses = [code for code, label in STATUS_LABELS.items() if label in selected_status_labels]
with limit_col:
    limit = st.selectbox("Rows", [100, 250, 500], index=1)

try:
    details = load_details(tuple(sorted(selected_statuses)), int(limit))
except APIError as exc:
    st.error(f"Unable to load reconciliation details: {exc}")
    details = pd.DataFrame()

if not details.empty:
    display = details.copy()
    if "status" in display.columns:
        display["status"] = display["status"].map(lambda value: STATUS_LABELS.get(str(value), str(value).replace("_", " ").title()))
    rename = {
        "transaction_id": "Transaction ID",
        "business_unit": "Business Unit",
        "status": "Status",
        "source_amount": "Source Amount",
        "ledger_amount": "Ledger Amount",
        "difference": "Difference",
        "exposure_inr": "Exposure (INR)",
        "reason": "Reason",
        "reconciled_at": "Reconciled At",
    }
    display = display[[c for c in rename if c in display.columns]].rename(columns=rename)
    for column in ["Source Amount", "Ledger Amount", "Difference"]:
        if column in display.columns:
            display[column] = display[column].map(lambda x: f"{float(x):,.2f}" if pd.notna(x) else "—")
    if "Exposure (INR)" in display.columns:
        display["Exposure (INR)"] = display["Exposure (INR)"].map(lambda x: _money(x) if pd.notna(x) else "—")
    render_table(display, height=430)
    st.download_button(
        "Download filtered reconciliation CSV",
        data=display.to_csv(index=False).encode("utf-8"),
        file_name="finsight_reconciliation.csv",
        mime="text/csv",
    )
else:
    st.info("No reconciliation records match the selected status filters.")
