"""The page's database access: connection pools as `timeclock_web`, the allowlist lookup, and badge transactions."""

# Standard library imports
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Self

# Third party imports
from psycopg import AsyncConnection, sql
from psycopg.conninfo import make_conninfo
from psycopg.rows import DictRow, dict_row
from psycopg_pool import AsyncConnectionPool

# Local folder imports
from .gate import Account

if TYPE_CHECKING:
  # Standard library imports
  from collections.abc import AsyncGenerator, Mapping, MutableMapping

  # Local folder imports
  from .gate import Gate
  from .settings import PageSettings

type Pool = AsyncConnectionPool[AsyncConnection[DictRow]]


@dataclass(frozen=True, slots=True)
class Pools:
  """One pool per database, both logged in as `timeclock_web`, which can do nothing until it takes a badge."""

  production: Pool
  test: Pool

  @classmethod
  def create(cls, settings: PageSettings) -> Self:
    """Closed pools for the two databases `settings` names; `open` starts them."""

    def pool(dbname: str) -> Pool:
      conninfo = make_conninfo(
        host=settings.postgres_host,
        port=settings.postgres_port,
        user="timeclock_web",
        password=settings.db_password.get_secret_value(),
        dbname=dbname,
        application_name="timeclock-web",
      )
      return AsyncConnectionPool(
        conninfo,
        connection_class=AsyncConnection[DictRow],
        kwargs={"autocommit": True, "row_factory": dict_row},
        min_size=1,
        max_size=4,
        # Postgres restarts on a deploy: drop the dead connections.
        check=AsyncConnectionPool[AsyncConnection[DictRow]].check_connection,
        open=False,
        name=f"timeclock-web:{dbname}",
      )

    return cls(pool(settings.production_db), pool(settings.test_db))

  async def open(self) -> None:
    """Start both pools without waiting for a connection, so the page starts while the database is down."""
    for pool in (self.production, self.test):
      await pool.open(wait=False)

  async def close(self) -> None:
    """Close both pools."""
    for pool in (self.production, self.test):
      await pool.close()


class PoolAccounts:
  """The production allowlist through `backend.editor_account`, the one thing `timeclock_web` may run (0014)."""

  def __init__(self, pools: Pools) -> None:
    """Read through the production pool of `pools`."""
    self._pools = pools

  async def lookup(self, email: str) -> Account | None:
    """The row for `email`, or None when it isn't listed."""
    async with self._pools.production.connection() as conn:
      cursor = await conn.execute("SELECT employee_id, developer FROM backend.editor_account(%s)", (email,))
      row = await cursor.fetchone()
    return None if row is None else Account(row["employee_id"], row["developer"])


class DatabaseChangedError(Exception):
  """The account's database is no longer the one the page shows (switched in another tab, or the developer mark went)."""


@asynccontextmanager
async def editor_transaction(
  pools: Pools,
  gate: Gate,
  storage: MutableMapping[str, Any],
  session: Mapping[str, Any],
  *,
  test_database: bool,
  touch: bool = True,
) -> AsyncGenerator[AsyncConnection[DictRow]]:
  """The one way page code reaches the data: the gate's check, then one transaction as the signed-in account's badge.

  `SET LOCAL` ends with the transaction, so a pooled connection never carries a badge into the next one. The production
  row's employee number goes to `timeclock.editor_employee_id` for the test database's self-edit check; production
  ignores it (spec 2026-11-05 §4.6). `test_database` is the database the page shows, so a save never lands in the other;
  `touch` is False for a read a live update starts, which isn't the user's activity.

  Raises:
    SignedOutError: The sign-in in `storage` has ended; nothing ran.
    DatabaseChangedError: The page shows the other database; nothing ran, and the page reloads.
  """
  signin = await gate.check(storage, session, touch=touch)
  if signin.test_database != test_database:
    raise DatabaseChangedError
  pool = pools.test if signin.test_database else pools.production
  async with pool.connection() as conn, conn.transaction():
    await conn.execute(sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(signin.email)))
    employee = "" if signin.employee_id is None else str(signin.employee_id)
    await conn.execute("SELECT set_config('timeclock.editor_employee_id', %s, true)", (employee,))
    yield conn
