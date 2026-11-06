"""The page's fixed copy (spec 2026-11-05 §3.4, §3.5), and how it writes times and durations (§2)."""

# Standard library imports
from datetime import timedelta
from types import MappingProxyType
from typing import TYPE_CHECKING, Final

# Third party imports
from timeclock_schema.enums import PunchKind

# Local folder imports
from .gate import EndReason

if TYPE_CHECKING:
  # Standard library imports
  from collections.abc import Mapping
  from datetime import datetime
  from zoneinfo import ZoneInfo

  # Local folder imports
  from .settings import PageSettings

REFUSED = "This account can't use this page. If it should have access, ask to have it added."
SIGNED_OUT = "You're signed out."
FAILED = "Sign-in with Zoho didn't work. Try again."
WRONG_PASSWORD = "That email and password don't match a test account."
DB_DOWN = "The database can't be reached right now. Try again in a minute."


def ended(reason: EndReason, settings: PageSettings) -> str | None:  # noqa: PLR0911 - one return per reason
  """Why the last sign-in ended, for the sign-in page; None when there's nothing to say."""
  match reason:
    case EndReason.NONE:
      return None
    case EndReason.IDLE:
      minutes = settings.idle_minutes
      return f"Signed out after {minutes} minute{'s' * (minutes != 1)} without activity; unsaved changes were discarded."
    case EndReason.EXPIRED:
      hours = settings.session_hours
      return f"Signed out after {hours} hour{'s' * (hours != 1)}; unsaved changes were discarded."
    case EndReason.REFUSED:
      return REFUSED
    case EndReason.FAILED:
      return FAILED
    case EndReason.WRONG_PASSWORD:
      return WRONG_PASSWORD
    case EndReason.SIGNED_OUT:
      return SIGNED_OUT


KIND_LABEL: Final[Mapping[PunchKind, str]] = MappingProxyType(
  {
    PunchKind.CLOCK_IN: "Clock in",
    PunchKind.CLOCK_OUT: "Clock out",
    PunchKind.BREAK_START: "Break start",
    PunchKind.BREAK_END: "Break end",
    PunchKind.LUNCH_START: "Lunch start",
    PunchKind.LUNCH_END: "Lunch end",
  }
)


def hm(delta: timedelta) -> str:
  """A duration as h:mm, seconds dropped as shift_summary drops them ("7:47"); `editor/rules.js`'s `hm` matches."""
  minutes = delta // timedelta(minutes=1)
  return f"{minutes // 60}:{minutes % 60:02d}"


def clock_time(at: datetime, tz: ZoneInfo, *, h24: bool) -> str:
  """A time of day in `tz`: "6:55 AM", or "06:55" with the 24-hour preference; `editor/rules.js`'s `clockTime` matches."""
  local = at.astimezone(tz)
  if h24:
    return f"{local.hour:02d}:{local.minute:02d}"
  afternoon, hour = divmod(local.hour, 12)
  return f"{hour or 12}:{local.minute:02d} {'PM' if afternoon else 'AM'}"
