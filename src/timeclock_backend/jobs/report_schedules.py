"""Report schedules (spec 2026-11-18): each report entry's cron line, read from production's `backend.report_schedules`.

The entries start idle. A listener applies every row on connect and on every `report_schedules` ping (schema 0029), so
an edit reschedules at once, without a release.
"""

# Standard library imports
import asyncio
import dataclasses
import logging
from typing import TYPE_CHECKING, Final

# Third party imports
import psycopg
from aeth_ext.errors.err_handling import alert
from cronsim import CronSimError

# First party imports
from timeclock_backend.databases import Database, connect
from timeclock_backend.scheduling import Cron
from timeclock_backend.settings import SETTINGS

if TYPE_CHECKING:
  # Standard library imports
  from collections.abc import Sequence

  # First party imports
  from timeclock_backend.scheduling import Entry, Runner

LOGGER: Final = logging.getLogger(__name__)
_MAX_BACKOFF: Final = 60.0


async def apply_report_schedules(runner: Runner, entries: Sequence[Entry], db: Database, seen: dict[str, str | None]) -> None:
  """Give each entry its row's line, skipping a line already in `seen` (the last handled per entry; None for no row).

  A skipped line is a reconnect, a ping for another row or a whitespace edit: a replace then would cancel a resting retry.
  A bad line alerts and keeps the current schedule until a restart (none at startup, so it's idle then); no row alerts
  and idles the entry. `seen` is updated either way, so each alerts once.
  """
  async with await connect(db.name) as conn:
    rows = {r["report"]: r["cron"] for r in await (await conn.execute("SELECT report, cron FROM backend.report_schedules")).fetchall()}
  for entry in entries:
    name = entry.name
    line = None if (raw := rows.get(name)) is None else " ".join(raw.split())
    if name in seen and seen[name] == line:
      continue
    seen[name] = line
    if line is None:
      runner.remove(name)
      # wait=False: the default joins the send threads on the loop, stalling the relay.
      alert(
        f"No schedule for {name}",
        f"backend.report_schedules in {db.name} has no row for {name}; it won't run until one is added.",
        wait=False,
      )
      continue
    try:
      schedule = Cron(line)
    except CronSimError as e:
      # Says when it's idle: "keeps its schedule" after a restart would hide a report that stopped.
      due = runner.next_run(name)
      kept = (
        "It won't run until the line is fixed."
        if due is None
        else f"It keeps its current schedule (next run {due.astimezone(SETTINGS.tz):%Y-%m-%d %H:%M}) until the backend"
        " restarts; then it won't run until the line is fixed."
      )
      alert(
        f"Bad schedule for {name}",
        f"backend.report_schedules in {db.name} has {line!r} for {name}, which isn't a cron line that runs ({e}). {kept}",
        wait=False,
      )
      continue
    runner.replace(name, dataclasses.replace(entry, schedule=schedule))
    LOGGER.info("%s: scheduled from %r (%s)", name, line, schedule.describe())


async def listen_for_report_schedules(runner: Runner, entries: Sequence[Entry], db: Database) -> None:
  """Apply `db`'s report schedules on every ping, and on every (re)connect for the pings missed; runs until cancelled."""
  seen: dict[str, str | None] = {}  # outlives a reconnect, so its pass changes nothing that hasn't changed
  backoff = 1.0
  while True:
    try:
      async with await connect(db.name, autocommit=True) as conn:
        await conn.execute("LISTEN report_schedules")
        await apply_report_schedules(runner, entries, db, seen)
        backoff = 1.0
        async for _ in conn.notifies():  # a ping only says "look": the rows say what to schedule
          await apply_report_schedules(runner, entries, db, seen)
    except psycopg.Error, OSError:
      LOGGER.warning("report schedule listener for %s lost its connection; retrying in %.0f s", db.name, backoff, exc_info=True)
      await asyncio.sleep(backoff)
      backoff = min(backoff * 2, _MAX_BACKOFF)
