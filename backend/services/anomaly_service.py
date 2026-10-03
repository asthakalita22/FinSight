"""
FinSight anomaly service
========================
Connects the Isolation Forest (ml/anomaly_detection.py) to PostgreSQL.

    run_anomaly_detection()   load -> clean -> train (or load model.pkl) -> score -> explain -> write tables
    summary / by_business_unit / trend / top_anomalies / score_histogram / latest_run   read the results back

Tables written (all REPLACED on every run, because they are fully derived from the transactions):
    anomaly_scores   one row per scored transaction (the population, for rates + score distribution)
    anomalies        only the flagged ones, with risk level and a plain-English explanation
    model_runs       one row per run (APPENDED): version, thresholds, what was excluded and why

`detected_at` is the SIMULATED detection time (18:00 on the day after the transaction), like the control exceptions,
because the data is a historical simulation.
"""
from __future__ import annotations

import io
import json
import time
from dataclasses import dataclass, field

import pandas as pd
from sqlalchemy import text

from backend.config import settings
from backend.services.pnl_service import _clean
from backend.utils.calculations import data_as_of
from backend.utils.data_access import load_ledger, load_transactions
from backend.utils.filters import Filters, build_where
from backend.utils.validators import validate_filters
from ml import anomaly_detection as ad

RISK_ORDER = ["HIGH", "MEDIUM", "LOW"]


# ----------------------------------------------------------------------------
# Run
# ----------------------------------------------------------------------------
@dataclass
class AnomalyResult:
    model_version: str = ""
    scored: int = 0
    flagged: int = 0
    by_risk: dict = field(default_factory=dict)
    excluded: dict = field(default_factory=dict)
    thresholds: dict = field(default_factory=dict)
    trained_new_model: bool = True
    seconds: float = 0.0


def _copy(cur, table: str, columns: list[str], frame: pd.DataFrame) -> int:
    buf = io.StringIO()
    frame[columns].to_csv(buf, index=False, header=False, float_format="%.4f", date_format="%Y-%m-%d %H:%M:%S")
    buf.seek(0)
    cur.copy_expert(f"COPY {table} ({', '.join(columns)}) FROM STDIN WITH (FORMAT csv)", buf)
    return cur.rowcount


def run_anomaly_detection(engine, cfg=settings, retrain: bool = True, verbose: bool = True) -> AnomalyResult:
    started = time.time()
    log = print if verbose else (lambda *a, **k: None)

    txn, led = load_transactions(engine), load_ledger(engine)
    bu_names = dict(pd.read_sql(text("SELECT id, name FROM business_units"), engine).values)
    df, excluded = ad.clean_for_scoring(txn, led)

    model = None
    if not retrain and ad.MODEL_PATH.exists():
        model = ad.AnomalyModel.load()
    trained_new = model is None
    if model is None:
        model = ad.train(df, seed=cfg.seed, contamination=cfg.anomaly_contamination, trees=cfg.anomaly_trees,
                         max_samples=cfg.anomaly_max_samples, profile=cfg.anomaly_feature_set)
        model.save()

    scored = ad.score_frame(df, model, bu_names)
    flagged = scored[scored["is_anomaly"]].copy()

    # ---- frames to write ------------------------------------------------------------------------------
    scores = pd.DataFrame({"source_row_id": scored["id"], "business_unit_id": scored["business_unit_id"],
                           "txn_date": scored["date"].dt.strftime("%Y-%m-%d"),
                           "anomaly_score": scored["anomaly_score"].round(4)})
    anomalies = pd.DataFrame({
        "transaction_id": flagged["transaction_id"], "anomaly_score": flagged["anomaly_score"].round(4),
        "risk_level": flagged["risk_level"], "model_version": model.version,
        "detected_at": flagged["date"] + pd.Timedelta(days=1, hours=18),
        "source_row_id": flagged["id"], "business_unit_id": flagged["business_unit_id"],
        "transaction_date": flagged["ts"], "amount_inr": flagged["amount_inr"].round(2), "reasons": flagged["reasons"]
    }).sort_values(["transaction_date", "transaction_id"])
    run_at = data_as_of(txn, led)

    raw = engine.raw_connection()
    try:
        with raw.cursor() as cur:
            cur.execute("TRUNCATE anomalies, anomaly_scores RESTART IDENTITY")
            _copy(cur, "anomaly_scores", list(scores.columns), scores)
            _copy(cur, "anomalies", list(anomalies.columns), anomalies)
            cur.execute("""INSERT INTO model_runs (model_version, feature_set, run_at, transactions_scored,
                              anomalies_flagged, contamination, threshold_flag, threshold_medium, threshold_high,
                              features, excluded)
                           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)""",
                        (model.version, model.profile, run_at.to_pydatetime(), len(scored), len(anomalies),
                         model.contamination, model.threshold_flag, model.threshold_medium, model.threshold_high,
                         ", ".join(model.feature_names), json.dumps(excluded)))
        raw.commit()
    except Exception:
        raw.rollback()
        raise
    finally:
        raw.close()

    result = AnomalyResult(
        model_version=model.version, scored=len(scored), flagged=len(anomalies), excluded=excluded,
        by_risk={r: int((anomalies["risk_level"] == r).sum()) for r in RISK_ORDER},
        thresholds={"flag": model.threshold_flag, "medium": model.threshold_medium, "high": model.threshold_high},
        trained_new_model=trained_new, seconds=round(time.time() - started, 1))
    log(f"  Model      : {model.version}  ({'trained now' if trained_new else 'loaded from ml/model.pkl'}; "
        f"feature set '{model.profile}', {len(model.feature_names)} inputs)")
    log(f"  Scored     : {len(scored):,} transactions  (left out: " + ", ".join(
        f"{k.replace('_', ' ')} {v:,}" for k, v in excluded.items() if v) + ")")
    log(f"  Flagged    : {len(anomalies):,} anomalies ({100 * len(anomalies) / len(scored):.2f}%)   "
        + "   ".join(f"{r} {result.by_risk[r]:,}" for r in RISK_ORDER))
    log(f"  Thresholds : flag >= {model.threshold_flag:.4f}   medium >= {model.threshold_medium:.4f}   high >= {model.threshold_high:.4f}")
    return result


# ----------------------------------------------------------------------------
# Read back
# ----------------------------------------------------------------------------
_FROM = """FROM anomaly_scores s
           JOIN transactions t ON t.id = s.source_row_id
           JOIN business_units bu ON bu.id = s.business_unit_id
           LEFT JOIN anomalies a ON a.source_row_id = s.source_row_id"""
_AGG = """COUNT(*) AS scored, COUNT(a.id) AS anomalies,
          COUNT(*) FILTER (WHERE a.risk_level = 'HIGH')   AS high,
          COUNT(*) FILTER (WHERE a.risk_level = 'MEDIUM') AS medium,
          COUNT(*) FILTER (WHERE a.risk_level = 'LOW')    AS low,
          COALESCE(SUM(ABS(a.amount_inr)), 0)::float8                                     AS exposure_inr,
          COALESCE(SUM(ABS(a.amount_inr)) FILTER (WHERE a.risk_level = 'HIGH'), 0)::float8 AS high_risk_exposure_inr"""


def _where(f: Filters | None):
    f = f or Filters()
    return build_where(f, "s.txn_date", "bu.name", "t.product")


def _with_rate(df: pd.DataFrame) -> pd.DataFrame:
    df["anomaly_rate_pct"] = (100.0 * df["anomalies"] / df["scored"]).where(df["scored"] > 0)
    return df


def latest_run(engine) -> dict | None:
    df = pd.read_sql(text("SELECT * FROM model_runs ORDER BY id DESC LIMIT 1"), engine)
    if df.empty:
        return None
    r = df.iloc[0].to_dict()
    return {k: (v.isoformat() if hasattr(v, "isoformat") else (float(v) if k.startswith(("threshold", "contamination")) else v))
            for k, v in r.items()}


def summary(engine, f: Filters | None = None) -> dict:
    """Anomaly counts by risk, anomaly rate (anomalies / scored transactions) and exposure for the filtered selection."""
    validate_filters(engine, f or Filters(), check_products=False)
    where, params = _where(f)
    row = pd.read_sql(text(f"SELECT {_AGG}, COALESCE(AVG(s.anomaly_score), 0)::float8 AS avg_score, "
                           f"COALESCE(MAX(s.anomaly_score), 0)::float8 AS max_score {_FROM}{where}"), engine, params=params).iloc[0]
    out = {k: (int(v) if k in ("scored", "anomalies", "high", "medium", "low") else float(v)) for k, v in row.items()}
    out["anomaly_rate_pct"] = round(100.0 * out["anomalies"] / out["scored"], 3) if out["scored"] else None
    run = latest_run(engine)
    out["model_version"] = run["model_version"] if run else None
    return {k: _clean(v) for k, v in out.items()}


def by_business_unit(engine, f: Filters | None = None) -> pd.DataFrame:
    validate_filters(engine, f or Filters(), check_products=False)
    where, params = _where(f)
    df = pd.read_sql(text(f"SELECT bu.name AS business_unit, {_AGG} {_FROM}{where} GROUP BY bu.name"), engine, params=params)
    return _with_rate(df).sort_values("anomaly_rate_pct", ascending=False).reset_index(drop=True)


def trend(engine, f: Filters | None = None, grain: str = "month") -> pd.DataFrame:
    if grain not in ("day", "month"):
        raise ValueError("grain must be 'day' or 'month'")
    validate_filters(engine, f or Filters(), check_products=False)
    where, params = _where(f)
    period = "to_char(s.txn_date, 'YYYY-MM')" if grain == "month" else "to_char(s.txn_date, 'YYYY-MM-DD')"
    df = pd.read_sql(text(f"SELECT {period} AS period, {_AGG} {_FROM}{where} GROUP BY 1 ORDER BY 1"), engine, params=params)
    return _with_rate(df)


def top_anomalies(engine, f: Filters | None = None, n: int = 20, order_by: str = "amount") -> pd.DataFrame:
    """The highest-value (order_by='amount') or highest-scoring (order_by='score') anomalies, with their explanations."""
    order = {"amount": "ABS(a.amount_inr) DESC", "score": "a.anomaly_score DESC, ABS(a.amount_inr) DESC"}
    if order_by not in order:
        raise ValueError(f"order_by must be one of {sorted(order)}")
    validate_filters(engine, f or Filters(), check_products=False)
    where, params = build_where(f or Filters(), "a.transaction_date::date", "bu.name", "t.product")
    params["n"] = int(n)
    return pd.read_sql(text(f"""
        SELECT a.transaction_id, a.transaction_date, bu.name AS business_unit, t.product, t.transaction_type,
               t.currency, t.amount::float8 AS amount, a.amount_inr::float8 AS amount_inr,
               a.anomaly_score::float8 AS anomaly_score, a.risk_level, a.reasons
        FROM anomalies a
        JOIN transactions t ON t.id = a.source_row_id
        LEFT JOIN business_units bu ON bu.id = a.business_unit_id{where}
        ORDER BY {order[order_by]} LIMIT :n"""), engine, params=params)


def score_histogram(engine, bins: int = 40) -> pd.DataFrame:
    """Distribution of anomaly scores over all scored transactions (bin_start, bin_end, count, flagged)."""
    return pd.read_sql(text("""
        WITH r AS (SELECT MIN(anomaly_score) lo, MAX(anomaly_score) hi FROM anomaly_scores),
             b AS (SELECT s.anomaly_score, LEAST(:bins - 1, FLOOR((s.anomaly_score - r.lo) / NULLIF(r.hi - r.lo, 0) * :bins))::int AS bin,
                          r.lo, r.hi, (a.id IS NOT NULL) AS flagged
                   FROM anomaly_scores s CROSS JOIN r LEFT JOIN anomalies a ON a.source_row_id = s.source_row_id)
        SELECT bin, (MIN(lo) + bin * (MIN(hi) - MIN(lo)) / :bins)::float8 AS bin_start,
               (MIN(lo) + (bin + 1) * (MIN(hi) - MIN(lo)) / :bins)::float8 AS bin_end,
               COUNT(*) AS transactions, COUNT(*) FILTER (WHERE flagged) AS flagged
        FROM b GROUP BY bin ORDER BY bin"""), engine, params={"bins": int(bins)})
