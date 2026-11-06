"""The page's fixed copy (spec 2026-11-05 §3.4, §3.5, §9), and how it writes times, durations and employee fields (§2)."""

# Standard library imports
from datetime import date, timedelta
from types import MappingProxyType
from typing import TYPE_CHECKING, Final

# Third party imports
from timeclock_schema.enums import EmployeeRole, Finger, PunchKind

# Local folder imports
from .gate import EndReason
from .rules import Flag, RecordAction

if TYPE_CHECKING:
  # Standard library imports
  from collections.abc import Mapping
  from datetime import datetime
  from zoneinfo import ZoneInfo

  # Local folder imports
  from .settings import PageSettings
  from .store import EmployeeChange, FieldValue, HistoryChange, Person

REFUSED = "This account can't use this page. If it should have access, ask to have it added."
SIGNED_OUT = "You're signed out."
FAILED = "Sign-in with Zoho didn't work. Try again."
WRONG_PASSWORD = "That email and password don't match a test account."
DB_DOWN = "The database can't be reached right now. Try again in a minute."
OWN_SHIFT = "This is your own shift. Another editor has to change it."
OWN_RECORD = (
  "This is you. You can change your name, contact details and PIN. Another editor has to change your group, role, fingerprint"
  " exemption and active status."
)
EMPLOYEE_GONE = "This employee isn't in the database."
EMPLOYEE_SAVE_FAILED = "The save didn't go through. Try again; your changes are still here."
EMPLOYEE_CHANGED = "This employee changed while you were editing. Check the marked fields and save again."
PIN_DIGITS_ONLY = "A PIN is digits only"
PIN_MISMATCH = "The PINs didn't match."
PIN_NOTE = "Digits only. It takes effect when you press Set PIN, and every clock switches to it as soon as it's connected."
PIN_SAVE_FAILED = "The PIN didn't go through. Try again."
EMPLOYEE_OFFLINE = "The connection is interrupted. Your changes are kept; you can carry on once it's back."
# The clock's own words for the same fields (timeclock-system `ui/text.py`).
FIRST_NAME_REQUIRED = "Enter a first name"
LAST_NAME_REQUIRED = "Enter a last name"
PHONE_INVALID = "Enter a 10-digit US phone number"
EMAIL_INVALID = "Enter an email like name@example.com"
SAVE_FAILED = "The save didn't go through. Reload the page and try again; your changes are kept in this browser."
# An added shift with no clock-out (§6.13); `editor/draft.js` says the same.
ADD_CLOCK_OUT = "Add the clock-out to save. Only a shift that started today, with nothing after it, can stay open."


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


def long_day(day: date, *, year: bool = True) -> str:
  """The editor's day: "Tuesday, November 3, 2026", or without the year "Tuesday, November 3"."""
  return f"{_WEEKDAYS[day.weekday()]}, {_MONTHS[day.month - 1]} {day.day}{f', {day.year}' * year}"


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


# A flag's chip color (spec §5.5).
CHIP: Final[Mapping[Flag, str]] = MappingProxyType(
  {Flag.NEEDS_FIXING: "red", Flag.AUTOMATIC: "orange", Flag.AUTOMATIC_FIXED: "grey", Flag.CHANGED: "grey", Flag.WORKING: "blue"}
)


def employee_details(person: Person, groups: Mapping[int, str], tz: ZoneInfo, *, h24: bool) -> str:
  """The technical details line under an employee's name (spec §11)."""
  return " · ".join(
    (
      f"employee id {person.id}",
      f"username {person.username}",
      f"group {groups.get(person.group_id, '?')} ({person.group_id})",
      f"role {person.role}",
      f"fingerprint exempt {'yes' if person.fingerprint_exempt else 'no'}",
      count(person.fingerprints, "fingerprint"),
      "active" if person.active else "switched off",
      f"phone {person.phone or 'none'}",
      f"email {person.email or 'none'}",
      f"updated {stamp(person.updated_at, tz, h24=h24)}",
      *(() if person.added_by is None else (f"added by {person.added_by}",)),
    )
  )


def change_line(change: HistoryChange, tz: ZoneInfo, *, h24: bool) -> str:
  """One change in History, worded as the editor's Your changes words it (`editor/draft.js`'s `changes`)."""
  old_kind, old_at, new_kind, new_at = change.old_kind, change.old_at, change.new_kind, change.new_at
  if change.action is RecordAction.INSERT and new_kind is not None and new_at is not None:
    word = _KIND_WORD[new_kind]
    return f"Added {'an' if word[0] in 'aeiou' else 'a'} {word} at {clock_time(new_at, tz, h24=h24)}"
  assert old_kind is not None and old_at is not None  # an amend or a void has a before
  was = clock_time(old_at, tz, h24=h24)
  if change.action is RecordAction.VOID or new_kind is None or new_at is None:
    return f"Removed the {_KIND_WORD[old_kind]} at {was}"
  now = clock_time(new_at, tz, h24=h24)
  if new_kind != old_kind and new_at != old_at:
    return f"{KIND_LABEL[old_kind]} at {was} to {KIND_LABEL[new_kind]} at {now}"
  if new_at != old_at:
    return f"{KIND_LABEL[old_kind]}: {was} to {now}"
  if new_kind != old_kind:
    return f"{was}: {KIND_LABEL[old_kind]} to {KIND_LABEL[new_kind]}"
  return f"Marked the {_KIND_WORD[old_kind]} at {was} as correct"


def phone_label(stored: str | None) -> str:
  """A stored phone (`+15555550142`) as people write it, "(555) 555-0142"; "" for none, and anything else as stored."""
  if stored is None:
    return ""
  digits = stored.removeprefix("+1")
  return f"({digits[:3]}) {digits[3:6]}-{digits[6:]}" if len(digits) == 10 and digits.isdecimal() else stored  # noqa: PLR2004


FINGER_LABEL: Final[Mapping[Finger, str]] = MappingProxyType({f: f.value.replace("_", " ").capitalize() for f in Finger})
ROLE_LABEL: Final[Mapping[EmployeeRole, str]] = MappingProxyType({r: r.value.capitalize() for r in EmployeeRole})
# A logged column's name in History and Your changes; any other column (one changed in DBeaver) shows as stored.
FIELD_LABEL: Final[Mapping[str, str]] = MappingProxyType(
  {
    "first_name": "First name",
    "middle_name": "Middle name",
    "last_name": "Last name",
    "phone": "Phone",
    "email": "Email",
    "group_id": "Group",
    "role": "Role",
    "fingerprint_exempt": "Fingerprint exemption",
    "active": "Status",
  }
)


def field_value(field: str, value: FieldValue, groups: Mapping[int, str]) -> str:  # noqa: PLR0911 - one return per field
  """A field's value in plain words; the change log's text form ("true", "2") reads as the editor's own values do."""
  if value is None or value == "":
    return "empty"
  match field:
    case "fingerprint_exempt":
      return "on" if value in {True, "true"} else "off"
    case "active":
      return "Active" if value in {True, "true"} else "Switched off"
    case "group_id":
      return groups.get(int(value), f"group {value}")
    case "role":
      return ROLE_LABEL[EmployeeRole(value)] if value in set(EmployeeRole) else str(value)
    case "phone":
      return phone_label(str(value))
    case _:
      return str(value)


def employee_change_line(change: EmployeeChange, groups: Mapping[int, str]) -> str:
  """One logged change in plain words: "Group: Warehouse to Office", "PIN changed" (never a value, spec §9)."""
  if change.field == "pin":
    return "PIN changed"
  old, new = (field_value(change.field, v, groups) for v in (change.old_value, change.new_value))
  return f"{FIELD_LABEL.get(change.field, change.field)}: {old} to {new}"


def switch_off_note(first: str) -> str:
  """What switching Active off does (spec §9, §10), shown before it's saved."""
  return (
    f"Switched off, {first} can't clock in at any clock. A shift {first} is working can still be finished. Shifts and history stay."
  )
