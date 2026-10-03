"""Risk and anomaly dashboard — Phase 10."""
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

st.set_page_config(page_title="FinSight · Risk & Anomalies", layout="wide")
inject_global_styles()


def _payload(response: Any) -> Any:
    return response.get("data", response) if isinstance(response, dict) else response


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
    value = float(value)
    return f"{value:.1f}%" if abs(value) >= 1 else f"{value:.2f}%"


def _money(value: Any) -> str:
    if value is None or pd.isna(value):
        return "—"
    return f"₹{float(value) / 1_000_000:,.1f}M"


@st.cache_data(ttl=60, show_spinner=False)
def load_summary() -> dict[str, Any]:
    return _payload(get_api_client().get("/api/v1/anomalies/summary"))


@st.cache_data(ttl=60, show_spinner=False)
def load_trend() -> pd.DataFrame:
    return _frame(_payload(get_api_client().get("/api/v1/anomalies/trend", {"grain": "month"})))


@st.cache_data(ttl=60, show_spinner=False)
def load_top(risk: str | None, limit: int) -> pd.DataFrame:
    params: dict[str, Any] = {"order_by": "amount", "limit": limit}
    if risk:
        params["risk"] = risk
    return _frame(_payload(get_api_client().get("/api/v1/anomalies/top", params)))


@st.cache_data(ttl=60, show_spinner=False)
def load_business_units() -> pd.DataFrame:
    return _frame(_payload(get_api_client().get("/api/v1/anomalies/by-business-unit")))


@st.cache_data(ttl=60, show_spinner=False)
def load_latest_run() -> dict[str, Any]:
    return _payload(get_api_client().get("/api/v1/anomalies/latest-run"))


render_brand()
st.title("Risk & Anomalies")
st.caption("Isolation Forest anomaly monitoring and transaction-level risk review")

try:
    summary = load_summary()
    trend = load_trend()
    by_bu = load_business_units()
    latest_run = load_latest_run()
except APIError as exc:
    st.error(f"Unable to load risk data: {exc}")
    st.info("Make sure the FinSight FastAPI service is running and refresh the page.")
    st.stop()

summary = summary if isinstance(summary, dict) else {}

# Support the existing anomaly-service field names while keeping the UI resilient.
total = summary.get("anomalies", summary.get("total_anomalies", summary.get("anomalies_detected", summary.get("total", 0))))
high = summary.get("high", summary.get("high_risk", 0))
medium = summary.get("medium", summary.get("medium_risk", 0))
rate = summary.get("anomaly_rate_pct", summary.get("anomaly_rate", summary.get("rate")))

render_kpis([
    {"label": "Anomalies Detected", "value": _number(total), "delta": None},
    {"label": "High Risk", "value": _number(high), "delta": None},
    {"label": "Medium Risk", "value": _number(medium), "delta": None},
    {"label": "Anomaly Rate", "value": _pct(rate), "delta": None},
])

st.divider()

trend_col, bu_col = st.columns(2, gap="large")
with trend_col:
    st.subheader("Anomaly Trend")
    if not trend.empty:
        x_col = "period" if "period" in trend.columns else "date" if "date" in trend.columns else None
        y_candidates = [c for c in ["anomalies", "anomaly_count", "count", "total_anomalies"] if c in trend.columns]
        y_col = y_candidates[0] if y_candidates else None
        if x_col and y_col:
            fig = go.Figure(go.Scatter(
                x=trend[x_col], y=trend[y_col], mode="lines+markers",
                line={"color": COLORS["red"], "width": 2},
                marker={"color": COLORS["red"], "size": 6},
            ))
            fig.update_layout(height=330, paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                              margin={"l": 8, "r": 8, "t": 12, "b": 42},
                              font={"color": COLORS["black"], "family": "Arial, sans-serif"}, showlegend=False)
            fig.update_xaxes(title_text=None, gridcolor=COLORS["border"])
            fig.update_yaxes(title_text="Anomalies", gridcolor=COLORS["border"], zeroline=False)
            st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False})
        else:
            st.info("Anomaly trend fields are not available.")
    else:
        st.info("No anomaly trend data is available.")

with bu_col:
    st.subheader("Anomalies by Business Unit")
    if not by_bu.empty:
        name_col = "business_unit" if "business_unit" in by_bu.columns else None
        count_candidates = [c for c in ["anomalies", "anomaly_count", "count", "total_anomalies"] if c in by_bu.columns]
        count_col = count_candidates[0] if count_candidates else None
        if name_col and count_col:
            plot = by_bu[[name_col, count_col]].sort_values(count_col)
            fig = go.Figure(go.Bar(x=plot[count_col], y=plot[name_col], orientation="h", marker_color=COLORS["yellow"]))
            fig.update_layout(height=330, paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                              margin={"l": 8, "r": 8, "t": 12, "b": 42},
                              font={"color": COLORS["black"], "family": "Arial, sans-serif"}, showlegend=False)
            fig.update_xaxes(title_text="Anomalies", gridcolor=COLORS["border"], zeroline=False)
            fig.update_yaxes(title_text=None, showgrid=False)
            st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False})
        else:
            st.info("Business-unit anomaly fields are not available.")
    else:
        st.info("No business-unit anomaly data is available.")

st.divider()
st.subheader("Transaction Risk Review")
st.caption("Read-only view of the highest-value detected anomalies.")

risk_col, rows_col = st.columns([2, 1])
with risk_col:
    selected_risk = st.selectbox("Risk level", ["All", "HIGH", "MEDIUM", "LOW"], index=0)
with rows_col:
    limit = st.selectbox("Rows", [20, 50, 100], index=0)

try:
    top = load_top(None if selected_risk == "All" else selected_risk, int(limit))
except APIError as exc:
    st.error(f"Unable to load anomaly transactions: {exc}")
    top = pd.DataFrame()

if not top.empty:
    rename = {
        "transaction_id": "Transaction ID", "amount": "Amount", "anomaly_score": "Anomaly Score",
        "risk_level": "Risk Level", "business_unit": "Business Unit", "business_unit_id": "Business Unit",
        "product": "Product", "transaction_date": "Transaction Date", "detected_at": "Detected At",
    }
    display = top[[c for c in rename if c in top.columns]].rename(columns=rename)
    for column in ["Amount"]:
        if column in display.columns:
            display[column] = display[column].map(lambda x: _money(x) if pd.notna(x) else "—")
    if "Anomaly Score" in display.columns:
        display["Anomaly Score"] = display["Anomaly Score"].map(lambda x: f"{float(x):.3f}" if pd.notna(x) else "—")
    render_table(display, height=430)
    st.download_button("Download anomaly review CSV", data=display.to_csv(index=False).encode("utf-8"),
                       file_name="finsight_anomalies.csv", mime="text/csv")
else:
    st.info("No anomalies match the selected risk level.")

with st.expander("Model run information"):
    if latest_run:
        st.json(latest_run)
    else:
        st.info("No model-run metadata is available.")
