"""Tests for the startup bootstrap."""

# Standard library imports
from types import SimpleNamespace
from uuid import uuid4
from zoneinfo import ZoneInfo

# Third party imports
import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo

# First party imports
from timeclock_backend import bootstrap as bootstrap_module, databases
from timeclock_backend.bootstrap import apply_migrations, bootstrap
from timeclock_backend.databases import Database, connect
from timeclock_backend.settings import SETTINGS


def _two_databases() -> tuple[Database, Database]:
  return (
    Database(f"tcb_{uuid4().hex[:12]}", "timeclock_clocks"),
    Database(f"tcb_{uuid4().hex[:12]}", "timeclock_test_clocks"),
  )


def _drop_databases(dsn: str, *names: str) -> None:
  with psycopg.connect(dsn, autocommit=True) as conn:
    for name in names:
      conn.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))


async def test_bootstrap_creates_and_migrates_each_database(pg_server: str) -> None:
  a, b = _two_databases()
  try:
    await bootstrap([a, b])
    for database in (a, b):
      async with await connect(database.name) as conn:
        migrations = await (await conn.execute("SELECT version, name FROM schema_migrations")).fetchall()
        groups = await (await conn.execute("SELECT count(*) AS n FROM groups")).fetchone()
      assert migrations == [{"version": 1, "name": "initial"}]
      assert groups == {"n": 1}
  finally:
    _drop_databases(pg_server, a.name, b.name)


async def test_bootstrap_is_idempotent(fresh_database: Database) -> None:
  await bootstrap([fresh_database])
  await bootstrap([fresh_database])
  async with await connect(fresh_database.name, autocommit=True) as conn:
    assert await apply_migrations(conn) == ()


async def test_bootstrap_writes_the_time_zone_and_projects(fresh_database: Database) -> None:
  await bootstrap([fresh_database])
  async with await connect(fresh_database.name) as conn:
    assert await (await conn.execute("SELECT tz FROM backend.environment")).fetchall() == [{"tz": SETTINGS.tz.key}]
    zones = await (await conn.execute("SELECT scheduled_tz, count(*) AS n FROM backend.auto_clock_out_runs GROUP BY 1")).fetchall()
  assert zones == [{"scheduled_tz": SETTINGS.tz.key, "n": 7}]


async def test_bootstrap_reprojects_only_when_the_zone_changes(fresh_database: Database) -> None:
  ids = "SELECT array_agg(id ORDER BY id) AS ids FROM backend.auto_clock_out_runs WHERE ran_at IS NULL AND scheduled_for > now()"
  await bootstrap([fresh_database])
  async with await connect(fresh_database.name) as conn:
    first = await (await conn.execute(ids)).fetchone()
  await bootstrap([fresh_database])
  async with await connect(fresh_database.name) as conn:
    assert await (await conn.execute(ids)).fetchone() == first  # unchanged zone: no reprojection
    await conn.execute("UPDATE backend.environment SET tz = 'UTC'")
  await bootstrap([fresh_database])
  async with await connect(fresh_database.name) as conn:
    assert await (await conn.execute("SELECT tz FROM backend.environment")).fetchone() == {"tz": SETTINGS.tz.key}
    zones = await (
      await conn.execute("SELECT DISTINCT scheduled_tz FROM backend.auto_clock_out_runs WHERE scheduled_for > now()")
    ).fetchall()
  assert zones == [{"scheduled_tz": SETTINGS.tz.key}]


async def test_connect_follows_the_clock_group(pg_server: str) -> None:
  a, b = _two_databases()
  prod, dev = f"tcb_prod_{uuid4().hex[:8]}", f"tcb_dev_{uuid4().hex[:8]}"
  try:
    await bootstrap([a, b])
    with psycopg.connect(pg_server, autocommit=True) as admin:
      admin.execute(sql.SQL("CREATE ROLE {} LOGIN PASSWORD 'pw' IN ROLE timeclock_clocks").format(sql.Identifier(prod)))
      admin.execute(sql.SQL("CREATE ROLE {} LOGIN PASSWORD 'pw' IN ROLE timeclock_test_clocks").format(sql.Identifier(dev)))

    def as_role(role: str, dbname: str) -> str:
      return make_conninfo(pg_server, user=role, password="pw", dbname=dbname)

    async with await psycopg.AsyncConnection.connect(as_role(prod, a.name)) as conn:
      row = await (await conn.execute("SELECT count(*) FROM punches")).fetchone()
      assert row == (0,)
      with pytest.raises(psycopg.errors.InsufficientPrivilege):
        await conn.execute("CREATE TEMP TABLE t (x int)")
    async with await psycopg.AsyncConnection.connect(as_role(dev, b.name)) as conn:
      row = await (await conn.execute("SELECT count(*) FROM punches")).fetchone()
      assert row == (0,)
    with pytest.raises(psycopg.OperationalError):
      await psycopg.AsyncConnection.connect(as_role(prod, b.name))
    with pytest.raises(psycopg.OperationalError):
      await psycopg.AsyncConnection.connect(as_role(dev, a.name))
  finally:
    _drop_databases(pg_server, a.name, b.name)
    with psycopg.connect(pg_server, autocommit=True) as admin:
      for role in (prod, dev):
        admin.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role)))


async def test_bootstrap_gives_up_after_its_attempts(monkeypatch: pytest.MonkeyPatch) -> None:
  monkeypatch.setattr(databases, "SERVER_CONNINFO", "host=127.0.0.1 port=1 user=postgres password=x connect_timeout=1")
  with pytest.raises(psycopg.OperationalError):
    await bootstrap([Database("tcb_never", "timeclock_clocks")], attempts=2, delay=0)


async def test_bootstrap_writes_a_link_zone_as_its_canonical_name(fresh_database: Database, monkeypatch: pytest.MonkeyPatch) -> None:
  # aeth_ext's default; the postgres:18 image's tzdata has no US/* links.
  monkeypatch.setattr(bootstrap_module, "SETTINGS", SimpleNamespace(tz=ZoneInfo("US/Eastern")))
  await bootstrap([fresh_database])
  async with await connect(fresh_database.name) as conn:
    assert await (await conn.execute("SELECT tz FROM backend.environment")).fetchall() == [{"tz": "America/New_York"}]
    assert await (await conn.execute("SELECT DISTINCT scheduled_tz FROM backend.auto_clock_out_runs")).fetchall() == [
      {"scheduled_tz": "America/New_York"}
    ]


async def test_bootstrap_fails_on_a_zone_postgres_has_no_name_for(fresh_database: Database, monkeypatch: pytest.MonkeyPatch) -> None:
  monkeypatch.setattr(bootstrap_module, "SETTINGS", SimpleNamespace(tz=SimpleNamespace(key="Nowhere/Fictional")))
  with pytest.raises(ValueError, match="Nowhere/Fictional"):
    await bootstrap([fresh_database])
