"""Unit tests for the ETL validate / transform stages - no database needed."""
import pandas as pd

from backend.services import etl_service as etl


def raw(table, rows):
    cols = etl.TABLE_SPECS[table]["columns"]
    return pd.DataFrame([{c: r.get(c) for c in cols} for r in rows], dtype="object").astype("str").where(
        lambda d: d != "None", None)


def good_txn(**kw):
    base = dict(transaction_id="txn-1", transaction_date="2026-01-05 10:00:00", business_unit_id="1",
                product="Equity", currency="usd", amount="10.50", transaction_type="revenue",
                source_system="TRADING_SYS", created_at="2026-01-05 10:01:00")
    return {**base, **kw}


# ---------------------------------------------------------------- validate
def test_validate_keeps_good_rows_and_types_them():
    good, bad = etl.validate("transactions", raw("transactions", [good_txn()]))
    assert len(good) == 1 and len(bad) == 0
    assert str(good["transaction_date"].dtype).startswith("datetime64")
    assert good["amount"].iloc[0] == 10.5


def test_validate_quarantines_structural_problems_with_reasons():
    rows = [
        good_txn(transaction_date="not-a-date"),
        good_txn(amount="abc"),
        good_txn(product=None),
        good_txn(transaction_type="GIFT"),
        good_txn(business_unit_id="1.5"),
        good_txn(),                                   # the only good one
    ]
    good, bad = etl.validate("transactions", raw("transactions", rows))
    assert len(good) == 1
    assert list(bad["reject_reason"]) == [
        "UNPARSEABLE_DATE:transaction_date", "NON_NUMERIC:amount", "MISSING_REQUIRED:product",
        "INVALID_TRANSACTION_TYPE", "NOT_INTEGER:business_unit_id"]


def test_business_rule_problems_are_NOT_rejected():
    """Missing id / currency / business unit must load so the controls can raise exceptions for them."""
    rows = [good_txn(transaction_id=None), good_txn(currency=None), good_txn(business_unit_id=None)]
    good, bad = etl.validate("transactions", raw("transactions", rows))
    assert len(good) == 3 and len(bad) == 0


def test_validate_rejects_missing_column_contract():
    import pytest
    df = pd.DataFrame({"id": ["1"]})
    with pytest.raises(KeyError):
        etl.validate("business_units", df)


# ---------------------------------------------------------------- transform
def make_tables(txn_rows, ledger_rows=()):
    ref = {
        "business_units": [dict(id="1", name="Equities", division="Markets", region="Mumbai")],
        "fx_rates": [dict(currency="INR", name="Rupee", rate_to_inr="1"),
                     dict(currency="USD", name="Dollar", rate_to_inr="80")],
        "transactions": txn_rows,
        "ledger_entries": list(ledger_rows),
    }
    # every table in the ETL contract must be present; unlisted ones are empty (so this helper cannot go stale)
    ref = {t: ref.get(t, []) for t in etl.TABLE_SPECS}
    return {t: etl.validate(t, raw(t, rows) if rows else pd.DataFrame(columns=etl.TABLE_SPECS[t]["columns"], dtype="object"))[0]
            for t, rows in ref.items()}


def test_transform_standardises_text_and_derives_amount_inr():
    tables, rejected, warns = etl.transform(make_tables([good_txn()]))
    t = tables["transactions"].iloc[0]
    assert (t["transaction_id"], t["currency"], t["transaction_type"]) == ("TXN-1", "USD", "REVENUE")
    assert t["amount_inr"] == 840.0                              # 10.50 x 80
    assert rejected == [] and warns == {}


def test_transform_unknown_currency_gives_null_amount_inr_and_warning():
    tables, _, warns = etl.transform(make_tables([good_txn(currency="XXX"), good_txn(currency=None)]))
    assert tables["transactions"]["amount_inr"].isna().all()
    assert any("without_amount_inr" in k for k in warns)


def test_transform_unknown_business_unit_on_transaction_becomes_null_not_rejected():
    tables, rejected, warns = etl.transform(make_tables([good_txn(business_unit_id="99")]))
    assert pd.isna(tables["transactions"]["business_unit_id"].iloc[0])
    assert rejected == []
    assert warns["transactions_unmapped_business_unit_set_null"] == 1


def test_transform_unknown_business_unit_on_ledger_is_rejected():
    ledger_rows = [dict(transaction_id="t1", ledger_date="2026-01-06", amount="5", currency="usd", business_unit_id="99"),
                   dict(transaction_id="t2", ledger_date="2026-01-06", amount="5", currency="usd", business_unit_id="1")]
    tables, rejected, _ = etl.transform(make_tables([good_txn()], ledger_rows))
    assert len(tables["ledger_entries"]) == 1
    assert rejected[0]["reject_reason"].iloc[0] == "UNKNOWN_BUSINESS_UNIT"
    assert tables["ledger_entries"]["ledger_date"].iloc[0] == "2026-01-06"      # date-only string for COPY
