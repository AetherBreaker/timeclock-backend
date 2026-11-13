"""Tests for the shift list (spec 2026-11-05 §7, mockup screens B and I), over real databases."""

# Standard library imports
from datetime import UTC, date, datetime, time
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

# Third party imports
from nicegui import ui
from page_sim import sign_in

# First party imports
from timeclock_backend.web import text

if TYPE_CHECKING:
  # Standard library imports
  from uuid import UUID

  # Third party imports
  from page_sim import DbPage, PageUnderTest

NY = ZoneInfo("America/New_York")


def _ny(day: int, clock: str, month: int = 11) -> datetime:
  return datetime.combine(date(2026, month, day), time.fromisoformat(clock), tzinfo=NY).astimezone(UTC)


def _shift(page: DbPage, employee: int, day: int, start: str, end: str | None, *, month: int = 11) -> UUID:
  first = page.db.punch(employee, "clock_in", _ny(day, start, month))
  if end is not None:
    page.db.punch(employee, "clock_out", _ny(day, end, month))
  return first


def _cards(page: DbPage) -> list[str]:
  """The employee cards' markers, top to bottom."""
  cards = sorted(page.user.find(marker="employee-card").elements, key=lambda e: e.id)
  markers = [card._markers for card in cards]  # pyright: ignore[reportPrivateUsage]
  return [next(m for m in found if m.startswith("employee-") and m != "employee-card") for found in markers]


def _set(page: DbPage, marker: str, value: object) -> None:
  assert page.user.client is not None
  with page.user.client:
    [select] = page.user.find(marker=marker).elements
    assert isinstance(select, ui.select)
    select.set_value(value)


async def test_the_list_opens_on_this_week_sorted_by_first_name_with_week_totals(db_page: DbPage) -> None:
  bob = db_page.db.employees[1]
  cy = db_page.db.employee("Cy", "Adams", "56")
  _shift(db_page, cy, 2, "07:00", "15:30")
  db_page.db.punch(cy, "break_start", _ny(2, "10:00"))
  db_page.db.punch(cy, "break_end", _ny(2, "10:15"))
  _shift(db_page, bob, 3, "08:00", "16:00")
  await db_page.user.open("/")
  await db_page.user.should_see("Nov 2 to 8, 2026")
  assert _cards(db_page) == [f"employee-{bob}", f"employee-{cy}"]
  await db_page.user.should_see("Cy Adams")
  await db_page.user.should_see("ID 56")
  await db_page.user.should_see(marker=f"week-paid-{cy}", content="8:30")
  await db_page.user.should_see(marker=f"week-worked-{cy}", content="8:15")
  await db_page.user.should_see(marker=f"week-paid-{bob}", content="8:00")
  await db_page.user.should_see(marker=f"week-worked-{bob}", content="8:00")
  await db_page.user.should_see("Mon, Nov 2")
  await db_page.user.should_see("1 · 0:15")
  await db_page.user.should_not_see("unpaid")  # Cy's break is all paid


async def test_a_break_past_the_limit_shows_its_unpaid_part_and_the_paid_hours(db_page: DbPage) -> None:
  # The default 15-minute limit leaves 5 of the break's 20 minutes unpaid; paid hours are worked plus paid breaks.
  bob = db_page.db.employees[1]
  first = _shift(db_page, bob, 3, "08:00", "16:00")
  for kind, clock in (("break_start", "10:00"), ("break_end", "10:20"), ("lunch_start", "12:00"), ("lunch_end", "12:30")):
    db_page.db.punch(bob, kind, _ny(3, clock))
  await db_page.user.open("/")
  await db_page.user.should_see("1 · 0:20")
  await db_page.user.should_see(marker=f"unpaid-{first}", content="(0:05 unpaid)")
  await db_page.user.should_see(marker=f"paid-{first}", content="7:25")
  await db_page.user.should_see(marker=f"week-paid-{bob}", content="7:25")
  await db_page.user.should_see(marker=f"week-worked-{bob}", content="7:10")


async def test_employees_with_no_shift_that_week_are_left_out(db_page: DbPage) -> None:
  bob = db_page.db.employees[1]
  cy = db_page.db.employee("Cy", "Adams", "56")
  _shift(db_page, bob, 27, "08:00", "16:00", month=10)
  _shift(db_page, cy, 3, "08:00", "16:00")
  await db_page.user.open("/")
  await db_page.user.should_see("Cy Adams")
  await db_page.user.should_not_see("Bob Lee")


async def test_an_open_shift_shows_working_and_hours_so_far(db_page: DbPage) -> None:
  _shift(db_page, db_page.db.employees[1], 5, "07:00", None)
  await db_page.user.open("/")
  await db_page.user.should_see("2:00 so far")
  await db_page.user.should_see("working")
  await db_page.user.should_see("Working now")


async def test_a_stray_punch_gets_its_own_row(db_page: DbPage) -> None:
  bob = db_page.db.employees[1]
  _shift(db_page, bob, 4, "07:00", "15:30")
  stray = db_page.db.punch(bob, "clock_out", _ny(4, "17:02"))
  await db_page.user.open("/")
  await db_page.user.should_see(marker=f"row-{stray}")
  await db_page.user.should_see("Clock out 5:02 PM")
  await db_page.user.should_see("Stray punch")


async def test_the_attention_bar_counts_and_filters(db_page: DbPage) -> None:
  bob = db_page.db.employees[1]
  monday = _shift(db_page, bob, 2, "07:00", "15:00")
  tuesday = _shift(db_page, bob, 3, "07:01", None)
  db_page.db.punch(bob, "clock_in", _ny(3, "07:04"))
  db_page.db.punch(bob, "clock_out", _ny(3, "15:33"))
  wednesday = _shift(db_page, bob, 4, "07:58", None)
  db_page.db.punch(bob, "clock_out", _ny(4, "23:00"), automatic=True)
  await db_page.user.open("/")
  await db_page.user.should_see("2 shifts need attention this week:")
  await db_page.user.should_see("1 automatic clock-out")
  await db_page.user.should_see("1 extra clock-in")
  db_page.user.find(marker="attention-extra:clock_in").click()
  await db_page.user.should_not_see(marker=f"row-{monday}")
  await db_page.user.should_not_see(marker=f"row-{wednesday}")
  await db_page.user.should_see(marker=f"row-{tuesday}")
  db_page.user.find(marker="attention-only").click()
  await db_page.user.should_see(marker=f"row-{wednesday}")
  await db_page.user.should_see(marker=f"row-{tuesday}")
  await db_page.user.should_not_see(marker=f"row-{monday}")


async def test_filters_combine(db_page: DbPage) -> None:
  warehouse = db_page.db.group("Warehouse")
  bob = db_page.db.employees[1]
  cy = db_page.db.employee("Cy", "Adams", "56", group_id=warehouse, role="manager")
  dee = db_page.db.employee("Dee", "Moss", "78", group_id=warehouse)
  for employee in (bob, cy, dee):
    _shift(db_page, employee, 3, "08:00", "16:00")
  await db_page.user.open("/")
  await db_page.user.should_see("Bob Lee")
  _set(db_page, "filter-group", warehouse)
  await db_page.user.should_not_see("Bob Lee")
  await db_page.user.should_see("Dee Moss")
  _set(db_page, "filter-role", "manager")
  await db_page.user.should_not_see("Dee Moss")
  assert _cards(db_page) == [f"employee-{cy}"]
  _set(db_page, "filter-role", None)
  _set(db_page, "filter-employee", dee)
  await db_page.user.should_not_see("Cy Adams")  # a refresh lands after the event
  assert _cards(db_page) == [f"employee-{dee}"]


async def test_a_group_narrows_the_employee_filter_to_its_people(db_page: DbPage) -> None:
  warehouse = db_page.db.group("Warehouse")
  ann, bob = db_page.db.employees
  dee = db_page.db.employee("Dee", "Moss", "78", group_id=warehouse)
  cy = db_page.db.employee("Cy", "Adams", "56", group_id=warehouse)
  _shift(db_page, bob, 3, "08:00", "16:00")
  await db_page.user.open("/")
  await db_page.user.should_see("Bob Lee")
  [select] = db_page.user.find(marker="filter-employee").elements
  assert isinstance(select, ui.select)
  assert list(select.options) == [None, ann, bob, cy, dee]  # by first name: Ann, Bob, Cy, Dee
  _set(db_page, "filter-group", warehouse)
  assert list(select.options) == [None, cy, dee]
  _set(db_page, "filter-group", None)
  assert list(select.options) == [None, ann, bob, cy, dee]


async def test_a_group_drops_a_chosen_employee_outside_it(db_page: DbPage) -> None:
  warehouse = db_page.db.group("Warehouse")
  bob = db_page.db.employees[1]
  dee = db_page.db.employee("Dee", "Moss", "78", group_id=warehouse)
  for employee in (bob, dee):
    _shift(db_page, employee, 3, "08:00", "16:00")
  await db_page.user.open("/")
  await db_page.user.should_see("Dee Moss")
  _set(db_page, "filter-employee", bob)
  await db_page.user.should_not_see("Dee Moss")
  _set(db_page, "filter-group", warehouse)
  await db_page.user.should_see("Dee Moss")
  [select] = db_page.user.find(marker="filter-employee").elements
  assert isinstance(select, ui.select)
  assert select.value is None
  assert _cards(db_page) == [f"employee-{dee}"]
  # The same from the address: an employee outside the group there is dropped, not an empty list.
  await db_page.user.open(f"/?group={warehouse}&employee={bob}")
  await db_page.user.should_see(marker=f"employee-{dee}")
  assert _cards(db_page) == [f"employee-{dee}"]


async def test_a_role_narrows_the_employee_filter_with_the_group(db_page: DbPage) -> None:
  warehouse = db_page.db.group("Warehouse")
  cy = db_page.db.employee("Cy", "Adams", "56", group_id=warehouse, role="manager")
  dee = db_page.db.employee("Dee", "Moss", "78", group_id=warehouse)
  eve = db_page.db.employee("Eve", "Hart", "90", role="manager")
  for employee in (cy, dee, eve):
    _shift(db_page, employee, 3, "08:00", "16:00")
  await db_page.user.open("/")
  await db_page.user.should_see("Dee Moss")
  [select] = db_page.user.find(marker="filter-employee").elements
  assert isinstance(select, ui.select)
  _set(db_page, "filter-employee", dee)
  await db_page.user.should_not_see("Cy Adams")
  _set(db_page, "filter-role", "manager")  # Dee isn't one: dropped
  await db_page.user.should_see("Cy Adams")
  assert select.value is None
  assert list(select.options) == [None, cy, eve]
  assert _cards(db_page) == [f"employee-{cy}", f"employee-{eve}"]
  _set(db_page, "filter-group", warehouse)
  assert list(select.options) == [None, cy]
  _set(db_page, "filter-role", None)
  assert list(select.options) == [None, cy, dee]
  await db_page.user.open(f"/?role=manager&employee={dee}")
  await db_page.user.should_see(marker=f"employee-{eve}")
  assert _cards(db_page) == [f"employee-{cy}", f"employee-{eve}"]


async def test_your_own_shifts_are_locked(db_page: DbPage) -> None:
  ann = db_page.db.employees[0]
  _shift(db_page, ann, 2, "09:00", "17:30")
  await db_page.user.open("/")
  await db_page.user.should_see("You: another editor changes these")
  await db_page.user.should_see(marker=f"own-{ann}")


async def test_the_week_arrows_move_a_week(db_page: DbPage) -> None:
  _shift(db_page, db_page.db.employees[1], 27, "08:00", "16:00", month=10)
  await db_page.user.open("/")
  await db_page.user.should_not_see("Bob Lee")
  db_page.user.find(marker="week-back").click()
  await db_page.user.should_see("Oct 26 to Nov 1, 2026")
  await db_page.user.should_see("Bob Lee")
  db_page.user.find(marker="this-week").click()
  await db_page.user.should_see("Nov 2 to 8, 2026")
  db_page.user.find(marker="week-next").click()
  await db_page.user.should_see("Nov 9 to 15, 2026")


async def test_an_overnight_shift_shows_in_both_weeks(db_page: DbPage) -> None:
  bob = db_page.db.employees[1]
  start = db_page.db.punch(bob, "clock_in", _ny(8, "22:00"))
  db_page.db.punch(bob, "clock_out", _ny(9, "06:00"))
  db_page.clock.now = _ny(12, "12:00")
  await sign_in(db_page, db_page.db.email)  # a week on, the fixture's sign-in has expired
  await db_page.user.open("/?week=2026-11-04")
  await db_page.user.should_see(marker=f"row-{start}")
  await db_page.user.should_see(marker=f"week-worked-{bob}", content="2:00")
  db_page.user.find(marker="week-next").click()
  await db_page.user.should_see("Nov 9 to 15, 2026")
  await db_page.user.should_see(marker=f"row-{start}")
  await db_page.user.should_see(marker=f"week-worked-{bob}", content="6:00")


async def test_technical_details_add_the_clock_and_punch_columns(db_page: DbPage) -> None:
  bob = db_page.db.employees[1]
  _shift(db_page, bob, 3, "07:01", None)
  db_page.db.punch(bob, "clock_in", _ny(3, "07:04"))
  db_page.db.punch(bob, "clock_out", _ny(3, "15:33"))
  await db_page.user.open("/")
  await db_page.user.should_not_see("Punches")
  db_page.user.find(marker="pref-technical").click()
  await db_page.user.should_see("Technical details on")
  await db_page.user.should_see("Punches")
  await db_page.user.should_see("3 (1 ignored)")
  await db_page.user.should_see(f"employee id {bob} · username 34 · group default (1) · role employee", retries=5)


async def test_24_hour_time(db_page: DbPage) -> None:
  _shift(db_page, db_page.db.employees[1], 3, "07:01", "15:33")
  await db_page.user.open("/")
  await db_page.user.should_see("3:33 PM")
  db_page.user.find(marker="pref-h24").click()
  await db_page.user.should_see("15:33")
  await db_page.user.should_see("07:01")


async def test_a_change_in_the_database_refreshes_the_open_list(db_page: DbPage) -> None:
  bob = db_page.db.employees[1]
  _shift(db_page, bob, 2, "08:00", "16:00")
  await db_page.user.open("/")
  await db_page.user.should_see("Mon, Nov 2")
  tuesday = _shift(db_page, bob, 3, "08:00", "16:00")
  await db_page.user.should_see(marker=f"row-{tuesday}", retries=50)


async def test_the_list_says_when_the_database_is_down(page: PageUnderTest) -> None:
  await sign_in(page)
  await page.user.should_see(text.DB_DOWN)


async def test_filters_in_the_address_open_filtered(db_page: DbPage) -> None:
  # Back to a filtered list, a reload or a bookmark; one that's gone since shows everyone.
  warehouse = db_page.db.group("Warehouse")
  bob = db_page.db.employees[1]
  dee = db_page.db.employee("Dee", "Moss", "78", group_id=warehouse)
  for employee in (bob, dee):
    _shift(db_page, employee, 3, "08:00", "16:00")
  await db_page.user.open(f"/?group={warehouse}")
  await db_page.user.should_see(marker=f"employee-{dee}")
  assert _cards(db_page) == [f"employee-{dee}"]
  await db_page.user.open(f"/?employee={bob}")
  await db_page.user.should_see(marker=f"employee-{bob}")
  assert _cards(db_page) == [f"employee-{bob}"]
  await db_page.user.open("/?group=999999&employee=999999")
  await db_page.user.should_see(marker=f"employee-{dee}")
  assert _cards(db_page) == [f"employee-{bob}", f"employee-{dee}"]


def _breaks_off(page: DbPage) -> None:
  with page.db.admin() as c:
    c.execute("UPDATE config.settings SET breaks_enabled = false")


async def test_breaks_off_hides_the_breaks_and_worked_columns_of_a_week_without_breaks(db_page: DbPage) -> None:
  bob = db_page.db.employees[1]
  first = _shift(db_page, bob, 3, "08:00", "16:00")
  _breaks_off(db_page)
  await db_page.user.open("/")
  await db_page.user.should_see(marker=f"week-paid-{bob}", content="8:00")
  await db_page.user.should_see(marker=f"paid-{first}", content="8:00")
  for marker in (f"head-breaks-{bob}", f"head-worked-{bob}", f"week-worked-{bob}"):
    await db_page.user.should_not_see(marker=marker)


async def test_breaks_off_keeps_the_columns_when_a_shift_has_a_break(db_page: DbPage) -> None:
  bob = db_page.db.employees[1]
  _shift(db_page, bob, 2, "08:00", "16:00")
  _shift(db_page, bob, 3, "08:00", "16:00")
  db_page.db.punch(bob, "break_start", _ny(3, "10:00"))
  db_page.db.punch(bob, "break_end", _ny(3, "10:15"))
  _breaks_off(db_page)
  await db_page.user.open("/")
  for marker in (f"head-breaks-{bob}", f"head-worked-{bob}", f"week-worked-{bob}"):
    await db_page.user.should_see(marker=marker)


async def test_breaks_off_keeps_the_columns_for_a_stray_break_punch(db_page: DbPage) -> None:
  bob = db_page.db.employees[1]
  _shift(db_page, bob, 4, "07:00", "15:30")
  stray = db_page.db.punch(bob, "break_end", _ny(4, "17:02"))
  _breaks_off(db_page)
  await db_page.user.open("/")
  await db_page.user.should_see(marker=f"row-{stray}")
  await db_page.user.should_see(marker=f"head-breaks-{bob}")
  await db_page.user.should_see(marker=f"head-worked-{bob}")


async def test_breaks_on_keeps_the_columns(db_page: DbPage) -> None:
  bob = db_page.db.employees[1]
  _shift(db_page, bob, 3, "08:00", "16:00")
  await db_page.user.open("/")
  for marker in (f"head-breaks-{bob}", f"head-worked-{bob}", f"week-worked-{bob}"):
    await db_page.user.should_see(marker=marker)


def _three_breaks(page: DbPage, employee: int) -> tuple[UUID, list[UUID]]:
  """Tuesday 8 to 4 with breaks at 9, 11 and 2, all written as a clock writes them (the owner's login counts as by hand)."""
  clock = "timeclock_clock_front"  # the owner's login counts as by hand, which would read as reviewed
  first = page.db.punch(employee, "clock_in", _ny(3, "08:00"), db_login=clock)
  starts = []
  for start, end in (("09:00", "09:10"), ("11:00", "11:10"), ("14:00", "14:10")):
    starts.append(page.db.punch(employee, "break_start", _ny(3, start), db_login=clock))
    page.db.punch(employee, "break_end", _ny(3, end), db_login=clock)
  page.db.punch(employee, "clock_out", _ny(3, "16:00"), db_login=clock)
  return first, starts


async def test_a_day_past_its_expected_breaks_needs_attention_until_marked_correct(db_page: DbPage) -> None:
  first, starts = _three_breaks(db_page, db_page.db.employees[1])
  await db_page.user.open("/")
  await db_page.user.should_see(marker=f"row-{first}")
  await db_page.user.should_see("3 breaks")
  await db_page.user.should_see(marker="attention-breaks", content="1 shift with too many breaks")
  db_page.user.find(marker="attention-breaks").click()
  await db_page.user.should_see(marker=f"row-{first}")
  # Marked as correct: an amend to the third break's own time.
  db_page.db.correct(starts[2], "amend", new_at=_ny(3, "14:00"))
  await db_page.user.open("/")
  await db_page.user.should_see("3 breaks, reviewed")
  await db_page.user.should_not_see(marker="attention-breaks")
  await db_page.user.should_not_see("Changed")  # the review says so alone


async def test_breaks_within_the_setting_need_nothing(db_page: DbPage) -> None:
  first, _ = _three_breaks(db_page, db_page.db.employees[1])
  for limit in (3, 0):  # 0: no limit
    with db_page.db.admin() as c:
      c.execute("UPDATE config.settings SET max_breaks_per_day = %s", (limit,))
    await db_page.user.open("/")
    await db_page.user.should_see(marker=f"row-{first}")
    await db_page.user.should_not_see("3 breaks")
    await db_page.user.should_not_see(marker="attention-breaks")
