"""
Alembic environment.

The database URL comes from the environment, never from ``alembic.ini``: the
same migration run has to work in a container, in CI, and on a laptop, and a
URL in a committed file would be both wrong and a place for a credential to
leak.

``target_metadata`` is the application's single ``Base``, imported through
``api.db.models`` so that *every* model is registered. Importing the models
package rather than individual modules is deliberate: a model that is defined
but never imported is a table autogenerate would silently propose dropping.
"""

from __future__ import annotations

import os
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Importing this package is what populates Base.metadata with all 13 tables.
import api.db.models  # noqa: E402,F401
from api.db.base import Base  # noqa: E402
from api.db.session import DATABASE_URL_ENV_VARS  # noqa: E402

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata

#: Batch mode rewrites an ALTER as a table copy, which SQLite needs because it
#: cannot ALTER a column. It is disabled while autogenerating the initial
#: migration: batch blocks cannot be wrapped in the existence guards that make
#: that migration idempotent, and a pure CREATE needs no ALTER anyway.
_BATCH = os.getenv("ALEMBIC_RENDER_AS_BATCH", "true").strip().lower() not in (
    "0",
    "false",
    "no",
)


def database_url() -> str:
    """
    Resolve the URL from the environment.

    Checked in the same order the application uses, so a migration can never
    run against a different database than the API talks to.
    """
    for name in DATABASE_URL_ENV_VARS:
        value = os.getenv(name)
        if value and value.strip():
            return value.strip()
    raise RuntimeError(
        "No database URL configured. Set one of: "
        + ", ".join(DATABASE_URL_ENV_VARS)
    )


def run_migrations_offline() -> None:
    """Emit SQL to stdout without connecting, for review before applying."""
    context.configure(
        url=database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """
    Apply migrations against a live connection.

    ``render_as_batch`` is on so SQLite -- which cannot ALTER a column -- is
    migrated through Alembic's table-copy strategy. That keeps the test and
    local-development path working against the same migration scripts the
    PostgreSQL deployment runs.
    """
    section = config.get_section(config.config_ini_section, {}) or {}
    section["sqlalchemy.url"] = database_url()

    connectable = engine_from_config(
        section, prefix="sqlalchemy.", poolclass=pool.NullPool
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
            render_as_batch=_BATCH and connection.dialect.name == "sqlite",
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
