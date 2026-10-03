"""Reports UI — implemented in later Streamlit phase."""

import streamlit as st

from frontend.components.styles import inject_global_styles

st.set_page_config(page_title="FinSight · Reports", layout="wide")
inject_global_styles()
st.title("Reports")
st.caption("Financial reporting and exports")
st.info("Reports UI is scheduled for a later Streamlit phase.")
