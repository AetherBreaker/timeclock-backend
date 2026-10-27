"""The daily automatic clock-out (R41): closes forgotten shifts, scheduled from `config.settings`."""

# Standard library imports
import asyncio
import logging
from collections import defaultdict
from contextlib import suppress
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Final
from uuid import uuid4

# Third party imports
import psycopg
from aeth_ext.errors.err_handling import alert
from apscheduler.jobstores.base import JobLookupError
from apscheduler.triggers.cron import CronTrigger
from psycopg import sql
from pydantic import ValidationError
from timeclock_schema.models import Punch, PunchCorrection, Settings as ConfigSettings
from timeclock_schema.shifts import auto_clock_out_due, build_timeline
from timeclock_schema.wire import NOTIFY_CHANNEL, Notification

# Local folder imports
from .databases import Database, connect  # APScheduler inspects run_auto_clock_out's annotations at runtime
from .settings import SETTINGS

if TYPE_CHECKING:
  # Local folder imports
  from .scheduler_config import Scheduler

LOGGER: Final = logging.getLogger(__name__)

# Open shifts older than this aren't auto-closed (spec §8.4).
LOOKBACK: Final = timedelta(days=14)
JOB_ID_PREFIX: Final = "auto_clock_out:"
_MAX_BACKOFF: Final = 60.0


async def read_settings(db: Database) -> ConfigSettings:
  """Read the `config.settings` row; defaults (with a warning) if it has none."""
  async with await connect(db.name) as conn:
    row = await (
      await conn.execute(
        "SELECT auto_clock_out_enabled, auto_clock_out_run_time, auto_clock_out_stamp_time, subtract_lunch, subtract_breaks "
        "FROM config.settings"
      )
    ).fetchone()
  if row is None:
    LOGGER.warning("config.settings in %s has no row; using the defaults", db.name)
    return ConfigSettings()
  return ConfigSettings.model_validate(row)


async def run_auto_clock_out(db: Database, *, today: date | None = None) -> int:
  """Insert a clock-out at today's local stamp time for every employee still on the clock (R41.1).

  Returns:
    The number of clock-outs inserted; 0 when disabled, when the settings are invalid (alerted), or after a database error
    (alerted; the next run retries).
  """
  today = today or datetime.now(SETTINGS.tz).date()
  settings = await read_settings(db)
  if not settings.auto_clock_out_enabled:
    return 0
  try:
    cfg = settings.auto_clock_out()
  except ValidationError as e:
    alert(f"Auto clock-out skipped in {db.name}", str(e))
    return 0
  # Built from the local date, so the stamp is right on a DST change day.
  stamp = datetime.combine(today, cfg.stamp_time, tzinfo=SETTINGS.tz).astimezone(UTC)
  try:
    async with await connect(db.name) as conn:
      punch_rows = await (
        await conn.execute(
          "SELECT id, employee_id, kind, at, instance_id, automatic, received_at, db_login FROM punches WHERE at >= %s",
          (stamp - LOOKBACK,),
        )
      ).fetchall()
      punches_by_employee: defaultdict[int, list[Punch]] = defaultdict(list)
      for row in punch_rows:
        punch = Punch.model_validate(row)
        punches_by_employee[punch.employee_id].append(punch)
      correction_rows = await (
        await conn.execute(
          "SELECT id, punch_id, action, new_at, new_kind, note, recorded_at FROM punch_corrections WHERE punch_id = ANY(%s)",
          ([r["id"] for r in punch_rows],),
        )
      ).fetchall()
      corrections = [PunchCorrection.model_validate(row) for row in correction_rows]
      # The schema decides who is due (R41.2). It ignores corrections for punches not passed in, so all go to every employee.
      due = [
        employee_id
        for employee_id, punches in punches_by_employee.items()
        if auto_clock_out_due(build_timeline(punches, corrections), stamp)
      ]
      # The connection's own transaction (committed on leaving the block) makes the inserts all-or-nothing.
      for employee_id in due:
        await conn.execute(
          "INSERT INTO punches (id, employee_id, kind, at, instance_id, automatic) VALUES (%s, %s, 'clock_out', %s, 'backend', true)",
          (uuid4(), employee_id, stamp),
        )
  except psycopg.Error as e:
    alert(f"Auto clock-out failed in {db.name}", str(e))
    return 0
  LOGGER.info("auto clock-out in %s closed %d shift(s) at %s", db.name, len(due), stamp)
  return len(due)


async def schedule_auto_clock_out(scheduler: Scheduler, db: Database, *, now: datetime | None = None) -> None:
  """(Re)schedule `db`'s job from its settings, then catch up if today's run time has passed (spec 8.4).

  A disabled setting removes the job. The run time is scheduled even when the stamp time is invalid; the run skips and alerts.
  """
  settings = await read_settings(db)
  job_id = f"{JOB_ID_PREFIX}{db.name}"
  if not settings.auto_clock_out_enabled:
    with suppress(JobLookupError):
      scheduler.remove_job(job_id)
    LOGGER.info("auto clock-out in %s is disabled; job removed", db.name)
    return
  t = settings.auto_clock_out_run_time
  scheduler.add_job(
    run_auto_clock_out,
    CronTrigger(hour=t.hour, minute=t.minute, second=t.second, timezone=SETTINGS.tz),
    args=(db,),
    id=job_id,
    replace_existing=True,
  )
  LOGGER.info("auto clock-out in %s scheduled daily at %s %s", db.name, t, SETTINGS.tz)
  # A restart after the run time would otherwise skip today; the run is idempotent, so catching up is safe.
  now_local = (now or datetime.now(UTC)).astimezone(SETTINGS.tz)
  if now_local.time() >= t:
    await run_auto_clock_out(db, today=now_local.date())


async def listen_for_settings(scheduler: Scheduler, db: Database) -> None:
  """Reschedule `db`'s job whenever `config.settings` changes; runs until cancelled.

  Every (re)connect reschedules too, since notifications sent while disconnected were missed.
  """
  backoff = 1.0
  while True:
    try:
      async with await connect(db.name, autocommit=True) as conn:
        await conn.execute(sql.SQL("LISTEN {}").format(sql.Identifier(NOTIFY_CHANNEL)))
        await schedule_auto_clock_out(scheduler, db)
        backoff = 1.0
        async for n in conn.notifies():
          if Notification.model_validate_json(n.payload).table == "config.settings":
            await schedule_auto_clock_out(scheduler, db)
    except psycopg.Error, OSError:
      LOGGER.warning("settings listener for %s lost its connection; retrying in %.0f s", db.name, backoff, exc_info=True)
      await asyncio.sleep(backoff)
      backoff = min(backoff * 2, _MAX_BACKOFF)
