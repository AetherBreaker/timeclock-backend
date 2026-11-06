"""Tests for the page's shift rules: entries, flags, hours, records and the save refusals (spec 2026-11-05 §5, §6)."""

# Standard library imports
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

# Third party imports
from timeclock_schema.enums import PunchKind
from timeclock_schema.models import Settings
from timeclock_schema.shifts import total_hours

# First party imports
from timeclock_backend.web.rules import (
  DraftPunch,
  EntryKind,
  Flag,
  PagePunch,
  Record,
  RecordAction,
  RefusalKind,
  apply,
  conflicts,
  entries,
  entry_hours,
  flags,
  overlapping,
  problem,
  records,
  refusal,
)

TZ = ZoneInfo("America/New_York")
LATER = datetime(2026, 11, 20, tzinfo=UTC)  # after every punch below
SETTINGS = Settings(paid_break_minutes=30)
IN, OUT = PunchKind.CLOCK_IN, PunchKind.CLOCK_OUT
BREAK_START, BREAK_END = PunchKind.BREAK_START, PunchKind.BREAK_END


def _at(clock: str, day: int = 3) -> datetime:
  hour, minute = map(int, clock.split(":"))
  return datetime(2026, 11, day, hour, minute, tzinfo=TZ).astimezone(UTC)


def _p(kind: PunchKind, clock: str, day: int = 3, **fields: Any) -> PagePunch:
  at = _at(clock, day)
  values: dict[str, Any] = {
    "id": uuid4(),
    "kind": kind,
    "at": at,
    "automatic": False,
    "original_kind": kind,
    "original_at": at,
    "version": None,
    "added_on_page": False,
    "instance_id": "front",
    "db_login": "clock_front",
    "received_at": None,
    "changed_by": None,
    "changed_at": None,
  }
  return PagePunch(**(values | fields))


def _draft(punch: PagePunch, **changes: Any) -> DraftPunch:
  values: dict[str, Any] = {
    "id": punch.id,
    "kind": punch.kind,
    "at": punch.at,
    "removed": False,
    "confirmed": False,
    "version": punch.version,
    "new": False,
  }
  return DraftPunch(**(values | changes))


def test_a_double_clock_in_is_one_shift_holding_the_extra() -> None:
  first, extra = _p(IN, "7:01"), _p(IN, "7:04")
  punches = [first, extra, _p(BREAK_START, "9:30"), _p(BREAK_END, "9:44"), _p(OUT, "15:33")]
  [entry] = entries(punches, as_of=LATER)
  assert entry.kind is EntryKind.SHIFT
  assert [p.id for p in entry.punches] == [p.id for p in punches]
  assert entry.ignored == {extra.id}
  assert flags(entry) == (Flag.NEEDS_FIXING,)
  assert problem(entry) == "extra:clock_in"


def test_strays_in_a_row_share_one_entry() -> None:
  shift = [_p(IN, "7:00"), _p(OUT, "15:30")]
  strays = [_p(OUT, "17:02"), _p(BREAK_START, "17:10")]
  later_shift = [_p(IN, "7:00", day=4), _p(OUT, "15:00", day=4)]
  last_stray = _p(OUT, "16:00", day=4)
  found = entries([*shift, *strays, *later_shift, last_stray], as_of=LATER)
  assert [e.kind for e in found] == [EntryKind.SHIFT, EntryKind.STRAY, EntryKind.SHIFT, EntryKind.STRAY]
  assert [p.id for p in found[1].punches] == [p.id for p in strays]
  assert found[1].ignored == {p.id for p in strays}
  assert [p.id for p in found[3].punches] == [last_stray.id]
  assert flags(found[1]) == (Flag.NEEDS_FIXING,)
  assert problem(found[1]) == "stray"


def test_an_anomaly_inside_an_open_shift_stays_in_it() -> None:
  first, extra = _p(IN, "7:00"), _p(IN, "9:00")
  [entry] = entries([first, extra], as_of=LATER)
  assert entry.end is None
  assert entry.ignored == {extra.id}
  assert flags(entry) == (Flag.NEEDS_FIXING, Flag.WORKING)


def test_an_overnight_shift_is_one_entry_under_its_clock_in() -> None:
  start, end = _p(IN, "22:00", day=1), _p(OUT, "6:00", day=2)
  [entry] = entries([start, end], as_of=LATER)
  assert entry.start == start.at
  assert entry.end == end.at
  assert entry.start.astimezone(TZ).day == 1


def test_a_punch_after_as_of_is_left_out() -> None:
  start, end = _p(IN, "7:00"), _p(OUT, "23:00")
  [entry] = entries([start, end], as_of=_at("15:00"))
  assert [p.id for p in entry.punches] == [start.id]
  assert flags(entry) == (Flag.WORKING,)


def test_flags_name_an_unreviewed_automatic_clock_out_and_a_fixed_one() -> None:
  start = _p(IN, "7:58")
  automatic = _p(OUT, "23:00", automatic=True, db_login="postgres")
  [entry] = entries([start, automatic], as_of=LATER)
  assert flags(entry) == (Flag.AUTOMATIC,)
  fixed = replace(automatic, at=_at("17:05"), version=uuid4())
  [entry] = entries([start, fixed], as_of=LATER)
  assert flags(entry) == (Flag.AUTOMATIC_FIXED,)


def test_a_hand_change_flags_the_shift_changed() -> None:
  out = _p(OUT, "15:00")
  for start in (
    _p(IN, "7:00", version=uuid4()),
    _p(IN, "7:00", added_on_page=True),
    _p(IN, "7:00", db_login="postgres"),
  ):
    [entry] = entries([start, out], as_of=LATER)
    assert flags(entry) == (Flag.CHANGED,), start
  automatic = _p(OUT, "23:00", automatic=True, db_login="postgres", version=uuid4())
  [entry] = entries([_p(IN, "7:00"), automatic], as_of=LATER)
  assert Flag.CHANGED not in flags(entry)


def test_entry_hours_charge_the_days_break_allowance_across_shifts() -> None:
  a = [_p(IN, "7:00"), _p(BREAK_START, "8:00"), _p(BREAK_END, "8:20"), _p(OUT, "11:00")]
  b = [_p(IN, "12:00"), _p(BREAK_START, "13:00"), _p(BREAK_END, "13:20"), _p(OUT, "16:00")]
  punches = [*a, *b]
  first, second = entries(punches, as_of=LATER)
  hours_a = entry_hours(first, punches, settings=SETTINGS, tz=TZ, now=LATER)
  hours_b = entry_hours(second, punches, settings=SETTINGS, tz=TZ, now=LATER)
  assert hours_a is not None and hours_b is not None
  assert (hours_a.worked, hours_a.paid_break, hours_a.unpaid) == (timedelta(hours=3, minutes=40), timedelta(minutes=20), timedelta())
  assert (hours_b.paid_break, hours_b.unpaid) == (timedelta(minutes=10), timedelta(minutes=10))
  schema_punches = [p.as_punch(0) for p in punches]
  assert hours_b == total_hours(schema_punches, settings=SETTINGS, tz=TZ, start=b[0].at, end=b[-1].at, now=LATER)


def test_a_stray_entry_has_no_hours() -> None:
  stray = _p(OUT, "5:00")
  [entry] = entries([stray], as_of=LATER)
  assert entry_hours(entry, [stray], settings=SETTINGS, tz=TZ, now=LATER) is None


def test_records_turn_a_draft_into_amends_voids_and_inserts() -> None:
  moved, retyped, both, removed, confirmed, untouched = (_p(OUT, f"{h}:00") for h in range(10, 16))
  base = {p.id: p for p in (moved, retyped, both, removed, confirmed, untouched)}
  added, added_then_removed = uuid4(), uuid4()
  draft = [
    _draft(moved, at=_at("17:05")),
    _draft(retyped, kind=BREAK_START),
    _draft(both, kind=BREAK_END, at=_at("12:30")),
    _draft(removed, removed=True),
    _draft(confirmed, confirmed=True),
    _draft(untouched),
    DraftPunch(added, IN, _at("6:00"), removed=False, confirmed=False, version=None, new=True),
    DraftPunch(added_then_removed, IN, _at("6:30"), removed=True, confirmed=False, version=None, new=True),
  ]
  assert records(base, draft) == [
    Record(RecordAction.AMEND, moved.id, None, _at("17:05")),
    Record(RecordAction.AMEND, retyped.id, BREAK_START, None),
    Record(RecordAction.AMEND, both.id, BREAK_END, _at("12:30")),
    Record(RecordAction.VOID, removed.id, None, None),
    Record(RecordAction.AMEND, confirmed.id, None, confirmed.at),
    Record(RecordAction.INSERT, added, IN, _at("6:00")),
  ]


def test_an_amend_names_every_field_that_differs_from_the_original() -> None:
  # The database applies a punch's newest correction alone, over the original (apply_corrections).
  fixed = _p(OUT, "17:05", original_at=_at("23:00"), version=uuid4())  # an automatic clock-out fixed to 5:05 PM
  retyped = _p(BREAK_START, "10:00", original_kind=OUT, version=uuid4())
  restored = _p(OUT, "17:05", original_at=_at("23:00"), version=uuid4())
  base = {p.id: p for p in (fixed, retyped, restored)}
  draft = [_draft(fixed, kind=BREAK_START), _draft(retyped, at=_at("10:05")), _draft(restored, at=_at("23:00"))]
  assert records(base, draft) == [
    Record(RecordAction.AMEND, fixed.id, BREAK_START, _at("17:05")),
    Record(RecordAction.AMEND, retyped.id, BREAK_START, _at("10:05")),
    Record(RecordAction.AMEND, restored.id, None, _at("23:00")),
  ]


def test_apply_reads_an_amend_over_the_original_as_the_database_does() -> None:
  fixed = _p(OUT, "17:05", original_at=_at("23:00"), version=uuid4())
  [after] = apply([fixed], [Record(RecordAction.AMEND, fixed.id, BREAK_START, None)])
  assert (after.kind, after.at) == (BREAK_START, _at("23:00"))


def test_conflicts_are_touched_punches_someone_else_changed() -> None:
  changed, gone, same = _p(IN, "7:00", version=uuid4()), _p(OUT, "15:00"), _p(BREAK_START, "9:00")
  base = {changed.id: changed, same.id: same}
  draft = [
    _draft(changed, version=None, at=_at("7:05")),
    _draft(gone, removed=True),
    _draft(same, at=_at("9:05")),
    DraftPunch(uuid4(), BREAK_END, _at("9:20"), removed=False, confirmed=False, version=None, new=True),
  ]
  assert conflicts(base, draft) == [changed.id, gone.id]


def test_apply_writes_the_records_into_the_punches() -> None:
  start, out = _p(IN, "7:00"), _p(OUT, "15:00")
  added = uuid4()
  after = apply(
    [start, out],
    [Record(RecordAction.AMEND, out.id, None, _at("16:00")), Record(RecordAction.INSERT, added, BREAK_START, _at("9:00"))],
  )
  by_id = {p.id: p for p in after}
  assert by_id[out.id].at == _at("16:00")
  assert by_id[out.id].version is not None
  assert by_id[added].added_on_page
  assert by_id[added].instance_id == "web"
  assert [p.id for p in apply([start, out], [Record(RecordAction.VOID, out.id, None, None)])] == [start.id]


def test_a_save_is_refused_while_an_edited_shift_holds_an_ignored_punch() -> None:
  first, extra, out = _p(IN, "7:01"), _p(IN, "7:04"), _p(OUT, "15:33")
  before = [first, extra, out]
  after = apply(before, [Record(RecordAction.AMEND, out.id, None, _at("15:30"))])
  found = refusal(before, after, {out.id}, as_of=LATER)
  assert found is not None
  assert (found.kind, found.punch_ids) == (RefusalKind.STILL_IGNORED, (extra.id,))
  resolved = apply(before, [Record(RecordAction.VOID, extra.id, None, None)])
  assert refusal(before, resolved, {extra.id}, as_of=LATER) is None


def test_a_save_is_refused_when_it_would_ignore_another_punch() -> None:
  monday = [_p(IN, "7:00", day=2), _p(OUT, "15:00", day=2)]
  tuesday = [_p(IN, "7:00", day=3), _p(OUT, "15:00", day=3)]
  before = [*monday, *tuesday]
  after = apply(before, [Record(RecordAction.VOID, monday[1].id, None, None)])
  found = refusal(before, after, {monday[1].id}, as_of=LATER)
  assert found is not None
  assert (found.kind, found.punch_ids) == (RefusalKind.NEWLY_IGNORED, (tuesday[0].id,))


def test_an_ignored_punch_in_an_untouched_shift_does_not_block() -> None:
  monday = [_p(IN, "7:00", day=2), _p(IN, "7:02", day=2), _p(OUT, "15:00", day=2)]
  tuesday = [_p(IN, "7:00", day=3), _p(OUT, "15:00", day=3)]
  before = [*monday, *tuesday]
  after = apply(before, [Record(RecordAction.AMEND, tuesday[1].id, None, _at("15:30"))])
  assert refusal(before, after, {tuesday[1].id}, as_of=LATER) is None


def test_a_new_shift_overlapping_one_returns_that_shift() -> None:
  existing = [_p(IN, "7:55", day=4), _p(OUT, "12:10", day=4)]
  found = overlapping(existing, _at("8:00", day=4), _at("16:30", day=4), as_of=LATER)
  assert found is not None
  assert found.key == existing[0].id
  assert overlapping(existing, _at("13:00", day=4), _at("16:30", day=4), as_of=LATER) is None


def test_ids_order_punches_at_the_same_instant() -> None:
  low, high = UUID(int=1), UUID(int=2)
  start = _p(IN, "7:00", id=high)
  out = _p(OUT, "7:00", id=low)
  # The clock-out sorts first, so it's a stray, and the clock-in opens a shift.
  assert [(e.kind, e.key) for e in entries([start, out], as_of=LATER)] == [(EntryKind.STRAY, low), (EntryKind.SHIFT, high)]
