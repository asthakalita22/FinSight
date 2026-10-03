"""Unit tests for the anomaly-detection module - synthetic data, no database."""
import numpy as np
import pandas as pd
import pytest

from ml import anomaly_detection as ad
from tests.test_controls import ledger, txn

BU_NAMES = {1: "Equities", 2: "Treasury", 3: "Derivatives"}


def synthetic(n=3000, seed=0, outliers=0, outlier_hour=3, outlier_factor=60):
    """Ordinary trading-hours transactions, plus `outliers` huge off-hours ones appended at the end."""
    rng = np.random.default_rng(seed)
    ts = (pd.Timestamp("2026-01-05") + pd.to_timedelta(rng.integers(0, 40, n), unit="D")
          + pd.to_timedelta(rng.integers(8, 18, n) * 3600 + rng.integers(0, 3600, n), unit="s"))
    df = pd.DataFrame({"id": np.arange(1, n + 1), "transaction_id": [f"T{i}" for i in range(1, n + 1)],
                       "business_unit_id": rng.integers(1, 4, n), "transaction_type": rng.choice(["REVENUE", "COST"], n),
                       "amount_inr": np.round(rng.lognormal(10, 0.5, n), 2), "ts": ts})
    if outliers:
        extra = df.iloc[:outliers].copy()
        extra["id"] = np.arange(n + 1, n + outliers + 1)
        extra["transaction_id"] = [f"OUT{i}" for i in range(outliers)]
        extra["amount_inr"] = extra["amount_inr"] * outlier_factor
        extra["ts"] = extra["ts"].dt.normalize() + pd.Timedelta(hours=outlier_hour, minutes=14)
        df = pd.concat([df, extra], ignore_index=True)
    df["date"] = df["ts"].dt.normalize()
    return df


# ---------------------------------------------------------------- cleaning
def test_cleaning_excludes_and_recovers_with_exact_counts():
    t = txn([
        {"transaction_id": "OK"},                                                                   # kept
        {"transaction_id": None},                                                                   # no id
        {"transaction_id": "DUP", "created_at": pd.Timestamp("2026-01-05 10:00")},                  # kept (oldest copy)
        {"transaction_id": "DUP", "created_at": pd.Timestamp("2026-01-06 10:00")},                  # extra copy
        {"transaction_id": "BAD", "amount": 0.0, "amount_inr": 0.0},                                # invalid amount
        {"transaction_id": "ADJ", "transaction_type": "ADJUSTMENT", "amount": -5.0, "amount_inr": -5.0},   # negative adjustment is legal
        {"transaction_id": "NOBU", "business_unit_id": pd.NA},                                      # recoverable from ledger
        {"transaction_id": "NOINR", "currency": None, "amount_inr": np.nan},                        # recoverable from ledger
        {"transaction_id": "LOST", "business_unit_id": pd.NA},                                      # not in ledger: unrecoverable
    ])
    l = ledger([{"transaction_id": "NOBU", "business_unit_id": 7}, {"transaction_id": "NOINR", "amount_inr": 321.0}])
    df, excl = ad.clean_for_scoring(t, l)
    assert excl == {"missing_transaction_id": 1, "duplicate_copy": 1, "invalid_amount": 1, "unrecoverable_fields": 1}
    assert sorted(df["transaction_id"]) == ["ADJ", "DUP", "NOBU", "NOINR", "OK"]
    assert df.loc[df.transaction_id == "NOBU", "business_unit_id"].iloc[0] == 7
    assert df.loc[df.transaction_id == "NOINR", "amount_inr"].iloc[0] == 321.0
    assert df.loc[df.transaction_id == "DUP", "id"].iloc[0] == 3                                   # the oldest copy survives


# ---------------------------------------------------------------- features
def test_features_log_amount_frequency_and_group_deviation():
    df = synthetic(2000)
    stats = ad.fit_group_stats(df)
    X = ad.build_features(df, stats, [1, 2, 3])
    assert np.allclose(X["amount_log"], np.log1p(df["amount_inr"]))
    assert (X["hour"] == df["ts"].dt.hour).all() and (X["day_of_month"] == df["ts"].dt.day).all()
    expected = df.groupby(["business_unit_id", "date"])["id"].transform("size")
    assert (X["daily_count"] == expected).all()                            # raw count kept for the explanations
    assert abs(X["amount_deviation"].median()) < 0.1                       # centred on each group's median
    assert 0.8 < X["amount_deviation"].std() < 1.3                         # ~unit spread for a roughly normal log-amount
    assert all(f"bu_{b}" in X for b in (1, 2, 3))                          # dummies are available to the 'onehot' profile


def test_transaction_frequency_is_relative_to_each_business_units_own_normal_day():
    """A small unit with 5 trades a day and a big one with 50 are both 'normal' on an ordinary day."""
    rng = np.random.default_rng(3)
    days = pd.date_range("2026-01-05", periods=60)
    rows = []
    for bu, per_day in ((1, 50), (2, 5)):
        for d in days:
            for _ in range(max(1, int(rng.normal(per_day, per_day * 0.1)))):
                rows.append({"business_unit_id": bu, "ts": d + pd.Timedelta(hours=10), "date": d})
    df = pd.DataFrame(rows)
    df["id"] = np.arange(len(df)) + 1
    df["transaction_id"] = df["id"].map(lambda i: f"T{i}")
    df["transaction_type"], df["amount_inr"] = "REVENUE", 1000.0
    X = ad.build_features(df, ad.fit_group_stats(df), [1, 2])
    per_day = X.groupby([df["business_unit_id"], df["date"]])["transaction_frequency"].first()      # one value per unit-day
    med = per_day.groupby(level=0).median()
    assert abs(med[1]) < 0.3 and abs(med[2]) < 0.3                         # both centred near 0 despite 10x different volume
    assert per_day.groupby(level=0).apply(lambda s: s.abs().max()).max() < 4          # and neither has runaway values
    busy = df[(df.business_unit_id == 2) & (df.date == days[0])].copy()
    extra = pd.concat([busy] * 12, ignore_index=True)                       # a day with 12x the usual volume for the small unit
    extra["id"] = np.arange(10_000, 10_000 + len(extra))
    both = pd.concat([df, extra], ignore_index=True)
    Xb = ad.build_features(both, ad.fit_group_stats(df), [1, 2])
    assert Xb["transaction_frequency"].iloc[-1] > 5                         # clearly flagged as a busy day for that unit


def test_identical_amounts_do_not_blow_up_the_deviation():
    df = synthetic(500)
    df["amount_inr"] = 1000.0                                              # zero spread inside every group
    df.loc[0, "amount_inr"] = 2000.0
    X = ad.build_features(df, ad.fit_group_stats(df), [1, 2, 3])
    assert np.isfinite(X["amount_deviation"]).all()
    assert X["amount_deviation"].iloc[0] == pytest.approx((np.log1p(2000) - np.log1p(1000)) / ad.MIN_SCALE)


def test_group_never_seen_in_training_falls_back_to_global_statistics():
    df = synthetic(500)
    stats = ad.fit_group_stats(df)
    new = df.iloc[:5].copy()
    new["business_unit_id"] = 99                                           # unknown business unit
    X = ad.build_features(new, stats, [1, 2, 3])
    assert np.isfinite(X["amount_deviation"]).all()
    assert (X[["bu_1", "bu_2", "bu_3"]].sum(axis=1) == 0).all()            # one-hot is all zeros, not an error


# ---------------------------------------------------------------- model
def test_training_validation():
    with pytest.raises(ValueError):
        ad.train(synthetic(500), contamination=0.9, trees=10)
    with pytest.raises(ValueError):
        ad.train(synthetic(50), trees=10)
    with pytest.raises(ValueError):
        ad.train(synthetic(500), trees=10, profile="nonsense")


def test_training_is_deterministic_per_seed():
    df = synthetic(1500)
    a, b, c = ad.train(df, seed=1, trees=50), ad.train(df, seed=1, trees=50), ad.train(df, seed=2, trees=50)
    X = ad.build_features(df, a.stats, a.bu_categories)
    assert np.array_equal(a.score(X), b.score(X)) and a.version == b.version
    assert not np.array_equal(a.score(X), c.score(X)) and a.version != c.version


def test_thresholds_and_flag_rate_follow_contamination():
    df = synthetic(4000)
    m = ad.train(df, contamination=0.02, trees=80)
    assert m.threshold_flag < m.threshold_medium < m.threshold_high
    s = ad.score_frame(df, m, explain_flagged=False)
    assert s["is_anomaly"].mean() == pytest.approx(0.02, abs=0.002)
    assert (s["risk_level"] == "HIGH").mean() == pytest.approx(0.002, abs=0.001)         # top c/10
    assert ((s["risk_level"] == "HIGH") | (s["risk_level"] == "MEDIUM")).mean() == pytest.approx(0.01, abs=0.002)  # top c/2
    assert s["risk_level"].isna().sum() == (~s["is_anomaly"]).sum()
    assert s["anomaly_score"].between(0, 1).all()


def test_risk_level_boundaries_are_exact():
    m = ad.train(synthetic(1000), trees=20)
    f, md, h = m.threshold_flag, m.threshold_medium, m.threshold_high
    got = list(m.risk_level([f - 1e-6, f, md - 1e-6, md, h - 1e-6, h, 0.99]))
    assert got == [None, "LOW", "LOW", "MEDIUM", "MEDIUM", "HIGH", "HIGH"]


def test_planted_outliers_are_found_ranked_first_and_explained():
    df = synthetic(3000, outliers=6)                                       # 6 huge off-hours transactions
    m = ad.train(df, contamination=0.01, trees=150)
    s = ad.score_frame(df, m, BU_NAMES)
    planted = s[s["transaction_id"].str.startswith("OUT")]
    assert planted["is_anomaly"].all()
    assert planted["anomaly_score"].min() > s[~s["transaction_id"].str.startswith("OUT")]["anomaly_score"].quantile(0.999)
    assert (planted["risk_level"] == "HIGH").sum() >= 3
    for reason in planted["reasons"]:
        assert "Booked at 03:14" in reason and "x the typical" in reason


# ---------------------------------------------------------------- persistence and fixed thresholds
def test_saved_model_reproduces_scores_and_keeps_its_thresholds(tmp_path):
    df = synthetic(2000)
    m = ad.train(df, trees=50)
    path = m.save(tmp_path / "model.pkl")
    loaded = ad.AnomalyModel.load(path)
    X = ad.build_features(df, m.stats, m.bu_categories)
    assert np.allclose(m.score(X), loaded.score(X), atol=1e-12)
    assert (loaded.threshold_flag, loaded.threshold_high, loaded.version) == (m.threshold_flag, m.threshold_high, m.version)

    # thresholds are FIXED at training time: a batch full of outliers is flagged far more often than 1%
    wild = synthetic(400, seed=5, outliers=100)
    flagged_share = ad.score_frame(wild.tail(100).reset_index(drop=True), loaded, explain_flagged=False)["is_anomaly"].mean()
    assert flagged_share > 0.8


# ---------------------------------------------------------------- profiles
def test_feature_profiles():
    df = synthetic(600)
    std, one, cmp_ = (ad.train(df, trees=20, profile=p) for p in ("standard", "onehot", "compact"))
    five = ["amount_log", "amount_deviation", "hour", "day_of_month", "transaction_frequency"]
    assert std.feature_names == five                                         # no raw historical average, no dummies
    assert one.feature_names == five + ["bu_1", "bu_2", "bu_3"]
    assert cmp_.feature_names == ["amount_log", "amount_deviation", "hour"]
    assert len({std.version, one.version, cmp_.version}) == 3 and std.profile == "standard"


def test_one_hot_business_units_make_small_units_look_anomalous_but_the_default_does_not():
    """Regression test for a real finding: Isolation Forest isolates rare binary dummies, so a small unit gets flagged
    far more often. The default profile must keep per-unit flag rates close together."""
    rng = np.random.default_rng(11)
    n = 6000
    df = synthetic(n, seed=11)
    df["business_unit_id"] = rng.choice([1, 2, 3], size=n, p=[0.6, 0.34, 0.06])          # unit 3 is small
    df["date"] = df["ts"].dt.normalize()

    def rate_spread(profile):
        m = ad.train(df, trees=120, profile=profile, contamination=0.02)
        flagged = ad.score_frame(df, m, explain_flagged=False)["is_anomaly"]
        rates = flagged.groupby(df["business_unit_id"]).mean()
        return rates.max() - rates.min(), rates[3]

    std_spread, std_small = rate_spread("standard")
    one_spread, one_small = rate_spread("onehot")
    assert std_small < 0.04                                                             # about the 2% target, not 3x it
    assert one_small > std_small and one_spread > std_spread


# ---------------------------------------------------------------- explanations
def test_explanations_cover_amount_small_amount_hour_and_volume_and_fallback():
    df = synthetic(2500)
    m = ad.train(df, trees=50)
    rows = df.iloc[:4].copy()
    rows["amount_inr"] = [df["amount_inr"].median() * 40, df["amount_inr"].median() / 40, df["amount_inr"].median(), df["amount_inr"].median()]
    rows["ts"] = [rows["ts"].iloc[0], rows["ts"].iloc[1], rows["ts"].iloc[2].normalize() + pd.Timedelta(hours=2), rows["ts"].iloc[3]]
    rows["transaction_type"] = "REVENUE"
    rows["business_unit_id"] = 1
    reasons = ad.explain(rows, m, BU_NAMES)
    assert "x the typical Equities REVENUE" in reasons[0]
    assert "only 0.0" in reasons[1] and "the typical Equities REVENUE" in reasons[1]
    assert "Booked at 02:00" in reasons[2] and "08:00-17:59" in reasons[2]
    assert reasons[3].startswith("Unusual combination") or "typical" in reasons[3]       # nothing extreme -> fallback or mild note


def test_explanation_strongest_signal_comes_first():
    df = synthetic(2500)
    m = ad.train(df, trees=50)
    med = df["amount_inr"].median()

    def reason(multiple):
        r = df.iloc[:1].copy()
        r["amount_inr"] = med * multiple
        r["ts"] = r["ts"].dt.normalize() + pd.Timedelta(hours=3)                          # always off-hours
        r["transaction_type"], r["business_unit_id"] = "REVENUE", 1
        return ad.explain(r, m, BU_NAMES)[0]

    huge, mild = reason(5000), reason(3)
    assert huge.startswith("Amount") and "; Booked at 03:" in huge            # enormous amount outranks the odd hour
    assert mild.startswith("Booked at 03:") and "; Amount" in mild            # mildly high amount ranks below it
    assert huge.count(";") == 1 and mild.count(";") == 1                      # exactly two reasons each
