"""Small HTTP client for the read-only FinSight FastAPI service."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import Any

import requests


@dataclass
class APIError(RuntimeError):
    """Structured error raised for failed API requests."""

    message: str
    status_code: int | None = None

    def __str__(self) -> str:
        if self.status_code is None:
            return self.message
        return f"{self.message} (HTTP {self.status_code})"


class FinSightAPI:
    """Read-only client for the FinSight API."""

    def __init__(self, base_url: str = "http://localhost:8000", timeout: float = 10.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """GET a JSON API endpoint and return its decoded payload."""
        url = f"{self.base_url}/{path.lstrip('/')}"
        try:
            response = requests.get(url, params=params, timeout=self.timeout)
        except requests.RequestException as exc:
            raise APIError(f"Could not reach FinSight API at {self.base_url}: {exc}") from exc

        if response.ok:
            return response.json()

        try:
            detail = response.json().get("detail", response.text)
        except ValueError:
            detail = response.text
        raise APIError(str(detail), response.status_code)

    def health(self) -> dict[str, Any]:
        """Return API/database health."""
        return self.get("/health")

    def options(self) -> dict[str, Any]:
        """Return dashboard filter reference data."""
        return self.get("/api/v1/options")


@lru_cache(maxsize=1)
def get_api_client(base_url: str = "http://localhost:8000") -> FinSightAPI:
    """Return one cached API client for the Streamlit process."""
    return FinSightAPI(base_url=base_url)
