"""Manual report sends (spec 2026-11-18): developers' requests in `backend.report_sends`, sent as they arrive.

The page inserts a request as the developer's badge (schema 0027 checks it) and the insert pings `report_sends`. Each
database has a listener here that sends its pending rows oldest first and writes the outcome back for the page to read.
"""

# Standard library imports
import asyncio
import logging
import traceback
from datetime import datetime, timedelta
from typing import Final

# Third party imports
import psycopg
from aeth_ext.errors.err_handling import alert

# First party imports
from timeclock_backend.databases import Database, connect
from timeclock_backend.jobs.payroll_report import PayrollReportJob
from timeclock_backend.reports import Period
from timeclock_backend.schedule import STATIC
from timeclock_backend.settings import SETTINGS

LOGGER: Final = logging.getLogger(__name__)
INTERRUPTED: Final = "interrupted mid-send; send again"
_MAX_BACKOFF: Final = 60.0
# SKIP LOCKED: two passes (a ping burst) never claim one row.
_CLAIM: Final = """
  UPDATE backend.report_sends SET status = 'sending'
  WHERE id = (SELECT id FROM backend.report_sends WHERE status = 'pending' ORDER BY id FOR UPDATE SKIP LOCKED LIMIT 1)
  RETURNING id, report, week_start, recipients, requested_by
"""


async def fail_interrupted(db: Database) -> int:
  """Fail the rows left mid-send, alert, and return how many; never resent, since the email may have gone.

  Only before a pass: the listener is its database's one claimer, so a `sending` row then is one a lost connection or a
  restart stranded.
  """
  async with await connect(db.name, autocommit=True) as conn:
    cursor = await conn.execute(
      "UPDATE backend.report_sends SET status = 'failed', error = %s, finished_at = now() WHERE status = 'sending' RETURNING id",
      (INTERRUPTED,),
    )
    stranded = [row["id"] for row in await cursor.fetchall()]
  if stranded:  # the requesters may have closed their dialogs
    alert(f"Manual report send(s) interrupted in {db.name}", f"Requests {stranded} were marked failed: {INTERRUPTED}.", wait=False)
  return len(stranded)


async def process_pending(db: Database) -> int:
  """Send `db`'s pending requests oldest first, one at a time, and return how many were handled.

  Each is re-checked first, since the insert's checks are as old as the request: every address still enrolled, the
  requester still a developer. A failure of any kind fails that row, alerts, and moves on to the next.
  """
  handled = 0
  while True:
    async with await connect(db.name, autocommit=True) as conn:
      row = await (await conn.execute(_CLAIM)).fetchone()
      if row is None:
        return handled
      handled += 1
      try:
        entry = next((e for e in STATIC if e.name == row["report"] and isinstance(e.job, PayrollReportJob)), None)
        if entry is None:
          raise ValueError(f"unknown report {row['report']}")
        assert isinstance(entry.job, PayrollReportJob)  # narrowed above, for the type checker
        enrolled = await conn.execute("SELECT email FROM backend.report_recipients WHERE report = %s", (row["report"],))
        current = {r["email"] for r in await enrolled.fetchall()}
        if missing := [r for r in row["recipients"] if r not in current]:
          raise ValueError(f"no longer enrolled for {row['report']}: {', '.join(missing)}")
        # CASE: pg_has_role raises for a role that's gone (a deleted badge), so it only runs once the role is known.
        allowed = await conn.execute(
          "SELECT CASE WHEN EXISTS (SELECT FROM pg_roles WHERE rolname = %(who)s)"
          " THEN pg_has_role(%(who)s, 'timeclock_report_senders', 'USAGE') ELSE false END AS ok",
          {"who": row["requested_by"]},
        )
        if not (await allowed.fetchone() or {}).get("ok"):
          raise ValueError(f"{row['requested_by']} is no longer a developer")
        job = PayrollReportJob(db, groups=entry.job.groups)
        job.name = row["report"]
        at = datetime.now(SETTINGS.tz)
        await job.send(
          Period(row["week_start"], row["week_start"] + timedelta(days=6)),
          row["recipients"],
          note=f"Sent by hand by {row['requested_by']} at {at:%Y-%m-%d %H:%M} ({SETTINGS.tz}).",
        )
      except Exception as exc:
        LOGGER.exception("manual report send %d in %s failed", row["id"], db.name)
        # Before the row's update, which fails too when the connection is what died. wait=False: the default joins the
        # senders, blocking the loop. Alerted: the requester may have closed the dialog.
        details = f"Request {row['id']} in {db.name} by {row['requested_by']}:\n\n{''.join(traceback.format_exception(exc))}"
        alert(f"Manual report send {row['id']} failed", details, wait=False, exc=exc)
        await conn.execute(
          "UPDATE backend.report_sends SET status = 'failed', error = %s, finished_at = now() WHERE id = %s",
          (str(exc) or type(exc).__name__, row["id"]),
        )
      else:
        await conn.execute("UPDATE backend.report_sends SET status = 'sent', finished_at = now() WHERE id = %s", (row["id"],))


async def listen_for_report_sends(db: Database) -> None:
  """Send `db`'s pending requests on every ping, and on every (re)connect for the pings missed; runs until cancelled."""
  backoff = 1.0
  while True:
    try:
      async with await connect(db.name, autocommit=True) as conn:
        await conn.execute("LISTEN report_sends")
        await fail_interrupted(db)
        await process_pending(db)
        backoff = 1.0
        async for _ in conn.notifies():  # a ping only says "look": the rows say what to send
          await process_pending(db)
    except psycopg.Error, OSError:
      LOGGER.warning("report send listener for %s lost its connection; retrying in %.0f s", db.name, backoff, exc_info=True)
      await asyncio.sleep(backoff)
      backoff = min(backoff * 2, _MAX_BACKOFF)
