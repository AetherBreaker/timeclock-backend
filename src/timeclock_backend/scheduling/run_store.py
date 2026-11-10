"""Where `catch_up` entries record their last completed run."""

# Standard library imports
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

# First party imports
from timeclock_backend.databases import connect

if TYPE_CHECKING:
  # Standard library imports
  from datetime import datetime

  # First party imports
  from timeclock_backend.databases import Database


@dataclass(frozen=True)
class RunRecord:
  """An entry's last completed run: the period it covered and the time it was scheduled for."""

  period: str
  scheduled_for: datetime


class RunStore(Protocol):
  """The runner's record of each `catch_up` entry's last completed run."""

  async def get(self, name: str) -> RunRecord | None:
    """The entry's record, or None when it has none."""
    ...

  async def record(self, name: str, period: str, scheduled_for: datetime) -> None:
    """Replace the entry's record with this run, completed now."""
    ...


class MemoryRunStore:
  """A `RunStore` in a dict, for tests."""

  def __init__(self) -> None:
    """Start empty."""
    self.records: dict[str, RunRecord] = {}

  async def get(self, name: str) -> RunRecord | None:
    """The entry's record, or None."""
    return self.records.get(name)

  async def record(self, name: str, period: str, scheduled_for: datetime) -> None:
    """Replace the entry's record."""
    self.records[name] = RunRecord(period, scheduled_for)


class PgRunStore:
  """A `RunStore` on `backend.scheduled_runs` in `database`."""

  def __init__(self, database: Database) -> None:
    """Store in `database`."""
    self.database = database

  async def get(self, name: str) -> RunRecord | None:
    """The entry's row, or None."""
    async with await connect(self.database.name, autocommit=True) as conn:
      cur = await conn.execute("SELECT period, scheduled_for FROM backend.scheduled_runs WHERE name = %s", (name,))
      row = await cur.fetchone()
    return None if row is None else RunRecord(row["period"], row["scheduled_for"])

  async def record(self, name: str, period: str, scheduled_for: datetime) -> None:
    """Upsert the entry's row."""
    async with await connect(self.database.name, autocommit=True) as conn:
      await conn.execute(
        "INSERT INTO backend.scheduled_runs (name, period, scheduled_for) VALUES (%s, %s, %s) "
        "ON CONFLICT (name) DO UPDATE SET period = EXCLUDED.period, scheduled_for = EXCLUDED.scheduled_for, completed_at = now()",
        (name, period, scheduled_for),
      )
