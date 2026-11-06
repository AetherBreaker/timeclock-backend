"""The shift list (spec 2026-11-05 §7; mockup screens B and I): one week of every employee's shifts, kept live."""

# Standard library imports
import asyncio
import logging
from datetime import date, datetime, time, timedelta
from functools import partial
from typing import TYPE_CHECKING, Any, Final
from urllib.parse import urlencode

# Third party imports
import psycopg
from nicegui import app, context, ui
from nicegui.elements.mixins.text_element import TextElement
from timeclock_schema.enums import EmployeeRole, PunchKind

# Local folder imports
from . import text
from .db import DatabaseChangedError, editor_transaction
from .gate import SignedOutError
from .rules import EntryKind, Flag, entries, entry_hours, flags, problem, week_hours
from .shell import prefs
from .store import load_context, load_punches

if TYPE_CHECKING:
  # Standard library imports
  from collections.abc import Callable, Mapping

  # Third party imports
  from nicegui.events import ValueChangeEventArguments

  # Local folder imports
  from .db import Pools
  from .gate import Gate, SignIn
  from .live import Live
  from .rules import Entry, PagePunch
  from .settings import PageSettings
  from .store import Context, Person

logger = logging.getLogger(__name__)

# The attention bar's counts, in the mockup's order: automatic clock-outs, then what needs fixing.
_ATTENTION_ORDER: Final = ("automatic", *(f"extra:{kind}" for kind in PunchKind), "stray", "mixed")


async def draw_shift_list(  # noqa: C901, PLR0915 - one page: its state, its reads and its drawing, top to bottom
  signin: SignIn,
  *,
  settings: PageSettings,
  pools: Pools,
  gate: Gate,
  live: Live,
  now: Callable[[], datetime],
  query: Mapping[str, str],
) -> None:
  """Draw the list below the frame, from the `/` URL's `week`, `employee`, `group`, `role` and `attention`."""
  # Captured now: a live update runs outside any request, where app.storage can't tell whose browser it is.
  storage, session, client = app.storage.user, app.storage.browser, context.client
  chosen = prefs()
  h24, technical = chosen["h24"], chosen["technical"]
  try:
    week: date | None = date.fromisoformat(query.get("week", ""))
  except ValueError:
    week = None
  employee = int(v) if (v := query.get("employee", "")).isdigit() else None
  group = int(v) if (v := query.get("group", "")).isdigit() else None
  role = v if (v := query.get("role")) in set(EmployeeRole) else None
  attention = query.get("attention") or None
  # The last read: (context, punches by employee, the week's Monday, its bounds, the read's now); None until one works.
  data: tuple[Context, dict[int, list[PagePunch]], date, datetime, datetime, datetime] | None = None
  down = False
  reading = asyncio.Lock()  # a live wake and a click read in turn, so an older read never lands over a newer one

  def address() -> str:
    params = {"week": week, "employee": employee, "group": group, "role": role, "attention": attention}
    present = {k: str(v) for k, v in params.items() if v is not None}
    return f"/?{urlencode(present)}" if present else "/"

  async def reread(*, touch: bool) -> None:
    nonlocal data, down, week
    async with reading:
      try:
        async with editor_transaction(pools, gate, storage, session, test_database=signin.test_database, touch=touch) as conn:
          ctx = await load_context(conn, default_tz=settings.tz)
          at = now()
          day = week or at.astimezone(ctx.tz).date()
          monday = day - timedelta(days=day.weekday())
          start = datetime.combine(monday, time(0), tzinfo=ctx.tz)
          end = datetime.combine(monday + timedelta(days=7), time(0), tzinfo=ctx.tz)  # its own midnight: DST weeks differ
          data, down = (ctx, await load_punches(conn, start=start, end=end), monday, start, end, at), False
          if week is not None:
            week = monday
      except SignedOutError:
        ui.navigate.to("/signin")
        return
      except DatabaseChangedError:
        ui.navigate.reload()
        return
      except psycopg.OperationalError:
        logger.warning("the shift list can't be read", exc_info=True)  # shown; the next live wake or reload tries again
        down = True
    if data is not None:
      ctx = data[0]
      week_label.set_text(text.week_label(data[2]))
      people = sorted(ctx.people.values(), key=lambda p: p.sort_key)
      options: list[tuple[ui.select, dict[Any, str]]] = [
        (employee_select, {None: "All employees", **{p.id: f"{p.name} · ID {p.username}" for p in people}}),
        (group_select, {None: "All groups", **dict(sorted(ctx.groups.items(), key=lambda g: g[1].casefold()))}),
      ]
      for select, wanted in options:
        if select.options != wanted:
          select.set_options(wanted, value=select.value if select.value in wanted else None)
    await rows.refresh()  # awaited: an unawaited one runs later, so the first read's would redraw the first drawing

  async def go(to: date | None) -> None:
    nonlocal week
    week = to
    ui.navigate.history.replace(address())
    await reread(touch=True)

  def step(days: int) -> Callable[[], Any]:
    return lambda: go((week or (data[2] if data else now().astimezone(settings.tz).date())) + timedelta(days=days))

  def choose(name: str, e: ValueChangeEventArguments) -> None:
    nonlocal employee, group, role
    match name:
      case "employee":
        employee = e.value
      case "group":
        group = e.value
      case _:
        role = e.value
    ui.navigate.history.replace(address())
    rows.refresh()

  def focus(key: str | None) -> None:
    nonlocal attention
    attention = key
    ui.navigate.history.replace(address())
    rows.refresh()

  with ui.column().classes("page-body shift-list"):
    with ui.row().classes("ctl"):
      ui.label("Shifts").classes("ctl-title")
      with ui.row().classes("wk"):
        with ui.button(on_click=step(-7)).props("flat dense round").mark("week-back"):
          ui.element("span").classes("page-icon icon-back")
        week_label = ui.label().classes("wk-label").mark("week-label")
        with ui.button(on_click=step(7)).props("flat dense round").mark("week-next"):
          ui.element("span").classes("page-icon icon-next")
      ui.button("This week", on_click=lambda: go(None)).props("flat no-caps").classes("link-button").mark("this-week")
      if technical:
        with ui.row().classes("chip blue"):
          ui.element("span").classes("page-icon icon-code")
          ui.label("Technical details on")
    with ui.row().classes("ctl"):
      employee_select = (
        ui.select({None: "All employees"}, value=employee, label="Employee", on_change=lambda e: choose("employee", e))
        .props("outlined dense options-dense")
        .classes("filter")
        .mark("filter-employee")
      )
      group_select = (
        ui.select({None: "All groups"}, value=group, label="Group", on_change=lambda e: choose("group", e))
        .props("outlined dense options-dense")
        .classes("filter")
        .mark("filter-group")
      )
      roles = {None: "All roles", **{r.value: r.value.capitalize() for r in EmployeeRole}}
      ui.select(roles, value=role, label="Role", on_change=lambda e: choose("role", e)).props("outlined dense options-dense").classes(
        "filter"
      ).mark("filter-role")

    @ui.refreshable
    def rows() -> None:  # noqa: C901, PLR0912, PLR0915 - the list's one drawing pass
      if down or data is None:
        with ui.row().classes("callout orange"):
          ui.element("span").classes("page-icon icon-warning")
          ui.label(text.DB_DOWN)
        return
      ctx, punches, _, start, end, at = data
      tz = ctx.tz
      # Each shown employee's entries meeting the week: one starting in it, or one running into it.
      shown: list[tuple[Person, list[PagePunch], list[tuple[Entry, tuple[Flag, ...], set[str]]]]] = []
      for employee_id, theirs in punches.items():
        person = ctx.people[employee_id]
        if (employee is not None and person.id != employee) or (group is not None and person.group_id != group):
          continue
        if role is not None and person.role != role:
          continue
        found = []
        for e in entries(theirs, as_of=at):
          if e.start < end and (e.start >= start or (e.end or at) > start):
            found_flags = flags(e)
            keys = {k for k in (problem(e), "automatic" if Flag.AUTOMATIC in found_flags else None) if k is not None}
            found.append((e, found_flags, keys))
        if found:
          shown.append((person, theirs, found))
      shown.sort(key=lambda s: s[0].sort_key)

      counts: dict[str, int] = {}
      needing = 0
      for _, _, found in shown:
        for _, _, keys in found:
          needing += bool(keys)
          for k in keys:
            counts[k] = counts.get(k, 0) + 1
      if needing or attention is not None:
        with ui.row().classes("callout orange attention").mark("attention-bar"):
          ui.element("span").classes("page-icon icon-warning")
          with ui.row().classes("attention-counts"):
            ui.label(text.attention_headline(needing) if needing else "Nothing needs attention this week.").classes("attention-lead")
            for k in sorted(counts, key=_ATTENTION_ORDER.index):
              color = "orange" if k == "automatic" else "red"
              ui.button(text.attention_label(k, counts[k]), on_click=partial(focus, None if attention == k else k)).props(
                "flat dense no-caps"
              ).classes(f"chip {color}{' on' * (attention == k)}").mark(f"attention-{k}")
          ui.switch(
            "Only shifts that need attention",
            value=attention == "all",
            on_change=lambda e: focus("all" if e.value else None),
          ).classes("toggle").mark("attention-only")

      drawn = 0
      for person, theirs, found in shown:
        visible = [f for f in found if attention is None or (f[2] if attention == "all" else attention in f[2])]
        if not visible:
          continue
        drawn += 1
        own = person.id == signin.employee_id
        with ui.element("div").classes("card").mark("employee-card", f"employee-{person.id}"):
          with ui.row().classes("emp"):
            ui.label(person.name).classes("nm" + " own" * own)
            ui.label(f"ID {person.username}").classes("eid")
            if own:
              with ui.row().classes("chip grey").mark(f"own-{person.id}"):
                ui.element("span").classes("page-icon icon-lock")
                ui.label("You: another editor changes these")
            total = week_hours(theirs, settings=ctx.settings, tz=tz, week=(start, end), now=at)
            with ui.row().classes("tot"):
              ui.label("Week:")
              ui.label(text.hm(total.worked)).classes("tot-hours").mark(f"week-total-{person.id}")
              ui.label("worked")
          if technical:
            ui.label(text.employee_details(person, ctx.groups, tz, h24=h24)).classes("empdet")
          with ui.element("table").classes("sh"):
            with ui.element("tr"):
              for heading in ("Day", "Clock in", "Clock out", "Breaks", "Lunch"):
                TextElement(tag="th", text=heading)
              TextElement(tag="th", text="Worked").classes("num")
              if technical:
                TextElement(tag="th", text="Clock")
                TextElement(tag="th", text="Punches")
              TextElement(tag="th", text="")
            for e, found_flags, _ in visible:
              edge = " red" if Flag.NEEDS_FIXING in found_flags else " orange" if Flag.AUTOMATIC in found_flags else ""
              link = f"/shift?{urlencode({'employee': person.id, 'punch': e.key})}"
              row = ui.element("tr").classes(f"r{edge}{' mine' * own}").mark(f"row-{e.key}")
              row.on("click", partial(ui.navigate.to, link))
              with row:
                TextElement(tag="td", text=text.day_label(e.start.astimezone(tz).date()))
                if e.kind is EntryKind.STRAY or e.shift is None:
                  listed = ", ".join(f"{text.KIND_LABEL[p.kind]} {text.clock_time(p.at, tz, h24=h24)}" for p in e.punches)
                  TextElement(tag="td", text=listed).props("colspan=4")
                  TextElement(tag="td", text="none").classes("num muted")
                else:
                  shift = e.shift
                  TextElement(tag="td", text=text.clock_time(shift.clock_in.at, tz, h24=h24))
                  if shift.clock_out is None:
                    TextElement(tag="td", text="working").classes("muted")
                  else:
                    TextElement(tag="td", text=text.clock_time(shift.clock_out.at, tz, h24=h24))
                  # Breaks as "<n> · h:mm"; lunch as "h:mm", with its count only when there are several.
                  for spans, bare in ((shift.breaks, False), (shift.lunches, True)):
                    length = text.hm(sum(((s.end or at) - s.start for s in spans), timedelta()))
                    shown_length = "" if not spans else length if bare and len(spans) == 1 else f"{len(spans)} · {length}"
                    TextElement(tag="td", text=shown_length)
                  hours = entry_hours(e, theirs, settings=ctx.settings, tz=tz, now=at)
                  assert hours is not None  # a shift's
                  so_far = " so far" if shift.clock_out is None else ""
                  TextElement(tag="td", text=f"{text.hm(hours.worked)}{so_far}").classes("num")
                if technical:
                  TextElement(tag="td", text=" · ".join(dict.fromkeys(p.instance_id for p in e.punches))).classes("mono")
                  notes = [
                    f"{n} {word}"
                    for n, word in (
                      (len(e.ignored), "ignored"),
                      (sum(p.automatic for p in e.punches), "automatic"),
                      (sum(p.version is not None for p in e.punches), "corrected"),
                    )
                    if n
                  ]
                  counted = f"{len(e.punches)} ({', '.join(notes)})" if notes else str(len(e.punches))
                  TextElement(tag="td", text=counted).classes("mono")
                with ui.element("td").classes("st"), ui.row().classes("chips"):
                  if own:
                    ui.element("span").classes("page-icon icon-lock")
                  for flag in found_flags:
                    label = text.flag_label(flag, problem(e), len(e.ignored))
                    ui.label(label).classes(f"chip {text.CHIP[flag]}")
      if not drawn:
        ui.label("No shifts this week." if not shown else "No shifts need attention this week.").classes("muted empty")

    await reread(touch=False)  # the page load just counted as activity
    rows()

  async def woken() -> None:
    with client:
      await reread(touch=False)  # a change someone else made isn't this user's activity

  client.on_delete(live.subscribe(test=signin.test_database, callback=woken))
