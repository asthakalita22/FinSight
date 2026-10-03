from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_phase10_pages_exist():
    assert (ROOT / "frontend" / "pages" / "05_Risk_Anomalies.py").exists()
    assert (ROOT / "frontend" / "pages" / "06_Liquidity.py").exists()


def test_risk_page_uses_phase6_api_contract():
    text = (ROOT / "frontend" / "pages" / "05_Risk_Anomalies.py").read_text(encoding="utf-8")
    for endpoint in ["/api/v1/anomalies/summary", "/api/v1/anomalies/trend", "/api/v1/anomalies/top", "/api/v1/anomalies/latest-run"]:
        assert endpoint in text


def test_liquidity_page_uses_phase6_api_contract():
    text = (ROOT / "frontend" / "pages" / "06_Liquidity.py").read_text(encoding="utf-8")
    for endpoint in ["/api/v1/liquidity/summary", "/api/v1/liquidity/monthly", "/api/v1/liquidity/daily", "/api/v1/liquidity/business-units"]:
        assert endpoint in text


def test_phase10_has_no_write_endpoints():
    for filename in ["05_Risk_Anomalies.py", "06_Liquidity.py"]:
        text = (ROOT / "frontend" / "pages" / filename).read_text(encoding="utf-8")
        assert ".post(" not in text
        assert ".patch(" not in text
        assert ".delete(" not in text
