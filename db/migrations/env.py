"""Alembic environment — resolves the database URL, runs migrations.

Deliberately self-contained (no ledgerforge imports) so `alembic` works on a
bare checkout with just the package installed. The URL comes from, in order:
the ``-x dsn=...`` command-line argument, the programmatic config attribute
set by ``ledgerforge.migrations``, LF_DATABASE_URL, DATABASE_URL. Both
``postgresql://`` and ``postgresql+psycopg://`` spellings are accepted.
"""

from __future__ import annotations

import os

from alembic import context
from sqlalchemy import engine_from_config, pool


def _resolve_url() -> str:
    cfg = context.config
    x_args = context.get_x_argument(as_dictionary=True)
    url = (
        x_args.get("dsn")
        or cfg.attributes.get("url")
        or os.environ.get("LF_DATABASE_URL")
        or os.environ.get("DATABASE_URL")
    )
    if not url:
        raise SystemExit(
            "no database URL: set LF_DATABASE_URL (or DATABASE_URL), or pass -x dsn=..."
        )
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://") :]
    return url


def _run_online() -> None:
    cfg = context.config
    section = cfg.get_section(cfg.config_ini_section, {})
    section["sqlalchemy.url"] = _resolve_url()
    connectable = engine_from_config(section, prefix="sqlalchemy.", poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=None)
        with context.begin_transaction():
            context.run_migrations()


def _run_offline() -> None:
    context.configure(
        url=_resolve_url(),
        target_metadata=None,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    _run_offline()
else:
    _run_online()
