"""
FinSight - Phase 5 verification (anomaly detection)
===================================================
  A. Integrity      accounting, thresholds, risk bands, exclusions, links back to the transactions
  B. Reproducible   the saved ml/model.pkl re-creates every stored score
  C. Quality        the UNSUPERVISED model is measured against the generator's answer key (it never sees it)
  D. Explanations   every flagged transaction is explained, and the numbers quoted in the text are TRUE
  E. Re-runs        deterministic, and can score with the saved model without retraining
  F. Service        every figure compared with independent SQL
  G. Profiles       both feature profiles work

Usage:
    python data/verify_phase5.py
"""
import re
import sys
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.metrics import average_precision_score, roc_auc_score
from sqlalchemy import text

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.config import settings  # noqa: E402
from backend.database import engine  # noqa: E402
from backend.services import anomaly_service as AS  # noqa: E402
from backend.utils.data_access import load_ledger, load_transactions  # noqa: E402
from backend.utils.filters import Filters  # noqa: E402
from ml import anomaly_detection as ad  # noqa: E402

MANIFEST = ROOT / "data" / "raw" / "injection_manifest.csv"
results = []


def q(sql, **params):
    return pd.read_sql(text(sql), engine, params=params or None)


def scalar(sql, **params):
    with engine.connect() as conn:
        return conn.execute(text(sql), params).scalar()


def check(name, ok, detail=""):
    results.append(bool(ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  -> {detail}" if detail else ""))


def raises(fn):
    try:
        fn()
    except ValueError:
        return True
    except Exception:
        return False
    return False


def main():
    run = q("SELECT * FROM model_runs ORDER BY id DESC LIMIT 1")
    if run.empty:
        print("No model run found. Run:  python automation/pipeline.py")
        sys.exit(1)
    run = run.iloc[0]
    runs_before = scalar("SELECT COUNT(*) FROM model_runs")
    n_scored, n_flag = int(run.transactions_scored), int(run.anomalies_flagged)
    thr_f, thr_m, thr_h = float(run.threshold_flag), float(run.threshold_medium), float(run.threshold_high)
    scores = q("SELECT source_row_id, business_unit_id, txn_date, anomaly_score::float8 AS score FROM anomaly_scores")
    an = q("""SELECT a.*, a.anomaly_score::float8 AS score, a.amount_inr::float8 AS inr FROM anomalies a""")

    # ================================================================== A
    print("\nA. INTEGRITY")
    check("anomaly_scores rows == transactions scored in the latest model run", len(scores) == n_scored, f"{n_scored:,}")
    check("anomalies rows == anomalies flagged in the latest model run", len(an) == n_flag, f"{n_flag:,}")
    check(f"Flag rate = contamination setting ({float(run.contamination):.2%})", abs(n_flag / n_scored - float(run.contamination)) < 0.0005,
          f"{100 * n_flag / n_scored:.3f}%")
    check("Thresholds are ordered: flag < medium < high", thr_f < thr_m < thr_h, f"{thr_f:.4f} < {thr_m:.4f} < {thr_h:.4f}")
    m = scores.merge(an[["source_row_id", "score", "risk_level"]], on="source_row_id", how="left", suffixes=("", "_a"))
    flagged = m.risk_level.notna()
    check("Every anomaly is one of the scored transactions, with the same score", len(an) == flagged.sum() and
          np.allclose(m.loc[flagged, "score"], m.loc[flagged, "score_a"], atol=1e-4))
    check("Exactly the transactions scoring >= the flag threshold are flagged (none missed, none extra)",
          bool((m.score[flagged] >= thr_f - 1e-4).all() and (m.score[~flagged] < thr_f + 1e-4).all()),
          f"lowest flagged {m.score[flagged].min():.4f}, highest unflagged {m.score[~flagged].max():.4f}")
    lv = an.set_index("risk_level").score
    ok = (lv["HIGH"] >= thr_h - 1e-4).all() and (lv["MEDIUM"].between(thr_m - 1e-4, thr_h + 1e-4)).all() and (lv["LOW"].between(thr_f - 1e-4, thr_m + 1e-4)).all()
    cnt = an.risk_level.value_counts()
    check("Risk levels follow the score bands (HIGH top 0.1%, MEDIUM next 0.4%, LOW next 0.5%)", ok,
          f"HIGH {cnt.get('HIGH', 0)}, MEDIUM {cnt.get('MEDIUM', 0)}, LOW {cnt.get('LOW', 0)}")
    check("Scores are valid probabilities-like values in (0, 1)", bool(scores.score.between(0, 1).all() and an.score.between(0, 1).all()))

    total_txn = scalar("SELECT COUNT(*) FROM transactions")
    ex = run.excluded
    sql_ex = {
        "missing_transaction_id": scalar("SELECT COUNT(*) FROM transactions WHERE transaction_id IS NULL"),
        "duplicate_copy": scalar("SELECT COUNT(*) - COUNT(DISTINCT transaction_id) FROM transactions WHERE transaction_id IS NOT NULL"),
        "invalid_amount": scalar("SELECT COUNT(*) FROM transactions t WHERE transaction_id IS NOT NULL AND amount <= 0 AND transaction_type <> 'ADJUSTMENT' "
                                 "AND id = (SELECT MIN(id) FROM transactions x WHERE x.transaction_id = t.transaction_id)"),
    }
    check("What was left out matches independent SQL (missing id, duplicate copies, invalid amounts)",
          all(ex[k] == v for k, v in sql_ex.items()), ", ".join(f"{k.replace('_', ' ')} {v:,}" for k, v in sql_ex.items()))
    check("Accounting: transactions scored + left out = all transactions", n_scored + sum(ex.values()) == total_txn,
          f"{n_scored:,} + {sum(ex.values()):,} = {total_txn:,}")
    bad = scalar("""SELECT COUNT(*) FROM anomaly_scores s JOIN transactions t ON t.id = s.source_row_id
                    WHERE t.transaction_id IS NULL OR (t.amount <= 0 AND t.transaction_type <> 'ADJUSTMENT')""")
    check("No transaction without an id, and no invalid-amount transaction, was scored", bad == 0, f"{bad} found")
    check("Each transaction id is flagged at most once (duplicate copies are never scored)", an.transaction_id.is_unique)
    check("Anomalies carry the transaction's date, amount and business unit",
          scalar("""SELECT COUNT(*) FROM anomalies a JOIN transactions t ON t.id = a.source_row_id
                    WHERE a.transaction_date <> t.transaction_date OR (t.amount_inr IS NOT NULL AND a.amount_inr <> t.amount_inr)""") == 0)
    check("A business unit the source system lost is recovered from the ledger",
          scalar("""SELECT COUNT(*) FROM anomaly_scores s JOIN transactions t ON t.id = s.source_row_id JOIN ledger_entries l ON l.transaction_id = t.transaction_id
                    WHERE t.business_unit_id IS NULL AND s.business_unit_id <> l.business_unit_id""") == 0
          and scalar("SELECT COUNT(*) FROM anomaly_scores s JOIN transactions t ON t.id = s.source_row_id WHERE t.business_unit_id IS NULL") > 0)
    check("Detection time is after the transaction", scalar("SELECT COUNT(*) FROM anomalies WHERE detected_at <= transaction_date") == 0)
    check("Every anomaly stores the model version of the latest run", (an.model_version == run.model_version).all(), run.model_version)

    # ================================================================== B
    print("\nB. THE SAVED MODEL IS THE ONE THAT PRODUCED THE DATABASE")
    model = ad.AnomalyModel.load()
    check("ml/model.pkl has the same version as the latest model run", model.version == run.model_version, model.version)
    txn, led = load_transactions(engine), load_ledger(engine)
    df, _ = ad.clean_for_scoring(txn, led)
    rescored = ad.score_frame(df, model, explain_flagged=False)
    r = rescored[["id", "anomaly_score", "is_anomaly"]].merge(scores, left_on="id", right_on="source_row_id")
    check(f"Re-scoring all {len(r):,} transactions with the saved model reproduces every stored score (to 4 decimals)",
          len(r) == n_scored and float((r.anomaly_score - r.score).abs().max()) <= 5.1e-5,
          f"max difference {float((r.anomaly_score - r.score).abs().max()):.6f}")
    check("... and exactly the same set of anomalies", set(rescored.loc[rescored.is_anomaly, "id"]) == set(an.source_row_id))

    # ================================================================== C
    print("\nC. QUALITY: the unsupervised model vs the generator's answer key (labels are used ONLY here)")
    if not MANIFEST.exists():
        check("injection_manifest.csv found", False, "run: python automation/pipeline.py --generate")
        return finish(runs_before)
    man = pd.read_csv(MANIFEST)
    unusual = set(man.loc[man.issue_type == "UNUSUAL_TRANSACTION", "transaction_id"])
    ids = q("SELECT id AS source_row_id, transaction_id FROM transactions")
    lab = scores.merge(ids, on="source_row_id")
    lab["y"] = lab.transaction_id.isin(unusual)
    lab = lab.merge(an[["source_row_id", "risk_level"]], on="source_row_id", how="left")
    y, s = lab.y.to_numpy(), lab.score.to_numpy()
    n_pos = int(y.sum())
    check("All injected unusual transactions were eligible for scoring (none excluded)", n_pos == len(unusual), f"{n_pos} of {len(unusual)}")
    auc, ap = roc_auc_score(y, s), average_precision_score(y, s)
    check("Ranking quality: ROC-AUC >= 0.95", auc >= 0.95, f"AUC {auc:.4f}   (average precision {ap:.3f} vs {n_pos / len(y):.3%} base rate)")
    check("Average precision >= 0.60", ap >= 0.60, f"{ap:.3f}")
    fl = lab[lab.risk_level.notna()]
    tp, recall, precision = int(fl.y.sum()), fl.y.sum() / n_pos, fl.y.mean()
    check("Flagged anomalies recover >= 70% of the injected unusual transactions", recall >= 0.70,
          f"recall {recall:.1%} ({tp} of {n_pos}); precision {precision:.1%} ({len(fl) - tp} false alarms); lift {precision / (n_pos / len(y)):.0f}x over random")
    tiers = {t: lab[lab.risk_level == t] for t in ["HIGH", "MEDIUM", "LOW"]}
    check("HIGH-risk tier is >= 95% real unusual transactions", tiers["HIGH"].y.mean() >= 0.95,
          f"HIGH {tiers['HIGH'].y.mean():.1%} (n={len(tiers['HIGH'])}), MEDIUM {tiers['MEDIUM'].y.mean():.1%} (n={len(tiers['MEDIUM'])}), LOW {tiers['LOW'].y.mean():.1%} (n={len(tiers['LOW'])})")
    check("Risk tiers are ordered by precision: HIGH > MEDIUM > LOW", tiers["HIGH"].y.mean() > tiers["MEDIUM"].y.mean() > tiers["LOW"].y.mean())
    unit_rate = lab.assign(flagged=lab.risk_level.notna()).groupby("business_unit_id")["flagged"].mean()
    overall = n_flag / n_scored
    check("Fairness (no labels needed): every business unit is flagged at 0.5x-1.5x the overall rate; spread <= 1 point",
          bool(((unit_rate >= 0.5 * overall) & (unit_rate <= 1.5 * overall)).all() and (unit_rate.max() - unit_rate.min()) <= 0.01),
          f"unit flag rates {100 * unit_rate.min():.2f}%-{100 * unit_rate.max():.2f}% (overall {100 * overall:.2f}%)")
    pct = (lab.score.rank(pct=True)[lab.y]).median()
    check("The median injected transaction scores in the top 1% of all scores", pct >= 0.99, f"median percentile {100 * pct:.2f}")
    missed = txn[txn.transaction_id.isin(set(lab.loc[lab.y & lab.risk_level.isna(), "transaction_id"]))]
    in_hours = int(((missed.transaction_date.dt.hour >= 8) & (missed.transaction_date.dt.hour <= 17)).sum())
    print(f"       the {len(missed)} missed ones: {in_hours} happened in business hours with only a moderate amount; "
          f"their size overlaps the natural heavy tail of normal transactions")

    # ================================================================== D
    print("\nD. EXPLANATIONS")
    check("Every anomaly has an explanation", an.reasons.fillna("").str.len().gt(10).all())
    tpr = an[an.transaction_id.isin(unusual)]
    informative = tpr.reasons.str.contains("Amount|Booked at", regex=True).mean()
    check("Explanations for the real unusual transactions name a concrete signal (amount or hour) in >= 95% of cases",
          informative >= 0.95, f"{informative:.1%}; the rest say 'unusual combination'")
    fallback = an.reasons.str.startswith("Unusual combination").mean()
    print(f"       explained with a concrete signal overall: {1 - fallback:.1%} (fallback text on {fallback:.1%})")

    pat = re.compile(r"Amount INR [\d,.]+M? is (?:only )?([\d.]+)x the typical (.+?) (REVENUE|COST|EXPENSE|ADJUSTMENT) transaction")
    names = dict(q("SELECT name, id FROM business_units").values)
    med = q("""SELECT s.business_unit_id AS bu, t.transaction_type AS typ,
                      percentile_cont(0.5) WITHIN GROUP (ORDER BY ABS(t.amount_inr))::float8 AS med
               FROM anomaly_scores s JOIN transactions t ON t.id = s.source_row_id WHERE t.amount_inr IS NOT NULL GROUP BY 1, 2""")
    med = {(int(r.bu), r.typ): r.med for r in med.itertuples()}
    sample = an[an.reasons.str.contains("x the typical")].sample(200, random_state=1)
    wrong = 0
    for r in sample.itertuples():
        mt = pat.search(r.reasons)
        truth = abs(r.inr) / med[(names[mt.group(2)], mt.group(3))]
        claimed = float(mt.group(1))
        if abs(claimed - truth) > max(0.06, 0.01 * truth):
            wrong += 1
    check("The 'N x the typical amount' claim is TRUE: 200 random explanations recomputed from the data independently", wrong == 0, f"{wrong} wrong")
    hours = an[an.reasons.str.contains("Booked at")]
    ok = all(f"Booked at {pd.Timestamp(t):%H:%M}" in rs for t, rs in zip(hours.transaction_date, hours.reasons))
    check(f"The 'Booked at HH:MM' claim matches the transaction's actual time on all {len(hours)} off-hours explanations", ok)
    off = q("SELECT COUNT(*) FROM anomalies WHERE EXTRACT(hour FROM transaction_date) NOT BETWEEN 8 AND 17").iloc[0, 0]
    check("Every off-hours anomaly says so in its explanation", off == len(hours), f"{off} off-hours anomalies")

    # ================================================================== E
    print("\nE. RE-RUNS")
    before = an.sort_values("source_row_id")[["source_row_id", "anomaly_score", "risk_level"]].reset_index(drop=True)
    res = AS.run_anomaly_detection(engine, retrain=True, verbose=False)
    after = q("SELECT source_row_id, anomaly_score, risk_level FROM anomalies ORDER BY source_row_id")
    after["anomaly_score"] = after.anomaly_score.astype(float)
    check("Re-training with the same seed gives the identical anomalies, scores and risk levels",
          len(before) == len(after) and (before.source_row_id.to_numpy() == after.source_row_id.to_numpy()).all()
          and np.allclose(before.anomaly_score.astype(float), after.anomaly_score, atol=1e-6)
          and (before.risk_level.to_numpy() == after.risk_level.to_numpy()).all() and res.trained_new_model)
    res2 = AS.run_anomaly_detection(engine, retrain=False, verbose=False)
    after2 = q("SELECT source_row_id FROM anomalies ORDER BY source_row_id")
    check("Scoring with the saved model (no training) gives the same anomalies", not res2.trained_new_model
          and (after2.source_row_id.to_numpy() == before.source_row_id.to_numpy()).all())
    check("Each run appends one row to model_runs (audit trail); anomalies are replaced, not appended",
          scalar("SELECT COUNT(*) FROM model_runs") == runs_before + 2 and scalar("SELECT COUNT(*) FROM anomalies") == n_flag)

    # ================================================================== F
    print("\nF. SERVICE vs independent SQL")
    s_all = AS.summary(engine)
    sql = q("""SELECT COUNT(*) sc, (SELECT COUNT(*) FROM anomalies) an, (SELECT COUNT(*) FROM anomalies WHERE risk_level='HIGH') hi,
                      (SELECT SUM(ABS(amount_inr)) FROM anomalies) ex FROM anomaly_scores""").iloc[0]
    check("Summary: scored, anomalies, HIGH count, exposure = SQL", s_all["scored"] == sql.sc and s_all["anomalies"] == sql.an
          and s_all["high"] == sql.hi and abs(s_all["exposure_inr"] - float(sql.ex)) < 0.5,
          f"{s_all['anomalies']:,} anomalies = {s_all['anomaly_rate_pct']}% of {s_all['scored']:,}; exposure INR {s_all['exposure_inr'] / 1e6:,.1f} M")
    check("Anomaly rate = anomalies / scored x 100", abs(s_all["anomaly_rate_pct"] - 100 * sql.an / sql.sc) < 0.001)
    bu = AS.by_business_unit(engine)
    check("Business units add up to the total (scored, anomalies, exposure)", int(bu.scored.sum()) == s_all["scored"] and int(bu.anomalies.sum()) == s_all["anomalies"]
          and abs(bu.exposure_inr.sum() - s_all["exposure_inr"]) < 0.5, f"{len(bu)} business units; highest rate: {bu.business_unit.iloc[0]} {bu.anomaly_rate_pct.iloc[0]:.2f}%")
    for grain in ("month", "day"):
        t = AS.trend(engine, grain=grain)
        check(f"Trend by {grain} adds up to the total ({len(t)} periods)", int(t.scored.sum()) == s_all["scored"] and int(t.anomalies.sum()) == s_all["anomalies"])
    top = AS.top_anomalies(engine, n=10)
    check("Top anomalies by amount: sorted, and the first equals SQL's largest", len(top) == 10 and top.amount_inr.abs().is_monotonic_decreasing
          and abs(abs(top.amount_inr.iloc[0]) - float(scalar("SELECT MAX(ABS(amount_inr)) FROM anomalies"))) < 0.01,
          f"largest INR {abs(top.amount_inr.iloc[0]) / 1e6:,.1f} M ({top.business_unit.iloc[0]}, {top.risk_level.iloc[0]})")
    sc = AS.top_anomalies(engine, n=5, order_by="score")
    check("Top anomalies by score: sorted descending", sc.anomaly_score.is_monotonic_decreasing and sc.risk_level.eq("HIGH").all())
    h = AS.score_histogram(engine, bins=40)
    check("Score histogram covers every scored transaction and every anomaly", int(h.transactions.sum()) == n_scored and int(h.flagged.sum()) == n_flag,
          f"{len(h)} non-empty bins")
    f = Filters(start_date=date(2026, 1, 1), end_date=date(2026, 3, 31), business_units=("Equities", "Derivatives"))
    got = AS.summary(engine, f)
    want = q("""SELECT COUNT(*) sc, COUNT(a.id) an FROM anomaly_scores s JOIN business_units b ON b.id = s.business_unit_id
                LEFT JOIN anomalies a ON a.source_row_id = s.source_row_id
                WHERE s.txn_date BETWEEN '2026-01-01' AND '2026-03-31' AND b.name IN ('Equities','Derivatives')""").iloc[0]
    check("Filters (Q1 2026, two business units) = raw SQL", got["scored"] == want.sc and got["anomalies"] == want.an, f"{got['anomalies']} of {got['scored']:,}")
    pf = AS.summary(engine, Filters(products=("Equity",)))
    check("Product filter works", 0 < pf["scored"] < s_all["scored"] and pf["scored"] == scalar(
        "SELECT COUNT(*) FROM anomaly_scores s JOIN transactions t ON t.id = s.source_row_id WHERE t.product = 'Equity'"))
    empty = AS.summary(engine, Filters(start_date=date(2030, 1, 1)))
    check("A range with no data returns zeros and an undefined rate (not an error)", empty["scored"] == 0 and empty["anomaly_rate_pct"] is None)
    check("Unknown business unit / bad grain / bad order are rejected", raises(lambda: AS.summary(engine, Filters(business_units=("Nope",))))
          and raises(lambda: AS.trend(engine, grain="week")) and raises(lambda: AS.top_anomalies(engine, order_by="colour")))
    vs = q("SELECT * FROM v_anomaly_summary").iloc[0]
    check("v_anomaly_summary agrees with the service", int(vs.anomalies) == s_all["anomalies"] and int(vs.high_risk) == s_all["high"]
          and abs(float(vs.anomaly_rate_pct) - s_all["anomaly_rate_pct"]) < 0.001)
    check("v_anomaly_detail holds every anomaly", scalar("SELECT COUNT(*) FROM v_anomaly_detail") == n_flag)

    # ================================================================== G
    print("\nG. FEATURE PROFILES (trained in memory, nothing written)")
    spreads = {}
    for prof in ("standard", "onehot", "compact"):
        mdl = ad.train(df, seed=settings.seed, profile=prof)
        sc_ = ad.score_frame(df, mdl, explain_flagged=False)
        yy = sc_.transaction_id.isin(unusual).to_numpy()
        a = roc_auc_score(yy, sc_.anomaly_score)
        rate = sc_.groupby("business_unit_id")["is_anomaly"].mean()
        spreads[prof] = rate.max() - rate.min()
        check(f"Profile '{prof}': {len(mdl.feature_names)} model inputs, ROC-AUC >= 0.95", a >= 0.95,
              f"AUC {a:.4f}; per-unit flag-rate spread {100 * spreads[prof]:.2f} points")
    check("One-hot business-unit dummies are measurably LESS fair than the default (why 'standard' has none)",
          spreads["onehot"] > 1.5 * spreads["standard"], f"{100 * spreads['onehot']:.2f} vs {100 * spreads['standard']:.2f} points")

    finish(runs_before)


def finish(runs_before):
    with engine.begin() as conn:        # remove the model_runs rows created by this verification
        conn.execute(text("DELETE FROM model_runs WHERE id > (SELECT id FROM (SELECT id FROM model_runs ORDER BY id LIMIT :n) x ORDER BY id DESC LIMIT 1)"),
                     {"n": runs_before})
    passed, total = sum(results), len(results)
    print(f"\n{'=' * 60}\nRESULT: {passed}/{total} checks passed")
    print("Phase 5 is complete. The anomaly model is reproducible, explained, and measurably effective."
          if passed == total else "Some checks failed - see FAIL lines above.")
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
