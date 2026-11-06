"""Shared fixtures.

Database tests need a throwaway PostgreSQL 18; without `TIMECLOCK_BACKEND_TEST_DSN` they skip. Start it:

  docker run --rm -d --name timeclock-backend-pg -e POSTGRES_PASSWORD=test -p 127.0.0.1:55432:5432 postgres:18

then point the tests at it, PowerShell `$env:TIMECLOCK_BACKEND_TEST_DSN = "postgresql://postgres:test@127.0.0.1:55432/postgres"`
or Git Bash `export TIMECLOCK_BACKEND_TEST_DSN=postgresql://postgres:test@127.0.0.1:55432/postgres`.
Stop it with `docker stop timeclock-backend-pg` (`--rm` deletes it).
"""

# Standard library imports
import asyncio
import importlib
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

# aeth_ext refuses to import without it.
os.environ.setdefault("ALERTS_EMAIL_PWD", "test")
# A name Postgres knows as is, so tests can expect `SETTINGS.tz.key` in backend.environment. aeth_ext's default, the link
# US/Eastern, is written as America/New_York (test_bootstrap_writes_a_link_zone_as_its_canonical_name).
os.environ.setdefault("TZ", "America/New_York")
# Read by NiceGUI on import: the page's tests keep its storage files out of the repo's root.
os.environ.setdefault("NICEGUI_STORAGE_PATH", str(Path(__file__).parents[1] / ".cache" / "nicegui"))

# Third party imports
import page_sim
import psycopg
import pytest
from fake_zoho import FakeZoho
from nicegui import app
from nicegui.testing import user_simulation
from page_sim import FakeAccounts, FakeClock, PageUnderTest, page_settings
from psycopg import sql

# First party imports
from timeclock_backend import databases
from timeclock_backend.databases import Database
from timeclock_backend.web.gate import Account

if TYPE_CHECKING:
  # Standard library imports
  from collections.abc import AsyncIterator, Callable


def pytest_asyncio_loop_factories(config: pytest.Config, item: pytest.Item) -> dict[str, Callable[[], asyncio.AbstractEventLoop]]:
  """The relay's tests run on production's loop (aeth_ext installs winloop/uvloop); the rest on the stdlib's.

  The stdlib loop hides relay bugs: a closed transport's socket raises `OSError` there, where uvloop/winloop return `None`.
  Psycopg's async mode can't use Windows' default Proactor loop, so the DB tests get the Selector loop there.
  pytest-asyncio 1.4 deprecates overriding the `event_loop_policy` fixture, so the loop is chosen through this hook.
  """
  if item.path.name == "test_relay.py":
    # By name: each is installed only on its own platform.
    return {"default": importlib.import_module("winloop" if sys.platform == "win32" else "uvloop").new_event_loop}
  return {"default": asyncio.SelectorEventLoop if sys.platform == "win32" else asyncio.new_event_loop}


@pytest.fixture
def pg_server(monkeypatch: pytest.MonkeyPatch) -> str:
  """The throwaway server's DSN, installed as `databases.SERVER_CONNINFO`; skips without one."""
  dsn = os.environ.get("TIMECLOCK_BACKEND_TEST_DSN")
  if not dsn:
    pytest.skip("TIMECLOCK_BACKEND_TEST_DSN is not set")
  monkeypatch.setattr(databases, "SERVER_CONNINFO", dsn)
  return dsn


@pytest.fixture
async def fresh_database(pg_server: str):
  """A uniquely named database (not yet created), force-dropped afterwards."""
  database = Database(f"tcb_{uuid4().hex[:12]}", "timeclock_clocks", production=True)
  yield database
  with psycopg.connect(pg_server, autocommit=True) as conn:
    conn.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(database.name)))


@pytest.fixture
def page_values() -> dict[str, str]:
  """More page variables for `page`, over its own; a test module or test overrides this."""
  return {}


@pytest.fixture
async def page(page_values: dict[str, str]) -> AsyncIterator[PageUnderTest]:
  """The page against a fake Zoho and a fake allowlist: ann@example.com (employee 7) and the developer dev@example.com."""
  zoho = FakeZoho("page")
  accounts = FakeAccounts({"ann@example.com": Account(7, developer=False), "dev@example.com": Account(None, developer=True)})
  clock = FakeClock()
  page_sim.BUILD_ARGS.clear()
  page_sim.BUILD_ARGS.update(
    settings=page_settings(
      **{"ZOHO_ACCOUNTS_URL": FakeZoho.ISSUER, "ZOHO_CLIENT_ID": "page", "PAGE_PUBLIC_URL": "http://test", **page_values}
    ),
    accounts=accounts,
    zoho_client_kwargs={"transport": zoho.transport},
    now=clock,
  )
  async with user_simulation(main_file=Path(__file__).with_name("page_main.py")) as user:
    app.storage.general.clear()  # it's a file under NICEGUI_STORAGE_PATH, shared by every test
    yield PageUnderTest(user, zoho, accounts, clock)
