"""The weekly payroll report (spec §7): one workbook per employee group for the last full Monday-to-Sunday week, in one email.

`python -m timeclock_backend.jobs.payroll_report` runs the registry's entry once for last week and sends a real email to its
recipients.
"""

# The registry imports this module; the `__main__` block imports it back, lazily.
# pyright: reportImportCycles=false

# Standard library imports
import asyncio
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import override

# Third party imports
from aeth_ext import initialize
from aeth_ext.utils import batch_send_emails, prepare_email_message

# First party imports
from timeclock_backend.databases import Database, connect
from timeclock_backend.reports import Period, ScopeEmployee, fill_tab, write_workbook
from timeclock_backend.reports.payroll import PAYROLL_TABS
from timeclock_backend.scheduling import JobBase
from timeclock_backend.settings import SETTINGS


class PayrollReportJob(JobBase):
  """Emails one payroll workbook per group (`groups=None`: every group) to `recipients`."""

  def __init__(self, database: Database, recipients: tuple[str, ...], groups: tuple[str, ...] | None = None) -> None:
    """The job reading `database`, for the named `groups` or every group."""
    self.database = database
    self.recipients = recipients
    self.groups = groups

  @staticmethod
  def week(scheduled_for: datetime) -> Period:
    """The last full Monday-to-Sunday week (local) ending before `scheduled_for`'s local day."""
    day = scheduled_for.astimezone(SETTINGS.tz).date()
    sunday = day - timedelta(days=day.weekday() + 1)
    return Period(sunday - timedelta(days=6), sunday)

  @override
  def period(self, scheduled_for: datetime) -> str:
    """The week's Monday, so a restart's catch-up sends each week once."""
    return self.week(scheduled_for).start.isoformat()

  @override
  async def main(self, scheduled_for: datetime) -> None:
    """Build every covered group's workbook in a temporary directory, email them together, and delete them.

    Raises:
      ValueError: A named group doesn't exist (a failed run, not a silently smaller email).
    """
    period = self.week(scheduled_for)
    stem = f"Timeclock Report {period.start} to {period.end}"
    async with await connect(self.database.name) as conn:
      known = {row["name"]: row["id"] for row in await (await conn.execute("SELECT id, name FROM groups ORDER BY id")).fetchall()}
      names = list(known) if self.groups is None else list(self.groups)
      if missing := [name for name in names if name not in known]:
        raise ValueError(f"unknown employee group(s): {', '.join(missing)}")
      employees = await (
        await conn.execute("SELECT id, group_id, concat_ws(' ', first_name, middle_name, last_name) AS name, active FROM employees")
      ).fetchall()
      scopes: dict[str, dict[int, ScopeEmployee]] = {
        name: {e["id"]: ScopeEmployee(e["name"], e["active"]) for e in employees if e["group_id"] == known[name]} for name in names
      }

      with TemporaryDirectory() as tmp:
        paths: list[Path] = []
        for name in names:
          path = Path(tmp) / f"{name} {stem}.xlsx"
          tabs = [(tab.name, await fill_tab(conn, tab, period, scopes[name])) for tab in PAYROLL_TABS]
          write_workbook(path, f"{name} {stem}", tabs)
          paths.append(path)
        # Placeholder wording until the owner settles it from the first smoke-test email (spec §7.2).
        msg = prepare_email_message(
          {
            "subject": f"{', '.join(names)} {stem}",
            "body": f"Timeclock reports for {period.start} to {period.end}, attached: {', '.join(names)}.",
            "from_addr": SETTINGS.alerts_email,
            "to_addrs": self.recipients,
            "attachments": paths,
          }
        )
        # Blocking SMTP: a worker thread keeps the relay's loop free.
        await asyncio.to_thread(batch_send_emails, msg)


if __name__ == "__main__":
  # Deferred: the registry imports this module.
  # First party imports
  from timeclock_backend.schedule import STATIC

  initialize(asyncio=True, logging=True)
  entry = next(e for e in STATIC if e.name == "payroll_report")
  asyncio.run(entry.job.main(datetime.now().astimezone()))
