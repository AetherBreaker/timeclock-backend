"""Tests for the automatic clock-out and its scheduling."""

# Standard library imports
import asyncio
from contextlib import suppress
from datetime import UTC, date, datetime, time
from itertools import count
from typing import TYPE_CHECKING, Any, NoReturn
from uuid import uuid4
from zoneinfo import ZoneInfo

# Third party imports
import psycopg
import pytest
from psycopg import sql
from timeclock_schema.wire import NOTIFY_CHANNEL

# First party imports
from timeclock_backend import auto_clock_out
from timeclock_backend.auto_clock_out import listen_for_settings, run_auto_clock_out, schedule_auto_clock_out
from timeclock_backend.bootstrap import bootstrap
from timeclock_backend.databases import Database, connect
from timeclock_backend.scheduler_config import Scheduler
from timeclock_backend.settings import SETTINGS

if TYPE_CHECKING:
  # Standard library imports
  from collections.abc import Callable

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


async def add_punch(db: Database, employee_id: int, kind: str, at: datetime) -> None:
  async with await connect(db.name) as conn:
    await conn.execute(
      "INSERT INTO punches (id, employee_id, kind, at, instance_id) VALUES (%s, %s, %s, %s, 'test')",
      (uuid4(), employee_id, kind, at),
    )


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


async def test_open_shift_closed_at_stamp(migrated: Database) -> None:
  e = await add_employee(migrated)
  await add_punch(migrated, e, "clock_in", local(D, 8))
  await set_settings(migrated, auto_clock_out_run_time=time(23), auto_clock_out_stamp_time=time(18))
  assert await run_auto_clock_out(migrated, today=D) == 1
  [row] = await clock_outs(migrated)
  assert row["at"] == local(D, 18)
  assert row["automatic"]
  assert row["instance_id"] == "backend"
  assert row["db_login"] == "postgres"


async def test_shift_started_after_stamp_is_left_open(migrated: Database) -> None:
  e = await add_employee(migrated)
  await add_punch(migrated, e, "clock_in", local(D, 19))
  await set_settings(migrated, auto_clock_out_run_time=time(23), auto_clock_out_stamp_time=time(18))
  assert await run_auto_clock_out(migrated, today=D) == 0
  assert await clock_outs(migrated) == []


async def test_concurrent_runs_insert_once(migrated: Database) -> None:
  e = await add_employee(migrated)
  await add_punch(migrated, e, "clock_in", local(D, 8))
  await set_settings(migrated, auto_clock_out_run_time=time(23), auto_clock_out_stamp_time=time(18))
  assert sorted(await asyncio.gather(run_auto_clock_out(migrated, today=D), run_auto_clock_out(migrated, today=D))) == [0, 1]
  assert len(await clock_outs(migrated)) == 1


@pytest.mark.parametrize(
  "now",
  [datetime(2026, 10, 28, 0, 0, 30, tzinfo=NY), datetime(2026, 10, 27, 23, 59, 59, tzinfo=NY)],
  ids=["fired-late-past-midnight", "on-time"],
)
async def test_scheduled_run_stamps_the_run_times_date(migrated: Database, monkeypatch: pytest.MonkeyPatch, now: datetime) -> None:
  """Without `today`, the run stamps the date of the latest run time, so a late firing past midnight isn't a day ahead.

  The clock is injected by replacing the module's `datetime` with a subclass whose `now()` is fixed.
  """

  class FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz: Any = None) -> FrozenDatetime:
      return cls.fromtimestamp(now.timestamp(), tz)

  monkeypatch.setattr(auto_clock_out, "datetime", FrozenDatetime)
  forgot = await add_employee(migrated)
  await add_punch(migrated, forgot, "clock_in", local(D, 8))
  just_in = await add_employee(migrated)
  await add_punch(migrated, just_in, "clock_in", datetime(2026, 10, 28, 0, 0, 10, tzinfo=NY))
  await set_settings(migrated, auto_clock_out_run_time=time(23, 59, 59), auto_clock_out_stamp_time=time(23))
  assert await run_auto_clock_out(migrated) == 1
  [row] = await clock_outs(migrated)
  assert (row["employee_id"], row["at"]) == (forgot, local(D, 23))


async def test_job_is_idempotent(migrated: Database) -> None:
  e = await add_employee(migrated)
  await add_punch(migrated, e, "clock_in", local(D, 8))
  await set_settings(migrated, auto_clock_out_run_time=time(23), auto_clock_out_stamp_time=time(18))
  assert await run_auto_clock_out(migrated, today=D) == 1
  assert await run_auto_clock_out(migrated, today=D) == 0
  assert len(await clock_outs(migrated)) == 1


async def test_break_closed_and_finished_shift_untouched(migrated: Database) -> None:
  on_break = await add_employee(migrated)
  await add_punch(migrated, on_break, "clock_in", local(D, 8))
  await add_punch(migrated, on_break, "break_start", local(D, 12))
  finished = await add_employee(migrated)
  await add_punch(migrated, finished, "clock_in", local(D, 8))
  await add_punch(migrated, finished, "clock_out", local(D, 16))
  await set_settings(migrated, auto_clock_out_run_time=time(23), auto_clock_out_stamp_time=time(18))
  assert await run_auto_clock_out(migrated, today=D) == 1
  automatic = [r for r in await clock_outs(migrated) if r["automatic"]]
  assert [r["employee_id"] for r in automatic] == [on_break]


async def test_invalid_settings_skip_and_alert(migrated: Database, alerts: list[tuple[str, str, dict[str, Any]]]) -> None:
  e = await add_employee(migrated)
  await add_punch(migrated, e, "clock_in", local(D, 8))
  await set_settings(migrated, auto_clock_out_run_time=time(23), auto_clock_out_stamp_time=time(23, 30))
  assert await run_auto_clock_out(migrated, today=D) == 0
  assert await clock_outs(migrated) == []
  [(reason, _, kwargs)] = alerts
  assert migrated.name in reason
  assert kwargs["in_except_block"] is False


async def test_disabled_does_nothing(migrated: Database) -> None:
  e = await add_employee(migrated)
  await add_punch(migrated, e, "clock_in", local(D, 8))
  await set_settings(migrated, auto_clock_out_enabled=False)
  assert await run_auto_clock_out(migrated, today=D) == 0
  assert await clock_outs(migrated) == []


async def test_database_error_alerts_and_skips(
  migrated: Database, alerts: list[tuple[str, str, dict[str, Any]]], monkeypatch: pytest.MonkeyPatch
) -> None:
  async def unreachable(*_args: object, **_kwargs: object) -> NoReturn:
    raise psycopg.OperationalError("connection refused")

  monkeypatch.setattr(auto_clock_out, "connect", unreachable)
  assert await run_auto_clock_out(migrated, today=D) == 0
  [(reason, _, _)] = alerts
  assert reason == f"Auto clock-out failed in {migrated.name}"


async def test_stamp_is_local_time_on_dst_change_day(migrated: Database) -> None:
  dst_end = date(2026, 11, 1)
  e = await add_employee(migrated)
  await add_punch(migrated, e, "clock_in", local(dst_end, 8))
  await set_settings(migrated, auto_clock_out_run_time=time(23), auto_clock_out_stamp_time=time(23))
  assert await run_auto_clock_out(migrated, today=dst_end) == 1
  [row] = await clock_outs(migrated)
  assert row["at"] == datetime(2026, 11, 2, 4, 0, tzinfo=UTC)


async def test_schedule_adds_job_at_run_time(migrated: Database) -> None:
  scheduler = Scheduler.init_scheduler()
  scheduler.start()
  try:
    e = await add_employee(migrated)
    await add_punch(migrated, e, "clock_in", local(D, 8))
    await set_settings(migrated, auto_clock_out_run_time=time(21, 30, 15), auto_clock_out_stamp_time=time(21))
    await schedule_auto_clock_out(scheduler, migrated, now=local(D, 10))
    job = scheduler.get_job(f"auto_clock_out:{migrated.name}")
    assert job is not None
    fields = {f.name: str(f) for f in job.trigger.fields}  # pyright: ignore[reportAttributeAccessIssue]
    assert (fields["hour"], fields["minute"], fields["second"]) == ("21", "30", "15")
    assert job.trigger.timezone == SETTINGS.tz  # pyright: ignore[reportAttributeAccessIssue]
    assert await clock_outs(migrated) == []
  finally:
    scheduler.shutdown(wait=False)


async def test_schedule_removes_job_when_disabled(migrated: Database) -> None:
  scheduler = Scheduler.init_scheduler()
  scheduler.start()
  try:
    job_id = f"auto_clock_out:{migrated.name}"
    await schedule_auto_clock_out(scheduler, migrated, now=local(D, 10))
    assert scheduler.get_job(job_id) is not None
    await set_settings(migrated, auto_clock_out_enabled=False)
    await schedule_auto_clock_out(scheduler, migrated, now=local(D, 10))
    assert scheduler.get_job(job_id) is None
  finally:
    scheduler.shutdown(wait=False)


async def test_catch_up_runs_when_past_run_time(migrated: Database) -> None:
  scheduler = Scheduler.init_scheduler()
  scheduler.start()
  try:
    e = await add_employee(migrated)
    await add_punch(migrated, e, "clock_in", local(D, 8))
    await set_settings(migrated, auto_clock_out_run_time=time(23), auto_clock_out_stamp_time=time(23))
    await schedule_auto_clock_out(scheduler, migrated, now=local(D, 23, 30))
    [row] = await clock_outs(migrated)
    assert row["at"] == local(D, 23)
  finally:
    scheduler.shutdown(wait=False)


async def test_settings_notification_reschedules(migrated: Database) -> None:
  scheduler = Scheduler.init_scheduler()
  scheduler.start()
  job_id = f"auto_clock_out:{migrated.name}"

  async def wait_for(predicate: Callable[[], bool]) -> None:
    async with asyncio.timeout(5):
      while not predicate():  # noqa: ASYNC110  # polls a scheduler's job table; there is nothing to await
        await asyncio.sleep(0.05)

  def trigger_hour() -> str | None:
    job = scheduler.get_job(job_id)
    if job is None:  # pyright: ignore[reportUnnecessaryComparison]  # apscheduler's inferred return type omits None
      return None
    return str(next(f for f in job.trigger.fields if f.name == "hour"))  # pyright: ignore[reportAttributeAccessIssue]

  task = asyncio.create_task(listen_for_settings(scheduler, migrated))
  try:
    await wait_for(lambda: scheduler.get_job(job_id) is not None)  # pyright: ignore[reportUnnecessaryComparison]
    await set_settings(migrated, auto_clock_out_run_time=time(20), auto_clock_out_stamp_time=time(20))
    await wait_for(lambda: trigger_hour() == "20")
    # Any login may NOTIFY: a malformed payload is skipped, and the listener keeps serving real changes.
    async with await connect(migrated.name, autocommit=True) as conn:
      await conn.execute(sql.SQL("NOTIFY {}, 'x'").format(sql.Identifier(NOTIFY_CHANNEL)))
    await set_settings(migrated, auto_clock_out_run_time=time(21), auto_clock_out_stamp_time=time(21))
    await wait_for(lambda: trigger_hour() == "21")
    assert not task.done()
  finally:
    task.cancel()
    with suppress(asyncio.CancelledError):
      await task
    scheduler.shutdown(wait=False)
