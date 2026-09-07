"""Alembic environment.

Migrations are hand-written -- autogenerate is deliberately not used, so
target_metadata stays None. The database URL always comes from the app config
(settings.database_url, i.e. the POSTGRES_* / CADENCE_DATABASE_URL env). It is
never pushed back into the ConfigParser: a URL-encoded '%' in the password
(e.g. %2A) is interpolation syntax there and would blow up.
"""

from __future__ import annotations

from logging.config import fileConfig

from sqlalchemy import create_engine, pool

from alembic import context
from app.core.config import settings

config = context.config
if config.config_file_name is not None:
    # Don't tear down loggers the caller already set up (app.prestart runs
    # `alembic upgrade` in-process right after configuring logfmt logging).
    fileConfig(config.config_file_name, disable_existing_loggers=False)

url = settings.database_url
target_metadata = None


def run_migrations_offline() -> None:
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = create_engine(url, poolclass=pool.NullPool, future=True)
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()
    connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
