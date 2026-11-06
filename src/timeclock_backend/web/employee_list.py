"""The Employees list (spec 2026-11-05 §9; mockup screen J): everyone, searchable and filtered, kept live."""

# Standard library imports
import asyncio
import logging
from functools import partial
from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode

# Third party imports
import psycopg
from nicegui import app, context, ui
from nicegui.elements.mixins.text_element import TextElement
from timeclock_schema.enums import EmployeeRole

# Local folder imports
from . import text
from .db import DatabaseChangedError, editor_transaction
from .gate import SignedOutError
from .shell import prefs
from .store import load_context

if TYPE_CHECKING:
  # Standard library imports
  from collections.abc import Mapping

  # Local folder imports
  from .db import Pools
  from .gate import Gate, SignIn
  from .live import Live
  from .settings import PageSettings
  from .store import Context, Person

logger = logging.getLogger(__name__)


def _matches(person: Person, search: str) -> bool:
  """Any part of the name, any case, or the start of the username (the ID the clock asks for)."""
  wanted = search.strip().casefold()
  names = (person.name, " ".join(n for n in (person.first_name, person.middle_name, person.last_name) if n))
  return not wanted or any(wanted in n.casefold() for n in names) or person.username.startswith(search.strip())


def _no_fingerprint(person: Person) -> bool:
  """Can't clock in with a finger: active, not exempt, and no print. A switched-off employee can't clock in anyway."""
  return person.active and not person.fingerprint_exempt and person.fingerprints == 0


async def draw_employee_list(  # noqa: C901, PLR0915 - one page: its state, its reads and its drawing, top to bottom
  signin: SignIn,
  *,
  settings: PageSettings,
  pools: Pools,
  gate: Gate,
  live: Live,
  query: Mapping[str, str],
) -> None:
  """Draw the list below the frame, from the `/employees` URL's `q`, `group`, `role` and `off`."""
  # Captured now: a live update runs outside any request, where app.storage can't tell whose browser it is.
  storage, session, client = app.storage.user, app.storage.browser, context.client
  chosen = prefs()
  h24, technical = chosen["h24"], chosen["technical"]
  search = query.get("q", "")
  group = int(v) if (v := query.get("group", "")).isdigit() else None
  role = v if (v := query.get("role")) in set(EmployeeRole) else None
  switched_off = query.get("off") == "1"
  data: Context | None = None
  down = False
  reading = asyncio.Lock()  # a live wake and the first read take turns, so an older read never lands over a newer one

  def address() -> str:
    params = {"q": search or None, "group": group, "role": role, "off": "1" if switched_off else None}
    present = {k: str(v) for k, v in params.items() if v is not None}
    return f"/employees?{urlencode(present)}" if present else "/employees"

  async def reread(*, touch: bool) -> None:
    nonlocal data, down, group
    async with reading:
      try:
        async with editor_transaction(pools, gate, storage, session, test_database=signin.test_database, touch=touch) as conn:
          data, down = await load_context(conn, default_tz=settings.tz), False
      except SignedOutError:
        ui.navigate.to("/signin")
        return
      except DatabaseChangedError:
        ui.navigate.reload()
        return
      except psycopg.OperationalError:
        logger.warning("the employee list can't be read", exc_info=True)  # the next wake tries again
        down = True
    if data is not None:
      wanted = {None: "All groups", **dict(sorted(data.groups.items(), key=lambda g: g[1].casefold()))}
      group = group if group in wanted else None  # one in the address that's gone since
      if group_select.options != wanted:
        group_select.set_options(wanted, value=group)
    await rows.refresh()

  def choose(name: str, value: Any) -> None:
    nonlocal search, group, role, switched_off
    match name:
      case "q":
        search = value or ""
      case "group":
        group = value
      case "role":
        role = value
      case _:
        switched_off = bool(value)
    ui.navigate.history.replace(address())
    rows.refresh()

  with ui.column().classes("page-body employee-list"):
    with ui.row().classes("ctl"):
      ui.label("Employees").classes("ctl-title")
      if technical:
        with ui.row().classes("chip blue"):
          ui.element("span").classes("page-icon icon-code")
          ui.label("Technical details on")
    with ui.row().classes("ctl"):
      ui.input("Search", placeholder="Name or ID", value=search, on_change=lambda e: choose("q", e.value)).props(
        "outlined dense clearable"
      ).classes("filter search").mark("employee-search")
      group_select = (
        # Its value comes with its options (reread): one they don't hold yet is refused.
        ui.select({None: "All groups"}, value=None, label="Group", on_change=lambda e: choose("group", e.value))
        .props("outlined dense options-dense")
        .classes("filter")
        .mark("filter-group")
      )
      roles = {None: "All roles", **dict(text.ROLE_LABEL)}
      ui.select(roles, value=role, label="Role", on_change=lambda e: choose("role", e.value)).props(
        "outlined dense options-dense"
      ).classes("filter").mark("filter-role")
      ui.space()
      ui.switch("Show switched-off employees", value=switched_off, on_change=lambda e: choose("off", e.value)).classes("toggle").mark(
        "show-switched-off"
      )

    @ui.refreshable
    def rows() -> None:  # noqa: C901 - the list's one drawing pass
      if down or data is None:
        with ui.row().classes("callout orange"):
          ui.element("span").classes("page-icon icon-warning")
          ui.label(text.DB_DOWN)
        return
      shown = [
        p
        for p in sorted(data.people.values(), key=lambda p: p.sort_key)
        if (switched_off or p.active)
        and (group is None or p.group_id == group)
        and (role is None or p.role == role)
        and _matches(p, search)
      ]
      if lacking := sum(map(_no_fingerprint, shown)):
        with ui.row().classes("callout red").mark("no-fingerprint-bar"):
          ui.element("span").classes("page-icon icon-fingerprint")
          ui.label(f"{text.count(lacking, 'employee')} can't clock in with a fingerprint: no fingerprint registered, and not exempt.")
      if not shown:
        ui.label("No employees match.").classes("muted empty")
        return
      with ui.element("div").classes("card"), ui.element("table").classes("sh"):
        with ui.element("tr"):
          for heading in ("Name", "ID", "Group", "Role", "Fingerprint", "Phone", "Email", "Status"):
            TextElement(tag="th", text=heading)
        for person in shown:
          own = person.id == signin.employee_id
          row = ui.element("tr").classes("r" + " off" * (not person.active)).mark("employee-row", f"employee-row-{person.id}")
          row.on("click", partial(ui.navigate.to, f"/employee?{urlencode({'id': person.id})}"))
          with row:
            with ui.element("td"), ui.row().classes("chips"):
              ui.label(person.name).classes("nm")
              if own:
                ui.element("span").classes("page-icon icon-lock").mark(f"own-{person.id}")
            TextElement(tag="td", text=person.username)
            TextElement(tag="td", text=data.groups.get(person.group_id, f"group {person.group_id}"))
            TextElement(tag="td", text=text.ROLE_LABEL[person.role])
            with ui.element("td"), ui.row().classes("chips"):
              if person.fingerprint_exempt:
                ui.label("Exempt (PIN only)")
              elif person.fingerprints:
                ui.label(f"{person.fingerprints} registered")
              else:
                ui.label("None registered").classes("muted")
              if _no_fingerprint(person):
                ui.label("No fingerprint").classes("chip red").mark("no-fingerprint", f"no-fingerprint-{person.id}")
            for value in (text.phone_label(person.phone), person.email):
              TextElement(tag="td", text=value or "none").classes("" if value else "muted")
            TextElement(tag="td", text="Active" if person.active else "Switched off")
          if technical:
            with ui.element("tr"):
              detail = TextElement(tag="td", text=text.employee_details(person, data.groups, data.tz, h24=h24)).classes("det-row")
              detail.props("colspan=8")
              detail.classes("empdet")

    await reread(touch=False)  # the page load just counted as activity
    rows()

  async def woken() -> None:
    with client:
      await reread(touch=False)  # a change someone else made isn't this user's activity

  client.on_delete(live.subscribe(test=signin.test_database, callback=woken))
