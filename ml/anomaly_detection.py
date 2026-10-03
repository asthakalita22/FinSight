"""
FinSight anomaly detection (Isolation Forest)
=============================================

Unsupervised: the model never sees a fraud label. It learns what ordinary transactions look like and
scores how easy each one is to ISOLATE with random splits. Few splits needed = unusual.

Pipeline
--------
    clean_for_scoring  ->  build_features  ->  IsolationForest  ->  score / flag / risk level  ->  explain

* anomaly_score   in (0, 1): the original Isolation Forest score 2^(-E[path length] / c(n)).
                  ~0.5 = nothing stands out, higher = more isolated. (scikit-learn returns the negative of this.)
* is_anomaly      score >= the (1 - contamination) quantile of the TRAINING scores (default: top 1%).
* risk_level      tail bands of the training scores: LOW = top `c`, MEDIUM = top `c/2`, HIGH = top `c/10`.
                  The cut-offs are stored with the model, so scoring new data uses the SAME thresholds.

Features
--------
Computed for every transaction (the spec's list):
    amount_log               log(1 + |amount in INR|)                       (INR so currencies are comparable)
    amount_deviation         robust z-score of amount_log inside its business-unit x transaction-type group,
                             i.e. "how far from this group's HISTORICAL AVERAGE is the amount"
    historical_average_log   log of that group's typical (median) amount - used by amount_deviation and the explanations
    hour                     hour of day, 0-23
    day_of_month             1-31
    transaction_frequency    how busy the day is for this business unit: robust z-score of its daily transaction
                             count (square-root scale) against ITS OWN normal day; a raw count would flag small units for being small
    bu_<id>                  one-hot business unit (only in the 'onehot' profile)

Which of them the FOREST sees is a "feature profile" (ANOMALY_FEATURE_SET in .env):
    standard (default)   amount_log, amount_deviation, hour, day_of_month, transaction_frequency.
                         Business unit enters through the two GROUP-RELATIVE features (amount_deviation is measured against
                         the unit's own typical amount, transaction_frequency against the unit's own normal day).
                         historical_average_log is not fed in raw: it is constant inside a group and already inside
                         amount_deviation.
    onehot               standard + one-hot business-unit dummies (the literal "encode business unit" reading).
                         Isolation Forest isolates rare binary features easily, so small units (Treasury, Investment
                         Banking) were flagged ~3x as often as large ones. Kept as an option to show the effect.
    compact              amount_log, amount_deviation, hour - sharper when only those carry signal (see README ablation)

Isolation Forest picks split features at random, so features that carry no signal dilute the ones that do. That is why
the profile matters, and why you should validate it on analyst feedback with your own data.

No scaling is applied: tree-based Isolation Forest chooses split points uniformly inside each feature's range,
so it is invariant to monotonic rescaling.

Cleaning (what is NOT scored, and why)
--------------------------------------
    missing transaction_id    cannot be referenced; the control engine raises CTL-001
    extra duplicate copies    the first copy represents the transaction; CTL-002 owns the copies
    amount <= 0 (non-adj.)    invalid value, not an unusual one; CTL-003 owns it
    unrecoverable fields      business unit / INR amount missing even after looking at the ledger
Fields lost by the source system (business unit, currency) are RECOVERED from the matching ledger entry.

Explainability
--------------
Every flagged transaction gets plain-English reasons derived from the same features (amount vs the group's typical
amount, rare hour of day, unusual daily volume). No black box: see `explain`.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from sklearn.ensemble import IsolationForest

from backend.config import settings

MODEL_FAMILY = "iforest-1.0"
MODEL_PATH = Path(__file__).with_name("model.pkl")

DEFAULT_CONTAMINATION = settings.anomaly_contamination
DEFAULT_TREES = settings.anomaly_trees
DEFAULT_MAX_SAMPLES = settings.anomaly_max_samples
DEFAULT_PROFILE = settings.anomaly_feature_set

# which engineered columns the forest is trained on (business-unit dummies are added for "standard")
FEATURE_PROFILES = {
    # business unit enters through the GROUP-RELATIVE amount_deviation and transaction_frequency (no dummies)
    "standard": {"numeric": ["amount_log", "amount_deviation", "hour", "day_of_month", "transaction_frequency"],
                 "business_unit_onehot": False},
    # the literal "encode business unit" reading of the spec: standard + one-hot dummies (measurably less fair, see README)
    "onehot": {"numeric": ["amount_log", "amount_deviation", "hour", "day_of_month", "transaction_frequency"],
               "business_unit_onehot": True},
    "compact": {"numeric": ["amount_log", "amount_deviation", "hour"], "business_unit_onehot": False},
}

NUMERIC_FEATURES = ["amount_log", "amount_deviation", "historical_average_log", "hour", "day_of_month",
                    "transaction_frequency"]                # all computed; each profile picks what the forest sees
RISK_ORDER = ["LOW", "MEDIUM", "HIGH"]

# thresholds used by the explanations
AMOUNT_Z_THRESHOLD = 2.0       # |robust z| of the amount inside its group (~ the 2-sigma 'notable' level)
RARE_HOUR_SHARE = 0.005        # an hour holding < 0.5% of all training transactions is "unusual"
FREQUENCY_Z_THRESHOLD = 3.0
MIN_SCALE = 0.1                # floor for a group's robust spread, so tiny groups cannot blow up the z-score


# ----------------------------------------------------------------------------
# 1. Cleaning
# ----------------------------------------------------------------------------
def clean_for_scoring(txn: pd.DataFrame, ledger: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    """Return (rows to score, counts of what was left out and why).
    `txn` needs: id, transaction_id, transaction_date, business_unit_id, product, currency, amount, amount_inr,
    transaction_type, created_at.   `ledger` needs: transaction_id, business_unit_id, amount_inr."""
    t = txn.copy()
    excluded = {}

    no_id = t["transaction_id"].isna()
    excluded["missing_transaction_id"] = int(no_id.sum())
    t = t[~no_id].sort_values(["created_at", "id"])

    extra = t["transaction_id"].duplicated(keep="first")
    excluded["duplicate_copy"] = int(extra.sum())
    t = t[~extra].copy()

    # recover what the source system lost from the ledger entry with the same transaction_id
    led1 = ledger.drop_duplicates("transaction_id").set_index("transaction_id")
    bu = t["business_unit_id"].astype("float64")
    t["business_unit_id"] = bu.fillna(t["transaction_id"].map(led1["business_unit_id"].astype("float64"))).astype("Int64")
    t["amount_inr"] = t["amount_inr"].fillna(t["transaction_id"].map(led1["amount_inr"]))

    invalid = (t["amount"] <= 0) & (t["transaction_type"] != "ADJUSTMENT")
    excluded["invalid_amount"] = int(invalid.sum())
    t = t[~invalid]

    unresolved = t["business_unit_id"].isna() | t["amount_inr"].isna() | (t["amount_inr"] == 0)
    excluded["unrecoverable_fields"] = int(unresolved.sum())
    t = t[~unresolved].copy()

    t["business_unit_id"] = t["business_unit_id"].astype(int)
    t["ts"] = pd.to_datetime(t["transaction_date"])
    t["date"] = t["ts"].dt.normalize()
    return t.sort_values("id").reset_index(drop=True), excluded


# ----------------------------------------------------------------------------
# 2. Features
# ----------------------------------------------------------------------------
@dataclass
class GroupStats:
    """Everything learned from the training data that feature building needs (so scoring is reproducible)."""
    group: dict = field(default_factory=dict)        # (business_unit_id, transaction_type) -> (median, scale) of amount_log
    global_median: float = 0.0
    global_scale: float = 1.0
    hour_share: dict = field(default_factory=dict)   # hour -> share of training transactions
    freq: dict = field(default_factory=dict)         # business_unit_id -> (median, scale) of sqrt(transactions per day)
    freq_median: float = 0.0                         # global fallback for a business unit never seen in training
    freq_scale: float = 1.0


def _robust_scale(values: pd.Series) -> float:
    mad = float(np.median(np.abs(values - np.median(values))))
    return max(1.4826 * mad, MIN_SCALE)


def _freq_scale(sqrt_counts: pd.Series) -> float:
    """Robust spread of a unit's daily transaction count ON THE SQUARE-ROOT SCALE (the variance-stabilising transform
    for counts: a Poisson count has roughly constant spread there, so small units are not penalised for their skew)."""
    mad = float(np.median(np.abs(sqrt_counts - np.median(sqrt_counts))))
    return max(1.4826 * mad, 0.15)


def _add_base_columns(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out["amount_log"] = np.log1p(out["amount_inr"].abs())
    out["hour"] = out["ts"].dt.hour
    out["day_of_month"] = out["ts"].dt.day
    out["daily_count"] = out.groupby(["business_unit_id", "date"])["id"].transform("size")
    return out


def fit_group_stats(df: pd.DataFrame) -> GroupStats:
    d = _add_base_columns(df)
    stats = GroupStats(global_median=float(d["amount_log"].median()), global_scale=_robust_scale(d["amount_log"]))
    for key, g in d.groupby(["business_unit_id", "transaction_type"]):
        stats.group[(int(key[0]), str(key[1]))] = (float(g["amount_log"].median()), _robust_scale(g["amount_log"]))
    stats.hour_share = {int(h): float(s) for h, s in d["hour"].value_counts(normalize=True).items()}
    daily = d.groupby(["business_unit_id", "date"]).size().rename("n").reset_index()
    daily["r"] = np.sqrt(daily["n"])
    for bu, g in daily.groupby("business_unit_id"):
        stats.freq[int(bu)] = (float(g["r"].median()), _freq_scale(g["r"]))
    stats.freq_median, stats.freq_scale = float(daily["r"].median()), _freq_scale(daily["r"])
    return stats


def build_features(df: pd.DataFrame, stats: GroupStats, bu_categories: list[int]) -> pd.DataFrame:
    """Numeric feature matrix. Groups never seen in training fall back to the global statistics."""
    d = _add_base_columns(df)
    keys = list(zip(d["business_unit_id"].astype(int), d["transaction_type"].astype(str)))
    med = np.array([stats.group.get(k, (stats.global_median, stats.global_scale))[0] for k in keys])
    scale = np.array([stats.group.get(k, (stats.global_median, stats.global_scale))[1] for k in keys])
    d["historical_average_log"] = med
    d["amount_deviation"] = (d["amount_log"].to_numpy() - med) / scale
    # transaction_frequency: how busy is today for THIS business unit compared with its own normal day (robust z-score).
    # A raw count would flag low-volume units simply for being small.
    fm = np.array([stats.freq.get(int(b), (stats.freq_median, stats.freq_scale))[0] for b in d["business_unit_id"]])
    fs = np.array([stats.freq.get(int(b), (stats.freq_median, stats.freq_scale))[1] for b in d["business_unit_id"]])
    d["transaction_frequency"] = (np.sqrt(d["daily_count"].to_numpy()) - fm) / fs
    X = d[NUMERIC_FEATURES + ["daily_count"]].astype(float).copy()          # daily_count is for explanations only
    for b in bu_categories:                                    # one-hot business unit
        X[f"bu_{b}"] = (d["business_unit_id"].astype(int) == b).astype(float)
    X.index = df.index
    return X


# ----------------------------------------------------------------------------
# 3. Model
# ----------------------------------------------------------------------------
@dataclass
class AnomalyModel:
    forest: IsolationForest
    stats: GroupStats
    bu_categories: list
    feature_names: list
    threshold_flag: float
    threshold_medium: float
    threshold_high: float
    contamination: float
    seed: int
    trained_on: int
    version: str
    profile: str = "standard"

    # -- scoring
    def score(self, X: pd.DataFrame) -> np.ndarray:
        """Anomaly score in (0, 1); higher = more anomalous."""
        return -self.forest.score_samples(X[self.feature_names])

    def is_anomaly(self, scores) -> np.ndarray:
        return np.asarray(scores) >= self.threshold_flag

    def risk_level(self, scores) -> np.ndarray:
        """None (not an anomaly) / LOW / MEDIUM / HIGH."""
        s = np.asarray(scores)
        return np.select([s >= self.threshold_high, s >= self.threshold_medium, s >= self.threshold_flag],
                         ["HIGH", "MEDIUM", "LOW"], default=None).astype(object)

    # -- persistence
    def save(self, path: Path = MODEL_PATH) -> Path:
        joblib.dump(self, path, compress=3)
        return Path(path)

    @staticmethod
    def load(path: Path = MODEL_PATH) -> "AnomalyModel":
        """Only load model files you created yourself (joblib/pickle can execute code)."""
        return joblib.load(path)


def make_version(feature_names, contamination, trees, max_samples, seed, trained_on, profile="standard") -> str:
    fingerprint = f"{profile}|{feature_names}|{contamination}|{trees}|{max_samples}|{seed}|{trained_on}"
    return f"{MODEL_FAMILY}+{hashlib.sha1(fingerprint.encode()).hexdigest()[:7]}"


def model_columns(profile: str, bu_categories: list[int]) -> list[str]:
    if profile not in FEATURE_PROFILES:
        raise ValueError(f"Unknown feature profile '{profile}'. Choose one of {sorted(FEATURE_PROFILES)}")
    spec = FEATURE_PROFILES[profile]
    return list(spec["numeric"]) + ([f"bu_{b}" for b in bu_categories] if spec["business_unit_onehot"] else [])


def train(df: pd.DataFrame, seed: int = 42, contamination: float = DEFAULT_CONTAMINATION,
          trees: int = DEFAULT_TREES, max_samples: int = DEFAULT_MAX_SAMPLES,
          profile: str = DEFAULT_PROFILE) -> AnomalyModel:
    """Fit the group statistics and the Isolation Forest on cleaned transactions, and derive the score thresholds."""
    if not 0 < contamination < 0.5:
        raise ValueError("contamination must be between 0 and 0.5")
    if len(df) < 100:
        raise ValueError(f"Need at least 100 clean transactions to train, got {len(df)}")
    stats = fit_group_stats(df)
    bu_categories = sorted(int(b) for b in df["business_unit_id"].unique())
    columns = model_columns(profile, bu_categories)
    X = build_features(df, stats, bu_categories)[columns]
    forest = IsolationForest(n_estimators=trees, max_samples=min(max_samples, len(df)), contamination="auto",
                             random_state=seed, n_jobs=-1).fit(X)
    scores = -forest.score_samples(X)
    return AnomalyModel(
        forest=forest, stats=stats, bu_categories=bu_categories, feature_names=columns,
        threshold_flag=float(np.quantile(scores, 1 - contamination)),
        threshold_medium=float(np.quantile(scores, 1 - contamination / 2)),
        threshold_high=float(np.quantile(scores, 1 - contamination / 10)),
        contamination=contamination, seed=seed, trained_on=len(df), profile=profile,
        version=make_version(columns, contamination, trees, min(max_samples, len(df)), seed, len(df), profile))


# ----------------------------------------------------------------------------
# 4. Explanations
# ----------------------------------------------------------------------------
def _m(x: float) -> str:
    return f"INR {x / 1e6:,.2f}M" if abs(x) >= 1e6 else f"INR {x:,.0f}"


def explain(df: pd.DataFrame, model: AnomalyModel, bu_names: dict | None = None) -> list[str]:
    """Plain-English reasons for each row of `df` (cleaned transactions), strongest signal first."""
    bu_names = bu_names or {}
    X = build_features(df, model.stats, model.bu_categories)
    normal_hours = sorted(h for h, s in model.stats.hour_share.items() if s >= 0.02)
    hours_txt = f"{min(normal_hours):02d}:00-{max(normal_hours):02d}:59" if normal_hours else "usual hours"
    out = []
    for i in range(len(df)):
        row = df.iloc[i]
        x = X.iloc[i]
        unit = bu_names.get(int(row["business_unit_id"]), f"unit {int(row['business_unit_id'])}")
        found = []                                                         # (strength, text)
        z = float(x["amount_deviation"])
        if abs(z) >= AMOUNT_Z_THRESHOLD:
            typical = float(np.expm1(x["historical_average_log"]))
            ratio = abs(row["amount_inr"]) / typical if typical else float("inf")
            word = f"{ratio:.1f}x the typical" if z > 0 else f"only {ratio:.2f}x the typical"
            found.append((abs(z), f"Amount {_m(abs(row['amount_inr']))} is {word} {unit} {row['transaction_type']} "
                                  f"transaction (median {_m(typical)})"))
        share = model.stats.hour_share.get(int(row["ts"].hour), 0.0)
        if share < RARE_HOUR_SHARE:
            found.append((10 * (RARE_HOUR_SHARE - share) / RARE_HOUR_SHARE + 2,
                          f"Booked at {row['ts']:%H:%M}, outside the normal {hours_txt} window "
                          f"(only {100 * share:.2f}% of transactions occur at this hour)"))
        fz = float(x["transaction_frequency"])
        if fz >= FREQUENCY_Z_THRESHOLD:
            typical = model.stats.freq.get(int(row["business_unit_id"]), (model.stats.freq_median, 1.0))[0] ** 2
            found.append((fz, f"{unit} booked {int(x['daily_count'])} transactions that day (typical {typical:.0f})"))
        found.sort(key=lambda p: -p[0])
        out.append("; ".join(t for _, t in found) if found else
                   "Unusual combination of features (no single feature is extreme)")
    return out


# ----------------------------------------------------------------------------
# 5. Convenience: score a cleaned frame
# ----------------------------------------------------------------------------
def score_frame(df: pd.DataFrame, model: AnomalyModel, bu_names: dict | None = None,
                explain_flagged: bool = True) -> pd.DataFrame:
    """Score every row; returns `df` with anomaly_score, is_anomaly, risk_level, reasons (reasons only for flagged rows)."""
    X = build_features(df, model.stats, model.bu_categories)
    scores = model.score(X)
    out = df.copy()
    out["anomaly_score"] = scores
    out["is_anomaly"] = model.is_anomaly(scores)
    out["risk_level"] = model.risk_level(scores)
    out["reasons"] = None
    if explain_flagged and out["is_anomaly"].any():
        flagged = out["is_anomaly"].to_numpy()
        out.loc[flagged, "reasons"] = explain(df[flagged], model, bu_names)
    return out
