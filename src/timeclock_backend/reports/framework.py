"""Report tabs, typed columns, zero rows for expected keys with no data, and the all-strings workbook writer.

A report declares `Tab`s only; display rules, zero rows and the workbook live here so every report shares them.
"""

# Standard library imports
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from enum import Enum, auto
from typing import TYPE_CHECKING, Any, LiteralString, NamedTuple

# Third party imports
import xlsxwriter

# First party imports
from timeclock_backend.settings import SETTINGS

if TYPE_CHECKING:
  # Standard library imports
  from collections.abc import Mapping, Sequence
  from pathlib import Path
  from zoneinfo import ZoneInfo

  # Third party imports
  from psycopg import AsyncConnection
  from psycopg.rows import DictRow


class Kind(Enum):
  """How a column's value is shown, and its value in a zero row."""

  NAME = auto()
  DAY = auto()
  TIMESTAMP = auto()
  DURATION = auto()
  COUNT = auto()
  YES_NO = auto()
  TEXT = auto()
  LABEL = auto()


@dataclass(frozen=True)
class Column:
  """A header `label` over the query's `field`; `labels` maps a `LABEL` column's stored values to their display text."""

  label: str
  field: str
  kind: Kind
  labels: Mapping[str, str] | None = None


class Grain(Enum):
  """What one row is, which fixes the keys that get zero rows."""

  PER_EVENT = auto()
  PER_EMPLOYEE = auto()
  PER_EMPLOYEE_DAY = auto()


@dataclass(frozen=True)
class Tab:
  """One worksheet. `query` returns `employee_id`, `employee_name` (and `day` for `PER_EMPLOYEE_DAY`) plus each column's `field`.

  Parameters: `%(employee_ids)s` (bigint[]), `%(start)s`/`%(end)s` (dates), `%(start_at)s`/`%(end_at)s` (local midnights as instants).
  """

  name: str
  grain: Grain
  columns: tuple[Column, ...]
  query: LiteralString
  order_by: tuple[str, ...]


@dataclass(frozen=True)
class Period:
  """The first and last local day of a report, both inclusive."""

  start: date
  end: date

  def days(self) -> list[date]:
    """Every day from `start` through `end`."""
    return [self.start + timedelta(days=n) for n in range((self.end - self.start).days + 1)]

  def bounds(self, tz: ZoneInfo) -> tuple[datetime, datetime]:
    """Local midnight of `start` and of the day after `end`, as UTC instants so their difference is real elapsed time (DST)."""
    return (
      datetime.combine(self.start, time(), tz).astimezone(UTC),
      datetime.combine(self.end + timedelta(days=1), time(), tz).astimezone(UTC),
    )


class ScopeEmployee(NamedTuple):
  """A scoped employee; only active ones get zero rows."""

  name: str
  active: bool


# What a column shows when its key had no data; NAME and DAY take the expected row's values instead.
_ZERO: Mapping[Kind, Any] = {Kind.DURATION: "0:00", Kind.COUNT: 0, Kind.YES_NO: False}


def _display(column: Column, value: Any) -> str:  # noqa: PLR0911
  if value is None:
    return ""
  match column.kind:
    case Kind.TIMESTAMP:
      return value.astimezone(SETTINGS.tz).strftime("%Y-%m-%d %H:%M:%S")
    case Kind.DAY:
      return value.isoformat()
    case Kind.YES_NO:
      return "Yes" if value else "No"
    case Kind.COUNT:
      return str(value)
    case Kind.LABEL:
      assert column.labels is not None
      return column.labels[value]
    case Kind.DURATION | Kind.TEXT | Kind.NAME:
      return value


async def fill_tab(  # noqa: C901
  conn: AsyncConnection[DictRow], tab: Tab, period: Period, scope: Mapping[int, ScopeEmployee]
) -> list[list[str]]:
  """The tab's header row then its display rows, zero rows included.

  The query gets every scoped id, inactive too, so an inactive employee's real rows come back; only active ones get zero rows.
  """
  start_at, end_at = period.bounds(SETTINGS.tz)
  cursor = await conn.execute(
    tab.query,
    {"employee_ids": list(scope), "start": period.start, "end": period.end, "start_at": start_at, "end_at": end_at},
  )
  rows: list[dict[str, Any]] = list(await cursor.fetchall())

  days: list[date | None]
  match tab.grain:
    case Grain.PER_EMPLOYEE_DAY:
      days = list(period.days())
    case Grain.PER_EMPLOYEE:
      days = [None]
    case Grain.PER_EVENT:
      days = []
  present = {(row["employee_id"], row.get("day")) for row in rows}
  for employee_id, employee in scope.items():
    if not employee.active:
      continue
    for day in days:
      if (employee_id, day) in present:
        continue
      zero = {"employee_id": employee_id, "employee_name": employee.name, "day": day}
      for column in tab.columns:
        match column.kind:
          case Kind.NAME:
            zero[column.field] = employee.name
          case Kind.DAY:
            zero[column.field] = day
          case _:
            zero[column.field] = _ZERO.get(column.kind)
      rows.append(zero)

  # A None sorts first: (False, ...) < (True, ...) and the placeholder keeps unorderable values out of the comparison.
  rows.sort(key=lambda row: [(row[f] is not None, row[f]) for f in tab.order_by])
  return [[c.label for c in tab.columns], *([_display(c, row[c.field]) for c in tab.columns] for row in rows)]


def write_workbook(path: Path, title: str, tabs: Sequence[tuple[str, list[list[str]]]]) -> None:
  """Write the tabs (name, header-first rows) to `path`; every value is a string so Excel never reinterprets it."""
  wb = xlsxwriter.Workbook(path)
  wb.set_properties({"title": title})
  # '@' is Excel's Text format: cells written as strings stay strings, and a value typed in later isn't converted either.
  text = wb.add_format({"num_format": "@"})
  header = wb.add_format({"num_format": "@", "bold": True})
  for name, rows in tabs:
    ws = wb.add_worksheet(name)
    for c in range(len(rows[0])):
      ws.set_column(c, c, min(60, max(len(r[c]) for r in rows) + 2), text)
    for r, row in enumerate(rows):
      for c, value in enumerate(row):
        if value:  # write_string, never write(): write() turns "=..." into a formula and number-like text into numbers
          ws.write_string(r, c, value, header if r == 0 else text)
    ws.freeze_panes(1, 0)
    ws.ignore_errors({"number_stored_as_text": "A1:XFD1048576"})
  wb.close()
