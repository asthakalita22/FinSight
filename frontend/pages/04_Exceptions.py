"""Exceptions dashboard — Phase 9 financial controls UI."""
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

st.set_page_config(page_title="FinSight · Exceptions", layout="wide")
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


def _money(value: Any) -> str:
    if value is None or pd.isna(value):
        return "—"
    return f"₹{float(value) / 1_000_000:,.1f}M"


@st.cache_data(ttl=60, show_spinner=False)
def load_summary() -> pd.DataFrame:
    return _frame(_payload(get_api_client().get("/api/v1/exceptions/summary")))


@st.cache_data(ttl=60, show_spinner=False)
def load_categories() -> pd.DataFrame:
    return _frame(_payload(get_api_client().get("/api/v1/exceptions/by-category")))


@st.cache_data(ttl=60, show_spinner=False)
def load_recent(severity: tuple[str, ...], status: tuple[str, ...], category: tuple[str, ...], limit: int) -> pd.DataFrame:
    params: dict[str, Any] = {"limit": limit}
    if severity:
        params["severity"] = list(severity)
    if status:
        params["status"] = list(status)
    if category:
        params["category"] = list(category)
    return _frame(_payload(get_api_client().get("/api/v1/exceptions/recent", params)))


render_brand()
st.title("Exceptions & Controls")
st.caption("Automated finance-control exceptions and review queue")

try:
    summary = load_summary()
    categories = load_categories()
except APIError as exc:
    st.error(f"Unable to load exception data: {exc}")
    st.info("Make sure the FinSight FastAPI service is running and refresh the page.")
    st.stop()

critical = int(summary.loc[summary["severity"].eq("CRITICAL"), "count"].sum()) if not summary.empty else 0
high = int(summary.loc[summary["severity"].eq("HIGH"), "count"].sum()) if not summary.empty else 0
total = int(summary["count"].sum()) if not summary.empty and "count" in summary.columns else 0
exposure = float(summary["exposure_inr"].sum()) if not summary.empty and "exposure_inr" in summary.columns else 0

st.markdown('<div class="fs-section">Control queue</div>', unsafe_allow_html=True)
render_kpis([
    {"label": "Total Exceptions", "value": _number(total), "delta": None},
    {"label": "Critical", "value": _number(critical), "delta": None},
    {"label": "High", "value": _number(high), "delta": None},
    {"label": "Exposure", "value": _money(exposure), "delta": None},
])

st.divider()

chart_col, category_col = st.columns(2, gap="large")
with chart_col:
    st.subheader("Severity Distribution")
    if not summary.empty and {"severity", "count"}.issubset(summary.columns):
        severity_order = ["CRITICAL", "HIGH", "MEDIUM", "LOW"]
        plot = summary.copy()
        plot["severity"] = pd.Categorical(plot["severity"], severity_order, ordered=True)
        plot = plot.sort_values("severity")
        colors = {
            "CRITICAL": COLORS["red"],
            "HIGH": "#B77A55",
            "MEDIUM": COLORS["yellow"],
            "LOW": COLORS["slate"],
        }
        fig = go.Figure(go.Bar(
            x=plot["severity"],
            y=plot["count"],
            marker_color=[colors.get(str(x), COLORS["slate"]) for x in plot["severity"]],
        ))
        fig.update_layout(
            height=330,
            paper_bgcolor="rgba(0,0,0,0)",
            plot_bgcolor="rgba(0,0,0,0)",
            margin={"l": 8, "r": 8, "t": 12, "b": 42},
            font={"color": COLORS["black"], "family": "Arial, sans-serif"},
            showlegend=False,
        )
        fig.update_xaxes(title_text=None, showgrid=False)
        fig.update_yaxes(title_text="Exceptions", gridcolor=COLORS["border"], zeroline=False)
        st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False})
    else:
        st.info("No severity summary is available.")

with category_col:
    st.subheader("Exceptions by Category")
    if not categories.empty and {"category", "count"}.issubset(categories.columns):
        plot = categories.groupby("category", as_index=False)["count"].sum().sort_values("count")
        fig = go.Figure(go.Bar(
            x=plot["count"],
            y=plot["category"],
            orientation="h",
            marker_color=COLORS["yellow"],
        ))
        fig.update_layout(
            height=330,
            paper_bgcolor="rgba(0,0,0,0)",
            plot_bgcolor="rgba(0,0,0,0)",
            margin={"l": 8, "r": 8, "t": 12, "b": 42},
            font={"color": COLORS["black"], "family": "Arial, sans-serif"},
            showlegend=False,
        )
        fig.update_xaxes(title_text="Exceptions", gridcolor=COLORS["border"], zeroline=False)
        fig.update_yaxes(title_text=None, showgrid=False)
        st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False})
    else:
        st.info("No category summary is available.")

st.divider()
st.subheader("Exception Review Queue")
st.caption("Read-only review view. Severity, status, category and exposure are sourced from the control engine.")

available_categories = sorted(categories["category"].dropna().astype(str).unique().tolist()) if not categories.empty and "category" in categories.columns else []
severity_options = ["CRITICAL", "HIGH", "MEDIUM", "LOW"]
status_options = ["OPEN", "IN_REVIEW", "RESOLVED", "IGNORED"]

c1, c2, c3, c4 = st.columns([1.1, 1.1, 1.8, 0.8])
with c1:
    selected_severity = st.multiselect("Severity", severity_options, default=[])
with c2:
    selected_status = st.multiselect("Status", status_options, default=["OPEN", "IN_REVIEW"])
with c3:
    selected_category = st.multiselect("Category", available_categories, default=[])
with c4:
    limit = st.selectbox("Rows", [100, 250, 500], index=1)

try:
    recent = load_recent(tuple(sorted(selected_severity)), tuple(sorted(selected_status)), tuple(sorted(selected_category)), int(limit))
except APIError as exc:
    st.error(f"Unable to load exception queue: {exc}")
    recent = pd.DataFrame()

if not recent.empty:
    display = recent.copy()
    rename = {
        "exception_code": "Exception",
        "transaction_id": "Transaction ID",
        "category": "Category",
        "severity": "Severity",
        "exposure_inr": "Exposure (INR)",
        "business_unit_id": "Business Unit",
        "description": "Description",
        "status": "Status",
        "owner": "Owner",
        "created_at": "Detected At",
        "resolved_at": "Resolved At",
    }
    display = display[[c for c in rename if c in display.columns]].rename(columns=rename)
    if "Exposure (INR)" in display.columns:
        display["Exposure (INR)"] = display["Exposure (INR)"].map(lambda x: _money(x) if pd.notna(x) else "—")
    render_table(display, height=470)
    st.download_button(
        "Download filtered exception CSV",
        data=display.to_csv(index=False).encode("utf-8"),
        file_name="finsight_exceptions.csv",
        mime="text/csv",
    )
else:
    st.info("No exceptions match the current filters.")
