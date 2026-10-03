"""FinSight Streamlit application shell (Phase 7).

Phase 7 provides the reusable visual system, navigation shell, API client,
and service/error states. Business dashboards are intentionally implemented
in later Streamlit phases.
"""

import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
import streamlit as st

from frontend.components.styles import inject_global_styles, render_brand, render_status
from frontend.services.api_client import APIError, get_api_client


st.set_page_config(
    page_title="FinSight",
    page_icon="◼",
    layout="wide",
    initial_sidebar_state="expanded",
)
inject_global_styles()

API_BASE_URL = os.getenv("FINSIGHT_API_URL", "http://localhost:8000")
api = get_api_client(API_BASE_URL)

with st.sidebar:
    render_brand()
    st.caption("Internal finance analytics and control platform")
    st.divider()

    st.markdown('<div class="fs-section">System</div>', unsafe_allow_html=True)
    try:
        health = api.health()
        render_status(bool(health.get("status") == "ok"), "API")
    except APIError:
        render_status(False, "API")

    st.divider()
    st.caption("Phase 7 · Application shell")

st.title("FinSight")
st.markdown("### Finance Control Center")
st.markdown(
    "A structured workspace for financial performance, reconciliation, controls, "
    "liquidity, and anomaly monitoring."
)

st.markdown('<div class="fs-section">Application status</div>', unsafe_allow_html=True)

try:
    health = api.health()
    render_status(True, "FastAPI")
    if health.get("database") == "ok":
        st.success("PostgreSQL connection is healthy.")
    else:
        st.warning("The API is running, but the database health check is not healthy.")
except APIError as exc:
    st.warning("The dashboard shell is running, but the FastAPI service is unavailable.")
    st.code(str(exc))

st.markdown('<div class="fs-section">What is available</div>', unsafe_allow_html=True)

left, right = st.columns(2)
with left:
    st.markdown(
        """
        <div class="fs-card">
        <strong>Finance analytics</strong><br>
        <span class="fs-muted">P&L, budget variance, liquidity and reconciliation APIs are available through the Phase 6 service.</span>
        </div>
        """,
        unsafe_allow_html=True,
    )
with right:
    st.markdown(
        """
        <div class="fs-card">
        <strong>Risk & controls</strong><br>
        <span class="fs-muted">Financial exceptions and ML anomaly results are exposed through the read-only API.</span>
        </div>
        """,
        unsafe_allow_html=True,
    )

st.info("Phase 7 establishes the application shell. Executive dashboard content is added in Phase 8.")
