"""Tests for the shift editor (spec 2026-11-05 §6, §8; mockups C to E, H): its page, its save, and its live base."""

# Standard library imports
import asyncio
from datetime import UTC, date, datetime, time, timedelta
from typing import TYPE_CHECKING, Any, LiteralString
from unittest.mock import AsyncMock
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

# Third party imports
import page_sim
from nicegui import app, ui
from nicegui.client import Client
from page_sim import page_settings
from timeclock_schema.shifts import week_bounds

# First party imports
from timeclock_backend.web import shift_editor, text
from timeclock_backend.web.db import editor_transaction
from timeclock_backend.web.gate import Gate, SignIn
from timeclock_backend.web.rules import EntryKind, apply, entries, entry_hours, records, week_hours
from timeclock_backend.web.shift_editor import ShiftEditor, draft_from_json, ms, save_shift
from timeclock_backend.web.store import load_context, load_punches, window_token

if TYPE_CHECKING:
  # Standard library imports
  from contextlib import AbstractAsyncContextManager

  # Third party imports
  import pytest
  from page_sim import DbPage, PagesDb, PageUnderTest
  from psycopg import AsyncConnection
  from psycopg.rows import DictRow

NY = ZoneInfo("America/New_York")
NOW = datetime(2026, 11, 5, 14, tzinfo=UTC)  # the fake clock's
WEEK = week_bounds(NOW, NY)


def _ny(day: int, clock: str) -> datetime:
  return datetime.combine(date(2026, 11, day), time.fromisoformat(clock), tzinfo=NY).astimezone(UTC)


def _editor(page: DbPage) -> ShiftEditor:
  [editor] = page.user.find(kind=ShiftEditor).elements
  return editor


def _edit(punch: UUID, version: UUID | None = None, **change: Any) -> dict[str, Any]:
  """A draft punch as the component sends it: `change` gives `kind` and `at` (a datetime), or `removed`/`confirmed`."""
  values = {"id": str(punch), "removed": False, "confirmed": False, "version": None if version is None else str(version), "new": False}
  return values | {k: ms(v) if isinstance(v, datetime) else v for k, v in change.items()}


def _widget(page: DbPage, marker: str) -> Any:
  [found] = page.user.find(marker=marker).elements
  return found


async def _send_save(page: DbPage, monkeypatch: pytest.MonkeyPatch, payload: dict[str, Any]) -> list[dict[str, Any]]:
  """Dispatch the component's save as NiceGUI dispatches a browser's event; returns the answers it got."""
  editor, answered, answers = _editor(page), asyncio.Event(), []
  monkeypatch.setattr(editor, "run_method", lambda _, result: (answers.append(result), answered.set()))
  [listener] = [key for key, found in editor._event_listeners.items() if found.type == "save"]  # pyright: ignore[reportPrivateUsage]
  editor._handle_event({"listener_id": listener, "args": payload})  # pyright: ignore[reportPrivateUsage]
  await asyncio.wait_for(answered.wait(), 10)
  return answers


def _badge(db: PagesDb) -> AbstractAsyncContextManager[AsyncConnection[DictRow]]:
  gate, storage, session = Gate(page_settings(), db.accounts, {}), {}, {}
  gate.start(storage, session, db.email)
  return editor_transaction(db.pools, gate, storage, session, test_database=False)


async def _token(db: PagesDb, employee: int) -> str:
  """The window's token as an editor opening now would get it."""
  async with _badge(db) as conn:
    return window_token((await load_punches(conn, start=WEEK[0], end=None, employee_id=employee)).get(employee, []))


async def _save(db: PagesDb, employee: int, draft: list[dict[str, Any]], **options: Any) -> dict[str, Any]:
  """`save_shift` as the page calls it, signed in as `db.email` (Ann), with `_payload`'s payload."""
  signin = SignIn(db.email, db.employees[0], developer=False, test_database=False, started=NOW)
  payload = await _payload(db, employee, draft, **options)
  async with _badge(db) as conn:
    return await save_shift(conn, signin=signin, employee_id=employee, week=WEEK, payload=payload, now=NOW, settings=db.settings)


async def _payload(db: PagesDb, employee: int, draft: list[dict[str, Any]], **options: Any) -> dict[str, Any]:
  """The component's save payload, with a preview worked out as the component's.

  `options`: `note`, `token` (else the window's current one), `focus` (else the drafted ids), `preview`, `accept`.
  """
  async with _badge(db) as conn:
    punches = (await load_punches(conn, start=WEEK[0], end=None, employee_id=employee)).get(employee, [])
    context = await load_context(conn, default_tz=NY)
    focus = options.get("focus", [d["id"] for d in draft])
    preview = options.get("preview")
    if preview is None:  # the component's numbers, which are Python's when the rules agree
      after = apply(punches, records({p.id: p for p in punches}, draft_from_json(draft, punches)))
      touched = {UUID(i) for i in focus} | {UUID(d["id"]) for d in draft}
      shown = [e for e in entries(after, as_of=NOW) if e.kind is EntryKind.SHIFT and any(p.id in touched for p in e.punches)]
      hours = {str(e.key): entry_hours(e, after, settings=context.settings, tz=NY, now=NOW) for e in shown}
      worked = week_hours(after, settings=context.settings, tz=NY, week=WEEK, now=NOW).worked
      preview = {
        "entries": {k: [ms(h.worked), ms(h.paid_break), ms(h.unpaid)] for k, h in hours.items() if h is not None},
        "week": ms(worked),
      }
  return {
    "draft": draft,
    "note": options.get("note", ""),
    "token": options.get("token", window_token(punches)),
    "focus": focus,
    "asOf": ms(NOW),
    "preview": preview,
    "acceptServer": options.get("accept", False),
  }


async def test_a_row_opens_its_shift_in_the_editor(db_page: DbPage) -> None:
  bob = db_page.db.employees[1]
  start = db_page.db.punch(bob, "clock_in", _ny(3, "08:00"))
  end = db_page.db.punch(bob, "clock_out", _ny(3, "16:00"))
  await db_page.user.open("/")
  db_page.user.find(marker=f"row-{start}").click()
  await db_page.user.should_see(kind=ShiftEditor)
  await db_page.user.should_see("Tuesday, November 3, 2026")
  await db_page.user.should_see("ID 34")
  base = _editor(db_page).props["base"]
  assert base["focus"] == [str(start), str(end)]
  assert (base["employee"]["id"], base["employee"]["first"], base["tz"], base["own"]) == (bob, "Bob", "America/New_York", False)
  assert base["week"] == [ms(WEEK[0]), ms(WEEK[1])]
  assert base["breaksEnabled"] is True


async def test_the_editor_is_told_breaks_are_off(db_page: DbPage) -> None:
  bob = db_page.db.employees[1]
  start = db_page.db.punch(bob, "clock_in", _ny(3, "08:00"))
  db_page.db.punch(bob, "clock_out", _ny(3, "16:00"))
  with db_page.db.admin() as conn:
    conn.execute("UPDATE config.settings SET breaks_enabled = false")
  await db_page.user.open("/")
  db_page.user.find(marker=f"row-{start}").click()
  await db_page.user.should_see(kind=ShiftEditor)
  assert _editor(db_page).props["base"]["breaksEnabled"] is False


async def _timeline_days(db_page: DbPage, *setup: LiteralString) -> dict[str, list[int | None]]:
  """The opened editor's `timelineDays` for Bob's shift on Tue Nov 3, after `setup` runs on the production database."""
  with db_page.db.admin() as conn:
    for statement in setup:
      conn.execute(statement)
  bob = db_page.db.employees[1]
  start = db_page.db.punch(bob, "clock_in", _ny(3, "08:00"))
  db_page.db.punch(bob, "clock_out", _ny(3, "16:00"))
  await db_page.user.open(f"/shift?employee={bob}&punch={start}")
  await db_page.user.should_see(kind=ShiftEditor)
  return _editor(db_page).props["base"]["timelineDays"]


async def test_the_timeline_runs_from_an_hour_before_opening_to_the_automatic_clock_out(db_page: DbPage) -> None:
  days = await _timeline_days(
    db_page, "INSERT INTO config.business_hours (weekday, opens, closes) SELECT d, '06:00', '18:00' FROM generate_series(1, 5) d"
  )
  assert sorted(days) == [f"2026-11-{d:02}" for d in range(2, 9)]  # the week's days
  assert days["2026-11-03"] == [ms(_ny(3, "05:00")), ms(_ny(3, "23:00"))]
  assert days["2026-11-07"] == [ms(_ny(7, "05:00")), ms(_ny(7, "23:00"))]  # Saturday, closed, takes the week's hours


async def test_the_timeline_ends_at_closing_when_that_is_later(db_page: DbPage) -> None:
  days = await _timeline_days(
    db_page,
    "INSERT INTO config.business_hours (weekday, opens, closes) VALUES (2, '07:30', '12:00'), (2, '13:00', '19:00')",
    "UPDATE config.settings SET auto_clock_out_stamp_time = '18:30'",
  )
  assert days["2026-11-03"] == [ms(_ny(3, "06:30")), ms(_ny(3, "19:00"))]


async def test_without_business_hours_only_the_automatic_clock_out_holds_the_timeline(db_page: DbPage) -> None:
  assert (await _timeline_days(db_page))["2026-11-03"] == [None, ms(_ny(3, "23:00"))]


async def test_without_hours_or_an_automatic_clock_out_the_timeline_fits_the_shift(db_page: DbPage) -> None:
  assert await _timeline_days(db_page, "UPDATE config.settings SET auto_clock_out_enabled = false") == {}


async def test_your_own_shift_opens_read_only(db_page: DbPage) -> None:
  ann = db_page.db.employees[0]
  start = db_page.db.punch(ann, "clock_in", _ny(3, "09:00"))
  db_page.db.punch(ann, "clock_out", _ny(3, "17:00"))
  await db_page.user.open(f"/shift?employee={ann}&punch={start}")
  await db_page.user.should_see("This is your own shift. Another editor has to change it.")
  assert _editor(db_page).props["base"]["own"] is True


async def test_a_shift_that_no_longer_exists_says_so(db_page: DbPage) -> None:
  await db_page.user.open(f"/shift?employee={db_page.db.employees[1]}&punch={uuid4()}")
  await db_page.user.should_see("This shift has changed since the list was drawn.")
  await db_page.user.should_not_see(kind=ShiftEditor)


async def test_saving_writes_the_records_with_the_note(pages_db: PagesDb) -> None:
  bob = pages_db.employees[1]
  pages_db.punch(bob, "clock_in", _ny(3, "07:58"))
  out = pages_db.punch(bob, "clock_out", _ny(3, "23:00"), automatic=True)
  result = await _save(pages_db, bob, [_edit(out, kind="clock_out", at=_ny(3, "17:05"))], note="left at 5")
  assert result.keys() == {"ok", "base"}
  with pages_db.admin() as conn:
    written = conn.execute("SELECT c.action::text, c.new_at, c.note, e.note FROM punch_corrections c JOIN edits e ON e.id = c.edit_id")
    assert written.fetchall() == [("amend", _ny(3, "17:05"), "left at 5", "left at 5")]
  [saved] = [p for p in result["base"]["punches"] if p["id"] == str(out)]
  assert saved["at"] == ms(_ny(3, "17:05"))


async def test_the_components_save_event_saves_and_answers(db_page: DbPage, monkeypatch: pytest.MonkeyPatch) -> None:
  # Dispatched as NiceGUI dispatches a browser's event, which inspects the handler's signature first.
  bob = db_page.db.employees[1]
  start = db_page.db.punch(bob, "clock_in", _ny(3, "07:58"))
  out = db_page.db.punch(bob, "clock_out", _ny(3, "23:00"), automatic=True)
  await db_page.user.open(f"/shift?employee={bob}&punch={start}")
  await db_page.user.should_see(kind=ShiftEditor)
  editor, answered, answers = _editor(db_page), asyncio.Event(), []
  # the browser runs saveResult
  monkeypatch.setattr(editor, "run_method", lambda _, result: (answers.append(result), answered.set()))
  [listener] = [key for key, found in editor._event_listeners.items() if found.type == "save"]  # pyright: ignore[reportPrivateUsage]
  payload = await _payload(db_page.db, bob, [_edit(out, kind="clock_out", at=_ny(3, "17:05"))], note="left at 5")
  editor._handle_event({"listener_id": listener, "args": payload})  # pyright: ignore[reportPrivateUsage]
  await asyncio.wait_for(answered.wait(), 10)
  assert answers[0].keys() == {"ok", "base"}
  with db_page.db.admin() as conn:
    assert conn.execute("SELECT note FROM punch_corrections").fetchall() == [("left at 5",)]


async def test_a_save_that_breaks_still_answers(db_page: DbPage, monkeypatch: pytest.MonkeyPatch) -> None:
  # Unanswered, the component would say "Saving..." until the page is reloaded.
  bob = db_page.db.employees[1]
  start = db_page.db.punch(bob, "clock_in", _ny(3, "07:58"))
  db_page.db.punch(bob, "clock_out", _ny(3, "16:00"))
  await db_page.user.open(f"/shift?employee={bob}&punch={start}")
  await db_page.user.should_see(kind=ShiftEditor)
  editor, answered, answers = _editor(db_page), asyncio.Event(), []
  monkeypatch.setattr(editor, "run_method", lambda _, result: (answers.append(result), answered.set()))
  monkeypatch.setattr(shift_editor, "save_shift", AsyncMock(side_effect=ZeroDivisionError))
  monkeypatch.setattr(app, "handle_exception", lambda _: None)  # the crash alert's path
  [listener] = [key for key, found in editor._event_listeners.items() if found.type == "save"]  # pyright: ignore[reportPrivateUsage]
  editor._handle_event({"listener_id": listener, "args": await _payload(db_page.db, bob, [])})  # pyright: ignore[reportPrivateUsage]
  await asyncio.wait_for(answered.wait(), 10)
  assert answers == [{"error": text.SAVE_FAILED}]


async def test_two_saves_of_the_same_punch_conflict(pages_db: PagesDb) -> None:
  bob = pages_db.employees[1]
  pages_db.punch(bob, "clock_in", _ny(3, "07:58"))
  out = pages_db.punch(bob, "clock_out", _ny(3, "23:00"), automatic=True)
  opened = await _token(pages_db, bob)  # both editors opened before either saved
  first = await _save(pages_db, bob, [_edit(out, kind="clock_out", at=_ny(3, "17:05"))], token=opened)
  second = await _save(pages_db, bob, [_edit(out, kind="clock_out", at=_ny(3, "17:30"))], token=opened)
  assert first.keys() == {"ok", "base"}
  assert second.keys() == {"changed"}
  with pages_db.admin() as conn:
    assert conn.execute("SELECT count(*) FROM punch_corrections").fetchone() == (1,)


async def test_an_outside_change_needs_confirming_then_saves(pages_db: PagesDb) -> None:
  bob = pages_db.employees[1]
  pages_db.punch(bob, "clock_in", _ny(3, "07:00"))
  out = pages_db.punch(bob, "clock_out", _ny(3, "15:00"))
  draft = [_edit(out, kind="clock_out", at=_ny(3, "15:30"))]
  opened = await _token(pages_db, bob)
  pages_db.punch(bob, "break_start", _ny(3, "10:00"))  # a clock sends in a late break
  pages_db.punch(bob, "break_end", _ny(3, "10:15"))
  changed = await _save(pages_db, bob, draft, token=opened)
  assert changed.keys() == {"changed"}
  saved = await _save(pages_db, bob, draft, token=changed["changed"]["token"])
  assert saved.keys() == {"ok", "base"}


async def test_a_save_that_leaves_an_ignored_punch_is_refused(pages_db: PagesDb) -> None:
  bob = pages_db.employees[1]
  start = pages_db.punch(bob, "clock_in", _ny(3, "07:01"))
  extra = pages_db.punch(bob, "clock_in", _ny(3, "07:04"))
  out = pages_db.punch(bob, "clock_out", _ny(3, "15:33"))
  result = await _save(pages_db, bob, [_edit(out, kind="clock_out", at=_ny(3, "15:45"))], focus=[str(start), str(extra), str(out)])
  assert result == {"refused": "This shift still holds a punch the hours ignore: Clock in at 7:04 AM. Fix it to save.", "open": None}


async def test_a_save_that_ignores_another_punch_is_refused(pages_db: PagesDb) -> None:
  bob = pages_db.employees[1]
  pages_db.punch(bob, "clock_in", _ny(3, "07:00"))
  out = pages_db.punch(bob, "clock_out", _ny(3, "15:00"))
  pages_db.punch(bob, "clock_in", _ny(4, "07:00"))
  pages_db.punch(bob, "clock_out", _ny(4, "15:00"))
  result = await _save(pages_db, bob, [_edit(out, removed=True, kind="clock_out", at=_ny(3, "15:00"))])
  assert result == {
    "refused": "Saving this would make the hours ignore another punch: Clock in at 7:00 AM on Wednesday, November 4. Change it so it still counts.",
    "open": None,
  }


async def test_a_differing_preview_is_a_fault_until_accepted(pages_db: PagesDb) -> None:
  bob = pages_db.employees[1]
  start = pages_db.punch(bob, "clock_in", _ny(3, "07:00"))
  out = pages_db.punch(bob, "clock_out", _ny(3, "15:00"))
  draft = [_edit(out, kind="clock_out", at=_ny(3, "15:30"))]
  wrong = {"entries": {str(start): [ms(timedelta(hours=9)), 0, 0]}, "week": ms(timedelta(hours=9))}
  fault = await _save(pages_db, bob, draft, preview=wrong)
  assert fault == {"fault": {"what": "Bob Lee's shift on Tuesday, November 3, 2026", "preview": "9:00", "server": "8:30"}}
  with pages_db.admin() as conn:
    assert conn.execute("SELECT count(*) FROM punch_corrections").fetchone() == (0,)
  assert (await _save(pages_db, bob, draft, preview=wrong, accept=True)).keys() == {"ok", "base"}


async def test_saving_your_own_shift_is_refused(pages_db: PagesDb) -> None:
  ann = pages_db.employees[0]
  pages_db.punch(ann, "clock_in", _ny(3, "09:00"))
  out = pages_db.punch(ann, "clock_out", _ny(3, "17:00"))
  result = await _save(pages_db, ann, [_edit(out, kind="clock_out", at=_ny(3, "17:30"))])
  assert result == {"refused": "This is your own shift. Another editor has to change it.", "open": None}


async def test_a_live_change_sends_the_open_editor_a_new_base(db_page: DbPage) -> None:
  bob = db_page.db.employees[1]
  start = db_page.db.punch(bob, "clock_in", _ny(3, "08:00"))
  db_page.db.punch(bob, "clock_out", _ny(3, "16:00"))
  await db_page.user.open(f"/shift?employee={bob}&punch={start}")
  await db_page.user.should_see(kind=ShiftEditor)
  opened = _editor(db_page).props["base"]["token"]
  late = db_page.db.punch(bob, "break_start", _ny(3, "10:00"))
  async with asyncio.timeout(10):
    while _editor(db_page).props["base"]["token"] == opened:  # noqa: ASYNC110 - props have no change event to wait on
      await asyncio.sleep(0.05)
  assert str(late) in [p["id"] for p in _editor(db_page).props["base"]["punches"]]


async def test_add_shift_offers_everyone_but_you(db_page: DbPage) -> None:
  await db_page.user.open("/shift/new")
  await db_page.user.should_see("Add a shift")
  [picker] = db_page.user.find(kind=ui.select, marker="add-employee").elements
  assert picker.options == {db_page.db.employees[1]: "Bob Lee · ID 34"}  # Ann, signed in, isn't offered


async def test_the_lists_add_shift_opens_add_shift(db_page: DbPage) -> None:
  await db_page.user.open("/")
  db_page.user.find(marker="add-shift").click()
  await db_page.user.should_see("Add a shift")


async def test_an_employee_and_a_day_open_an_empty_editor(db_page: DbPage) -> None:
  bob = db_page.db.employees[1]
  db_page.db.punch(bob, "clock_in", _ny(4, "07:55"))
  await db_page.user.open(f"/shift/new?employee={bob}&date=2026-11-04")
  await db_page.user.should_see(kind=ShiftEditor)
  props = _editor(db_page).props
  assert (props["base"]["focus"], props["base"]["day"], props["options"]["add"], len(props["base"]["punches"])) == (
    [],
    "2026-11-04",
    True,
    1,
  )


async def test_adding_a_shift_inserts_its_punches(pages_db: PagesDb) -> None:
  bob = pages_db.employees[1]
  times = [("clock_in", "08:00"), ("lunch_start", "12:00"), ("lunch_end", "12:30"), ("clock_out", "16:30")]
  new = [_edit(uuid4(), kind=kind, at=_ny(4, at), new=True) for kind, at in times]
  result = await _save(pages_db, bob, new, focus=[], note="forgot his badge")
  assert result.keys() == {"ok", "base"}
  with pages_db.admin() as conn:
    added = conn.execute("SELECT kind::text, at, instance_id FROM punches ORDER BY at").fetchall()
    assert added == [(kind, _ny(4, at), "web") for kind, at in times]
    assert conn.execute("SELECT count(*), max(note) FROM edits").fetchone() == (1, "forgot his badge")


async def test_an_overlapping_new_shift_is_refused_with_a_link(pages_db: PagesDb) -> None:
  bob = pages_db.employees[1]
  first = pages_db.punch(bob, "clock_in", _ny(4, "07:55"))
  pages_db.punch(bob, "clock_out", _ny(4, "12:10"))
  new = [_edit(uuid4(), kind="clock_in", at=_ny(4, "08:00"), new=True), _edit(uuid4(), kind="clock_out", at=_ny(4, "16:30"), new=True)]
  result = await _save(pages_db, bob, new, focus=[])
  assert result == {
    "refused": "This overlaps Bob's shift on Wednesday, November 4, from 7:55 AM to 12:10 PM. Change the times, or open that shift instead.",
    "open": f"/shift?employee={bob}&punch={first}",
  }


async def test_adding_a_shift_goes_back_to_the_list_and_says_so(db_page: DbPage, monkeypatch: pytest.MonkeyPatch) -> None:
  bob = db_page.db.employees[1]
  await db_page.user.open(f"/shift/new?employee={bob}&date=2026-11-04&back=week%3D2026-11-02%26role%3Demployee")
  await db_page.user.should_see(kind=ShiftEditor)
  new = [_edit(uuid4(), kind="clock_in", at=_ny(4, "08:00"), new=True), _edit(uuid4(), kind="clock_out", at=_ny(4, "16:30"), new=True)]
  assert (await _send_save(db_page, monkeypatch, await _payload(db_page.db, bob, new, focus=[])))[0].keys() == {"ok", "base"}
  assert _editor(db_page).props["options"]["back"] == "/?week=2026-11-02&role=employee"  # where the component goes on ok
  await db_page.user.open("/?week=2026-11-02&role=employee")
  await db_page.user.should_see("Saved: Bob Lee, Wed, Nov 4.")


async def test_saving_goes_back_to_the_list_as_it_was_left_and_says_so(db_page: DbPage, monkeypatch: pytest.MonkeyPatch) -> None:
  bob = db_page.db.employees[1]
  start = db_page.db.punch(bob, "clock_in", _ny(3, "07:58"))
  out = db_page.db.punch(bob, "clock_out", _ny(3, "23:00"), automatic=True)
  await db_page.user.open("/?week=2026-11-02&role=employee")
  db_page.user.find(marker=f"row-{start}").click()
  await db_page.user.should_see(kind=ShiftEditor)
  assert _widget(db_page, "crumb").props["href"] == "/?week=2026-11-02&role=employee"
  answers = await _send_save(db_page, monkeypatch, await _payload(db_page.db, bob, [_edit(out, kind="clock_out", at=_ny(3, "17:05"))]))
  assert answers[0].keys() == {"ok", "base"}
  assert _editor(db_page).props["options"]["back"] == "/?week=2026-11-02&role=employee"  # where the component goes on ok
  await db_page.user.open("/?week=2026-11-02&role=employee")
  await db_page.user.should_see("Saved: Bob Lee, Tue, Nov 3.")
  await db_page.user.open("/?week=2026-11-02&role=employee")  # a reload, or the next list: the note is gone
  await db_page.user.should_see(marker=f"row-{start}")
  await db_page.user.should_not_see("Saved: Bob Lee, Tue, Nov 3.")


async def test_an_editor_opened_directly_goes_back_to_its_week(db_page: DbPage) -> None:
  bob = db_page.db.employees[1]
  start = db_page.db.punch(bob, "clock_in", _ny(3, "07:58"))
  await db_page.user.open(f"/shift?employee={bob}&punch={start}")
  await db_page.user.should_see(kind=ShiftEditor)
  assert _widget(db_page, "crumb").props["href"] == "/?week=2026-11-02"
  assert _editor(db_page).props["options"]["back"] == "/?week=2026-11-02"


async def test_add_shift_keeps_the_list_through_its_pickers(db_page: DbPage) -> None:
  bob = db_page.db.employees[1]
  await db_page.user.open("/?week=2026-11-02&role=employee")
  db_page.user.find(marker="add-shift").click()
  await db_page.user.should_see("Add a shift")
  assert _widget(db_page, "crumb").props["href"] == "/?week=2026-11-02&role=employee"
  assert db_page.user.client is not None
  with db_page.user.client:
    _widget(db_page, "add-employee").set_value(bob)  # a choice is a page load
  await db_page.user.should_see(marker="add-date")
  assert _widget(db_page, "add-employee").value == bob
  assert _widget(db_page, "crumb").props["href"] == "/?week=2026-11-02&role=employee"


async def test_only_the_shift_editor_outlives_a_dropped_connection(page: PageUnderTest) -> None:
  # By path, the latest registration of each: every test's build registers the pages again.
  timeouts = {p.path: p.reconnect_timeout for p in Client.page_routes.pages.values()}
  assert timeouts["/shift"] == timeouts["/shift/new"] == page_sim.BUILD_ARGS["settings"].idle_minutes * 60
  assert timeouts["/"] is None
  assert timeouts["/signin"] is None


async def test_an_added_shift_from_before_today_needs_its_clock_out(pages_db: PagesDb) -> None:
  bob = pages_db.employees[1]
  yesterday = [_edit(uuid4(), kind="clock_in", at=_ny(4, "08:00"), new=True)]
  assert await _save(pages_db, bob, yesterday, focus=[]) == {"refused": text.ADD_CLOCK_OUT, "open": None}
  today = [_edit(uuid4(), kind="clock_in", at=_ny(5, "08:00"), new=True)]  # the fake clock's 9:00 AM
  assert (await _save(pages_db, bob, today, focus=[])).keys() == {"ok", "base"}


async def test_an_added_open_shift_with_punches_after_it_needs_its_clock_out(pages_db: PagesDb) -> None:
  bob = pages_db.employees[1]
  pages_db.punch(bob, "clock_in", _ny(5, "08:15"))
  pages_db.punch(bob, "clock_out", _ny(5, "08:45"))
  earlier = [_edit(uuid4(), kind="clock_in", at=_ny(5, "07:00"), new=True)]
  assert await _save(pages_db, bob, earlier, focus=[]) == {"refused": text.ADD_CLOCK_OUT, "open": None}
