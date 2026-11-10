"""The registry (spec §3): every scheduled entry the backend runs. Nothing else declares entries."""

# Standard library imports
from typing import Final

# First party imports
from timeclock_backend.databases import DATABASES
from timeclock_backend.jobs.auto_clock_out import AutoClockOutJob
from timeclock_backend.scheduling import Entry

STATIC: Final[tuple[Entry, ...]] = ()

# Idle until the first pass (startup's settings listener) schedules each one.
DYNAMIC: Final[tuple[Entry, ...]] = tuple(Entry(f"auto_clock_out:{db.name}", None, AutoClockOutJob(db)) for db in DATABASES)
