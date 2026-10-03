"""Liquidity UI — implemented in later Streamlit phase."""

import streamlit as st

from frontend.components.styles import inject_global_styles

st.set_page_config(page_title="FinSight · Liquidity", layout="wide")
inject_global_styles()
st.title("Liquidity")
st.caption("Cash, buffer and funding monitoring")
st.info("Liquidity UI is scheduled for a later Streamlit phase.")
