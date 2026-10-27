"""The automatic clock-out (R41): runs the rows of `backend.auto_clock_out_runs`, scheduled from that table."""

# Standard library imports
import asyncio
import logging
from collections import defaultdict
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from typing import Final
from uuid import uuid4

# Third party imports
import psycopg
from aeth_ext.errors.err_handling import alert
from apscheduler.jobstores.base import JobLookupError
from apscheduler.triggers.date import DateTrigger
from psycopg import sql
from pydantic import ValidationError
from timeclock_schema.models import AutoClockOutRun, Punch, PunchCorrection
from timeclock_schema.shifts import auto_clock_out_due, build_timeline
from timeclock_schema.wire import NOTIFY_CHANNEL, Notification

# Local folder imports
from .databases import Database, connect  # APScheduler evaluates the jobs' annotations (inspect.signature) at runtime
from .scheduler_config import Scheduler  # noqa: TC001  # likewise: sync_auto_clock_out is a job

LOGGER: Final = logging.getLogger(__name__)

# Open shifts older than this aren't auto-closed (spec §8.4).
LOOKBACK: Final = timedelta(days=14)
JOB_ID_PREFIX: Final = "auto_clock_out:"
_MAX_BACKOFF: Final = 60.0
# Serializes overlapping runs on one database (across processes, which _PASS_LOCKS doesn't reach); distinct from bootstrap.MIGRATION_LOCK_KEY.
_RUN_LOCK_KEY: Final = 7_461_726_502
RETRY_DELAY: Final = timedelta(minutes=10)
# Bounds the pass's wait for the projection lock, which an owner's open SQL-editor transaction on config.settings holds.
_LOCK_TIMEOUT: Final = "1min"
# Serializes one database's passes (the listener's and the job's), so a stale pass can't overwrite a fresher pass's job.
_PASS_LOCKS: Final[defaultdict[str, asyncio.Lock]] = defaultdict(asyncio.Lock)


async def run_auto_clock_out(db: Database, run_id: int) -> int:
  """Run one `backend.auto_clock_out_runs` row: a clock-out at its stamp for every employee still on the clock (R41.1).

  The clock-outs and the row's `ran_at`/`closed` commit in one transaction, so a run completes or leaves no trace. A stamp
  still in the future is written as is (owner, 2026-10-27); the clock ignores a punch until its time comes.

  Returns:
    The number of clock-outs inserted; 0 when the row has already run or doesn't exist.

  Raises:
    psycopg.Error: On a database error, with nothing written.
  """
  async with await connect(db.name) as conn:
    # First in the transaction: an overlapping run waits here, then reads this one's committed inserts (READ COMMITTED).
    await conn.execute("SELECT pg_advisory_xact_lock(%s)", (_RUN_LOCK_KEY,))
    run = await (
      await conn.execute(
        "SELECT id, scheduled_for, scheduled_tz, stamp, ran_at, closed FROM backend.auto_clock_out_runs "
        "WHERE id = %s AND ran_at IS NULL FOR UPDATE",
        (run_id,),
      )
    ).fetchone()
    if run is None:
      return 0
    stamp = AutoClockOutRun.model_validate(run).stamp
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
    for employee_id in due:
      await conn.execute(
        "INSERT INTO punches (id, employee_id, kind, at, instance_id, automatic) VALUES (%s, %s, 'clock_out', %s, 'backend', true)",
        (uuid4(), employee_id, stamp),
      )
    await conn.execute(
      "UPDATE backend.auto_clock_out_runs SET ran_at = clock_timestamp(), closed = %s WHERE id = %s", (len(due), run_id)
    )
  LOGGER.info("auto clock-out run %d in %s closed %d shift(s) at %s", run_id, db.name, len(due), stamp)
  return len(due)


async def sync_auto_clock_out(scheduler: Scheduler, db: Database) -> None:
  """One pass over `db`'s runs (spec §8.4): extend the projection, run due rows oldest first, schedule the next pass.

  Runs at startup, on each `config.settings` notification and listener reconnect, and as the job at each row's time. Due rows
  have passed, haven't run, are within `LOOKBACK`, and run only while enabled. The pass stops at its first failed run: a newer
  run closing a shift first would leave the older run nothing to close. A database error alerts and retries after
  `RETRY_DELAY`.
  """
  job_id = f"{JOB_ID_PREFIX}{db.name}"
  async with _PASS_LOCKS[db.name]:
    next_at: datetime | None
    try:
      async with await connect(db.name, autocommit=True) as conn:
        await conn.execute("SELECT set_config('lock_timeout', %s, false)", (_LOCK_TIMEOUT,))
        # The triggers project only on edits; this extends the projection as the days pass.
        await conn.execute("SELECT backend.project_auto_clock_out_runs()")
        due = await (
          await conn.execute(
            "SELECT id FROM backend.auto_clock_out_runs WHERE ran_at IS NULL AND scheduled_for <= now() "
            "AND scheduled_for > now() - %s AND EXISTS (SELECT FROM config.settings WHERE auto_clock_out_enabled) "
            "ORDER BY scheduled_for, id",
            (LOOKBACK,),
          )
        ).fetchall()
      for row in due:
        await run_auto_clock_out(db, row["id"])
      # Read after the runs, so the job never lands on a row that came due while they ran.
      async with await connect(db.name, autocommit=True) as conn:
        upcoming = await (
          await conn.execute(
            "SELECT min(scheduled_for) AS at FROM backend.auto_clock_out_runs WHERE ran_at IS NULL AND scheduled_for > now()"
          )
        ).fetchone()
      next_at = None if upcoming is None else upcoming["at"]
    except psycopg.Error as e:
      # wait=False: the default joins the send threads on the loop for up to ~21 s, stalling the relay.
      alert(f"Auto clock-out failed in {db.name}", str(e), wait=False)
      next_at = datetime.now(UTC) + RETRY_DELAY
    if next_at is None:
      with suppress(JobLookupError):
        scheduler.remove_job(job_id)
      LOGGER.info("auto clock-out in %s has no run ahead; job removed", db.name)
      return
    # No misfire grace: a late firing still runs, since a dropped one-shot job would never reschedule itself.
    scheduler.add_job(
      sync_auto_clock_out,
      DateTrigger(run_date=next_at),
      args=(scheduler, db),
      id=job_id,
      replace_existing=True,
      misfire_grace_time=None,  # pyright: ignore[reportArgumentType]  # None (no limit) is documented; the inferred type is int
    )
    LOGGER.info("next auto clock-out pass in %s at %s", db.name, next_at)


async def listen_for_settings(scheduler: Scheduler, db: Database) -> None:
  """Run a pass whenever `config.settings` changes; runs until cancelled.

  Every (re)connect runs one too, since notifications sent while disconnected were missed.
  """
  backoff = 1.0
  while True:
    try:
      async with await connect(db.name, autocommit=True) as conn:
        await conn.execute(sql.SQL("LISTEN {}").format(sql.Identifier(NOTIFY_CHANNEL)))
        await sync_auto_clock_out(scheduler, db)
        backoff = 1.0
        async for n in conn.notifies():
          try:
            table = Notification.model_validate_json(n.payload).table
          except ValidationError:
            # NOTIFY needs no privilege, so any login can send a malformed payload; it mustn't restart the process.
            LOGGER.warning("ignored a malformed %s notification in %s: %.200r", NOTIFY_CHANNEL, db.name, n.payload)
            continue
          if table == "config.settings":
            await sync_auto_clock_out(scheduler, db)
    except psycopg.Error, OSError:
      LOGGER.warning("settings listener for %s lost its connection; retrying in %.0f s", db.name, backoff, exc_info=True)
      await asyncio.sleep(backoff)
      backoff = min(backoff * 2, _MAX_BACKOFF)
