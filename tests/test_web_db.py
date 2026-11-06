"""Tests for the page's database access: the allowlist lookup, and transactions as the signed-in account's badge."""

# Standard library imports
from contextlib import AsyncExitStack
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

# Third party imports
import psycopg
import pytest
from page_sim import FakeClock, page_settings

# First party imports
from timeclock_backend.web.db import DatabaseChangedError, editor_transaction
from timeclock_backend.web.gate import Account, EndReason, Gate, SignedOutError

if TYPE_CHECKING:
  # Third party imports
  from page_sim import PagesDb

# A page punch as the schema's tests add one: it names its save.
_PAGE_PUNCH = (
  "WITH e AS (INSERT INTO edits (note) VALUES ('') RETURNING id)"
  " INSERT INTO punches (employee_id, kind, at, instance_id, edit_id) SELECT %s, 'clock_in', %s, 'web', e.id FROM e"
)


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
  await gate.choose_database(storage, session, test=True)
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
  await gate.choose_database(storage, session, test=True)  # another tab switches; this page still shows production
  ran = False
  with pytest.raises(DatabaseChangedError):
    async with editor_transaction(pages_db.pools, gate, storage, session, test_database=False):
      ran = True
  assert not ran


async def test_the_page_login_alone_can_read_nothing(pages_db: PagesDb) -> None:
  async with pages_db.pools.production.connection() as conn:
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
      await conn.execute("SELECT * FROM punches")


async def test_a_transaction_without_touch_is_not_activity(pages_db: PagesDb) -> None:
  clock = FakeClock()
  gate, storage, session = Gate(page_settings(), pages_db.accounts, {}, now=clock), {}, {}
  gate.start(storage, session, pages_db.email)
  started = storage["signin"]["active"]
  clock.advance(minutes=5)
  async with editor_transaction(pages_db.pools, gate, storage, session, test_database=False, touch=False):
    pass
  assert storage["signin"]["active"] == started
  async with editor_transaction(pages_db.pools, gate, storage, session, test_database=False):
    pass
  assert storage["signin"]["active"] == clock.now.isoformat()
