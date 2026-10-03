"""Source-level contract tests for the Phase 9 controls UI."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_reconciliation_page_is_phase9_ui():
    source = _read("frontend/pages/03_Reconciliation.py")
    for endpoint in [
        "/api/v1/reconciliation/summary",
        "/api/v1/reconciliation/daily",
        "/api/v1/reconciliation/by-business-unit",
        "/api/v1/reconciliation/details",
    ]:
        assert endpoint in source
    for label in ["Match Rate", "Mismatched", "Missing", "Duplicates", "Invalid", "Reconciliation Detail"]:
        assert label in source


def test_exceptions_page_supports_required_controls():
    source = _read("frontend/pages/04_Exceptions.py")
    for endpoint in [
        "/api/v1/exceptions/summary",
        "/api/v1/exceptions/by-category",
        "/api/v1/exceptions/recent",
    ]:
        assert endpoint in source
    for label in ["Severity", "Status", "Category", "Exposure", "Exception Review Queue"]:
        assert label in source


def test_reconciliation_backend_detail_endpoint_is_read_only():
    source = _read("backend/routers/reconciliation.py")
    assert '@router.get("/details")' in source
    assert "FROM reconciliations r" in source
    assert "LIMIT :limit" in source
    assert "@router.post" not in source
    assert "@router.patch" not in source
    assert "@router.delete" not in source


def test_exception_backend_supports_category_filter():
    source = _read("backend/routers/exceptions.py")
    assert "category: list[str] | None" in source
    assert 'category = ANY(:category)' in source


def test_phase9_pages_keep_project_import_bootstrap():
    for path in ["frontend/pages/03_Reconciliation.py", "frontend/pages/04_Exceptions.py"]:
        source = _read(path)
        assert "PROJECT_ROOT = Path(__file__).resolve().parents[2]" in source
        assert "sys.path.insert(0, str(PROJECT_ROOT))" in source
