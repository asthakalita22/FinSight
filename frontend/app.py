"""FinSight Streamlit placeholder (Phase 0/1).
Shows database status and table row counts. Replaced by the real
multipage dashboard in Phase 7-8."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd
import streamlit as st
from sqlalchemy import text

from backend.database import engine
from backend.services import liquidity_service as liquidity
from backend.services import pnl_service as pnl_svc

st.set_page_config(page_title="FinSight", page_icon="◼", layout="wide")

st.markdown(
    """
    <style>
      h1, h2, h3 { color: #111111; letter-spacing: -0.01em; }
      [data-testid="stMetricValue"] { color: #111111; }
      .fs-sub { color: #6F7378; margin-top: -0.6rem; }
    </style>
    """,
    unsafe_allow_html=True,
)

st.title("FinSight")
st.markdown('<p class="fs-sub">Financial Control Center · Phase 4 status page</p>', unsafe_allow_html=True)

TABLES = [
    "business_units", "fx_rates", "liquidity_targets", "transactions", "ledger_entries", "budgets",
    "pnl", "cash_flows", "reconciliations", "exceptions", "anomalies",
]

try:
    counts = []
    with engine.connect() as conn:
        for t in TABLES:
            counts.append({"table": t, "rows": conn.execute(text(f"SELECT COUNT(*) FROM {t}")).scalar()})
    st.success("● Database connected")
    df = pd.DataFrame(counts)
    cols = st.columns(4)
    for i, row in df.head(4).iterrows():
        cols[i].metric(row["table"], f"{row['rows']:,}")
    st.dataframe(df, width="stretch", hide_index=True)
    if int(df.loc[df["table"] == "pnl", "rows"].iloc[0]):
        st.subheader("Finance snapshot")
        s = pnl_svc.summary(engine)
        cash = liquidity.summary(engine)
        trend = pnl_svc.trend(engine, grain="month")
        latest = trend.iloc[-1]
        m = lambda x: f"₹{x / 1e6:,.1f}M"  # noqa: E731
        c = st.columns(4)
        c[0].metric("Revenue", m(s["revenue"]), f"{latest['revenue_mom_pct']:+.1f}% MoM (latest month)")
        c[1].metric("Net P&L", m(s["net_pnl"]), f"{s['variance_pct']:+.1f}% vs budget")
        c[2].metric("Cash position", m(cash["closing_cash"]), f"{cash['coverage_ratio']:.2f}x liquidity buffer")
        c[3].metric("Peak funding need", m(cash["peak_funding_requirement"]), f"{cash['days_in_shortfall']} shortfall days")
        chart = trend.set_index("period")[["revenue", "total_expenses", "net_pnl"]] / 1e6
        st.caption("Monthly revenue, expenses and net P&L (₹M)")
        st.bar_chart(chart)

    rec_rows = int(df.loc[df["table"] == "reconciliations", "rows"].iloc[0])
    if rec_rows:
        st.subheader("Reconciliation: source system vs finance ledger")
        with engine.connect() as conn:
            r = pd.read_sql(text("SELECT * FROM v_reconciliation_summary"), conn).iloc[0]
        cols = st.columns(6)
        cols[0].metric("Match rate", f"{float(r['match_rate']):.2f}%")
        cols[1].metric("Matched", f"{int(r['matched']):,}")
        cols[2].metric("Mismatched", f"{int(r['mismatched']):,}")
        cols[3].metric("Missing", f"{int(r['missing']):,}")
        cols[4].metric("Duplicates", f"{int(r['duplicates']):,}")
        cols[5].metric("Invalid", f"{int(r['invalid']):,}")
    else:
        st.info("No reconciliation yet. Run:  python automation/pipeline.py")

    exc_rows = int(df.loc[df["table"] == "exceptions", "rows"].iloc[0])
    if exc_rows:
        st.subheader("Exceptions raised by the control engine")
        with engine.connect() as conn:
            by_sev = pd.read_sql(text(
                "SELECT severity, COUNT(*) AS exceptions FROM exceptions GROUP BY 1"), conn)
            by_cat = pd.read_sql(text(
                "SELECT category, COUNT(*) AS exceptions FROM exceptions GROUP BY 1 ORDER BY 2 DESC"), conn)
        order = ["CRITICAL", "HIGH", "MEDIUM", "LOW"]
        by_sev = by_sev.set_index("severity").reindex(order).fillna(0).astype(int).reset_index()
        c1, c2 = st.columns(2)
        c1.dataframe(by_sev, width="stretch", hide_index=True)
        c2.dataframe(by_cat, width="stretch", hide_index=True)
    else:
        st.info("No exceptions yet. Run:  python automation/pipeline.py")
except Exception as exc:  # noqa: BLE001
    st.error("Database not reachable. Start it with `docker compose up -d` and load data.")
    st.code(str(exc))
