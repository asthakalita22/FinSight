from pathlib import Path


ROOT = Path(__file__).parents[1]
OVERVIEW = (ROOT / "frontend" / "pages" / "01_Overview.py").read_text(encoding="utf-8")
CHARTS = (ROOT / "frontend" / "components" / "charts.py").read_text(encoding="utf-8")
STYLES = (ROOT / "frontend" / "components" / "styles.py").read_text(encoding="utf-8")


def test_overview_uses_financial_units_and_hides_plotly_modebar():
    assert 'chart_df[column] = chart_df[column] / 1_000_000' in OVERVIEW
    assert 'displayModeBar' in OVERVIEW


def test_charts_use_finsight_palette_and_no_duplicate_plot_titles():
    assert 'COLORS["yellow"]' in CHARTS
    assert 'COLORS["black"]' in CHARTS
    assert 'title=None' in CHARTS


def test_risk_summary_uses_shared_visual_system():
    assert "fs-risk-high" in STYLES
    assert "fs-risk-medium" in STYLES
    assert "fs-risk-other" in STYLES
