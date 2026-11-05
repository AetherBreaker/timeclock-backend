"""The databases the backend serves, and connections to them."""

# Standard library imports
from dataclasses import dataclass
from typing import Final

# Third party imports
import psycopg
from psycopg.conninfo import make_conninfo
from psycopg.rows import DictRow, dict_row

# Local folder imports
from .settings import SETTINGS


@dataclass(frozen=True, slots=True)
class Database:
  """One served database, the role its clocks log in through, and whether it's production (`backend.deployment`)."""

  name: str
  clock_group: str
  production: bool


DATABASES: Final = (
  Database("timeclock", "timeclock_clocks", production=True),
  Database("timeclock_test", "timeclock_test_clocks", production=False),
)

# Not `Final`: tests point it at a throwaway server. Names no database; `connect` adds one.
SERVER_CONNINFO = make_conninfo(
  host=SETTINGS.postgres_host,
  port=SETTINGS.postgres_port,
  user="postgres",
  password=SETTINGS.postgres_password.get_secret_value(),
)


async def connect(dbname: str, *, autocommit: bool = False) -> psycopg.AsyncConnection[DictRow]:
  """Open a connection to `dbname` on the server named by `SERVER_CONNINFO` (read at call time)."""
  return await psycopg.AsyncConnection[DictRow].connect(
    make_conninfo(SERVER_CONNINFO, dbname=dbname), autocommit=autocommit, row_factory=dict_row
  )
