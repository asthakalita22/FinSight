"""Financial Performance — implemented in later Streamlit phase."""

import streamlit as st

from frontend.components.styles import inject_global_styles

st.set_page_config(page_title="FinSight · Financial Performance", layout="wide")
inject_global_styles()
st.title("Financial Performance")
st.caption("P&L, budget and variance analysis")
st.info("Financial Performance UI is scheduled for a later Streamlit phase.")
