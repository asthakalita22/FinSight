"""Reusable KPI presentation helpers for FinSight."""

from collections.abc import Sequence

import streamlit as st



def render_kpis(items: Sequence[dict]) -> None:
    """Render a row of KPI metrics from dictionaries.

    Each item may contain label, value, delta, and delta_color.
    """
    if not items:
        return
    columns = st.columns(len(items))
    for column, item in zip(columns, items):
        with column:
            column.metric(
                item.get("label", ""),
                item.get("value", "—"),
                item.get("delta"),
                delta_color=item.get("delta_color", "normal"),
            )
