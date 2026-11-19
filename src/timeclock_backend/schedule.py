"""The registry (spec §3): every scheduled entry the backend runs. Nothing else declares entries."""

# Standard library imports
from datetime import timedelta
from typing import Final

# First party imports
from timeclock_backend.databases import DATABASES
from timeclock_backend.jobs.auto_clock_out import AutoClockOutJob
from timeclock_backend.jobs.payroll_report import PayrollReportJob
from timeclock_backend.scheduling import Entry, Retry

# None for now: the payroll report's time is a row of production's backend.report_schedules, so its entry is dynamic.
STATIC: Final[tuple[Entry, ...]] = ()

# Idle until a listener's first pass schedules each one: startup's report schedule listener the payroll report (its
# recipients are backend.report_recipients rows), its settings listeners the clock-outs.
DYNAMIC: Final[tuple[Entry, ...]] = (
  Entry(
    "payroll_report",
    None,
    PayrollReportJob(next(db for db in DATABASES if db.production)),
    catch_up=True,
    retry=Retry(timedelta(minutes=15), 4),
  ),
  *(Entry(f"auto_clock_out:{db.name}", None, AutoClockOutJob(db)) for db in DATABASES),
)
