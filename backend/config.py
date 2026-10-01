"""Central configuration for FinSight. Values come from the .env file."""
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(PROJECT_ROOT / ".env")


@dataclass(frozen=True)
class Settings:
    postgres_user: str = os.getenv("POSTGRES_USER", "finsight")
    postgres_password: str = os.getenv("POSTGRES_PASSWORD", "finsight_pw")
    postgres_db: str = os.getenv("POSTGRES_DB", "finsight")
    postgres_host: str = os.getenv("POSTGRES_HOST", "localhost")
    postgres_port: int = int(os.getenv("POSTGRES_PORT", "5433"))
    seed: int = int(os.getenv("FINSIGHT_SEED", "42"))

    # ---- financial control thresholds (Phase 2) -------------------------
    # Rule 7: flag a business-unit month if |Actual P&L - Budget P&L| / |Budget| exceeds this %
    variance_threshold_pct: float = float(os.getenv("CONTROL_VARIANCE_THRESHOLD_PCT", "15"))
    # Severity is bumped one level at "high value" and two levels at "critical value" exposure (INR)
    high_value_inr: float = float(os.getenv("CONTROL_HIGH_VALUE_INR", "100000"))
    critical_value_inr: float = float(os.getenv("CONTROL_CRITICAL_VALUE_INR", "250000"))
    # Source vs ledger amounts within this absolute tolerance count as equal
    amount_tolerance: float = float(os.getenv("CONTROL_AMOUNT_TOLERANCE", "0.01"))

    # ---- reconciliation (Phase 3) ---------------------------------------
    # A ledger entry may post 0..N calendar days AFTER the transaction date and still match.
    # The data posts 0-2 business days later, i.e. up to 4 calendar days across a weekend.
    recon_date_tolerance_days: int = int(os.getenv("RECON_DATE_TOLERANCE_DAYS", "4"))

    @property
    def database_url(self) -> str:
        return (
            f"postgresql+psycopg2://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )


settings = Settings()
