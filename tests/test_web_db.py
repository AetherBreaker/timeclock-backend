"""Tests for the page's database access: the allowlist lookup, and transactions as the signed-in account's badge."""

# Standard library imports
from contextlib import AsyncExitStack
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from uuid import uuid4

# Third party imports
import psycopg
import pytest
from page_sim import page_settings
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict

# First party imports
from timeclock_backend.bootstrap import bootstrap
from timeclock_backend.databases import Database
from timeclock_backend.web.db import DatabaseChangedError, PoolAccounts, Pools, editor_transaction
from timeclock_backend.web.gate import Account, EndReason, Gate, SignedOutError

if TYPE_CHECKING:
  # Standard library imports
  from collections.abc import AsyncIterator

# A page punch as the schema's tests add one: it names its save.
_PAGE_PUNCH = (
  "WITH e AS (INSERT INTO edits (note) VALUES ('') RETURNING id)"
  " INSERT INTO punches (employee_id, kind, at, instance_id, edit_id) SELECT %s, 'clock_in', %s, 'web', e.id FROM e"
)
_EMPLOYEE = (
  "INSERT INTO employees (first_name, last_name, pin_username, pin_hash)"
  " VALUES (%s, 'Lee', %s, crypt('1234', gen_salt('bf'))) RETURNING id"
)


@dataclass
class PagesDb:
  dsn: str
  prod: Database
  test: Database
  email: str
  employees: tuple[int, int]  # the same two in both databases; the first is the allowlist row's
  pools: Pools
  accounts: PoolAccounts

  def admin(self, database: Database) -> psycopg.Connection[Any]:
    return psycopg.connect(self.dsn, dbname=database.name, autocommit=True)


@pytest.fixture
async def pages_db(pg_server: str) -> AsyncIterator[PagesDb]:
  prod = Database(f"tcb_{uuid4().hex[:12]}", "timeclock_clocks", production=True)
  test = Database(f"tcb_{uuid4().hex[:12]}", "timeclock_test_clocks", production=False)
  email = f"ed-{uuid4().hex[:8]}@example.com"
  await bootstrap([prod, test])
  with psycopg.connect(pg_server, autocommit=True) as admin:
    admin.execute("ALTER ROLE timeclock_web PASSWORD 'web-test'")
  ids: list[tuple[int, int]] = []
  for database in (prod, test):
    with psycopg.connect(pg_server, dbname=database.name, autocommit=True) as conn:
      first = conn.execute(_EMPLOYEE, ("Ann", "12")).fetchone()
      second = conn.execute(_EMPLOYEE, ("Bob", "34")).fetchone()
      assert first is not None and second is not None
      ids.append((first[0], second[0]))
  assert ids[0] == ids[1]
  with psycopg.connect(pg_server, dbname=prod.name, autocommit=True) as conn:
    conn.execute("INSERT INTO backend.editors (email, employee_id) VALUES (%s, %s)", (email, ids[0][0]))
  server = conninfo_to_dict(pg_server)
  settings = page_settings(
    PAGE_POSTGRES_HOST=server["host"],
    PAGE_POSTGRES_PORT=server["port"],
    TIMECLOCK_WEB_PASSWORD="web-test",
    PAGE_PRODUCTION_DB=prod.name,
    PAGE_TEST_DB=test.name,
  )
  pools = Pools.create(settings)
  await pools.open()
  try:
    yield PagesDb(pg_server, prod, test, email, ids[0], pools, PoolAccounts(pools))
  finally:
    await pools.close()
    with psycopg.connect(pg_server, dbname=prod.name, autocommit=True) as conn:
      conn.execute("DELETE FROM backend.editors WHERE email = %s", (email,))  # drops the badge
    with psycopg.connect(pg_server, autocommit=True) as admin:
      for database in (prod, test):
        admin.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(database.name)))


def _gate(db: PagesDb) -> Gate:
  return Gate(page_settings(), db.accounts, {})


async def test_the_allowlist_comes_from_production(pages_db: PagesDb) -> None:
  assert await pages_db.accounts.lookup(pages_db.email) == Account(pages_db.employees[0], developer=False)
  assert await pages_db.accounts.lookup(pages_db.email.upper()) == Account(pages_db.employees[0], developer=False)
  with pages_db.admin(pages_db.test) as conn:
    conn.execute("INSERT INTO backend.editors (email) VALUES ('only-in-test@example.com')")
  assert await pages_db.accounts.lookup("only-in-test@example.com") is None


async def test_a_transaction_acts_as_the_badge(pages_db: PagesDb) -> None:
  gate, storage, session = _gate(pages_db), {}, {}
  gate.start(storage, session, pages_db.email)
  async with editor_transaction(pages_db.pools, gate, storage, session, test_database=False) as conn:
    row = await (await conn.execute("SELECT current_user AS badge, session_user AS login")).fetchone()
  assert row == {"badge": pages_db.email, "login": "timeclock_web"}


async def test_the_badge_ends_with_the_transaction(pages_db: PagesDb) -> None:
  gate, storage, session = _gate(pages_db), {}, {}
  gate.start(storage, session, pages_db.email)
  async with editor_transaction(pages_db.pools, gate, storage, session, test_database=False) as conn:
    pid = conn.info.backend_pid
  with pytest.raises(RuntimeError):
    async with editor_transaction(pages_db.pools, gate, storage, session, test_database=False):
      raise RuntimeError
  pool = pages_db.pools.production
  async with AsyncExitStack() as stack:  # every connection the pool holds, the badge's among them
    held = [await stack.enter_async_context(pool.connection()) for _ in range(pool.get_stats()["pool_size"])]
    assert pid in {conn.info.backend_pid for conn in held}
    for conn in held:
      assert await (await conn.execute("SELECT current_user AS who")).fetchone() == {"who": "timeclock_web"}


async def test_a_deleted_row_refuses_the_transaction(pages_db: PagesDb) -> None:
  gate, storage, session = _gate(pages_db), {}, {}
  gate.start(storage, session, pages_db.email)
  with pages_db.admin(pages_db.prod) as conn:
    conn.execute("DELETE FROM backend.editors WHERE email = %s", (pages_db.email,))
  ran = False
  with pytest.raises(SignedOutError) as raised:
    async with editor_transaction(pages_db.pools, gate, storage, session, test_database=False):
      ran = True
  assert raised.value.reason is EndReason.REFUSED
  assert not ran


async def test_the_self_edit_block_holds_in_the_test_database(pages_db: PagesDb) -> None:
  with pages_db.admin(pages_db.prod) as conn:
    conn.execute("UPDATE backend.editors SET developer = true WHERE email = %s", (pages_db.email,))
  gate, storage, session = _gate(pages_db), {}, {}
  gate.start(storage, session, pages_db.email)
  gate.choose_database(pages_db.email, test=True)
  own, other = pages_db.employees
  at = datetime.now(UTC) - timedelta(hours=1)
  with pytest.raises(psycopg.errors.InsufficientPrivilege, match="your own punches"):
    async with editor_transaction(pages_db.pools, gate, storage, session, test_database=True) as conn:
      assert conn.info.dbname == pages_db.test.name
      await conn.execute(_PAGE_PUNCH, (own, at))
  async with editor_transaction(pages_db.pools, gate, storage, session, test_database=True) as conn:
    await conn.execute(_PAGE_PUNCH, (other, at))
  with pages_db.admin(pages_db.test) as conn:
    rows = conn.execute("SELECT employee_id, db_login FROM punches").fetchall()
  assert rows == [(other, pages_db.email)]


async def test_a_transaction_refuses_a_database_the_page_did_not_draw(pages_db: PagesDb) -> None:
  with pages_db.admin(pages_db.prod) as conn:
    conn.execute("UPDATE backend.editors SET developer = true WHERE email = %s", (pages_db.email,))
  gate, storage, session = _gate(pages_db), {}, {}
  gate.start(storage, session, pages_db.email)
  gate.choose_database(pages_db.email, test=True)  # another tab switches; this page still shows production
  ran = False
  with pytest.raises(DatabaseChangedError):
    async with editor_transaction(pages_db.pools, gate, storage, session, test_database=False):
      ran = True
  assert not ran


async def test_the_page_login_alone_can_read_nothing(pages_db: PagesDb) -> None:
  async with pages_db.pools.production.connection() as conn:
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
      await conn.execute("SELECT * FROM punches")
