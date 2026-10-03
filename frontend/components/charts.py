"""Reusable Plotly chart helpers for FinSight."""

from collections.abc import Iterable

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go

from frontend.components.styles import COLORS


def _base_layout(fig: go.Figure, height: int = 360) -> go.Figure:
    fig.update_layout(
        height=height,
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        margin={"l": 10, "r": 10, "t": 45, "b": 10},
        font={"color": COLORS["black"], "family": "Arial, sans-serif"},
        legend={"orientation": "h", "yanchor": "bottom", "y": 1.01, "x": 0},
    )
    fig.update_xaxes(showgrid=False, linecolor=COLORS["border"])
    fig.update_yaxes(gridcolor=COLORS["border"], zeroline=False)
    return fig


def empty_figure(message: str = "No data available") -> go.Figure:
    """Return an intentionally empty chart with a useful message."""
    fig = go.Figure()
    fig.add_annotation(text=message, x=0.5, y=0.5, showarrow=False, font={"color": COLORS["slate"]})
    fig.update_xaxes(visible=False)
    fig.update_yaxes(visible=False)
    return _base_layout(fig, height=280)


def line_chart(df: pd.DataFrame, x: str, y: Iterable[str] | str, title: str = "") -> go.Figure:
    """Create a restrained multi-series line chart."""
    if df.empty:
        return empty_figure()
    fig = px.line(df, x=x, y=y, markers=True, title=title)
    fig.update_traces(line={"width": 2})
    return _base_layout(fig)


def bar_chart(df: pd.DataFrame, x: str, y: str, title: str = "") -> go.Figure:
    """Create a restrained bar chart."""
    if df.empty:
        return empty_figure()
    fig = px.bar(df, x=x, y=y, title=title)
    return _base_layout(fig)
