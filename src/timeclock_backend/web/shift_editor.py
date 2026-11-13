"""The shift editor (spec 2026-11-05 §6, §8; mockups C to E, H): Python reads, re-checks and saves; the component edits.

The draft lives in the browser (`editor/shift_editor.js` over `editor/draft.js`), so editing carries on through a dropped
connection. A save sends it whole, and `save_shift` re-checks it against the punches as they stand: the same rules
in Python decide, and their numbers are the ones written (§6.9).
"""

# Standard library imports
import logging
from datetime import UTC, date, datetime, time, timedelta
from typing import TYPE_CHECKING, Any, Final
from urllib.parse import parse_qsl, urlencode
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
  overlapping,
  problem,
  records,
  refusal,
  week_hours,
)
from .shell import prefs
from .store import load_context, load_history, load_punches, lock_employee, punch_time, save_edit, window_token

if TYPE_CHECKING:
  # Standard library imports
  from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
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
    """The answer to a `save`; on ok the component then goes back to the list, so nothing of it is kept here."""
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
  """The component's `base`: the employee's window of punches and what the editor needs to preview their hours.

  `timelineDays` is the least a timeline shows, per local day of the week (owner, 2026-11-11): from an hour before
  opening to the later of closing and the automatic clock-out's stamp. A day with no hours of its own (the weekend)
  takes the week's earliest opening and latest closing; with no hours at all only the automatic clock-out holds the end.
  """
  stamp = [ctx.settings.auto_clock_out_stamp_time] if ctx.settings.auto_clock_out_enabled else []
  first = week[0].astimezone(ctx.tz).date()
  days: dict[str, list[int | None]] = {}
  for d in (first + timedelta(days=n) for n in range((week[1].astimezone(ctx.tz).date() - first).days)):
    hours = [h for h in ctx.hours if h.weekday == d.isoweekday()] or ctx.hours
    if ends := [*(h.closes for h in hours), *stamp]:
      opens = min((h.opens for h in hours), default=None)
      days[d.isoformat()] = [
        None if opens is None else ms(datetime.combine(d, opens, tzinfo=ctx.tz) - timedelta(hours=1)),
        ms(datetime.combine(d, max(ends), tzinfo=ctx.tz)),
      ]
  return {
    "employee": {"id": person.id, "name": person.name, "first": person.first_name, "username": person.username},
    "own": own,
    "tz": ctx.tz.key,
    "paidBreakMinutes": ctx.settings.paid_break_minutes,
    "breakLimitMinutes": ctx.settings.break_limit_minutes,
    "breaksEnabled": ctx.settings.breaks_enabled,
    "now": ms(now),
    "week": [ms(week[0]), ms(week[1])],
    "token": window_token(punches),
    "punches": [punch_json(p) for p in punches],
    "focus": [str(i) for i in focus],
    "day": None if day is None else day.isoformat(),
    "timelineDays": days,
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


async def save_shift(  # noqa: C901, PLR0911 - one save's checks in the order they refuse; each return is an answer
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
  if not focus:  # adding a shift (§6.13): one meeting a shift the employee has is refused, with a link to that shift
    new = {d.id for d in draft if d.new}
    added = [p for p in after if p.id in new]
    if added and (met := overlapping(punches, added[0].at, added[-1].at, as_of=as_of)) is not None:
      when = f"{text.long_day(met.start.astimezone(ctx.tz).date(), year=False)}, from {text.clock_time(met.start, ctx.tz, h24=h24)}"
      end = "now" if met.end is None else text.clock_time(met.end, ctx.tz, h24=h24)
      return {
        "refused": f"This overlaps {person.first_name}'s shift on {when} to {end}. Change the times, or open that shift instead.",
        "open": f"/shift?{urlencode({'employee': employee_id, 'punch': met.punches[0].id})}",
      }
    # Left open, it would run on to now and the next automatic clock-out: only someone working now may be added so.
    if added and all(p.kind is not PunchKind.CLOCK_OUT for p in added):
      start = added[0].at
      if start.astimezone(ctx.tz).date() != as_of.astimezone(ctx.tz).date() or any(p.at > start for p in punches):
        return {"refused": text.ADD_CLOCK_OUT, "open": None}
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


def _entry_holding(punches: Sequence[PagePunch], ids: Collection[UUID], *, as_of: datetime) -> Entry | None:
  return next((e for e in entries(punches, as_of=as_of) if any(p.id in ids for p in e.punches)), None)


async def draw_shift_editor(  # noqa: C901, PLR0912, PLR0915 - one page in two modes: its read, title, component and save
  signin: SignIn,
  *,
  settings: PageSettings,
  pools: Pools,
  gate: Gate,
  live: Live,
  now: Callable[[], datetime],
  query: Mapping[str, str],
  add: bool = False,
) -> None:
  """Draw `/shift?employee=<id>&punch=<id>`: the entry holding that punch, in the window from its week on.

  With `add`, `/shift/new?employee=<id>&date=<YYYY-MM-DD>` (§6.13): pickers for both, each choice a page load, then the
  editor empty over that day's week. A saved shift then opens as any other.
  """
  storage, session, client = app.storage.user, app.storage.browser, context.client
  chosen = prefs()
  h24, technical = chosen["h24"], chosen["technical"]
  employee = int(v) if (v := query.get("employee", "")).isdigit() else None
  try:
    punch_id = None if add else UUID(query.get("punch", ""))
    day = date.fromisoformat(query["date"]) if add and query.get("date") else None
  except ValueError:
    punch_id = day = None
  body = ui.column().classes("page-body shift-editor")
  # The list as it was left (its rows and Add shift pass their query as `back`), where the crumb and a save go back to;
  # without one, the shift's week.
  listed = None if "back" not in query else f"/?{q}" if (q := urlencode(parse_qsl(query["back"]))) else "/"

  def crumb(week_start: date | None) -> None:
    back = listed or ("/" if week_start is None else f"/?{urlencode({'week': week_start.isoformat()})}")
    with ui.link(target=back).classes("crumb").mark("crumb"):
      ui.element("span").classes("page-icon icon-arrow-back")
      ui.label("Shifts")

  def gone(week_start: date | None) -> None:
    with body:
      crumb(week_start)
      with ui.row().classes("callout grey"):
        ui.element("span").classes("page-icon icon-info")
        ui.label("This shift has changed since the list was drawn.")

  if not add and (employee is None or punch_id is None):
    gone(None)
    return
  try:
    async with editor_transaction(pools, gate, storage, session, test_database=signin.test_database, touch=False) as conn:
      ctx = await load_context(conn, default_tz=settings.tz)
      opened = now()
      if add:  # your own shifts aren't yours to add (§6.12), so you aren't offered
        chose = employee is not None and employee != signin.employee_id
        week = week_bounds(datetime.combine(day, time(12), tzinfo=ctx.tz), ctx.tz) if chose and day is not None else None
      else:
        assert employee is not None and punch_id is not None
        at = await punch_time(conn, employee_id=employee, punch_id=punch_id)
        week = None if at is None else week_bounds(at, ctx.tz)
      punches = (
        []
        if week is None or employee is None
        else (await load_punches(conn, start=week[0], end=None, employee_id=employee)).get(employee, [])
      )
      entry = None if punch_id is None else _entry_holding(punches, {punch_id}, as_of=opened)
      span: tuple[datetime, datetime] | None = None
      history: list[HistoryItem] = []
      if entry is not None and employee is not None:
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

  tz = ctx.tz
  person = None if employee is None else ctx.people.get(employee)
  if add:
    with body:
      crumb(None if week is None else week[0].astimezone(tz).date())
      ui.label("Add a shift").classes("title-name")
      others = {
        p.id: f"{p.name} · ID {p.username}"
        for p in sorted(ctx.people.values(), key=lambda p: p.sort_key)
        if p.id != signin.employee_id
      }

      def pick(**changed: object) -> None:
        wanted = {"employee": employee, "date": None if day is None else day.isoformat(), "back": query.get("back")} | changed
        ui.navigate.to(f"/shift/new?{urlencode({k: v for k, v in wanted.items() if v is not None})}")

      with ui.row().classes("ctl"):
        ui.select(
          others, value=employee if employee in others else None, label="Employee", on_change=lambda e: pick(employee=e.value)
        ).props("outlined dense options-dense").classes("filter wide").mark("add-employee")
        # The browser's own date field: its value is empty until a whole date is in it.
        ui.input("Date", value="" if day is None else day.isoformat(), on_change=lambda e: e.value and pick(date=e.value)).props(
          "type=date outlined dense stack-label"
        ).classes("filter").mark("add-date")
    if week is None or person is None or day is None:
      return
  elif entry is None or person is None or week is None or span is None:
    gone(None if week is None else week[0].astimezone(tz).date())
    return

  own = employee == signin.employee_id
  opened_ids = [] if entry is None else [p.id for p in entry.punches]  # in punch order, as the base sends them
  focus = set(opened_ids)
  database = "test" if signin.test_database else "production"
  stored_as = f"timeclock-draft:{signin.email}:{database}:{employee}:"
  key = None if entry is None else entry.key  # the address's punch, and the stored draft's name
  back = listed or f"/?{urlencode({'week': week[0].astimezone(tz).date().isoformat()})}"
  shown_day = day if entry is None else entry.start.astimezone(tz).date()
  assert shown_day is not None  # add mode returned above without a day
  with body:
    if entry is not None:
      crumb(week[0].astimezone(tz).date())
      with ui.row().classes("title"):
        ui.label(person.name).classes("title-name")
        ui.label(f"ID {person.username}").classes("eid")
        ui.label(text.long_day(entry.start.astimezone(tz).date())).classes("title-day")

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
    editor = ShiftEditor(
      base=base_json(ctx, person, own=own, punches=punches, focus=opened_ids, week=week, now=opened, day=day),
      history=history_json(history, tz, h24=h24),
      options={
        "h24": h24,
        "technical": technical,
        "contact": settings.fault_contact,
        "storageKey": f"{stored_as}{f'new:{day}' if key is None else key}",
        "add": add,
        "back": back,
      },
    )
  token = editor.props["base"]["token"]

  async def save(e: GenericEventArguments) -> None:
    nonlocal token
    payload = e.args if isinstance(e.args, dict) else e.args[0]
    result: dict[str, Any]
    try:
      async with editor_transaction(pools, gate, storage, session, test_database=signin.test_database) as conn:
        result = await save_shift(
          conn, signin=signin, employee_id=person.id, week=week, payload=payload, now=now(), settings=settings, h24=h24
        )
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
      storage["saved"] = f"Saved: {person.name}, {text.day_label(shown_day)}."  # said by the list it goes back to
    elif "changed" in result:
      token = result["changed"]["token"]
    editor.send_result(result)  # on ok the component drops the browser's stored draft, then goes back to the list

  editor.on("save", save)

  async def woken() -> None:
    nonlocal token
    try:
      async with editor_transaction(pools, gate, storage, session, test_database=signin.test_database, touch=False) as conn:
        fresh = (await load_punches(conn, start=week[0], end=None, employee_id=person.id)).get(person.id, [])
        if window_token(fresh) == token:
          return
        items = [] if span is None else await load_history(conn, employee_id=person.id, start=span[0], end=span[1])
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
        base_json(ctx, person, own=own, punches=fresh, focus=opened_ids, week=week, now=now(), day=day),
        history_json(items, tz, h24=h24),
      )
      if entry is not None and (shown := _entry_holding(fresh, focus, as_of=now())) is not None:
        chips.refresh(shown)

  client.on_delete(live.subscribe(test=signin.test_database, callback=woken))
