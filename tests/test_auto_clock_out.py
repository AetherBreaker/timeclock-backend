"""Tests for the automatic clock-out and its scheduling."""

# Standard library imports
import asyncio
from datetime import UTC, date, datetime, time, timedelta
from itertools import count
from typing import Any
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

# Third party imports
import psycopg
import pytest
from psycopg import sql

# First party imports
from timeclock_backend import auto_clock_out
from timeclock_backend.auto_clock_out import run_auto_clock_out
from timeclock_backend.bootstrap import bootstrap
from timeclock_backend.databases import Database, connect

NY = ZoneInfo("America/New_York")
D = date(2026, 10, 27)
_pins = count(1000)


@pytest.fixture
async def migrated(fresh_database: Database) -> Database:
  await bootstrap([fresh_database])
  return fresh_database


@pytest.fixture(autouse=True)
def alerts(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str, dict[str, Any]]]:
  """Record `alert` calls (aeth_ext's is a no-op outside `-O`); every call must pass `wait=False` so it can't block the loop."""
  recorded: list[tuple[str, str, dict[str, Any]]] = []

  def record(reason: str, details: str, **kwargs: Any) -> None:
    assert kwargs.get("wait") is False
    recorded.append((reason, details, kwargs))

  monkeypatch.setattr(auto_clock_out, "alert", record, raising=False)  # interim: nothing alerts until the pass (Task 7)
  return recorded


async def add_employee(db: Database) -> int:
  async with await connect(db.name) as conn:
    row = await (
      await conn.execute(
        "INSERT INTO employees (name, pin_username, pin_hash) VALUES ('Ann', %s, crypt('1234', gen_salt('bf'))) RETURNING id",
        (str(next(_pins)),),
      )
    ).fetchone()
  assert row is not None
  return row["id"]


async def add_punch(db: Database, employee_id: int, kind: str, at: datetime) -> UUID:
  punch_id = uuid4()
  async with await connect(db.name) as conn:
    await conn.execute(
      "INSERT INTO punches (id, employee_id, kind, at, instance_id) VALUES (%s, %s, %s, %s, 'test')",
      (punch_id, employee_id, kind, at),
    )
  return punch_id


async def void(db: Database, punch_id: UUID) -> None:
  async with await connect(db.name) as conn:
    await conn.execute("INSERT INTO punch_corrections (punch_id, action, note) VALUES (%s, 'void', 'test')", (punch_id,))


async def set_settings(db: Database, **cols: object) -> None:
  assignments = sql.SQL(", ").join(sql.SQL("{} = %s").format(sql.Identifier(name)) for name in cols)
  async with await connect(db.name) as conn:
    await conn.execute(sql.SQL("UPDATE config.settings SET {}").format(assignments), tuple(cols.values()))


async def clock_outs(db: Database) -> list[dict]:
  async with await connect(db.name) as conn:
    cursor = await conn.execute("SELECT employee_id, at, automatic, instance_id, db_login FROM punches WHERE kind = 'clock_out'")
    return await cursor.fetchall()


def local(day: date, hour: int, minute: int = 0) -> datetime:
  return datetime.combine(day, time(hour, minute), NY)


async def add_run(db: Database, scheduled_for: datetime, stamp: datetime | None = None, zone: str = "America/New_York") -> int:
  async with await connect(db.name) as conn:
    row = await (
      await conn.execute(
        "INSERT INTO backend.auto_clock_out_runs (scheduled_for, scheduled_tz, stamp) VALUES (%s, %s, %s) RETURNING id",
        (scheduled_for, zone, scheduled_for if stamp is None else stamp),
      )
    ).fetchone()
  assert row is not None
  return row["id"]


async def run_row(db: Database, run_id: int) -> dict:
  async with await connect(db.name) as conn:
    row = await (await conn.execute("SELECT ran_at, closed FROM backend.auto_clock_out_runs WHERE id = %s", (run_id,))).fetchone()
  assert row is not None
  return row


async def test_run_closes_open_shift_at_its_stamp(migrated: Database) -> None:
  e = await add_employee(migrated)
  await add_punch(migrated, e, "clock_in", local(D, 8))
  run_id = await add_run(migrated, local(D, 23), local(D, 18))
  assert await run_auto_clock_out(migrated, run_id) == 1
  [row] = await clock_outs(migrated)
  assert row["at"] == local(D, 18)
  assert row["automatic"]
  assert row["instance_id"] == "backend"
  assert row["db_login"] == "postgres"
  record = await run_row(migrated, run_id)
  assert record["closed"] == 1
  assert record["ran_at"] is not None


async def test_stamp_after_the_run_is_written_as_stamped(migrated: Database) -> None:
  """A stamp later than the run, even one still in the future, is written at once (owner, 2026-10-27)."""
  e = await add_employee(migrated)
  await add_punch(migrated, e, "clock_in", local(D, 8))
  stamp = datetime.now(UTC) + timedelta(hours=1)
  assert await run_auto_clock_out(migrated, await add_run(migrated, datetime.now(UTC), stamp)) == 1
  [row] = await clock_outs(migrated)
  assert row["at"] == stamp


async def test_shift_started_after_stamp_waits_for_the_next_run(migrated: Database) -> None:
  e = await add_employee(migrated)
  await add_punch(migrated, e, "clock_in", local(D, 19))
  run_id = await add_run(migrated, local(D, 23), local(D, 18))
  assert await run_auto_clock_out(migrated, run_id) == 0
  assert await clock_outs(migrated) == []
  assert (await run_row(migrated, run_id))["closed"] == 0
  next_day = D + timedelta(days=1)
  assert await run_auto_clock_out(migrated, await add_run(migrated, local(next_day, 23), local(next_day, 18))) == 1
  [row] = await clock_outs(migrated)
  assert row["at"] == local(next_day, 18)


async def test_voided_clock_out_reopens_shift(migrated: Database) -> None:
  e = await add_employee(migrated)
  await add_punch(migrated, e, "clock_in", local(D, 8))
  await void(migrated, await add_punch(migrated, e, "clock_out", local(D, 16)))
  assert await run_auto_clock_out(migrated, await add_run(migrated, local(D, 23), local(D, 18))) == 1
  assert [r["at"] for r in await clock_outs(migrated) if r["automatic"]] == [local(D, 18)]


async def test_voided_clock_in_leaves_nothing_open(migrated: Database) -> None:
  e = await add_employee(migrated)
  await void(migrated, await add_punch(migrated, e, "clock_in", local(D, 8)))
  assert await run_auto_clock_out(migrated, await add_run(migrated, local(D, 23), local(D, 18))) == 0
  assert await clock_outs(migrated) == []


async def test_break_closed_and_finished_shift_untouched(migrated: Database) -> None:
  on_break = await add_employee(migrated)
  await add_punch(migrated, on_break, "clock_in", local(D, 8))
  await add_punch(migrated, on_break, "break_start", local(D, 12))
  finished = await add_employee(migrated)
  await add_punch(migrated, finished, "clock_in", local(D, 8))
  await add_punch(migrated, finished, "clock_out", local(D, 16))
  assert await run_auto_clock_out(migrated, await add_run(migrated, local(D, 23), local(D, 18))) == 1
  automatic = [r for r in await clock_outs(migrated) if r["automatic"]]
  assert [r["employee_id"] for r in automatic] == [on_break]


async def test_row_that_ran_is_not_rerun(migrated: Database) -> None:
  e = await add_employee(migrated)
  await add_punch(migrated, e, "clock_in", local(D, 8))
  run_id = await add_run(migrated, local(D, 23), local(D, 18))
  assert await run_auto_clock_out(migrated, run_id) == 1
  record = await run_row(migrated, run_id)
  assert await run_auto_clock_out(migrated, run_id) == 0
  assert len(await clock_outs(migrated)) == 1
  assert await run_row(migrated, run_id) == record


async def test_concurrent_runs_of_one_row_insert_once(migrated: Database) -> None:
  e = await add_employee(migrated)
  await add_punch(migrated, e, "clock_in", local(D, 8))
  r = await add_run(migrated, local(D, 23), local(D, 18))
  assert sorted(await asyncio.gather(run_auto_clock_out(migrated, r), run_auto_clock_out(migrated, r))) == [0, 1]
  assert len(await clock_outs(migrated)) == 1
  assert (await run_row(migrated, r))["closed"] == 1


async def test_missing_row_runs_nothing(migrated: Database) -> None:
  e = await add_employee(migrated)
  await add_punch(migrated, e, "clock_in", local(D, 8))
  assert await run_auto_clock_out(migrated, 999_999) == 0
  assert await clock_outs(migrated) == []


async def test_failed_run_leaves_no_trace(migrated: Database) -> None:
  e = await add_employee(migrated)
  await add_punch(migrated, e, "clock_in", local(D, 8))
  run_id = await add_run(migrated, local(D, 23), local(D, 18))
  async with await connect(migrated.name) as conn:  # the row update fails after the clock-out inserts
    await conn.execute(
      "CREATE FUNCTION refuse() RETURNS trigger LANGUAGE plpgsql AS $$BEGIN RAISE EXCEPTION 'refused'; END$$;"
      "CREATE TRIGGER refuse BEFORE UPDATE ON backend.auto_clock_out_runs FOR EACH ROW EXECUTE FUNCTION refuse()"
    )
  with pytest.raises(psycopg.errors.RaiseException):
    await run_auto_clock_out(migrated, run_id)
  assert await clock_outs(migrated) == []
  assert await run_row(migrated, run_id) == {"ran_at": None, "closed": None}
