"""The scheduler uses the settings time zone, the custom executor and the misfire defaults, and runs coroutine jobs."""

# Standard library imports
import asyncio

# First party imports
from timeclock_backend.scheduler_config import CustomAsyncIOExecutor, Scheduler
from timeclock_backend.settings import SETTINGS

MISFIRE_GRACE_SECS = 60


def test_scheduler_uses_settings_tz_and_the_custom_executor():
  s = Scheduler.init_scheduler()
  assert s.timezone == SETTINGS.tz
  assert isinstance(s._executors["default"], CustomAsyncIOExecutor)
  assert s._job_defaults["misfire_grace_time"] == MISFIRE_GRACE_SECS
  assert s._job_defaults["coalesce"] is True


async def test_coroutine_job_runs():
  s, ran = Scheduler.init_scheduler(), asyncio.Event()

  async def job():
    ran.set()

  s.start()
  s.add_job(job, id="t")  # no trigger: runs once now
  await asyncio.wait_for(ran.wait(), 2)
  s.shutdown(wait=False)
