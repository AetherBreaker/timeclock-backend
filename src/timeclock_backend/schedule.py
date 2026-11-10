"""The registry (spec §3): every scheduled entry the backend runs. Nothing else declares entries."""

# Standard library imports
from datetime import timedelta
from typing import Final

# First party imports
from timeclock_backend.databases import DATABASES
from timeclock_backend.jobs.auto_clock_out import AutoClockOutJob
from timeclock_backend.jobs.payroll_report import PayrollReportJob
from timeclock_backend.scheduling import Cron, Entry, Retry

# Placeholder recipient and schedule until the owner settles the real entries (spec §7.1).
STATIC: Final[tuple[Entry, ...]] = (
  Entry(
    "payroll_report",
    Cron("0 6 * * MON"),
    PayrollReportJob(next(db for db in DATABASES if db.production), ("jacob.ogden@sweetfiretobacco.com",)),
    catch_up=True,
    retry=Retry(timedelta(minutes=15), 4),
  ),
)

# Idle until the first pass (startup's settings listener) schedules each one.
DYNAMIC: Final[tuple[Entry, ...]] = tuple(Entry(f"auto_clock_out:{db.name}", None, AutoClockOutJob(db)) for db in DATABASES)
