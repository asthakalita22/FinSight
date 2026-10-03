"""Executive Overview — implemented in Phase 8."""

import streamlit as st

from frontend.components.styles import inject_global_styles

st.set_page_config(page_title="FinSight · Overview", layout="wide")
inject_global_styles()
st.title("Executive Overview")
st.caption("Phase 8 dashboard implementation")
st.info("The executive KPI and trend dashboard is intentionally reserved for Phase 8.")
