"""Exceptions UI — implemented in Phase 9."""

import streamlit as st

from frontend.components.styles import inject_global_styles

st.set_page_config(page_title="FinSight · Exceptions", layout="wide")
inject_global_styles()
st.title("Exceptions & Controls")
st.caption("Financial control exceptions and review queue")
st.info("Exceptions UI is scheduled for Phase 9.")
