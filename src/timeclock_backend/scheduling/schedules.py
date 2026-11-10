"""Schedule types: a cron line in a zone, or a one-off time."""

# Standard library imports
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

# Third party imports
from cronsim import CronSim

# First party imports
# Local application imports
from timeclock_backend.settings import SETTINGS

if TYPE_CHECKING:
  # Standard library imports
  from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class Cron:
  """A 5-field cron line evaluated in `tz` (default: the settings zone); a bad line raises `CronSimError` at construction."""

  expr: str
  tz: ZoneInfo = field(default_factory=lambda: SETTINGS.tz)

  def __post_init__(self) -> None:
    """Parse the line now so a bad one fails at construction."""
    CronSim(self.expr, datetime.now(self.tz))

  def next_after(self, t: datetime) -> datetime:
    """First fire time strictly after aware `t`; `CronSim` may yield `t` itself when it is a fire time."""
    t = t.astimezone(self.tz)
    for fire in CronSim(self.expr, t):
      if fire > t:
        return fire
    raise AssertionError  # pragma: no cover  # CronSim iterates indefinitely

  def latest_before(self, t: datetime) -> datetime:
    """Most recent fire time at or before aware `t`."""
    # Reverse iteration is strictly before its start; start one minute past `t`'s minute so a fire at `t` is included.
    start = t.astimezone(self.tz).replace(second=0, microsecond=0) + timedelta(minutes=1)
    return next(CronSim(self.expr, start, reverse=True))

  def describe(self) -> str:
    """The line in English."""
    return CronSim(self.expr, datetime.now(self.tz)).explain()


@dataclass(frozen=True)
class At:
  """A one-off time; it always runs, however late."""

  when: datetime

  def __post_init__(self) -> None:
    """Reject a naive time."""
    if self.when.tzinfo is None:
      msg = "At needs an aware datetime"
      raise ValueError(msg)

  def next_after(self, t: datetime) -> datetime:  # an `At` ignores `t`
    """Its time, however late."""
    return self.when

  def describe(self) -> str:
    """The time in English."""
    return f"once at {self.when.isoformat()}"


type Schedule = Cron | At
