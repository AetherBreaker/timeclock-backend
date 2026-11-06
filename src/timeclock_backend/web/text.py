"""The page's fixed copy (spec 2026-11-05 §3.4, §3.5), and how it writes times and durations (§2)."""

# Standard library imports
from datetime import date, timedelta
from types import MappingProxyType
from typing import TYPE_CHECKING, Final

# Third party imports
from timeclock_schema.enums import PunchKind

# Local folder imports
from .gate import EndReason
from .rules import Flag

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


# Spelled out rather than strftime's, which follow the container's locale.
_MONTHS: Final = (
  "January",
  "February",
  "March",
  "April",
  "May",
  "June",
  "July",
  "August",
  "September",
  "October",
  "November",
  "December",
)
_WEEKDAYS: Final = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
# A punch kind in the middle of a sentence ("2 extra clock-ins").
_KIND_WORD: Final[Mapping[PunchKind, str]] = MappingProxyType(
  {
    PunchKind.CLOCK_IN: "clock-in",
    PunchKind.CLOCK_OUT: "clock-out",
    PunchKind.BREAK_START: "break start",
    PunchKind.BREAK_END: "break end",
    PunchKind.LUNCH_START: "lunch start",
    PunchKind.LUNCH_END: "lunch end",
  }
)


def count(n: int, word: str, words: str | None = None) -> str:
  """A count and its noun: "1 shift", "2 shifts"."""
  return f"{n} {word if n == 1 else words or word + 's'}"


def day_label(day: date) -> str:
  """The shift list's day: "Mon, Nov 2"."""
  return f"{_WEEKDAYS[day.weekday()][:3]}, {_MONTHS[day.month - 1][:3]} {day.day}"


def long_day(day: date) -> str:
  """The editor's day: "Tuesday, November 3, 2026"."""
  return f"{_WEEKDAYS[day.weekday()]}, {_MONTHS[day.month - 1]} {day.day}, {day.year}"


def week_label(monday: date) -> str:
  """The shift list's week: "Nov 2 to 8, 2026", "Oct 26 to Nov 1, 2026", "Dec 28, 2026 to Jan 3, 2027"."""
  sunday = monday + timedelta(days=6)
  first, last = _MONTHS[monday.month - 1][:3], _MONTHS[sunday.month - 1][:3]
  if monday.year != sunday.year:
    return f"{first} {monday.day}, {monday.year} to {last} {sunday.day}, {sunday.year}"
  if monday.month != sunday.month:
    return f"{first} {monday.day} to {last} {sunday.day}, {sunday.year}"
  return f"{first} {monday.day} to {sunday.day}, {sunday.year}"


def stamp(at: datetime, tz: ZoneInfo, *, h24: bool) -> str:
  """A moment in technical details: "Oct 28, 2:14 PM"."""
  local = at.astimezone(tz)
  return f"{_MONTHS[local.month - 1][:3]} {local.day}, {clock_time(at, tz, h24=h24)}"


def flag_label(flag: Flag, problem: str | None, ignored: int) -> str:  # noqa: PLR0911 - one return per flag
  """A row's chip (spec §5.5); `problem` and `ignored` say what needs fixing."""
  match flag:
    case Flag.NEEDS_FIXING if problem == "stray":
      return "Stray punch" if ignored == 1 else "Stray punches"
    case Flag.NEEDS_FIXING if problem is not None and problem.startswith("extra:"):
      word = _KIND_WORD[PunchKind(problem.removeprefix("extra:"))]
      return f"Extra {word}" if ignored == 1 else f"{ignored} extra {word}s"
    case Flag.NEEDS_FIXING:
      return "Needs fixing"
    case Flag.AUTOMATIC:
      return "Automatic clock-out"
    case Flag.AUTOMATIC_FIXED:
      return "Automatic clock-out, fixed"
    case Flag.CHANGED:
      return "Changed"
    case Flag.WORKING:
      return "Working now"


def attention_label(key: str, n: int) -> str:
  """One count in the attention bar: "2 automatic clock-outs", "1 extra clock-in", "3 stray punches"."""
  if key == "automatic":
    return count(n, "automatic clock-out")
  if key == "stray":
    return count(n, "stray punch", "stray punches")
  if key.startswith("extra:"):
    return count(n, f"extra {_KIND_WORD[PunchKind(key.removeprefix('extra:'))]}")
  return f"{count(n, 'shift')} that {'needs' if n == 1 else 'need'} fixing"


def attention_headline(n: int) -> str:
  """The attention bar's lead: "2 shifts need attention this week:"."""
  return f"{count(n, 'shift')} {'needs' if n == 1 else 'need'} attention this week:"
