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
from typing import TYPE_CHECKING
from uuid import uuid4

# aeth_ext refuses to import without it.
os.environ.setdefault("ALERTS_EMAIL_PWD", "test")

# Third party imports
import psycopg
import pytest
from psycopg import sql

# First party imports
from timeclock_backend import databases
from timeclock_backend.databases import Database

if TYPE_CHECKING:
  # Standard library imports
  from collections.abc import Callable


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
  database = Database(f"tcb_{uuid4().hex[:12]}", "timeclock_clocks")
  yield database
  with psycopg.connect(pg_server, autocommit=True) as conn:
    conn.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(database.name)))
