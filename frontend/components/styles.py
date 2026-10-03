"""Shared FinSight Streamlit visual system."""

import streamlit as st


COLORS = {
    "off_white": "#F8F8F5",
    "black": "#111111",
    "yellow": "#D9B84C",
    "charcoal": "#252525",
    "green": "#5D8068",
    "red": "#A86464",
    "slate": "#6F7378",
    "border": "#D9D9D2",
}


def inject_global_styles() -> None:
    """Apply the shared institutional-finance styling to the current page."""
    st.markdown(
        f"""
        <style>
        :root {{
            --fs-off-white: {COLORS['off_white']};
            --fs-black: {COLORS['black']};
            --fs-yellow: {COLORS['yellow']};
            --fs-charcoal: {COLORS['charcoal']};
            --fs-green: {COLORS['green']};
            --fs-red: {COLORS['red']};
            --fs-slate: {COLORS['slate']};
            --fs-border: {COLORS['border']};
        }}

        .stApp {{ background: var(--fs-off-white); }}
        h1, h2, h3 {{
            color: var(--fs-black);
            letter-spacing: -0.02em;
        }}
        p, label, [data-testid="stCaptionContainer"] {{
            color: var(--fs-slate);
        }}
        [data-testid="stMetricValue"] {{
            color: var(--fs-black);
            font-weight: 650;
        }}
        [data-testid="stMetricLabel"] {{
            color: var(--fs-slate);
        }}
        [data-testid="stSidebar"] {{
            background: #F1F1EB;
            border-right: 1px solid var(--fs-border);
        }}
        [data-testid="stSidebarNav"] {{ padding-top: 1rem; }}
        div[data-testid="stVerticalBlockBorderWrapper"] {{
            border-color: var(--fs-border);
        }}
        .fs-brand {{
            font-size: 1.45rem;
            font-weight: 700;
            color: var(--fs-black);
            letter-spacing: -0.03em;
            margin-bottom: 0.1rem;
        }}
        .fs-kicker {{
            color: var(--fs-slate);
            font-size: 0.78rem;
            text-transform: uppercase;
            letter-spacing: 0.11em;
            margin-bottom: 1.2rem;
        }}
        .fs-status {{
            display: inline-flex;
            align-items: center;
            gap: 0.45rem;
            padding: 0.32rem 0.65rem;
            border: 1px solid var(--fs-border);
            border-radius: 999px;
            background: rgba(255,255,255,0.55);
            color: var(--fs-slate);
            font-size: 0.78rem;
        }}
        .fs-dot {{
            width: 0.48rem;
            height: 0.48rem;
            border-radius: 50%;
            background: var(--fs-green);
            display: inline-block;
        }}
        .fs-dot-error {{ background: var(--fs-red); }}
        .fs-section {{
            margin-top: 1.35rem;
            margin-bottom: 0.65rem;
            font-size: 0.78rem;
            font-weight: 700;
            text-transform: uppercase;
            letter-spacing: 0.09em;
            color: var(--fs-slate);
        }}
        .fs-card {{
            border: 1px solid var(--fs-border);
            border-radius: 10px;
            padding: 1rem 1.05rem;
            background: rgba(255,255,255,0.55);
        }}
        .fs-muted {{ color: var(--fs-slate); }}
        </style>
        """,
        unsafe_allow_html=True,
    )


def render_brand() -> None:
    """Render the compact FinSight brand block."""
    st.markdown('<div class="fs-brand">FinSight</div>', unsafe_allow_html=True)
    st.markdown('<div class="fs-kicker">Finance Control Center</div>', unsafe_allow_html=True)


def render_status(healthy: bool, label: str = "API") -> None:
    """Render a compact service health indicator."""
    dot_class = "" if healthy else " fs-dot-error"
    state = "Connected" if healthy else "Unavailable"
    st.markdown(
        f'<div class="fs-status"><span class="fs-dot{dot_class}"></span>{label}: {state}</div>',
        unsafe_allow_html=True,
    )
