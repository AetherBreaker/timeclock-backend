"""Test helpers shared by the scheduling tests: a fake clock and a job that records its runs."""

# Standard library imports
import asyncio
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, override
from zoneinfo import ZoneInfo

# First party imports
# Local application imports
from timeclock_backend.scheduling import Cron, JobBase

if TYPE_CHECKING:
  # Standard library imports
  from collections.abc import Callable

NY = ZoneInfo("America/New_York")
HOURLY = Cron("0 * * * *", NY)


def ny(hour: int, minute: int = 0, second: int = 0, day: int = 10) -> datetime:
  return datetime(2026, 11, day, hour, minute, second, tzinfo=NY)


class FakeClock:
  """`sleep` advances `now` by the time asked plus a one-off `lag`, and yields once."""

  def __init__(self, t: datetime) -> None:
    self.t = t.astimezone(UTC)
    self.lag = timedelta()

  def now(self) -> datetime:
    return self.t

  async def sleep(self, seconds: float) -> None:
    self.t += timedelta(seconds=seconds) + self.lag
    self.lag = timedelta()
    await asyncio.sleep(0)


class RecordingJob(JobBase):
  """Records each run's `scheduled_for` and start time; fails its first `fails` runs (-1: always); blocks on `block` if set."""

  def __init__(self, clock: FakeClock, *, fails: int = 0, block: asyncio.Event | None = None) -> None:
    self.clock = clock
    self.fails = fails
    self.block = block
    self.during: Callable[[], None] | None = None  # called once, at the start of the next run
    self.seen: list[datetime] = []
    self.ran_at: list[datetime] = []
    self.ended: list[datetime] = []
    self.failures: list[tuple[int, bool]] = []
    self.cancelled = False

  @override
  async def main(self, scheduled_for: datetime) -> None:
    self.seen.append(scheduled_for)
    self.ran_at.append(self.clock.now())
    if self.during:
      during, self.during = self.during, None
      during()
    try:
      if self.block:
        await self.block.wait()
    except asyncio.CancelledError:
      self.cancelled = True
      raise
    self.ended.append(scheduled_for)
    if self.fails:
      self.fails -= 1
      msg = "boom"
      raise RuntimeError(msg)

  @override
  async def on_failure(
    self, scheduled_for: datetime, error: Exception, attempt: int, giving_up: bool, *, restart_before: datetime | None = None
  ) -> None:
    self.failures.append((attempt, giving_up))
    await super().on_failure(scheduled_for, error, attempt, giving_up, restart_before=restart_before)


async def until(condition: Callable[[], bool]) -> None:
  for _ in range(100_000):
    if condition():
      return
    await asyncio.sleep(0)
  raise AssertionError("condition never held")
