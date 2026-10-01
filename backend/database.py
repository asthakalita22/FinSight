"""Database helpers: a SQLAlchemy engine and a raw psycopg2 connection."""
import time

import psycopg2
from sqlalchemy import create_engine

from backend.config import settings

engine = create_engine(settings.database_url, pool_pre_ping=True, future=True)


def get_raw_connection(retries: int = 15, delay: float = 2.0):
    """Open a psycopg2 connection, retrying while the container is still booting."""
    last_error = None
    for attempt in range(1, retries + 1):
        try:
            return psycopg2.connect(
                host=settings.postgres_host,
                port=settings.postgres_port,
                user=settings.postgres_user,
                password=settings.postgres_password,
                dbname=settings.postgres_db,
            )
        except psycopg2.OperationalError as exc:  # database not ready yet
            last_error = exc
            print(f"  waiting for PostgreSQL ({attempt}/{retries})...")
            time.sleep(delay)
    raise SystemExit(
        "\nCould not connect to PostgreSQL.\n"
        "  1. Is Docker Desktop running?\n"
        "  2. Did you run:  docker compose up -d   ?\n"
        "  3. Do the values in .env match docker-compose.yml (port 5433)?\n"
        f"\nOriginal error: {last_error}"
    )
