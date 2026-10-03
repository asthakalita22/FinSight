"""Risk & Anomalies UI — implemented in later Streamlit phase."""

import streamlit as st

from frontend.components.styles import inject_global_styles

st.set_page_config(page_title="FinSight · Risk & Anomalies", layout="wide")
inject_global_styles()
st.title("Risk & Anomalies")
st.caption("ML-based anomaly monitoring")
st.info("Risk and anomaly UI is scheduled for a later Streamlit phase.")
