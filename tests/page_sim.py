"""Test helpers shared by the page's tests: fakes, a settable clock, signing in, and the databases `pages_db` makes."""

# Standard library imports
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlsplit

# Third party imports
import psycopg
from psycopg import sql

# First party imports
from timeclock_backend.web.settings import PageSettings

if TYPE_CHECKING:
  # Standard library imports
  from uuid import UUID

  # Third party imports
  from fake_zoho import FakeZoho
  from nicegui.testing import User

  # First party imports
  from timeclock_backend.databases import Database
  from timeclock_backend.web.db import PoolAccounts, Pools
  from timeclock_backend.web.gate import Account

# What the next `page_main.py` run passes to `build`; the `page` fixture fills it.
BUILD_ARGS: dict[str, Any] = {}


def page_settings(**values: object) -> PageSettings:
  """`PageSettings` from these variables (by name), over the environment and the defaults."""
  return PageSettings.model_validate({"ALERTS_EMAIL_PWD": "test", **values})


@dataclass
class FakeAccounts:
  """The allowlist as a dict; counts lookups, and raises `fail` when set."""

  accounts: dict[str, Account] = field(default_factory=dict)
  lookups: int = 0
  fail: Exception | None = None

  async def lookup(self, email: str) -> Account | None:
    self.lookups += 1
    if self.fail is not None:
      raise self.fail
    return self.accounts.get(email)


@dataclass
class FakeClock:
  now: datetime = field(default_factory=lambda: datetime(2026, 11, 5, 14, 0, tzinfo=UTC))

  def __call__(self) -> datetime:
    return self.now

  def advance(self, **delta: float) -> None:
    self.now += timedelta(**delta)


@dataclass
class PageUnderTest:
  user: User
  zoho: FakeZoho
  accounts: FakeAccounts
  clock: FakeClock


def authorize_query(location: str) -> dict[str, str]:
  return {key: values[0] for key, values in parse_qs(urlsplit(location).query).items()}


async def sign_in(page: PageUnderTest, email: str | None = None) -> None:
  """Sign in through the fake Zoho as `email` (its default, ann@example.com, when None), as a browser would."""
  if email is not None:
    page.zoho.claims["email"] = email
  started = await page.user.http_client.get("/signin/zoho")
  query = authorize_query(started.headers["location"])
  page.zoho.nonce = query["nonce"]
  # Then the page Zoho's reply redirects to, as the browser's address: NiceGUI's simulated reload re-requests the path
  # opened, which for the reply itself would replay a used state.
  reply = await page.user.http_client.get(f"/auth/zoho?code=c&state={query['state']}")
  await page.user.open(reply.headers["location"])


EMPLOYEE_SQL = (
  "INSERT INTO employees (first_name, last_name, pin_username, pin_hash)"
  " VALUES (%s, %s, %s, crypt('1234', gen_salt('bf'))) RETURNING id"
)


@dataclass
class PagesDb:
  """Two bootstrapped databases, production's allowlisting `email` as the first of two employees."""

  dsn: str
  prod: Database
  test: Database
  email: str
  employees: tuple[int, int]  # Ann Lee and Bob Lee, the same in both databases; the first is the allowlist row's
  pools: Pools
  accounts: PoolAccounts
  settings: PageSettings

  def admin(self, database: Database | None = None) -> psycopg.Connection[Any]:
    """A superuser connection to `database`, production by default: the owner's DBeaver."""
    return psycopg.connect(self.dsn, dbname=(database or self.prod).name, autocommit=True)

  def punch(self, employee: int, kind: str, at: datetime, *, database: Database | None = None, **columns: object) -> UUID:
    """A punch added as a clock would, but by the owner's login; `columns` overrides the rest (instance_id, automatic)."""
    values = {"employee_id": employee, "kind": kind, "at": at, "instance_id": "front"} | columns
    query = sql.SQL("INSERT INTO punches ({}) VALUES ({}) RETURNING id").format(
      sql.SQL(", ").join(map(sql.Identifier, values)), sql.SQL(", ").join([sql.Placeholder()] * len(values))
    )
    with self.admin(database) as conn:
      row = conn.execute(query, list(values.values())).fetchone()
    assert row is not None
    return row[0]

  def employee(self, first: str, last: str, username: str, *, group_id: int = 1, role: str = "employee") -> int:
    """Another employee in production, added by hand."""
    with self.admin() as conn:
      row = conn.execute(EMPLOYEE_SQL, (first, last, username)).fetchone()
      assert row is not None
      conn.execute("UPDATE employees SET group_id = %s, role = %s WHERE id = %s", (group_id, role, row[0]))
    return row[0]

  def group(self, name: str) -> int:
    """A new group in production."""
    with self.admin() as conn:
      row = conn.execute("INSERT INTO groups (name) VALUES (%s) RETURNING id", (name,)).fetchone()
    assert row is not None
    return row[0]

  def correct(
    self,
    punch: UUID,
    action: str,
    *,
    new_at: datetime | None = None,
    new_kind: str | None = None,
    note: str = "",
    database: Database | None = None,
  ) -> UUID:
    """A correction made by hand in DBeaver."""
    with self.admin(database) as conn:
      row = conn.execute(
        "INSERT INTO punch_corrections (punch_id, action, new_at, new_kind, note) VALUES (%s, %s, %s, %s, %s) RETURNING id",
        (punch, action, new_at, new_kind, note),
      ).fetchone()
    assert row is not None
    return row[0]


@dataclass
class DbPage(PageUnderTest):
  """The page signed in as `db.email` (Ann Lee's account) over real databases, its clock at Thu 2026-11-05 9:00 AM EST."""

  db: PagesDb
