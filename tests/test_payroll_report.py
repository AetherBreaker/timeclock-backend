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
  from uuid import UUID

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
  "Paid Hours",
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
WEEKLY_HOURS_HEADER = ["Employee Name", "Paid Hours", "Hours", "Breaks", "Lunches", "Unpaid Breaks"]

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


async def add_employee(db: Database, first: str, group: str, *, active: bool = True) -> int:
  async with await connect(db.name) as conn:
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


async def punch(db: Database, employee_id: int, kind: str, at: datetime) -> UUID:
  punch_id = uuid4()
  async with await connect(db.name) as conn:
    await conn.execute(
      "INSERT INTO punches (id, employee_id, kind, at, instance_id) VALUES (%s, %s, %s, %s, 'test')",
      (punch_id, employee_id, kind, at.astimezone(UTC)),
    )
  return punch_id


async def breaks_off(db: Database) -> None:
  async with await connect(db.name) as conn:
    await conn.execute("UPDATE config.settings SET breaks_enabled = false")


def job(db: Database, groups: tuple[str, ...] | None = None) -> PayrollReportJob:
  return PayrollReportJob(db, RECIPIENTS, groups)


SHIFT_SUMMARY_SHORT = [
  "Employee Name",
  "Day",
  "Clock In",
  "Clock Out",
  "Corrected",
  "Paid Hours",
  "Lunches",
  "Lunch Count",
  "Lunch Times",
]
WEEKLY_HOURS_SHORT = ["Employee Name", "Paid Hours", "Lunches"]


async def shift_with_break(db: Database, employee_id: int) -> tuple[UUID, UUID]:
  """Tuesday 9:00 to 17:00 with a 12:00 to 12:10 break; the break's two punch ids."""
  ids = [
    await punch(db, employee_id, kind, datetime(2026, 11, 3, hour, minute, tzinfo=NY))
    for kind, hour, minute in (("clock_in", 9, 0), ("break_start", 12, 0), ("break_end", 12, 10), ("clock_out", 17, 0))
  ]
  return ids[1], ids[2]


async def test_breaks_off_and_no_break_punches_drop_the_break_columns_and_hours(migrated: Database, sent: Sent) -> None:
  ann = await add_employee(migrated, "Ann", "Office")
  await punch(migrated, ann, "clock_in", datetime(2026, 11, 3, 9, tzinfo=NY))
  await punch(migrated, ann, "clock_out", datetime(2026, 11, 3, 17, tzinfo=NY))
  await breaks_off(migrated)
  await job(migrated, ("Office",)).main(SCHEDULED)
  [(_, tabs)] = sent
  assert tabs[f"{BOOK}/Punch Log"][0] == PUNCH_LOG_HEADER
  assert tabs[f"{BOOK}/Shift Summary"][0] == SHIFT_SUMMARY_SHORT
  assert tabs[f"{BOOK}/Shift Summary"][2][:6] == ["Ann Lee", "2026-11-03", "2026-11-03 09:00:00", "2026-11-03 17:00:00", "Yes", "8:00"]
  assert tabs[f"{BOOK}/Weekly Hours"] == [WEEKLY_HOURS_SHORT, ["Ann Lee", "8:00", "0:00"]]


async def test_breaks_off_with_a_break_punch_keeps_every_column(migrated: Database, sent: Sent) -> None:
  await shift_with_break(migrated, await add_employee(migrated, "Ann", "Office"))
  await breaks_off(migrated)
  await job(migrated, ("Office",)).main(SCHEDULED)
  [(_, tabs)] = sent
  assert tabs[f"{BOOK}/Shift Summary"][0] == SHIFT_SUMMARY_HEADER
  assert tabs[f"{BOOK}/Weekly Hours"][0] == WEEKLY_HOURS_HEADER


async def test_breaks_on_keeps_every_column_without_break_punches(migrated: Database, sent: Sent) -> None:
  ann = await add_employee(migrated, "Ann", "Office")
  await punch(migrated, ann, "clock_in", datetime(2026, 11, 3, 9, tzinfo=NY))
  await punch(migrated, ann, "clock_out", datetime(2026, 11, 3, 17, tzinfo=NY))
  await job(migrated, ("Office",)).main(SCHEDULED)
  [(_, tabs)] = sent
  assert tabs[f"{BOOK}/Shift Summary"][0] == SHIFT_SUMMARY_HEADER
  assert tabs[f"{BOOK}/Weekly Hours"][0] == WEEKLY_HOURS_HEADER


async def test_another_groups_break_doesnt_keep_this_groups_columns(migrated: Database, sent: Sent) -> None:
  ann = await add_employee(migrated, "Ann", "Office")
  await punch(migrated, ann, "clock_in", datetime(2026, 11, 3, 9, tzinfo=NY))
  await punch(migrated, ann, "clock_out", datetime(2026, 11, 3, 17, tzinfo=NY))
  await shift_with_break(migrated, await add_employee(migrated, "Bob", "Warehouse"))
  await breaks_off(migrated)
  await job(migrated, ("Office", "Warehouse")).main(SCHEDULED)
  [(_, tabs)] = sent
  warehouse = BOOK.replace("Office", "Warehouse")
  assert tabs[f"{BOOK}/Shift Summary"][0] == SHIFT_SUMMARY_SHORT
  assert tabs[f"{BOOK}/Weekly Hours"][0] == WEEKLY_HOURS_SHORT
  assert tabs[f"{warehouse}/Shift Summary"][0] == SHIFT_SUMMARY_HEADER
  assert tabs[f"{warehouse}/Weekly Hours"][0] == WEEKLY_HOURS_HEADER


async def test_a_voided_break_punch_doesnt_keep_the_columns(migrated: Database, sent: Sent) -> None:
  start, end = await shift_with_break(migrated, await add_employee(migrated, "Ann", "Office"))
  async with await connect(migrated.name) as conn:
    for punch_id in (start, end):
      await conn.execute("INSERT INTO punch_corrections (punch_id, action) VALUES (%s, 'void')", (punch_id,))
  await breaks_off(migrated)
  await job(migrated, ("Office",)).main(SCHEDULED)
  [(_, tabs)] = sent
  assert tabs[f"{BOOK}/Shift Summary"][0] == SHIFT_SUMMARY_SHORT
  assert tabs[f"{BOOK}/Weekly Hours"] == [WEEKLY_HOURS_SHORT, ["Ann Lee", "8:00", "0:00"]]


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


async def test_four_recipients_all_get_the_email(migrated: Database, sent: Sent) -> None:
  # aeth_ext reads any 4-item sequence of strings as one Address, so a bare 4-tuple of recipients failed every run.
  recipients = ("a@example.com", "b@example.com", "c@example.com", "d@example.com")
  await add_employee(migrated, "Ann", "Office")
  await PayrollReportJob(migrated, recipients).main(SCHEDULED)
  [(msg, _)] = sent
  assert [a.addr_spec for a in msg["To"].addresses] == list(recipients)


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
  assert tabs[f"{BOOK}/Weekly Hours"][1][:4] == ["Ann Lee", "8:00", "8:00", "0:00"]
  summary = tabs[f"{BOOK}/Shift Summary"]
  assert len(summary) == 1 + 7  # one row per day of the week
  # Corrected is Yes: the test inserts as the postgres login, which the view counts as a hand edit.
  assert summary[2][:8] == ["Ann Lee", "2026-11-03", "2026-11-03 09:00:00", "2026-11-03 17:00:00", "Yes", "8:00", "8:00", "0:00"]


async def test_empty_group_gets_zero_rows_and_no_punches(migrated: Database, sent: Sent) -> None:
  await add_employee(migrated, "Ann", "Office")
  await add_employee(migrated, "Bob", "Office", active=False)
  await job(migrated, ("Office",)).main(SCHEDULED)
  [(_, tabs)] = sent
  assert tabs[f"{BOOK}/Punch Log"] == [PUNCH_LOG_HEADER]
  assert tabs[f"{BOOK}/Weekly Hours"][1:] == [["Ann Lee", "0:00", "0:00", "0:00", "0:00", "0:00"]]
  assert {r[0] for r in tabs[f"{BOOK}/Shift Summary"][1:]} == {"Ann Lee"}


async def test_every_group_run_is_one_workbook_per_group(migrated: Database, sent: Sent) -> None:
  await add_employee(migrated, "Ann", "Office")
  await job(migrated).main(SCHEDULED)
  # Bootstrap's own `default` group is covered too.
  assert [p.get_filename() for p in sent[0][0].iter_attachments()] == [
    "default Timeclock Report 2026-11-02 to 2026-11-08.xlsx",
    BOOK,
  ]


async def test_paid_hours_is_hours_plus_paid_breaks(migrated: Database, sent: Sent) -> None:
  ann = await add_employee(migrated, "Ann", "Office")
  for kind, hour, minute in (("clock_in", 9, 0), ("break_start", 12, 0), ("break_end", 12, 10), ("clock_out", 17, 0)):
    await punch(migrated, ann, kind, datetime(2026, 11, 3, hour, minute, tzinfo=NY))
  await job(migrated, ("Office",)).main(SCHEDULED)
  [(_, tabs)] = sent
  [weekly] = tabs[f"{BOOK}/Weekly Hours"][1:]
  assert weekly == ["Ann Lee", "8:00", "7:50", "0:10", "0:00", "0:00"]
  shift = tabs[f"{BOOK}/Shift Summary"][2]
  assert shift[5:8] == ["8:00", "7:50", "0:10"]


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
