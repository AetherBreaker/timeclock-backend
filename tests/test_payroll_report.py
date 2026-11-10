"""Tests for the payroll report: its week, its workbooks and the email that carries them."""

# Standard library imports
import io
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from itertools import count
from typing import TYPE_CHECKING, Any
from uuid import uuid4
from zoneinfo import ZoneInfo

# Third party imports
import openpyxl
import pytest

# First party imports
from timeclock_backend.bootstrap import bootstrap
from timeclock_backend.databases import Database, connect
from timeclock_backend.jobs import payroll_report
from timeclock_backend.jobs.payroll_report import PayrollReportJob
from timeclock_backend.reports import Period
from timeclock_backend.schedule import STATIC
from timeclock_backend.scheduling import Retry

if TYPE_CHECKING:
  # Standard library imports
  from email.message import EmailMessage
  from pathlib import Path

NY = ZoneInfo("America/New_York")
SCHEDULED = datetime(2026, 11, 9, 6, tzinfo=NY)  # the Monday after the week 2026-11-02 to 2026-11-08
RECIPIENTS = ("jacob.ogden@sweetfiretobacco.com",)
BOOK = "Office Timeclock Report 2026-11-02 to 2026-11-08.xlsx"
_pins = count(3000)

PUNCH_LOG_HEADER = ["Employee Name", "Punch Type", "Time", "Correction Note", "Corrected At"]
SHIFT_SUMMARY_HEADER = [
  "Employee Name",
  "Day",
  "Clock In",
  "Clock Out",
  "Corrected",
  "Shift Length",
  "Hours",
  "Breaks",
  "Unpaid Breaks",
  "Lunches",
  "Break Count",
  "Lunch Count",
  "Breaks Over Limit",
  "Break Overage",
  "Break Times (Minutes Over Limit)",
  "Lunch Times",
]
WEEKLY_HOURS_HEADER = ["Employee Name", "Hours", "Breaks", "Lunches", "Unpaid Breaks"]

type Sent = list[tuple[EmailMessage, dict[str, list[list[Any]]]]]


@pytest.fixture
async def migrated(fresh_database: Database) -> Database:
  await bootstrap([fresh_database])
  return fresh_database


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch) -> Sent:
  """Capture the message instead of sending it, with each attachment's tabs read back as `"<file>/<tab>"` to its rows."""
  captured: Sent = []

  def capture(msg: EmailMessage | Sequence[EmailMessage]) -> None:
    assert not isinstance(msg, Sequence)
    tabs: dict[str, list[list[Any]]] = {}
    for part in msg.iter_attachments():
      wb = openpyxl.load_workbook(io.BytesIO(part.get_content()))
      for ws in wb.worksheets:
        tabs[f"{part.get_filename()}/{ws.title}"] = [["" if v is None else v for v in row] for row in ws.iter_rows(values_only=True)]
    captured.append((msg, tabs))

  monkeypatch.setattr(payroll_report, "batch_send_emails", capture)
  return captured


async def add_employee(db: Database, first: str, group: str | None, *, active: bool = True) -> int:
  async with await connect(db.name) as conn:
    group_id = None
    if group is None:
      # employees.group_id is NOT NULL today; the job still handles a missing group (spec §7.2), so the test lifts the constraint.
      await conn.execute("ALTER TABLE employees ALTER COLUMN group_id DROP NOT NULL")
    else:
      await conn.execute("INSERT INTO groups (name) VALUES (%s) ON CONFLICT (name) DO NOTHING", (group,))
      row = await (await conn.execute("SELECT id FROM groups WHERE name = %s", (group,))).fetchone()
      assert row is not None
      group_id = row["id"]
    row = await (
      await conn.execute(
        "INSERT INTO employees (first_name, last_name, pin_username, pin_hash, group_id, active) "
        "VALUES (%s, 'Lee', %s, crypt('1234', gen_salt('bf')), %s, %s) RETURNING id",
        (first, str(next(_pins)), group_id, active),
      )
    ).fetchone()
  assert row is not None
  return row["id"]


async def punch(db: Database, employee_id: int, kind: str, at: datetime) -> None:
  async with await connect(db.name) as conn:
    await conn.execute(
      "INSERT INTO punches (id, employee_id, kind, at, instance_id) VALUES (%s, %s, %s, %s, 'test')",
      (uuid4(), employee_id, kind, at.astimezone(UTC)),
    )


def job(db: Database, groups: tuple[str, ...] | None = None) -> PayrollReportJob:
  return PayrollReportJob(db, RECIPIENTS, groups)


def test_week_is_the_last_full_monday_to_sunday() -> None:
  assert PayrollReportJob.week(datetime(2026, 11, 9, 6, tzinfo=NY)) == Period(date(2026, 11, 2), date(2026, 11, 8))
  assert PayrollReportJob.week(datetime(2026, 11, 8, 23, tzinfo=NY)) == Period(date(2026, 10, 26), date(2026, 11, 1))


def test_period_is_the_mondays_date() -> None:
  assert job(Database("x", "y", production=True)).period(SCHEDULED) == "2026-11-02"


async def test_week_window_spans_the_fall_back_sunday(migrated: Database, sent: Sent) -> None:
  ann = await add_employee(migrated, "Ann", "Office")
  await punch(migrated, ann, "clock_in", datetime(2026, 11, 1, 23, 30, tzinfo=NY))  # Sunday, 25 hours long
  await punch(migrated, ann, "clock_out", datetime(2026, 11, 2, 1, 0, tzinfo=NY))
  await job(migrated, ("Office",)).main(datetime(2026, 11, 2, 6, tzinfo=NY))
  [(_, tabs)] = sent
  log = tabs["Office Timeclock Report 2026-10-26 to 2026-11-01.xlsx/Punch Log"]
  assert [row[1:3] for row in log[1:]] == [["Clocked In", "2026-11-01 23:30:00"]]


async def test_one_email_with_one_workbook_per_group(migrated: Database, sent: Sent) -> None:
  ann = await add_employee(migrated, "Ann", "Office")
  bob = await add_employee(migrated, "Bob", "Warehouse")
  await punch(migrated, ann, "clock_in", datetime(2026, 11, 3, 9, tzinfo=NY))
  await punch(migrated, bob, "clock_in", datetime(2026, 11, 3, 10, tzinfo=NY))
  await job(migrated, ("Office", "Warehouse")).main(SCHEDULED)
  [(msg, tabs)] = sent
  assert msg["Subject"] == "Office, Warehouse Timeclock Report 2026-11-02 to 2026-11-08"
  assert msg["To"] == RECIPIENTS[0]
  body = msg.get_body()
  assert body is not None
  assert body.get_content().strip() == "Timeclock reports for 2026-11-02 to 2026-11-08, attached: Office, Warehouse."
  warehouse = "Warehouse Timeclock Report 2026-11-02 to 2026-11-08.xlsx"
  assert [p.get_filename() for p in msg.iter_attachments()] == [BOOK, warehouse]
  assert [r[0] for r in tabs[f"{BOOK}/Punch Log"][1:]] == ["Ann Lee"]
  assert [r[0] for r in tabs[f"{warehouse}/Punch Log"][1:]] == ["Bob Lee"]


async def test_workbook_tabs_and_headers_match_the_spec(migrated: Database, sent: Sent) -> None:
  ann = await add_employee(migrated, "Ann", "Office")
  await punch(migrated, ann, "clock_in", datetime(2026, 11, 3, 9, tzinfo=NY))
  await punch(migrated, ann, "clock_out", datetime(2026, 11, 3, 17, tzinfo=NY))
  await job(migrated, ("Office",)).main(SCHEDULED)
  [(_, tabs)] = sent
  assert list(tabs) == [f"{BOOK}/Punch Log", f"{BOOK}/Shift Summary", f"{BOOK}/Weekly Hours"]
  assert tabs[f"{BOOK}/Punch Log"][0] == PUNCH_LOG_HEADER
  assert tabs[f"{BOOK}/Shift Summary"][0] == SHIFT_SUMMARY_HEADER
  assert tabs[f"{BOOK}/Weekly Hours"][0] == WEEKLY_HOURS_HEADER
  assert tabs[f"{BOOK}/Punch Log"][1] == ["Ann Lee", "Clocked In", "2026-11-03 09:00:00", "", ""]
  assert tabs[f"{BOOK}/Weekly Hours"][1][:2] == ["Ann Lee", "8:00"]
  summary = tabs[f"{BOOK}/Shift Summary"]
  assert len(summary) == 1 + 7  # one row per day of the week
  assert summary[2][:6] == [
    "Ann Lee",
    "2026-11-03",
    "2026-11-03 09:00:00",
    "2026-11-03 17:00:00",
    "Yes",
    "8:00",
  ]  # Yes: inserted by the postgres login, which the view counts as a hand edit


async def test_empty_group_gets_zero_rows_and_no_punches(migrated: Database, sent: Sent) -> None:
  await add_employee(migrated, "Ann", "Office")
  await add_employee(migrated, "Bob", "Office", active=False)
  await job(migrated, ("Office",)).main(SCHEDULED)
  [(_, tabs)] = sent
  assert tabs[f"{BOOK}/Punch Log"] == [PUNCH_LOG_HEADER]
  assert tabs[f"{BOOK}/Weekly Hours"][1:] == [["Ann Lee", "0:00", "0:00", "0:00", "0:00"]]
  assert {r[0] for r in tabs[f"{BOOK}/Shift Summary"][1:]} == {"Ann Lee"}


async def test_no_group_workbook_only_when_ungrouped_punches(migrated: Database, sent: Sent) -> None:
  await add_employee(migrated, "Ann", "Office")
  cat = await add_employee(migrated, "Cat", None)
  no_group = "No Group Timeclock Report 2026-11-02 to 2026-11-08.xlsx"
  await job(migrated).main(SCHEDULED)
  # Bootstrap's own `default` group is covered too.
  assert [p.get_filename() for p in sent[0][0].iter_attachments()] == [
    "default Timeclock Report 2026-11-02 to 2026-11-08.xlsx",
    BOOK,
  ]
  await punch(migrated, cat, "clock_in", datetime(2026, 11, 3, 9, tzinfo=NY))
  await job(migrated).main(SCHEDULED)
  msg, tabs = sent[1]
  assert [p.get_filename() for p in msg.iter_attachments()][-1] == no_group
  assert msg["Subject"] == "default, Office, No Group Timeclock Report 2026-11-02 to 2026-11-08"
  assert tabs[f"{no_group}/Punch Log"][1][0] == "Cat Lee"
  await job(migrated, ("Office",)).main(SCHEDULED)
  assert [p.get_filename() for p in sent[2][0].iter_attachments()] == [BOOK]  # a named-group entry ignores them


async def test_unknown_group_fails_the_run(migrated: Database, sent: Sent) -> None:
  await add_employee(migrated, "Ann", "Office")
  with pytest.raises(ValueError, match="Nope"):
    await job(migrated, ("Office", "Nope")).main(SCHEDULED)
  assert sent == []


async def test_send_failure_leaves_no_files(migrated: Database, monkeypatch: pytest.MonkeyPatch) -> None:
  await add_employee(migrated, "Ann", "Office")
  attached: list[Path] = []
  real_prepare = payroll_report.prepare_email_message

  def spy(parts: Any) -> EmailMessage:
    attached.extend(parts["attachments"])
    return real_prepare(parts)

  def boom(msg: EmailMessage | Sequence[EmailMessage]) -> None:
    raise OSError("smtp down")

  monkeypatch.setattr(payroll_report, "prepare_email_message", spy)
  monkeypatch.setattr(payroll_report, "batch_send_emails", boom)
  with pytest.raises(OSError, match="smtp down"):
    await job(migrated, ("Office",)).main(SCHEDULED)
  assert attached
  assert not any(path.parent.exists() for path in attached)


def test_registry_entry_is_the_placeholder() -> None:
  e = next(e for e in STATIC if e.name == "payroll_report")
  assert e.schedule is not None
  assert (e.catch_up, e.retry, e.schedule.describe()) == (True, Retry(timedelta(minutes=15), 4), "At 06:00 on Monday")
