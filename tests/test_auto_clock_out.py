"""Tests for the automatic clock-out and its scheduling."""

# Standard library imports
import asyncio
from contextlib import suppress
from datetime import UTC, date, datetime, time, timedelta
from itertools import count
from typing import TYPE_CHECKING, Any, NoReturn
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

# Third party imports
import psycopg
import pytest
from apscheduler.triggers.date import DateTrigger
from psycopg import sql
from timeclock_schema.wire import NOTIFY_CHANNEL

# First party imports
from timeclock_backend import auto_clock_out
from timeclock_backend.auto_clock_out import RETRY_DELAY, listen_for_settings, run_auto_clock_out, sync_auto_clock_out
from timeclock_backend.bootstrap import bootstrap
from timeclock_backend.databases import Database, connect
from timeclock_backend.scheduler_config import Scheduler

if TYPE_CHECKING:
  # Standard library imports
  from collections.abc import AsyncIterator, Callable

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

  monkeypatch.setattr(auto_clock_out, "alert", record)
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


def noon_zone() -> str:
  """An Etc zone where it's now 12:xx, so a 23:00 run today is hours ahead whenever the test runs."""
  offset = 12 - datetime.now(UTC).hour  # -11..12; Etc names invert the sign (Etc/GMT-5 is UTC+5)
  return "UTC" if offset == 0 else f"Etc/GMT{-offset:+d}"


@pytest.fixture
async def noon(migrated: Database) -> str:
  """Moves `migrated`'s runs to `noon_zone()`: bootstrap's rows go, and the 7 reprojected 23:00 runs are all hours ahead."""
  zone = noon_zone()
  async with await connect(migrated.name) as conn:
    await conn.execute("DELETE FROM backend.auto_clock_out_runs")
    await conn.execute("UPDATE backend.environment SET tz = %s", (zone,))
  return zone


@pytest.fixture
async def scheduler() -> AsyncIterator[Scheduler]:
  s = Scheduler.init_scheduler()
  s.start()
  yield s
  s.shutdown(wait=False)


async def future_runs(db: Database) -> list[dict]:
  async with await connect(db.name) as conn:
    cursor = await conn.execute(
      "SELECT id, scheduled_for, stamp FROM backend.auto_clock_out_runs WHERE ran_at IS NULL AND scheduled_for > now() ORDER BY scheduled_for"
    )
    return await cursor.fetchall()


def job_time(scheduler: Scheduler, db: Database) -> datetime | None:
  job = scheduler.get_job(f"auto_clock_out:{db.name}")
  # Pyright infers get_job from APScheduler's untyped source: a tuple, never None.
  return None if job is None else job.next_run_time  # pyright: ignore[reportUnnecessaryComparison, reportAttributeAccessIssue]


async def wait_for(predicate: Callable[[], bool]) -> None:
  async with asyncio.timeout(5):
    while not predicate():  # noqa: ASYNC110  # polls a scheduler's job table; there is nothing to await
      await asyncio.sleep(0.05)


async def test_pass_runs_missed_rows_oldest_first(migrated: Database, noon: str, scheduler: Scheduler) -> None:
  now = datetime.now(UTC)
  r3, r2, r1 = [await add_run(migrated, now - timedelta(days=n), now - timedelta(days=n, hours=1), zone=noon) for n in (3, 2, 1)]
  e = await add_employee(migrated)
  await add_punch(migrated, e, "clock_in", now - timedelta(days=3, hours=5))
  await sync_auto_clock_out(scheduler, migrated)
  rows = [await run_row(migrated, r) for r in (r3, r2, r1)]
  assert [r["closed"] for r in rows] == [1, 0, 0]
  assert rows[0]["ran_at"] < rows[1]["ran_at"] < rows[2]["ran_at"]
  [row] = await clock_outs(migrated)
  assert row["at"] == now - timedelta(days=3, hours=1)


async def test_pass_ignores_rows_older_than_lookback(migrated: Database, noon: str, scheduler: Scheduler) -> None:
  now = datetime.now(UTC)
  old = await add_run(migrated, now - timedelta(days=15), zone=noon)
  e = await add_employee(migrated)
  await add_punch(migrated, e, "clock_in", now - timedelta(days=16))
  await sync_auto_clock_out(scheduler, migrated)
  assert (await run_row(migrated, old))["ran_at"] is None
  assert await clock_outs(migrated) == []


async def test_pass_schedules_the_next_row(migrated: Database, noon: str, scheduler: Scheduler) -> None:
  await sync_auto_clock_out(scheduler, migrated)
  upcoming = await future_runs(migrated)
  assert len(upcoming) == 7  # noqa: PLR2004  # the projection's 7 days
  assert job_time(scheduler, migrated) == upcoming[0]["scheduled_for"]
  job = scheduler.get_job(f"auto_clock_out:{migrated.name}")
  assert job.misfire_grace_time is None  # pyright: ignore[reportAttributeAccessIssue]  # as in job_time
  assert isinstance(job.trigger, DateTrigger)  # pyright: ignore[reportAttributeAccessIssue]


async def test_pass_extends_the_projection(migrated: Database, noon: str, scheduler: Scheduler) -> None:
  async with await connect(migrated.name) as conn:
    await conn.execute("DELETE FROM backend.auto_clock_out_runs WHERE id <> (SELECT min(id) FROM backend.auto_clock_out_runs)")
  await sync_auto_clock_out(scheduler, migrated)
  async with await connect(migrated.name) as conn:
    days = await (
      await conn.execute(
        "SELECT (scheduled_for AT TIME ZONE scheduled_tz)::date AS d FROM backend.auto_clock_out_runs WHERE scheduled_for > now() ORDER BY 1"
      )
    ).fetchall()
    today = await (await conn.execute("SELECT (now() AT TIME ZONE %s)::date AS d", (noon,))).fetchone()
  assert today is not None
  assert [r["d"] for r in days] == [today["d"] + timedelta(days=i) for i in range(7)]


async def test_run_time_moved_into_the_past_runs_today_at_once(migrated: Database, noon: str, scheduler: Scheduler) -> None:
  now = datetime.now(UTC)
  zone = ZoneInfo(noon)
  e = await add_employee(migrated)
  await add_punch(migrated, e, "clock_in", now - timedelta(hours=14))  # yesterday evening in the noon zone
  await set_settings(migrated, auto_clock_out_run_time=time(0, 0, 1), auto_clock_out_stamp_time=time(0))
  await sync_auto_clock_out(scheduler, migrated)
  async with await connect(migrated.name) as conn:
    [ran] = await (await conn.execute("SELECT stamp, closed FROM backend.auto_clock_out_runs WHERE ran_at IS NOT NULL")).fetchall()
  today = now.astimezone(zone).date()
  assert ran == {"stamp": datetime.combine(today, time(0), zone), "closed": 1}
  [row] = await clock_outs(migrated)
  assert row["at"] == ran["stamp"]
  upcoming = await future_runs(migrated)
  assert upcoming[0]["scheduled_for"] == datetime.combine(today + timedelta(days=1), time(0, 0, 1), zone)
  assert job_time(scheduler, migrated) == upcoming[0]["scheduled_for"]


async def test_pass_writes_a_later_stamp_on_the_runs_day(migrated: Database, noon: str, scheduler: Scheduler) -> None:
  """A stamp later than the run lands on the run's own day and is written at once, though still ahead (owner, 2026-10-27)."""
  now = datetime.now(UTC)
  zone = ZoneInfo(noon)
  e = await add_employee(migrated)
  await add_punch(migrated, e, "clock_in", now - timedelta(hours=1))
  await set_settings(migrated, auto_clock_out_run_time=time(0, 0, 1), auto_clock_out_stamp_time=time(23))
  await sync_auto_clock_out(scheduler, migrated)
  [row] = await clock_outs(migrated)
  assert row["at"] == datetime.combine(now.astimezone(zone).date(), time(23), zone)
  assert row["at"] > datetime.now(UTC)


async def test_disabled_pass_runs_nothing_and_removes_the_job(migrated: Database, noon: str, scheduler: Scheduler) -> None:
  now = datetime.now(UTC)
  e = await add_employee(migrated)
  await add_punch(migrated, e, "clock_in", now - timedelta(days=2))
  await sync_auto_clock_out(scheduler, migrated)
  assert job_time(scheduler, migrated) is not None
  missed = await add_run(migrated, now - timedelta(days=1), zone=noon)  # after the first pass, so it doesn't run there
  await set_settings(migrated, auto_clock_out_enabled=False)
  await sync_auto_clock_out(scheduler, migrated)
  assert await clock_outs(migrated) == []
  assert (await run_row(migrated, missed))["ran_at"] is None
  assert job_time(scheduler, migrated) is None
  assert await future_runs(migrated) == []


async def test_pass_stops_at_the_first_failed_run_and_retries(
  migrated: Database, noon: str, scheduler: Scheduler, alerts: list[tuple[str, str, dict[str, Any]]], monkeypatch: pytest.MonkeyPatch
) -> None:
  now = datetime.now(UTC)
  older = await add_run(migrated, now - timedelta(days=2), zone=noon)
  await add_run(migrated, now - timedelta(days=1), zone=noon)
  calls: list[int] = []

  async def failing(_db: Database, run_id: int) -> NoReturn:
    calls.append(run_id)
    raise psycopg.OperationalError("connection lost")

  monkeypatch.setattr(auto_clock_out, "run_auto_clock_out", failing)
  before = datetime.now(UTC)
  await sync_auto_clock_out(scheduler, migrated)
  assert calls == [older]
  [(reason, _, _)] = alerts
  assert reason == f"Auto clock-out failed in {migrated.name}"
  retry = job_time(scheduler, migrated)
  assert retry is not None
  assert before + RETRY_DELAY <= retry <= datetime.now(UTC) + RETRY_DELAY


async def test_pass_database_error_alerts_and_retries(
  migrated: Database, scheduler: Scheduler, alerts: list[tuple[str, str, dict[str, Any]]], monkeypatch: pytest.MonkeyPatch
) -> None:
  async def unreachable(*_args: object, **_kwargs: object) -> NoReturn:
    raise psycopg.OperationalError("connection refused")

  monkeypatch.setattr(auto_clock_out, "connect", unreachable)
  before = datetime.now(UTC)
  await sync_auto_clock_out(scheduler, migrated)
  [(reason, _, _)] = alerts
  assert reason == f"Auto clock-out failed in {migrated.name}"
  retry = job_time(scheduler, migrated)
  assert retry is not None
  assert before + RETRY_DELAY <= retry <= datetime.now(UTC) + RETRY_DELAY


async def test_pass_blocked_by_an_open_settings_edit_alerts_and_retries(
  migrated: Database, noon: str, scheduler: Scheduler, alerts: list[tuple[str, str, dict[str, Any]]], monkeypatch: pytest.MonkeyPatch
) -> None:
  monkeypatch.setattr(auto_clock_out, "_LOCK_TIMEOUT", "200ms")
  before = datetime.now(UTC)
  async with await connect(migrated.name) as editor:  # an SQL-editor transaction left open: its trigger holds the projection lock
    await editor.execute("UPDATE config.settings SET subtract_breaks = true")
    await sync_auto_clock_out(scheduler, migrated)
    await editor.rollback()
  [(reason, _, _)] = alerts
  assert reason == f"Auto clock-out failed in {migrated.name}"
  retry = job_time(scheduler, migrated)
  assert retry is not None
  assert before + RETRY_DELAY <= retry <= datetime.now(UTC) + RETRY_DELAY


async def test_settings_notification_reschedules(migrated: Database, noon: str, scheduler: Scheduler) -> None:
  task = asyncio.create_task(listen_for_settings(scheduler, migrated))
  try:
    await wait_for(lambda: job_time(scheduler, migrated) is not None)
    await set_settings(migrated, auto_clock_out_run_time=time(20), auto_clock_out_stamp_time=time(20))
    expected = (await future_runs(migrated))[0]["scheduled_for"]
    await wait_for(lambda: job_time(scheduler, migrated) == expected)
    # Any login may NOTIFY: a malformed payload is skipped, and the listener keeps serving real changes.
    async with await connect(migrated.name, autocommit=True) as conn:
      await conn.execute(sql.SQL("NOTIFY {}, 'x'").format(sql.Identifier(NOTIFY_CHANNEL)))
    await set_settings(migrated, auto_clock_out_run_time=time(21), auto_clock_out_stamp_time=time(21))
    expected = (await future_runs(migrated))[0]["scheduled_for"]
    await wait_for(lambda: job_time(scheduler, migrated) == expected)
    assert not task.done()
  finally:
    task.cancel()
    with suppress(asyncio.CancelledError):
      await task
