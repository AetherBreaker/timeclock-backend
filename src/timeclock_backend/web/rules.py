"""The page's shift rules (spec 2026-11-05 §5, §6): entries and flags over the schema's timeline, and what a save writes.

Shifts aren't stored: `entries` works them out from an employee's corrected punches with `build_timeline`, the hours'
own walk, so the page's shifts are payroll's. `editor/rules.js` is a copy for the editor's preview; the drift test holds
the two to the same answers, so a change here goes there too.
"""

# Standard library imports
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import TYPE_CHECKING, Final
from uuid import UUID

# Third party imports
from timeclock_schema.enums import PunchKind
from timeclock_schema.models import Punch
from timeclock_schema.shifts import HoursTotals, Shift, build_timeline, total_hours

if TYPE_CHECKING:
  # Standard library imports
  from collections.abc import Collection, Iterable, Mapping, Sequence
  from datetime import date, datetime
  from zoneinfo import ZoneInfo

  # Third party imports
  from timeclock_schema.models import Settings

# Punches added on the page say so in `instance_id` (spec §14); `edit_id` is what the database checks.
PAGE_INSTANCE_ID: Final = "web"
# The version of a punch a save is about to correct, in `apply`'s result.
PENDING: Final = UUID(int=0)


@dataclass(frozen=True, slots=True)
class PagePunch:
  """A punch after its newest correction; a voided punch is never one.

  `version` is that correction's id (None when uncorrected), `changed_by`/`changed_at` its account and time.
  """

  id: UUID
  kind: PunchKind
  at: datetime
  automatic: bool
  original_kind: PunchKind
  original_at: datetime
  version: UUID | None
  added_on_page: bool
  instance_id: str
  db_login: str | None
  received_at: datetime | None
  changed_by: str | None
  changed_at: datetime | None

  @property
  def by_hand(self) -> bool:
    """Changed by a person: corrected, added on the page, or added by the owner's login (shift_summary's `corrected`).

    The backend's automatic clock-out shares the owner's postgres login, hence `automatic`.
    """
    return self.version is not None or self.added_on_page or (self.db_login == "postgres" and not self.automatic)

  def as_punch(self, employee_id: int) -> Punch:
    """The schema's punch at this effective kind and time, for its rules."""
    return Punch(
      id=self.id, employee_id=employee_id, kind=self.kind, at=self.at, instance_id=self.instance_id, automatic=self.automatic
    )


class EntryKind(StrEnum):
  """A shift, or punches the hours ignore outside any shift (spec §5.3)."""

  SHIFT = "shift"
  STRAY = "stray"


@dataclass(frozen=True, slots=True)
class Entry:
  """One row of the shift list: a shift with every punch inside its span, or a run of stray punches."""

  kind: EntryKind
  punches: tuple[PagePunch, ...]  # by (at, id); a shift's first is its clock-in
  ignored: frozenset[UUID]
  shift: Shift | None

  @property
  def key(self) -> UUID:
    """The first punch's id, which names the entry in links: entries aren't stored."""
    return self.punches[0].id

  @property
  def start(self) -> datetime:
    """The first punch's time."""
    return self.punches[0].at

  @property
  def end(self) -> datetime | None:
    """A shift's clock-out time (None while open); a stray run's last punch time."""
    if self.shift is None:
      return self.punches[-1].at
    return None if self.shift.clock_out is None else self.shift.clock_out.at

  @property
  def clock_out(self) -> PagePunch | None:
    """A shift's clock-out punch."""
    if self.shift is None or self.shift.clock_out is None:
      return None
    out = self.shift.clock_out.id
    return next(p for p in self.punches if p.id == out)


def entries(punches: Iterable[PagePunch], *, as_of: datetime) -> list[Entry]:
  """Group one employee's punches into shifts and stray runs, chronologically (spec §5.2, §5.3).

  A shift spans its clock-in to its clock-out (an open one, to the end of time), and every punch in that span is the
  shift's, the ones the hours ignore too; ignored punches outside every span form stray runs, consecutive ones sharing
  one. Punches after `as_of` are left out, as the hours leave them out (`build_timeline`). Spans compare `(at, id)`, the
  walk's own order, so punches at one instant land where the walk put them.
  """
  ordered = sorted((p for p in punches if p.at <= as_of), key=lambda p: (p.at, p.id))
  timeline = build_timeline([p.as_punch(0) for p in ordered], as_of=as_of)
  ignored = {a.punch.id for a in timeline.anomalies}
  spans = [
    ((s.clock_in.at, s.clock_in.id), None if s.clock_out is None else (s.clock_out.at, s.clock_out.id), s) for s in timeline.shifts
  ]
  groups: list[tuple[Shift | None, list[PagePunch]]] = []
  span = 0
  for p in ordered:
    key = (p.at, p.id)
    while span < len(spans) and (end := spans[span][1]) is not None and end < key:
      span += 1
    shift = spans[span][2] if span < len(spans) and spans[span][0] <= key else None
    if groups and groups[-1][0] is shift:
      groups[-1][1].append(p)
    else:
      groups.append((shift, [p]))
  return [
    Entry(
      EntryKind.STRAY if shift is None else EntryKind.SHIFT,
      tuple(group),
      frozenset(p.id for p in group if p.id in ignored),
      shift,
    )
    for shift, group in groups
  ]


class Flag(StrEnum):
  """What a row's chips say (spec §5.5), in display order."""

  NEEDS_FIXING = "needs_fixing"  # red: an ignored punch, or a stray run
  AUTOMATIC = "automatic"  # orange: an automatic clock-out nobody has reviewed
  AUTOMATIC_FIXED = "automatic_fixed"  # grey
  BREAKS = "breaks"  # orange: a break past the day's expected count (`over_breaks`) nobody has reviewed
  BREAKS_REVIEWED = "breaks_reviewed"  # grey
  CHANGED = "changed"  # grey: a punch changed or added by hand
  WORKING = "working"  # blue: open


def over_breaks(found: Iterable[Entry], *, limit: int, tz: ZoneInfo) -> dict[UUID, int]:
  """The breaks past `limit` (`max_breaks_per_day`; 0: none) in their local day, by start punch, with the day's count.

  A break is a shift's break start the hours count. Its day is its own local date, as the paid break allowance charges
  it. `found` comes in time order (`entries`), so each day's list does too.
  """
  days: dict[date, list[UUID]] = {}
  for e in found:
    if e.kind is EntryKind.SHIFT:
      for p in e.punches:
        if p.kind is PunchKind.BREAK_START and p.id not in e.ignored:
          days.setdefault(p.at.astimezone(tz).date(), []).append(p.id)
  return {} if limit == 0 else {i: len(ids) for ids in days.values() for i in ids[limit:]}


def flags(entry: Entry, *, over: Mapping[UUID, int]) -> tuple[Flag, ...]:
  """The entry's flags; `over` is `over_breaks` over the employee's entries.

  A reviewed automatic clock-out, or an over break reviewed (corrected, if only marked as correct, or added on the page),
  says so alone rather than also as Changed.
  """
  found: list[Flag] = []
  if entry.kind is EntryKind.STRAY or entry.ignored:
    found.append(Flag.NEEDS_FIXING)
  out = entry.clock_out
  automatic = out if out is not None and out.automatic else None
  if automatic is not None:
    found.append(Flag.AUTOMATIC if automatic.version is None else Flag.AUTOMATIC_FIXED)
  if extra := [p for p in entry.punches if p.id in over]:
    found.append(Flag.BREAKS_REVIEWED if all(p.by_hand for p in extra) else Flag.BREAKS)
  if any(p.by_hand for p in entry.punches if p is not automatic and p.id not in over):
    found.append(Flag.CHANGED)
  if entry.kind is EntryKind.SHIFT and entry.end is None:
    found.append(Flag.WORKING)
  return tuple(found)


def problem(entry: Entry) -> str | None:
  """What needs fixing, for the chip and the attention bar: "stray", "extra:<kind>", "mixed", or None."""
  if entry.kind is EntryKind.STRAY:
    return "stray"
  kinds = {p.kind for p in entry.punches if p.id in entry.ignored}
  if not kinds:
    return None
  return f"extra:{kinds.pop()}" if len(kinds) == 1 else "mixed"


def entry_hours(entry: Entry, punches: Sequence[PagePunch], *, settings: Settings, tz: ZoneInfo, now: datetime) -> HoursTotals | None:
  """A shift's hours by `total_hours` over its own span; None for a stray run, which the hours ignore.

  Every loaded punch goes in, so the day's break allowance is charged across its shifts.
  """
  if entry.kind is EntryKind.STRAY:
    return None
  return total_hours([p.as_punch(0) for p in punches], settings=settings, tz=tz, start=entry.start, end=entry.end or now, now=now)


def week_hours(
  punches: Sequence[PagePunch], *, settings: Settings, tz: ZoneInfo, week: tuple[datetime, datetime], now: datetime
) -> HoursTotals:
  """The employee's hours inside `week`, as payroll counts them."""
  return total_hours([p.as_punch(0) for p in punches], settings=settings, tz=tz, start=week[0], end=week[1], now=now)


@dataclass(frozen=True, slots=True)
class DraftPunch:
  """What the editor wants for one punch it touched; `version` is the one it was edited from (None when new)."""

  id: UUID
  kind: PunchKind
  at: datetime
  removed: bool
  confirmed: bool  # Mark as correct (spec §6.3)
  version: UUID | None
  new: bool


class RecordAction(StrEnum):
  """What a save writes for one punch (spec §6.2); the user never sees these words."""

  AMEND = "amend"
  VOID = "void"
  INSERT = "insert"


@dataclass(frozen=True, slots=True)
class Record:
  """One row a save writes: an INSERT names both, an AMEND what differs from the punch's original (None: the original's).

  The database applies a punch's newest correction alone, over the original (`apply_corrections`), so an amend restates
  any earlier correction it keeps; one back to the original names the original time, as an amend must name something.
  """

  action: RecordAction
  punch_id: UUID
  kind: PunchKind | None
  at: datetime | None


def conflicts(base: Mapping[UUID, PagePunch], draft: Iterable[DraftPunch]) -> list[UUID]:
  """Touched punches someone else changed or removed since the draft's edit began (spec §6.1)."""
  return [d.id for d in draft if not d.new and (d.id not in base or base[d.id].version != d.version)]


def records(base: Mapping[UUID, PagePunch], draft: Iterable[DraftPunch]) -> list[Record]:
  """The rows a draft writes against `base`, in draft order; `conflicts` must be empty first.

  Mark as correct is an amend to the same values: one row then tells who confirmed it (spec §6.3).
  """
  found: list[Record] = []
  for d in draft:
    if d.new:
      if not d.removed:
        found.append(Record(RecordAction.INSERT, d.id, d.kind, d.at))
      continue
    was = base[d.id]
    if d.removed:
      found.append(Record(RecordAction.VOID, d.id, None, None))
    elif d.kind != was.kind or d.at != was.at or d.confirmed:
      kind = d.kind if d.kind != was.original_kind else None
      at = d.at if d.at != was.original_at else None
      found.append(Record(RecordAction.AMEND, d.id, kind, was.original_at if kind is None and at is None else at))
  return found


def apply(base: Sequence[PagePunch], records: Iterable[Record]) -> list[PagePunch]:
  """The punches as they'll read once `records` are written, sorted by (at, id); amended ones carry `PENDING`."""
  by_id = {p.id: p for p in base}
  for r in records:
    match r.action:
      case RecordAction.VOID:
        del by_id[r.punch_id]
      case RecordAction.AMEND:  # over the original, as the database applies it
        was = by_id[r.punch_id]
        kind, at = was.original_kind if r.kind is None else r.kind, was.original_at if r.at is None else r.at
        by_id[r.punch_id] = replace(was, kind=kind, at=at, version=PENDING)
      case RecordAction.INSERT:
        assert r.kind is not None and r.at is not None  # an INSERT names both
        by_id[r.punch_id] = PagePunch(
          id=r.punch_id,
          kind=r.kind,
          at=r.at,
          automatic=False,
          original_kind=r.kind,
          original_at=r.at,
          version=None,
          added_on_page=True,
          instance_id=PAGE_INSTANCE_ID,
          db_login=None,
          received_at=None,
          changed_by=None,
          changed_at=None,
        )
  return sorted(by_id.values(), key=lambda p: (p.at, p.id))


class RefusalKind(StrEnum):
  """Why a save is refused (spec §6.4, §6.13)."""

  STILL_IGNORED = "still_ignored"  # an edited shift still holds a punch the hours ignore
  NEWLY_IGNORED = "newly_ignored"  # a punch the hours counted would be ignored
  OVERLAP = "overlap"  # a new shift overlaps one the employee has


@dataclass(frozen=True, slots=True)
class Refusal:
  """A refused save: the punches at fault, in time order, and the entry they're in (or the overlapped shift)."""

  kind: RefusalKind
  punch_ids: tuple[UUID, ...]
  entry_key: UUID | None


def refusal(before: Sequence[PagePunch], after: Sequence[PagePunch], touched: Collection[UUID], *, as_of: datetime) -> Refusal | None:
  """Why saving `after` over `before` leaves an ignored punch behind, or None (spec §6.4).

  A punch the hours counted that they'd now ignore comes first: it's what the change did, wherever it lands. Then an
  entry holding a touched punch (the draft's or the opened entry's) may hold no ignored punch at all. Ignored punches
  elsewhere were there before and stay someone else's fix.
  """
  before_entries = entries(before, as_of=as_of)
  was_ignored = set().union(*(e.ignored for e in before_entries))
  counted = {p.id for e in before_entries for p in e.punches} - was_ignored
  for e in entries(after, as_of=as_of):
    newly = tuple(p.id for p in e.punches if p.id in e.ignored and p.id in counted)
    if newly:
      return Refusal(RefusalKind.NEWLY_IGNORED, newly, e.key)
  for e in entries(after, as_of=as_of):
    if e.ignored and any(p.id in touched for p in e.punches):
      return Refusal(RefusalKind.STILL_IGNORED, tuple(p.id for p in e.punches if p.id in e.ignored), e.key)
  return None


def overlapping(punches: Sequence[PagePunch], start: datetime, end: datetime, *, as_of: datetime) -> Entry | None:
  """The first of the employee's shifts meeting `[start, end]` (an open one runs to `as_of`), or None (spec §6.13)."""
  for e in entries(punches, as_of=as_of):
    if e.kind is EntryKind.SHIFT and e.start <= end and (e.end or as_of) >= start:
      return e
  return None
