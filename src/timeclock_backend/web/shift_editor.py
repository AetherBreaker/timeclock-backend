"""The shift editor (spec 2026-11-05 §6, §8; mockups C to E, H): Python reads, re-checks and saves; the component edits.

The draft lives in the browser (`editor/shift_editor.js` over `editor/draft.js`), so editing carries on through a dropped
connection. A save sends it whole, and `save_shift` re-checks it against the punches as they stand: the same rules
in Python decide, and their numbers are the ones written (§6.9).
"""

# Standard library imports
import logging
from datetime import UTC, datetime, time, timedelta
from typing import TYPE_CHECKING, Any, Final
from urllib.parse import urlencode
from uuid import UUID

# Third party imports
import psycopg
from nicegui import app, context, ui
from nicegui.events import GenericEventArguments  # noqa: TC002 - NiceGUI evaluates a handler's annotations to call it
from timeclock_schema.enums import PunchKind
from timeclock_schema.shifts import week_bounds

# Local folder imports
from . import text
from .db import DatabaseChangedError, editor_transaction
from .gate import SignedOutError
from .rules import (
  DraftPunch,
  EntryKind,
  RefusalKind,
  apply,
  conflicts,
  entries,
  entry_hours,
  flags,
  problem,
  records,
  refusal,
  week_hours,
)
from .shell import prefs
from .store import load_context, load_history, load_punches, lock_employee, punch_time, save_edit, window_token

if TYPE_CHECKING:
  # Standard library imports
  from collections.abc import Callable, Iterable, Mapping, Sequence
  from datetime import date
  from zoneinfo import ZoneInfo

  # Third party imports
  from psycopg import AsyncConnection
  from psycopg.rows import DictRow

  # Local folder imports
  from .db import Pools
  from .gate import Gate, SignIn
  from .live import Live
  from .rules import Entry, PagePunch, Refusal
  from .settings import PageSettings
  from .store import Context, HistoryItem, Person

logger = logging.getLogger(__name__)

EPOCH: Final = datetime(1970, 1, 1, tzinfo=UTC)
# How far the browser's clock is trusted for an open shift's hours "so far" (§6.9).
AS_OF_SLACK: Final = timedelta(minutes=5)
# The browser has times to the millisecond, the database to the microsecond, so hours may differ by a few milliseconds.
HOURS_SLACK: Final = timedelta(seconds=1)


class ShiftEditor(ui.element, component="editor/shift_editor.js", esm={"shift-editor": "editor"}):
  """The editing component, below the title Python draws; `save` events carry the save payload."""

  def __init__(self, *, base: dict[str, Any], history: list[dict[str, Any]], options: dict[str, Any]) -> None:
    """Start the component on `base` (the window's punches), with the shift's `history` and the page's `options`."""
    super().__init__()
    self._props["base"] = base
    self._props["history"] = history
    self._props["options"] = options

  def set_base(self, base: dict[str, Any], history: list[dict[str, Any]]) -> None:
    """Newer punches, which the component rebases its draft onto (§6.1)."""
    self._props["base"] = base
    self._props["history"] = history
    self.update()

  def send_result(self, result: dict[str, Any]) -> None:
    """The answer to a `save`.

    A saved base and history are kept here unsent: the component takes them from the answer, and a props push would
    rebase its new draft onto that base a second time, out of order with the answer.
    """
    if "ok" in result:
      with self._props.suspend_updates():
        self._props["base"] = result["base"]
        self._props["history"] = result["history"]
    self.run_method("saveResult", result)


def ms(value: datetime | timedelta) -> int:
  """A moment as epoch milliseconds, or a duration as milliseconds, floored: the component's units."""
  return (value - EPOCH if isinstance(value, datetime) else value) // timedelta(milliseconds=1)


def _moment(value: int | None) -> datetime | None:
  return None if value is None else EPOCH + timedelta(milliseconds=value)


def punch_json(p: PagePunch) -> dict[str, Any]:
  """A punch as `editor/rules.js` takes it."""
  return {
    "id": str(p.id),
    "kind": p.kind.value,
    "at": ms(p.at),
    "automatic": p.automatic,
    "originalKind": p.original_kind.value,
    "originalAt": ms(p.original_at),
    "version": None if p.version is None else str(p.version),
    "addedOnPage": p.added_on_page,
    "instanceId": p.instance_id,
    "dbLogin": p.db_login,
    "receivedAt": None if p.received_at is None else ms(p.received_at),
    "changedBy": p.changed_by,
    "changedAt": None if p.changed_at is None else ms(p.changed_at),
  }


def base_json(
  ctx: Context,
  person: Person,
  *,
  own: bool,
  punches: Sequence[PagePunch],
  focus: Iterable[UUID | str],
  week: tuple[datetime, datetime],
  now: datetime,
  day: date | None = None,
) -> dict[str, Any]:
  """The component's `base`: the employee's window of punches and what the editor needs to preview their hours."""
  return {
    "employee": {"id": person.id, "name": person.name, "first": person.first_name, "username": person.username},
    "own": own,
    "tz": ctx.tz.key,
    "paidBreakMinutes": ctx.settings.paid_break_minutes,
    "now": ms(now),
    "week": [ms(week[0]), ms(week[1])],
    "token": window_token(punches),
    "punches": [punch_json(p) for p in punches],
    "focus": [str(i) for i in focus],
    "day": None if day is None else day.isoformat(),
  }


def history_json(items: Iterable[HistoryItem], tz: ZoneInfo, *, h24: bool) -> list[dict[str, Any]]:
  """The History card's items, newest first; `detail` is for technical details (§11)."""
  found = []
  for item in items:
    ids = [f"{c.action} {str(c.correction_id or c.punch_id)[:8]}…" for c in item.changes]
    save = [] if item.edit_id is None else [f"edit {str(item.edit_id)[:8]}…"]
    found.append(
      {
        "when": text.stamp(item.when, tz, h24=h24),
        "who": item.who,
        "lines": [text.change_line(c, tz, h24=h24) for c in item.changes],
        "note": item.note,
        "detail": " · ".join([*save, *ids, f"by {item.who}"]),
      }
    )
  return found


def draft_from_json(draft: Iterable[Mapping[str, Any]], punches: Sequence[PagePunch]) -> list[DraftPunch]:
  """The component's draft punches; a time it left alone stays the database's own, which has microseconds.

  Raises:
    KeyError, TypeError, ValueError: The draft isn't the component's shape.
  """
  by_id = {p.id: p for p in punches}
  found = []
  for d in draft:
    punch_id, sent = UUID(d["id"]), int(d["at"])
    was = None if d["new"] else by_id.get(punch_id)
    exact = [] if was is None else [t for t in (was.at, was.original_at) if ms(t) == sent]
    at = exact[0] if exact else _moment(sent)
    assert at is not None  # sent is an int
    found.append(
      DraftPunch(
        id=punch_id,
        kind=PunchKind(d["kind"]),
        at=at,
        removed=bool(d["removed"]),
        confirmed=bool(d["confirmed"]),
        version=None if d["version"] is None else UUID(d["version"]),
        new=bool(d["new"]),
      )
    )
  return found


def _refused(found: Refusal, after: Sequence[PagePunch], tz: ZoneInfo, *, h24: bool) -> str:
  by_id = {p.id: p for p in after}
  at = [by_id[i] for i in found.punch_ids]
  if found.kind is RefusalKind.STILL_IGNORED:
    listed = ", ".join(f"{text.KIND_LABEL[p.kind]} at {text.clock_time(p.at, tz, h24=h24)}" for p in at)
    many = len(at) > 1
    return f"This shift still holds {f'{len(at)} punches' if many else 'a punch'} the hours ignore: {listed}. Fix {'them' if many else 'it'} to save."
  listed = ", ".join(
    f"{text.KIND_LABEL[p.kind]} at {text.clock_time(p.at, tz, h24=h24)} on {text.long_day(p.at.astimezone(tz).date(), year=False)}"
    for p in at
  )
  if len(at) > 1:
    return f"Saving this would make the hours ignore {len(at)} other punches: {listed}. Change it so they still count."
  return f"Saving this would make the hours ignore another punch: {listed}. Change it so it still counts."


async def save_shift(  # noqa: PLR0911 - one save's checks in the order they refuse; each return is an answer
  conn: AsyncConnection[DictRow],
  *,
  signin: SignIn,
  employee_id: int,
  week: tuple[datetime, datetime],
  payload: Mapping[str, Any],
  now: datetime,
  settings: PageSettings,
  h24: bool = False,
) -> dict[str, Any]:
  """Check a save against the punches as they stand and write it, inside the caller's `editor_transaction` (§6).

  Its answer is the component's: `ok` with the new base, `changed` with the punches someone else changed meanwhile,
  `refused` in plain words, or `fault` when the browser's hours differ from these (unless it accepts these).

  Raises:
    KeyError, TypeError, ValueError: The payload isn't the component's shape.
  """
  if employee_id == signin.employee_id:  # the database refuses it too; this says why first
    return {"refused": text.OWN_SHIFT, "open": None}
  await lock_employee(conn, employee_id)  # two saves for one employee take turns, so the token check below holds
  ctx = await load_context(conn, default_tz=settings.tz)
  person = ctx.people[employee_id]
  punches = (await load_punches(conn, start=week[0], end=None, employee_id=employee_id)).get(employee_id, [])
  focus = [UUID(i) for i in payload["focus"]]

  def base(found: Sequence[PagePunch]) -> dict[str, Any]:
    return base_json(ctx, person, own=False, punches=found, focus=focus, week=week, now=now)

  draft = draft_from_json(payload["draft"], punches)
  by_id = {p.id: p for p in punches}
  if payload["token"] != window_token(punches) or conflicts(by_id, draft):
    return {"changed": base(punches)}
  found = records(by_id, draft)
  if not found:
    return {"refused": "There's nothing to save.", "open": None}
  after = apply(punches, found)
  sent = _moment(int(payload["asOf"]))
  as_of = sent if sent is not None and abs(sent - now) <= AS_OF_SLACK else now
  touched = set(focus) | {d.id for d in draft}
  if (refused := refusal(punches, after, touched, as_of=as_of)) is not None:
    return {"refused": _refused(refused, after, ctx.tz, h24=h24), "open": None}
  if not payload.get("acceptServer"):
    preview = payload["preview"]
    shown = [e for e in entries(after, as_of=as_of) if e.kind is EntryKind.SHIFT and any(p.id in touched for p in e.punches)]
    for e in shown:
      hours = entry_hours(e, after, settings=ctx.settings, tz=ctx.tz, now=as_of)
      assert hours is not None  # a shift's
      theirs = preview["entries"].get(str(e.key))
      ours = (hours.worked, hours.paid_break, hours.unpaid)
      if theirs is None or any(abs(timedelta(milliseconds=t) - o) > HOURS_SLACK for t, o in zip(theirs, ours, strict=True)):
        what = f"{person.name}'s shift on {text.long_day(e.start.astimezone(ctx.tz).date())}"
        return {
          "fault": {
            "what": what,
            "preview": "none" if theirs is None else text.hm(timedelta(milliseconds=theirs[0])),
            "server": text.hm(hours.worked),
          }
        }
    worked = week_hours(after, settings=ctx.settings, tz=ctx.tz, week=week, now=as_of).worked
    if abs(timedelta(milliseconds=preview["week"]) - worked) > HOURS_SLACK:
      what = f"{person.first_name}'s week of {text.week_label(week[0].astimezone(ctx.tz).date())}"
      return {"fault": {"what": what, "preview": text.hm(timedelta(milliseconds=preview["week"])), "server": text.hm(worked)}}
  note = str(payload.get("note", "")).strip()
  await save_edit(conn, employee_id=employee_id, records=found, note=note)
  saved = (await load_punches(conn, start=week[0], end=None, employee_id=employee_id)).get(employee_id, [])
  return {"ok": True, "base": base(saved)}


def _entry_holding(punches: Sequence[PagePunch], punch_id: UUID, *, as_of: datetime) -> Entry | None:
  return next((e for e in entries(punches, as_of=as_of) if any(p.id == punch_id for p in e.punches)), None)


async def draw_shift_editor(  # noqa: C901, PLR0915 - one page: its read, its title, its component and its save
  signin: SignIn,
  *,
  settings: PageSettings,
  pools: Pools,
  gate: Gate,
  live: Live,
  now: Callable[[], datetime],
  query: Mapping[str, str],
) -> None:
  """Draw `/shift?employee=<id>&punch=<id>`: the entry holding that punch, in the window from its week on."""
  storage, session, client = app.storage.user, app.storage.browser, context.client
  chosen = prefs()
  h24, technical = chosen["h24"], chosen["technical"]
  employee = int(v) if (v := query.get("employee", "")).isdigit() else None
  try:
    punch_id = UUID(query.get("punch", ""))
  except ValueError:
    punch_id = None
  body = ui.column().classes("page-body shift-editor")

  def crumb(week_start: date | None) -> None:
    back = "/" if week_start is None else f"/?{urlencode({'week': week_start.isoformat()})}"
    with ui.link(target=back).classes("crumb").mark("crumb"):
      ui.element("span").classes("page-icon icon-arrow-back")
      ui.label("Shifts")

  def gone(week_start: date | None) -> None:
    with body:
      crumb(week_start)
      with ui.row().classes("callout grey"):
        ui.element("span").classes("page-icon icon-info")
        ui.label("This shift has changed since the list was drawn.")

  if employee is None or punch_id is None:
    gone(None)
    return
  try:
    async with editor_transaction(pools, gate, storage, session, test_database=signin.test_database, touch=False) as conn:
      ctx = await load_context(conn, default_tz=settings.tz)
      at = await punch_time(conn, employee_id=employee, punch_id=punch_id)
      week = None if at is None else week_bounds(at, ctx.tz)
      punches = [] if week is None else (await load_punches(conn, start=week[0], end=None, employee_id=employee)).get(employee, [])
      opened = now()
      entry = _entry_holding(punches, punch_id, as_of=opened)
      span: tuple[datetime, datetime] | None = None
      history: list[HistoryItem] = []
      if entry is not None:
        first, last = entry.start.astimezone(ctx.tz).date(), (entry.end or opened).astimezone(ctx.tz).date()
        span = (datetime.combine(first, time(0), tzinfo=ctx.tz), datetime.combine(last + timedelta(days=1), time(0), tzinfo=ctx.tz))
        history = await load_history(conn, employee_id=employee, start=span[0], end=span[1])
  except SignedOutError:
    ui.navigate.to("/signin")
    return
  except DatabaseChangedError:
    ui.navigate.reload()
    return
  except psycopg.OperationalError:
    logger.warning("the shift editor can't be read", exc_info=True)
    with body, ui.row().classes("callout orange"):
      ui.element("span").classes("page-icon icon-warning")
      ui.label(text.DB_DOWN)
    return

  person = ctx.people.get(employee)
  if entry is None or person is None or week is None or span is None:
    gone(None if week is None else week[0].astimezone(ctx.tz).date())
    return

  tz = ctx.tz
  own = employee == signin.employee_id
  day = entry.start.astimezone(tz).date()
  with body:
    crumb(week[0].astimezone(tz).date())
    with ui.row().classes("title"):
      ui.label(person.name).classes("title-name")
      ui.label(f"ID {person.username}").classes("eid")
      ui.label(text.long_day(day)).classes("title-day")

      @ui.refreshable
      def chips(shown: Entry) -> None:
        for flag in flags(shown):
          ui.label(text.flag_label(flag, problem(shown), len(shown.ignored))).classes(f"chip {text.CHIP[flag]}")

      chips(entry)
    if technical:
      ui.label(text.employee_details(person, ctx.groups, tz, h24=h24)).classes("empdet")
    if own:
      with ui.row().classes("callout grey"):
        ui.element("span").classes("page-icon icon-lock")
        ui.label(text.OWN_SHIFT)
    database = "test" if signin.test_database else "production"
    editor = ShiftEditor(
      base=base_json(ctx, person, own=own, punches=punches, focus=[p.id for p in entry.punches], week=week, now=opened),
      history=history_json(history, tz, h24=h24),
      options={
        "h24": h24,
        "technical": technical,
        "contact": settings.fault_contact,
        "storageKey": f"timeclock-draft:{signin.email}:{database}:{employee}:{entry.key}",
        "add": False,
        "back": f"/?{urlencode({'week': week[0].astimezone(tz).date().isoformat()})}",
      },
    )
  token = editor.props["base"]["token"]
  focus = {p.id for p in entry.punches}

  async def save(e: GenericEventArguments) -> None:
    nonlocal token
    payload = e.args if isinstance(e.args, dict) else e.args[0]
    result: dict[str, Any]
    fresh: list[PagePunch] = []
    try:
      async with editor_transaction(pools, gate, storage, session, test_database=signin.test_database) as conn:
        result = await save_shift(
          conn, signin=signin, employee_id=employee, week=week, payload=payload, now=now(), settings=settings, h24=h24
        )
        if "ok" in result:  # read in the save's transaction, so they hold the save
          fresh = (await load_punches(conn, start=week[0], end=None, employee_id=employee)).get(employee, [])
          items = await load_history(conn, employee_id=employee, start=span[0], end=span[1])
          result["history"] = history_json(items, tz, h24=h24)
    except SignedOutError:
      result = {"signedOut": True}
    except DatabaseChangedError:
      result = {"reload": True}
    except psycopg.OperationalError:
      logger.warning("a save couldn't reach the database", exc_info=True)
      result = {"error": text.DB_DOWN}
    except KeyError, TypeError, ValueError, psycopg.Error:  # a malformed draft, or one the database refused
      logger.exception("a save failed")
      result = {"error": text.SAVE_FAILED}
    except Exception:
      editor.send_result({"error": text.SAVE_FAILED})  # a bug: answered so the editor isn't left saving, then alerted
      raise
    if "ok" in result:
      token = result["base"]["token"]
      if (held := next((e for e in entries(fresh, as_of=now()) if focus & {p.id for p in e.punches}), None)) is not None:
        chips.refresh(held)
    elif "changed" in result:
      token = result["changed"]["token"]
    editor.send_result(result)

  editor.on("save", save)

  async def woken() -> None:
    nonlocal token
    try:
      async with editor_transaction(pools, gate, storage, session, test_database=signin.test_database, touch=False) as conn:
        fresh = (await load_punches(conn, start=week[0], end=None, employee_id=employee)).get(employee, [])
        if window_token(fresh) == token:
          return
        items = await load_history(conn, employee_id=employee, start=span[0], end=span[1])
    except SignedOutError:
      with client:
        ui.navigate.to("/signin")
      return
    except DatabaseChangedError:
      with client:
        ui.navigate.reload()
      return
    except psycopg.OperationalError:
      logger.warning("the shift editor's live update can't reach the database", exc_info=True)  # the next wake tries again
      return
    token = window_token(fresh)
    with client:
      editor.set_base(
        base_json(ctx, person, own=own, punches=fresh, focus=list(focus), week=week, now=now()), history_json(items, tz, h24=h24)
      )
      if (held := next((e for e in entries(fresh, as_of=now()) if focus & {p.id for p in e.punches}), None)) is not None:
        chips.refresh(held)

  client.on_delete(live.subscribe(test=signin.test_database, callback=woken))
