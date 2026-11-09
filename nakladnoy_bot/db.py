"""Ulanish havzasi va SQL migratsiyalar."""
from __future__ import annotations

import json
import logging
from pathlib import Path

import asyncpg

from .config import ROOT

log = logging.getLogger(__name__)
MIGRATIONS_DIR = ROOT / "migrations"
MIGRATION_LOCK = 748211
INSTANCE_LOCK = 748212


async def _init_connection(conn: asyncpg.Connection) -> None:
    await conn.set_type_codec("jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog")


async def create_pool(dsn: str) -> asyncpg.Pool:
    return await asyncpg.create_pool(dsn, min_size=1, max_size=8, init=_init_connection)


async def migrate(pool: asyncpg.Pool, directory: Path = MIGRATIONS_DIR) -> list[str]:
    applied: list[str] = []
    async with pool.acquire() as conn:
        await conn.execute("SELECT pg_advisory_lock($1)", MIGRATION_LOCK)
        try:
            await conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations ("
                "version text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
            )
            done = {r["version"] for r in await conn.fetch("SELECT version FROM schema_migrations")}
            for path in sorted(directory.glob("*.sql")):
                if path.name in done:
                    continue
                async with conn.transaction():
                    await conn.execute(path.read_text(encoding="utf-8"))
                    await conn.execute("INSERT INTO schema_migrations (version) VALUES ($1)", path.name)
                applied.append(path.name)
                log.info("Migratsiya qo'llandi: %s", path.name)
        finally:
            await conn.execute("SELECT pg_advisory_unlock($1)", MIGRATION_LOCK)
    return applied
