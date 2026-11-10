"""The payroll report's tabs (spec §8): declarations only, over the `punch_log`, `shift_summary` and `weekly_hours` views."""

# Standard library imports
from typing import Final

# First party imports
from timeclock_backend.reports.framework import Column, Grain, Kind, Tab

_PUNCH_TYPES: Final = {
  "clock_in": "Clocked In",
  "clock_out": "Clocked Out",
  "break_start": "Break Started",
  "break_end": "Break Ended",
  "lunch_start": "Lunch Started",
  "lunch_end": "Lunch Ended",
}

PAYROLL_TABS: Final = (
  Tab(
    name="Punch Log",
    grain=Grain.PER_EVENT,
    columns=(
      Column("Employee Name", "employee_name", Kind.NAME),
      Column("Punch Type", "kind", Kind.LABEL, _PUNCH_TYPES),
      Column("Time", "at", Kind.TIMESTAMP),
      Column("Correction Note", "correction_note", Kind.TEXT),
      Column("Corrected At", "corrected_at", Kind.TIMESTAMP),
    ),
    query="""
      SELECT employee_id, employee_name, kind, at, correction_note, corrected_at, id
      FROM punch_log
      WHERE at >= %(start_at)s AND at < %(end_at)s AND employee_id = ANY(%(employee_ids)s)
    """,
    order_by=("at", "id"),
  ),
  Tab(
    name="Shift Summary",
    grain=Grain.PER_EMPLOYEE_DAY,
    columns=(
      Column("Employee Name", "employee_name", Kind.NAME),
      Column("Day", "day", Kind.DAY),
      Column("Clock In", "clock_in", Kind.TIMESTAMP),
      Column("Clock Out", "clock_out", Kind.TIMESTAMP),
      Column("Corrected", "corrected", Kind.YES_NO),
      Column("Shift Length", "shift_length", Kind.DURATION),
      Column("Hours", "hours", Kind.DURATION),
      Column("Breaks", "breaks", Kind.DURATION),
      Column("Unpaid Breaks", "unpaid_breaks", Kind.DURATION),
      Column("Lunches", "lunches", Kind.DURATION),
      Column("Break Count", "break_count", Kind.COUNT),
      Column("Lunch Count", "lunch_count", Kind.COUNT),
      Column("Breaks Over Limit", "breaks_over_limit", Kind.COUNT),
      Column("Break Overage", "break_overage", Kind.DURATION),
      Column("Break Times (Minutes Over Limit)", "break_times", Kind.TEXT),
      Column("Lunch Times", "lunch_times", Kind.TEXT),
    ),
    query="""
      SELECT employee_id, employee_name, day, clock_in, clock_out, corrected, shift_length, hours, breaks, unpaid_breaks, lunches,
             break_count, lunch_count, breaks_over_limit, break_overage, break_times, lunch_times
      FROM shift_summary
      WHERE day BETWEEN %(start)s AND %(end)s AND employee_id = ANY(%(employee_ids)s)
    """,
    order_by=("day", "employee_name", "clock_in"),
  ),
  Tab(
    name="Weekly Hours",
    grain=Grain.PER_EMPLOYEE,
    columns=(
      Column("Employee Name", "employee_name", Kind.NAME),
      Column("Hours", "hours", Kind.DURATION),
      Column("Breaks", "breaks", Kind.DURATION),
      Column("Lunches", "lunches", Kind.DURATION),
      Column("Unpaid Breaks", "unpaid_breaks", Kind.DURATION),
    ),
    query="""
      SELECT employee_id, employee_name, hours, breaks, lunches, unpaid_breaks
      FROM weekly_hours
      WHERE week_of = %(start)s AND employee_id = ANY(%(employee_ids)s)
    """,
    order_by=("employee_name",),
  ),
)
