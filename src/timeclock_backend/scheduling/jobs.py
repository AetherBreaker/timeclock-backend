"""Jobs and the entries that schedule them."""

# The runner and its jobs refer to each other for type checking only; nothing imports in a cycle at runtime.
# pyright: reportImportCycles=false

# Standard library imports
import logging
import traceback
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

# Third party imports
from aeth_ext.errors.err_handling import alert

if TYPE_CHECKING:
  # Standard library imports
  from datetime import datetime, timedelta

  # Local folder imports
  from .runner import Runner
  from .schedules import Schedule

LOGGER: Final = logging.getLogger(__name__)


class JobBase(ABC):
  """A job: subclasses define `main`; shared behaviour (failure alert, retry) lives here and in the runner.

  The runner sets `name` and `runner` when it registers the job's entry.
  """

  name: str
  runner: Runner | None = None

  @abstractmethod
  async def main(self, scheduled_for: datetime) -> None:
    """Run once for the scheduled time `scheduled_for`."""

  def period(self, scheduled_for: datetime) -> str:
    """The key a run is recorded under."""
    return scheduled_for.isoformat()

  async def on_failure(
    self, scheduled_for: datetime, error: Exception, attempt: int, giving_up: bool, *, restart_before: datetime | None = None
  ) -> None:
    """Alert on a run's first failure and when retrying gives up; log the retries between.

    `restart_before` is set when this failure is final for a `catch_up` entry: a restart before then still runs the run.
    """
    details = f"Run for {scheduled_for.isoformat()}, attempt {attempt}{' (giving up)' if giving_up else ''}:\n\n"
    if restart_before is not None:
      details += f"Not recorded: a restart before {restart_before.isoformat()} runs it once; after that this run is skipped.\n\n"
    if attempt == 1 or giving_up:
      LOGGER.error("scheduled job %s failed (attempt %d)", self.name, attempt, exc_info=error)
      # wait=False: the default joins the senders, blocking the loop for up to ~21 s.
      alert(f"Scheduled job {self.name} failed", details + "".join(traceback.format_exception(error)), wait=False, exc=error)
    else:
      LOGGER.warning("scheduled job %s failed again (attempt %d)", self.name, attempt, exc_info=error)


@dataclass(frozen=True)
class Retry:
  """Retry a failed run every `interval`, at most `max_retries` times and never past the entry's next scheduled time."""

  interval: timedelta
  max_retries: int


@dataclass(frozen=True)
class Entry:
  """A registry entry; `schedule=None` is idle, allowed only in the dynamic table."""

  name: str
  schedule: Schedule | None
  job: JobBase
  catch_up: bool = False
  retry: Retry | None = None
