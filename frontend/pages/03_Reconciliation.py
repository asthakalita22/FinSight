"""Reconciliation UI — implemented in Phase 9."""

import streamlit as st

from frontend.components.styles import inject_global_styles

st.set_page_config(page_title="FinSight · Reconciliation", layout="wide")
inject_global_styles()
st.title("Reconciliation")
st.caption("Source system vs finance ledger")
st.info("Reconciliation UI is scheduled for Phase 9.")
