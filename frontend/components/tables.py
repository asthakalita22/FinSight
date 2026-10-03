"""Reusable table helpers for FinSight."""

import pandas as pd
import streamlit as st


def render_table(df: pd.DataFrame, *, height: int = 360, hide_index: bool = True) -> None:
    """Render a standard FinSight data table."""
    if df.empty:
        st.info("No records match the current selection.")
        return
    st.dataframe(df, width="stretch", height=height, hide_index=hide_index)


def render_empty_state(message: str) -> None:
    """Render a neutral empty state."""
    st.info(message)
