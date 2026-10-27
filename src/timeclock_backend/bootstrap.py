"""Startup bootstrap: create both databases, apply the schema's migrations, set up the clock group roles."""

# Standard library imports
import asyncio
import logging
from typing import TYPE_CHECKING, Final

# Third party imports
import psycopg
from psycopg import sql
from timeclock_schema.migrations import load_migrations

# Local folder imports
from .databases import DATABASES, Database, connect
from .settings import SETTINGS

if TYPE_CHECKING:
  # Standard library imports
  from collections.abc import Sequence

  # Third party imports
  from psycopg import AsyncConnection
  from psycopg.rows import DictRow

LOGGER: Final = logging.getLogger(__name__)

# Fixed forever: every backend instance must contend on the same advisory lock.
MIGRATION_LOCK_KEY: Final = 7_461_726_501


async def apply_migrations(conn: AsyncConnection[DictRow]) -> tuple[int, ...]:
  """Apply the schema's pending migrations, each in its own transaction.

  Args:
    conn: An autocommit connection to the target database.

  Returns:
    The versions applied by this call.
  """
  applied: list[int] = []
  await conn.execute("SELECT pg_advisory_lock(%s)", (MIGRATION_LOCK_KEY,))
  try:
    await conn.execute(
      "CREATE TABLE IF NOT EXISTS schema_migrations "
      "(version int PRIMARY KEY, name text NOT NULL, applied_at timestamptz NOT NULL DEFAULT now())"
    )
    done = {row["version"] for row in await (await conn.execute("SELECT version FROM schema_migrations")).fetchall()}
    for m in sorted(load_migrations(), key=lambda m: m.version):
      if m.version in done:
        continue
      async with conn.transaction():
        # Bytes: psycopg's typing rejects a non-literal `str`, and a parameterless execute runs every statement in the file.
        await conn.execute(m.sql.encode())
        await conn.execute("INSERT INTO schema_migrations (version, name) VALUES (%s, %s)", (m.version, m.name))
      applied.append(m.version)
      LOGGER.info("applied migration %d (%s) to %s", m.version, m.name, conn.info.dbname)
  finally:
    await conn.execute("SELECT pg_advisory_unlock(%s)", (MIGRATION_LOCK_KEY,))
  return tuple(applied)


async def bootstrap(databases: Sequence[Database] = DATABASES, *, attempts: int = 30, delay: float = 2.0) -> None:
  """Create, migrate and open up `databases`; idempotent, so it runs on every start.

  Clock logins are created by the owner by hand (§4.4), never here.

  Args:
    databases: The databases to prepare.
    attempts: How many times to try reaching the server; the last failure re-raises (the sidecar never came up).
    delay: Seconds between attempts.
  """
  attempt = 0
  while True:
    attempt += 1
    try:
      maintenance = await connect("postgres", autocommit=True)
      break
    except psycopg.OperationalError:
      if attempt >= attempts:
        raise
      await asyncio.sleep(delay)

  async with maintenance:
    for database in databases:
      cursor = await maintenance.execute("SELECT 1 FROM pg_database WHERE datname = %s", (database.name,))
      if await cursor.fetchone() is None:
        await maintenance.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database.name)))

    for database in databases:
      async with await connect(database.name, autocommit=True) as conn:
        await apply_migrations(conn)
        # The runs' zone (spec §8.4): a new or changed value reprojects them in the same transaction; an unchanged one
        # updates nothing, so it fires nothing.
        await conn.execute(
          "INSERT INTO backend.environment (tz) VALUES (%s) ON CONFLICT (id) DO UPDATE SET tz = EXCLUDED.tz "
          "WHERE backend.environment.tz IS DISTINCT FROM EXCLUDED.tz",
          (SETTINGS.tz.key,),
        )

    for database in databases:
      group, name = sql.Identifier(database.clock_group), sql.Identifier(database.name)
      cursor = await maintenance.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (database.clock_group,))
      if await cursor.fetchone() is None:
        await maintenance.execute(sql.SQL("CREATE ROLE {} NOLOGIN").format(group))
      await maintenance.execute(sql.SQL("GRANT timeclock_clock_access TO {}").format(group))
      # No role but `postgres` may create temporary tables (schema review, 2026-10-26).
      await maintenance.execute(sql.SQL("REVOKE CONNECT, TEMPORARY ON DATABASE {} FROM PUBLIC").format(name))
      await maintenance.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(name, group))
