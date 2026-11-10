"""Tests for the report framework: zero rows by grain, display rules and the all-strings workbook."""

# Standard library imports
from datetime import UTC, date, datetime, timedelta
from itertools import count
from typing import TYPE_CHECKING
from uuid import uuid4
from zoneinfo import ZoneInfo

# Third party imports
import openpyxl
import pytest

# First party imports
from timeclock_backend.bootstrap import bootstrap
from timeclock_backend.databases import Database, connect
from timeclock_backend.reports import Column, Grain, Kind, Period, ScopeEmployee, Tab, fill_tab, write_workbook

if TYPE_CHECKING:
  # Standard library imports
  from pathlib import Path

NY = ZoneInfo("America/New_York")
WEEK = Period(date(2026, 10, 26), date(2026, 11, 1))  # Monday to Sunday; the Sunday has 25 hours
_pins = count(2000)

# Each query follows the Tab contract; the real payroll queries read views, these read the punches table directly.
SHIFTS = Tab(
  name="Shifts",
  grain=Grain.PER_EMPLOYEE_DAY,
  columns=(
    Column("Employee Name", "employee_name", Kind.NAME),
    Column("Day", "day", Kind.DAY),
    Column("Clock In", "clock_in", Kind.TIMESTAMP),
    Column("Corrected", "corrected", Kind.YES_NO),
    Column("Hours", "hours", Kind.DURATION),
    Column("Break Count", "break_count", Kind.COUNT),
    Column("Note", "note", Kind.TEXT),
    Column("Kind", "label", Kind.LABEL, {"x": "Ex"}),
  ),
  query="""
    SELECT e.id AS employee_id, e.first_name || ' ' || e.last_name AS employee_name,
           (p.at AT TIME ZONE 'America/New_York')::date AS day, p.at AS clock_in, false AS corrected,
           '8:00' AS hours, 0 AS break_count, NULL AS note, 'x' AS label
    FROM punches p JOIN employees e ON e.id = p.employee_id
    WHERE p.employee_id = ANY(%(employee_ids)s) AND p.kind = 'clock_in' AND p.at >= %(start_at)s AND p.at < %(end_at)s
  """,
  order_by=("employee_name", "day", "clock_in"),
)
PER_EMPLOYEE = Tab(
  name="Weekly",
  grain=Grain.PER_EMPLOYEE,
  columns=(
    Column("Employee Name", "employee_name", Kind.NAME),
    Column("Hours", "hours", Kind.DURATION),
    Column("Breaks", "breaks", Kind.DURATION),
  ),
  query="""
    SELECT e.id AS employee_id, e.first_name || ' ' || e.last_name AS employee_name, '8:00' AS hours, '0:30' AS breaks
    FROM employees e WHERE e.id = ANY(%(employee_ids)s) AND EXISTS (
      SELECT FROM punches p WHERE p.employee_id = e.id AND p.at >= %(start_at)s AND p.at < %(end_at)s)
  """,
  order_by=("employee_name",),
)
PER_EVENT = Tab(
  name="Punches",
  grain=Grain.PER_EVENT,
  columns=(Column("Employee Name", "employee_name", Kind.NAME), Column("Time", "at", Kind.TIMESTAMP)),
  query="""
    SELECT e.id AS employee_id, e.first_name || ' ' || e.last_name AS employee_name, p.at
    FROM punches p JOIN employees e ON e.id = p.employee_id
    WHERE p.employee_id = ANY(%(employee_ids)s) AND p.at >= %(start_at)s AND p.at < %(end_at)s
  """,
  order_by=("at",),
)


@pytest.fixture
async def migrated(fresh_database: Database) -> Database:
  await bootstrap([fresh_database])
  return fresh_database


async def add_employee(db: Database, first: str) -> int:
  async with await connect(db.name) as conn:
    row = await (
      await conn.execute(
        "INSERT INTO employees (first_name, last_name, pin_username, pin_hash) VALUES (%s, 'Lee', %s, crypt('1234', gen_salt('bf'))) RETURNING id",
        (first, str(next(_pins))),
      )
    ).fetchone()
  assert row is not None
  return row["id"]


async def clock_in(db: Database, employee_id: int, at: datetime) -> None:
  async with await connect(db.name) as conn:
    await conn.execute(
      "INSERT INTO punches (id, employee_id, kind, at, instance_id) VALUES (%s, %s, 'clock_in', %s, 'test')",
      (uuid4(), employee_id, at),
    )


def local(day: date, hour: int) -> datetime:
  return datetime(day.year, day.month, day.day, hour, tzinfo=NY).astimezone(UTC)


async def test_per_employee_day_fills_every_day_with_zeroes(migrated: Database) -> None:
  ann = await add_employee(migrated, "Ann")
  await clock_in(migrated, ann, local(date(2026, 10, 26), 9))
  async with await connect(migrated.name) as conn:
    rows = await fill_tab(conn, SHIFTS, WEEK, {ann: ScopeEmployee("Ann Lee", active=True)})
  assert rows[0] == ["Employee Name", "Day", "Clock In", "Corrected", "Hours", "Break Count", "Note", "Kind"]
  assert len(rows) == 1 + len(WEEK.days())
  assert rows[1] == ["Ann Lee", "2026-10-26", "2026-10-26 09:00:00", "No", "8:00", "0", "", "Ex"]
  assert rows[2] == ["Ann Lee", "2026-10-27", "", "No", "0:00", "0", "", ""]
  assert [r[1] for r in rows[1:]] == [d.isoformat() for d in WEEK.days()]


async def test_per_employee_fills_absent_employees(migrated: Database) -> None:
  ann = await add_employee(migrated, "Ann")
  bob = await add_employee(migrated, "Bob")
  await clock_in(migrated, ann, local(date(2026, 10, 27), 9))
  scope = {ann: ScopeEmployee("Ann Lee", active=True), bob: ScopeEmployee("Bob Lee", active=True)}
  async with await connect(migrated.name) as conn:
    rows = await fill_tab(conn, PER_EMPLOYEE, WEEK, scope)
  assert rows == [["Employee Name", "Hours", "Breaks"], ["Ann Lee", "8:00", "0:30"], ["Bob Lee", "0:00", "0:00"]]


async def test_per_event_adds_no_rows(migrated: Database) -> None:
  ann = await add_employee(migrated, "Ann")
  async with await connect(migrated.name) as conn:
    rows = await fill_tab(conn, PER_EVENT, WEEK, {ann: ScopeEmployee("Ann Lee", active=True)})
  assert rows == [["Employee Name", "Time"]]


async def test_a_day_with_two_shifts_keeps_two_rows(migrated: Database) -> None:
  ann = await add_employee(migrated, "Ann")
  await clock_in(migrated, ann, local(date(2026, 10, 28), 6))
  await clock_in(migrated, ann, local(date(2026, 10, 28), 18))
  async with await connect(migrated.name) as conn:
    rows = await fill_tab(conn, SHIFTS, WEEK, {ann: ScopeEmployee("Ann Lee", active=True)})
  assert len(rows) == 1 + len(WEEK.days()) + 1
  assert [r[2] for r in rows if r[1] == "2026-10-28"] == ["2026-10-28 06:00:00", "2026-10-28 18:00:00"]


async def test_inactive_employee_with_rows_gets_zero_rows_too(migrated: Database) -> None:
  ann = await add_employee(migrated, "Ann")
  bob = await add_employee(migrated, "Bob")
  await clock_in(migrated, bob, local(date(2026, 10, 29), 9))
  scope = {ann: ScopeEmployee("Ann Lee", active=True), bob: ScopeEmployee("Bob Lee", active=False)}
  async with await connect(migrated.name) as conn:
    rows = await fill_tab(conn, SHIFTS, WEEK, scope)
  assert len([r for r in rows if r[0] == "Bob Lee"]) == len(WEEK.days())  # his one real row, zero rows for the other days
  assert len([r for r in rows if r[0] == "Ann Lee"]) == len(WEEK.days())


async def test_an_inactive_employee_without_rows_gets_none(migrated: Database) -> None:
  bob = await add_employee(migrated, "Bob")
  async with await connect(migrated.name) as conn:
    rows = await fill_tab(conn, SHIFTS, WEEK, {bob: ScopeEmployee("Bob Lee", active=False)})
  assert len(rows) == 1


def test_formula_and_number_like_text_stay_strings(tmp_path: Path) -> None:
  path = tmp_path / "r.xlsx"
  write_workbook(path, "T", [("Tab", [["Name"], ["=SUM(A1)"], ["007"], ["1:30"]])])
  sheet = openpyxl.load_workbook(path)["Tab"]
  for row, value in ((2, "=SUM(A1)"), (3, "007"), (4, "1:30")):
    cell = sheet.cell(row, 1)
    assert (cell.data_type, cell.value, cell.number_format) == ("s", value, "@")


def test_workbook_title_tabs_and_frozen_header(tmp_path: Path) -> None:
  path = tmp_path / "r.xlsx"
  write_workbook(path, "Group Report", [("One", [["A", "B"], ["x", ""]]), ("Two", [["C"]])])
  book = openpyxl.load_workbook(path)
  assert book.properties.title == "Group Report"
  assert book.sheetnames == ["One", "Two"]
  assert book["One"].freeze_panes == "A2"
  assert book["One"]["A1"].font.bold
  assert not book["One"]["A2"].font.bold
  assert book["One"].column_dimensions["A"].width > len("A")


def test_period_bounds_are_local_midnights() -> None:
  start, end = WEEK.bounds(NY)
  assert end - start == timedelta(days=7, hours=1)  # the fall-back Sunday has 25 hours
  assert start.astimezone(NY).hour == 0
  assert end.astimezone(NY).hour == 0


async def test_a_per_employee_query_returning_day_gets_no_spurious_zero_rows(migrated: Database) -> None:
  ann = await add_employee(migrated, "Ann")
  await clock_in(migrated, ann, local(date(2026, 10, 27), 9))
  tab = Tab(
    name=PER_EMPLOYEE.name,
    grain=Grain.PER_EMPLOYEE,
    columns=PER_EMPLOYEE.columns,
    query=PER_EMPLOYEE.query.replace("'8:00' AS hours", "'8:00' AS hours, current_date AS day"),
    order_by=PER_EMPLOYEE.order_by,
  )
  async with await connect(migrated.name) as conn:
    rows = await fill_tab(conn, tab, WEEK, {ann: ScopeEmployee("Ann Lee", active=True)})
  assert rows == [["Employee Name", "Hours", "Breaks"], ["Ann Lee", "8:00", "0:30"]]
