"""Runtime configuration, read from environment variables (no config file in V1)."""

from __future__ import annotations

import os
from urllib.parse import quote


def _build_database_url() -> str:
    explicit = os.environ.get("CADENCE_DATABASE_URL")
    if explicit:
        return explicit
    user = os.environ.get("POSTGRES_USER", "cadence")
    password = os.environ.get("POSTGRES_PASSWORD", "")
    name = os.environ.get("POSTGRES_DB", "cadence")
    host = os.environ.get("POSTGRES_HOST", "db")
    port = os.environ.get("POSTGRES_PORT", "5432")
    return (
        f"postgresql+psycopg2://{quote(user, safe='')}:{quote(password, safe='')}"
        f"@{host}:{port}/{name}"
    )


class Settings:
    def __init__(self) -> None:
        self.database_url: str = _build_database_url()
        # Shared secret guarding the admin provisioning endpoints (X-Admin-Key).
        self.admin_key: str = os.environ.get("CADENCE_ADMIN_KEY", "")
        if not self.admin_key:
            raise RuntimeError("CADENCE_ADMIN_KEY is required")


settings = Settings()
