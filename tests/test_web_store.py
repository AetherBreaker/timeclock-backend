"""Tests for what the page reads and writes as the signed-in account's badge (spec 2026-11-05 §5, §6.10, §6.11)."""

# Standard library imports
from datetime import UTC, date, datetime, time
from typing import TYPE_CHECKING
from uuid import uuid4
from zoneinfo import ZoneInfo

# Third party imports
from page_sim import page_settings
from timeclock_schema.enums import PunchKind
from timeclock_schema.models import Settings

# First party imports
from timeclock_backend.web.db import editor_transaction
from timeclock_backend.web.gate import Gate
from timeclock_backend.web.rules import Record, RecordAction
from timeclock_backend.web.store import load_context, load_history, load_punches, save_edit, window_token

if TYPE_CHECKING:
  # Standard library imports
  from contextlib import AbstractAsyncContextManager

  # Third party imports
  from page_sim import PagesDb
  from psycopg import AsyncConnection
  from psycopg.rows import DictRow

NY = ZoneInfo("America/New_York")


def _ny(day: int, clock: str, month: int = 11) -> datetime:
  return datetime.combine(date(2026, month, day), time.fromisoformat(clock), tzinfo=NY).astimezone(UTC)


def _as_badge(db: PagesDb) -> AbstractAsyncContextManager[AsyncConnection[DictRow]]:
  gate, storage, session = Gate(page_settings(), db.accounts, {}), {}, {}
  gate.start(storage, session, db.email)
  return editor_transaction(db.pools, gate, storage, session, test_database=False)


async def test_load_context_reads_the_zone_settings_groups_and_people(pages_db: PagesDb) -> None:
  async with _as_badge(pages_db) as conn:
    context = await load_context(conn, default_tz=ZoneInfo("UTC"))
  assert context.tz.key == "America/New_York"  # backend.environment, which bootstrap writes from TZ
  assert context.settings == Settings()  # bootstrap's row holds the defaults
  assert context.groups == {1: "default"}
  ann, bob = pages_db.employees
  assert set(context.people) == {ann, bob}
  assert (context.people[ann].name, context.people[ann].username, context.people[ann].fingerprints) == ("Ann Lee", "12", 0)
  assert min(context.people.values(), key=lambda p: p.sort_key).id == ann


async def test_load_punches_starts_after_the_last_clock_out_before_the_range(pages_db: PagesDb) -> None:
  bob = pages_db.employees[1]
  pages_db.punch(bob, "clock_in", _ny(1, "07:00"))
  pages_db.punch(bob, "clock_out", _ny(1, "15:00"))
  open_in = pages_db.punch(bob, "clock_in", _ny(1, "20:00"))
  break_start = pages_db.punch(bob, "break_start", _ny(2, "08:00"))
  async with _as_badge(pages_db) as conn:
    loaded = await load_punches(conn, start=_ny(2, "00:00"), end=_ny(9, "00:00"))
  assert [p.id for p in loaded[bob]] == [open_in, break_start]


async def test_load_punches_reaches_the_first_clock_out_after_the_range(pages_db: PagesDb) -> None:
  bob = pages_db.employees[1]
  start = pages_db.punch(bob, "clock_in", _ny(8, "22:00"))
  end = pages_db.punch(bob, "clock_out", _ny(9, "06:00"))
  later = pages_db.punch(bob, "clock_in", _ny(9, "22:00"))
  async with _as_badge(pages_db) as conn:
    loaded = await load_punches(conn, start=_ny(2, "00:00"), end=_ny(9, "00:00"))
    one = await load_punches(conn, start=_ny(2, "00:00"), end=None, employee_id=bob)
  assert [p.id for p in loaded[bob]] == [start, end]
  assert [p.id for p in one[bob]] == [start, end, later]
  assert set(one) == {bob}


async def test_load_punches_goes_by_corrected_time(pages_db: PagesDb) -> None:
  bob = pages_db.employees[1]
  moved = pages_db.punch(bob, "clock_in", _ny(20, "08:00", month=10))
  correction = pages_db.correct(moved, "amend", new_at=_ny(3, "08:00"))
  voided = pages_db.punch(bob, "clock_out", _ny(3, "16:00"))
  pages_db.correct(voided, "void")
  kept = pages_db.punch(bob, "clock_out", _ny(3, "17:00"), automatic=True)
  async with _as_badge(pages_db) as conn:
    loaded = await load_punches(conn, start=_ny(2, "00:00"), end=_ny(9, "00:00"))
  first, second = loaded[bob]
  assert (first.id, first.at, first.original_at, first.version) == (moved, _ny(3, "08:00"), _ny(20, "08:00", month=10), correction)
  assert (first.changed_by, first.kind) == ("postgres", PunchKind.CLOCK_IN)
  assert (second.id, second.automatic, second.version, second.instance_id) == (kept, True, None, "front")
  assert window_token(loaded[bob]) != window_token([first])


async def test_save_edit_writes_one_save_with_its_note(pages_db: PagesDb) -> None:
  bob = pages_db.employees[1]
  pages_db.punch(bob, "clock_in", _ny(3, "07:00"))
  out = pages_db.punch(bob, "clock_out", _ny(3, "15:00"))
  stray = pages_db.punch(bob, "clock_out", _ny(3, "17:00"))
  added = uuid4()
  async with _as_badge(pages_db) as conn:
    edit = await save_edit(
      conn,
      employee_id=bob,
      records=[
        Record(RecordAction.AMEND, out, None, _ny(3, "15:30")),
        Record(RecordAction.VOID, stray, None, None),
        Record(RecordAction.INSERT, added, PunchKind.BREAK_START, _ny(3, "10:00")),
      ],
      note="left at 3:30",
    )
  with pages_db.admin() as conn:
    assert conn.execute("SELECT id, note, db_login FROM edits").fetchall() == [(edit, "left at 3:30", pages_db.email)]
    corrections = conn.execute(
      "SELECT punch_id, action::text, new_at, note, edit_id, db_login FROM punch_corrections ORDER BY punch_id = %s DESC", (out,)
    ).fetchall()
    assert corrections == [
      (out, "amend", _ny(3, "15:30"), "left at 3:30", edit, pages_db.email),
      (stray, "void", None, "left at 3:30", edit, pages_db.email),
    ]
    inserted = conn.execute("SELECT employee_id, kind::text, at, instance_id, edit_id, db_login FROM punches WHERE id = %s", (added,))
    assert inserted.fetchall() == [(bob, "break_start", _ny(3, "10:00"), "web", edit, pages_db.email)]


async def test_history_lists_each_save_with_old_and_new_values(pages_db: PagesDb) -> None:
  bob = pages_db.employees[1]
  start = pages_db.punch(bob, "clock_in", _ny(3, "07:00"))
  out = pages_db.punch(bob, "clock_out", _ny(3, "15:00"))
  stray = pages_db.punch(bob, "clock_out", _ny(3, "17:00"))
  pages_db.punch(bob, "clock_in", _ny(5, "07:00"))  # another day: not this day's history
  added = uuid4()
  async with _as_badge(pages_db) as conn:
    edit = await save_edit(
      conn,
      employee_id=bob,
      records=[
        Record(RecordAction.AMEND, out, None, _ny(3, "15:30")),
        Record(RecordAction.VOID, stray, None, None),
        Record(RecordAction.INSERT, added, PunchKind.BREAK_START, _ny(3, "10:00")),
      ],
      note="left at 3:30",
    )
  by_hand = pages_db.correct(start, "amend", new_at=_ny(3, "06:55"), note="by hand")
  async with _as_badge(pages_db) as conn:
    history = await load_history(conn, employee_id=bob, start=_ny(3, "00:00"), end=_ny(4, "00:00"))
  latest, page = history
  assert (latest.who, latest.note, latest.edit_id) == ("postgres", "by hand", None)
  [change] = latest.changes
  assert (change.action, change.old_at, change.new_at, change.correction_id) == (
    RecordAction.AMEND,
    _ny(3, "07:00"),
    _ny(3, "06:55"),
    by_hand,
  )
  assert (page.who, page.note, page.edit_id) == (pages_db.email, "left at 3:30", edit)
  assert [(c.punch_id, c.action, c.old_at, c.new_kind, c.new_at) for c in page.changes] == [
    (added, RecordAction.INSERT, None, PunchKind.BREAK_START, _ny(3, "10:00")),
    (out, RecordAction.AMEND, _ny(3, "15:00"), None, _ny(3, "15:30")),
    (stray, RecordAction.VOID, _ny(3, "17:00"), None, None),
  ]
