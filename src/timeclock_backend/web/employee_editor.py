"""The employee editor (spec 2026-11-05 §9; mockups K to M): NiceGUI widgets over a draft the page keeps server-side.

Unlike the shift editor, it doesn't work offline (§9): the draft lives in this page's NiceGUI client, which outlives a
dropped connection as long as the sign-in would (`app.py`), so the changes are kept but can't be added to until it's
back. A save writes only the changed columns; the database logs each (0016) and refuses your own locked fields.
"""

# Standard library imports
import asyncio
import logging
import re
from functools import partial
from typing import TYPE_CHECKING, Any

# Third party imports
import bcrypt
import psycopg
from nicegui import app, context, ui
from timeclock_schema.models import EMAIL_PATTERN

# Local folder imports
from . import text
from .db import DatabaseChangedError, editor_transaction
from .gate import SignedOutError
from .shell import prefs
from .store import EDITABLE, LOCKED_FOR_SELF, employee_row, load_context, load_employee_history, load_fingerprints, save_employee

if TYPE_CHECKING:
  # Standard library imports
  from collections.abc import Mapping
  from datetime import datetime
  from zoneinfo import ZoneInfo

  # Third party imports
  from psycopg import AsyncConnection
  from psycopg.rows import DictRow
  from pydantic import SecretStr

  # Local folder imports
  from .db import Pools
  from .gate import Gate, SignIn
  from .live import Live
  from .settings import PageSettings
  from .store import Context, EmployeeHistoryItem, FieldValue, Fingerprint

logger = logging.getLogger(__name__)

_PHONE_PUNCTUATION = re.compile(r"[\s().-]")
_TEXT_FIELDS = frozenset({"first_name", "middle_name", "last_name", "phone", "email"})
type _Read = tuple[Context, dict[str, FieldValue] | None, list[Fingerprint], list[EmployeeHistoryItem]]


def normalize_phone(raw: str) -> str:
  """A US phone typed in any common format as `+1` and ten digits, the one stored format.

  The clock's `timeclock_system.clock.normalize_phone`, copied: the two must agree.

  Raises:
    ValueError: It isn't a ten-digit US number.
  """
  digits = _PHONE_PUNCTUATION.sub("", raw)
  digits = digits.removeprefix("+1") if digits.startswith("+") else digits
  if len(digits) == 11 and digits.startswith("1"):  # noqa: PLR2004  # the country code without its plus
    digits = digits[1:]
  if not (len(digits) == 10 and digits.isascii() and digits.isdecimal()):  # noqa: PLR2004
    raise ValueError("not a ten-digit US phone number")
  return f"+1{digits}"


def hash_pin(pin: SecretStr) -> str:
  """`pin`'s bcrypt hash with the `$2a$` prefix the clock uses: pgcrypto's `crypt()` can't check bcrypt's default `$2b$`."""
  return bcrypt.hashpw(pin.get_secret_value().encode(), bcrypt.gensalt(prefix=b"2a")).decode()


def parse(field: str, raw: Any) -> tuple[FieldValue, str | None]:
  """A widget's value as stored, or why it can't be: the clock's checks (names required, its phone and email formats).

  Text is stripped, and an empty optional field is None, never `''` (the email CHECK would refuse it).
  """
  if field not in _TEXT_FIELDS:
    return raw, None
  typed = (raw or "").strip()
  if field in {"first_name", "last_name"} and not typed:
    return None, text.FIRST_NAME_REQUIRED if field == "first_name" else text.LAST_NAME_REQUIRED
  if field == "phone" and typed:
    try:
      return normalize_phone(typed), None
    except ValueError:
      return None, text.PHONE_INVALID
  if field == "email" and typed and re.fullmatch(EMAIL_PATTERN, typed) is None:
    return None, text.EMAIL_INVALID
  return typed or None, None


def _shown(field: str, value: FieldValue) -> Any:
  """A stored value as its widget holds it."""
  if field == "phone":
    return text.phone_label(value if isinstance(value, str) else None)
  return "" if value is None and field in _TEXT_FIELDS else value


def _stored(value: FieldValue) -> str:
  """A value as the database holds it, for technical details: text quoted, `null`, `true`."""
  if isinstance(value, str):
    return repr(value)
  return "null" if value is None else str(value).lower()


def _moment(at: datetime, tz: ZoneInfo) -> str:
  """A moment to the second, for technical details: "2026-10-28 14:10:37"."""
  return at.astimezone(tz).strftime("%Y-%m-%d %H:%M:%S")


async def _read(conn: AsyncConnection[DictRow], employee_id: int, *, default_tz: ZoneInfo) -> _Read:
  """Everything the editor shows of the employee, in one transaction."""
  ctx = await load_context(conn, default_tz=default_tz)
  return (
    ctx,
    await employee_row(conn, employee_id),
    await load_fingerprints(conn, employee_id),
    await load_employee_history(conn, employee_id),
  )


async def draw_employee_editor(  # noqa: C901, PLR0915 - one page: its state, its reads, its drawing and its save, top to bottom
  signin: SignIn,
  *,
  settings: PageSettings,
  pools: Pools,
  gate: Gate,
  live: Live,
  query: Mapping[str, str],
) -> None:
  """Draw `/employee?id=<id>`: the employee's fields, prints and history, a draft of changes, and its save."""
  # Captured now: a live update runs outside any request, where app.storage can't tell whose browser it is.
  storage, session, client = app.storage.user, app.storage.browser, context.client
  chosen = prefs()
  h24, technical = chosen["h24"], chosen["technical"]
  employee = int(v) if (v := query.get("id", "")).isdigit() else None
  body = ui.column().classes("page-body employee-editor offline-inert")  # inert while the socket is down (app.py)
  with body, ui.link(target="/employees").classes("crumb").mark("crumb"):
    ui.element("span").classes("page-icon icon-arrow-back")
    ui.label("Employees")

  def gone(message: str) -> None:
    with body, ui.row().classes("callout grey"):
      ui.element("span").classes("page-icon icon-info")
      ui.label(message)

  if employee is None:
    gone(text.EMPLOYEE_GONE)
    return
  try:
    async with editor_transaction(pools, gate, storage, session, test_database=signin.test_database, touch=False) as conn:
      ctx, row, prints, history = await _read(conn, employee, default_tz=settings.tz)
  except SignedOutError:
    ui.navigate.to("/signin")
    return
  except DatabaseChangedError:
    ui.navigate.reload()
    return
  except psycopg.OperationalError:
    logger.warning("the employee editor can't be read", exc_info=True)
    gone(text.DB_DOWN)
    return
  if row is None or employee not in ctx.people:
    gone(text.EMPLOYEE_GONE)
    return

  own = employee == signin.employee_id
  base: dict[str, FieldValue] = row
  draft: dict[str, FieldValue] = {}  # the fields changed, as they'd be stored
  errors: dict[str, str] = {}  # the fields that can't be stored as typed
  message: tuple[str, str] | None = None  # (callout color, text) after a save
  clashing: set[str] = set()  # drafted fields someone else changed meanwhile, until a side is picked (spec §6.1)
  outside: list[str] = []  # other fields someone else changed while there's a draft, which Save then confirms
  saving = False
  busy = asyncio.Lock()  # a save and a live update take turns, so neither lands over the other's newer read
  widgets: dict[str, Any] = {}
  groups = dict(sorted(ctx.groups.items(), key=lambda g: g[1].casefold()))
  roles = {r.value: label for r, label in text.ROLE_LABEL.items()}

  def last_change(field: str) -> EmployeeHistoryItem | None:
    return next((item for item in history if any(c.field == field for c in item.changes)), None)

  def take(fresh: _Read) -> bool:
    """Show the employee as just read, keeping the draft (spec §6.1); False when they're gone."""
    nonlocal ctx, prints, history, base
    ctx, row, prints, history = fresh
    if row is None:
      return False
    sorted_groups = dict(sorted(ctx.groups.items(), key=lambda g: g[1].casefold()))
    if widgets["group_id"].options != sorted_groups:  # before any value below moves to a new group
      widgets["group_id"].set_options(sorted_groups)
    moved = [f for f in EDITABLE if row[f] != base[f]]
    base = row
    for field in moved:
      if field in draft:
        if draft[field] == row[field]:
          draft.pop(field)
        else:
          clashing.add(field)
      elif field not in errors:  # one being typed keeps what's typed
        if draft and field not in outside:
          outside.append(field)
        widgets[field].set_value(_shown(field, row[field]))  # equal to the base now, so it drafts nothing
    title.refresh()
    fingerprints.refresh()
    past.refresh()
    return True

  def keep(field: str, *, mine: bool) -> None:
    clashing.discard(field)
    if mine:
      redraw()
    else:
      undo(field)

  def redraw() -> None:
    clashing.intersection_update(draft)  # undoing a field settles its pick
    if not draft:
      outside.clear()
    for field, widget in widgets.items():
      widget.classes(remove="changed clash")
      if field in draft:
        widget.classes(add="clash" if field in clashing else "changed")
    notes.refresh()
    changes.refresh()
    actions.refresh()
    switched_off.refresh()

  def changed(field: str, raw: Any) -> None:
    nonlocal message
    value, error = parse(field, raw)
    if error is not None:
      errors[field] = error
      draft.pop(field, None)
    else:
      errors.pop(field, None)
      if value == base[field]:
        draft.pop(field, None)
      else:
        draft[field] = value
    message = None
    redraw()

  def undo(field: str) -> None:
    widgets[field].set_value(_shown(field, base[field]))  # its change handler drops the draft's

  def discard() -> None:
    for field in [*draft, *errors]:
      undo(field)
    note.set_value("")

  def text_field(field: str, label: str, *, placeholder: str = "") -> None:
    widgets[field] = (
      ui.input(
        label,
        value=_shown(field, base[field]),
        placeholder=placeholder,
        validation=lambda raw: parse(field, raw)[1],
        on_change=lambda e: changed(field, e.value),
      )
      .props("outlined dense stack-label")
      .classes("w-full")
      .mark(f"field-{field}")
    )
    notes(field)

  def locked(field: str, widget: Any) -> None:
    widgets[field] = widget.mark(f"field-{field}")
    if own and field in LOCKED_FOR_SELF:
      widget.disable()
      ui.element("span").classes("page-icon icon-lock")

  @ui.refreshable
  def notes(field: str) -> None:
    if field in clashing:
      found = last_change(field)
      by = "" if found is None else f", saved {text.clock_time(found.when, ctx.tz, h24=h24)} by {found.who}"
      theirs, yours = (text.field_value(field, v, ctx.groups) for v in (base[field], draft[field]))
      with ui.column().classes("callout red clash-pick").mark(f"theirs-{field}"):
        ui.label(f"Theirs: {theirs}{by} · Yours: {yours}")
        with ui.row():
          keep_theirs = ui.button("Keep theirs", on_click=partial(keep, field, mine=False)).props("outline dense no-caps")
          keep_theirs.mark(f"keep-theirs-{field}")
          ui.button("Keep mine", on_click=partial(keep, field, mine=True)).props("outline dense no-caps").mark(f"keep-mine-{field}")
    elif field in draft:
      with ui.row().classes("was"):
        ui.label(f"Was {text.field_value(field, base[field], ctx.groups)}")
        ui.button("Undo", on_click=lambda: undo(field)).props("flat dense no-caps").classes("btn text")
    if technical:
      moved = f" → {_stored(draft[field])}" if field in draft else ""
      ui.label(f"{field} {_stored(base[field])}{moved}").classes("det")

  @ui.refreshable
  def title() -> None:
    person = ctx.people[employee]
    with ui.row().classes("title"):
      ui.label(f"{base['first_name']} {base['last_name']}").classes("title-name")
      ui.label(f"ID {person.username}").classes("eid")
      ui.label("Active" if base["active"] else "Switched off").classes("chip " + ("grey" if base["active"] else "orange"))
      if own:
        with ui.row().classes("chip grey"):
          ui.element("span").classes("page-icon icon-lock")
          ui.label("You")
    if technical:
      ui.label(text.employee_details(person, ctx.groups, ctx.tz, h24=h24)).classes("empdet")

  @ui.refreshable
  def fingerprints() -> None:
    for fp in prints:
      with ui.row().classes("print"):
        ui.element("span").classes("page-icon icon-fingerprint")
        ui.label(f"{text.FINGER_LABEL[fp.finger]}, registered {text.stamp(fp.registered_at, ctx.tz, h24=h24)}")
      if technical:
        detail = f"{str(fp.id)[:8]}… · {fp.finger.value} · registered {_moment(fp.registered_at, ctx.tz)} · instance {fp.instance_id}"
        ui.label(f"{detail} · written by {fp.db_login}").classes("det")
    if not prints:
      with ui.row().classes("print"):
        ui.label("None registered").classes("muted")
        if base["active"] and not base["fingerprint_exempt"]:
          ui.label("No fingerprint").classes("chip red")

  @ui.refreshable
  def switched_off() -> None:
    if base["active"] and draft.get("active") is False:
      with ui.row().classes("callout orange"):
        ui.element("span").classes("page-icon icon-warning")
        ui.label(text.switch_off_note(str(draft.get("first_name") or base["first_name"])))

  @ui.refreshable
  def past() -> None:
    if not history:
      ui.label("No earlier changes.").classes("muted")
      return
    with ui.element("ul").classes("hist"):
      for item in history:
        with ui.element("li"):
          ui.label(f"{text.stamp(item.when, ctx.tz, h24=h24)} · {item.who}").classes("muted").mark("history-line")
          for change in item.changes:
            ui.label(text.employee_change_line(change, ctx.groups)).mark("history-line")
          if item.note:
            ui.label(f'"{item.note}"').classes("hist-note").mark("history-line")
          if technical:
            for change in item.changes:
              values = (
                "pin_hash (value not logged)"
                if change.field == "pin"
                else f"{change.field} {change.old_value or 'null'} → {change.new_value or 'null'}"
              )
              recorded = f"written by {item.who} · recorded {_moment(item.when, ctx.tz)}"
              ui.label(f"change {str(change.id)[:8]}… · {values} · {recorded}").classes("det")

  @ui.refreshable
  def changes() -> None:
    if outside:
      whos = list(dict.fromkeys(found.who for f in outside if (found := last_change(f)) is not None)) or ["Someone"]
      labels = [text.FIELD_LABEL[f] for f in EDITABLE if f in outside]
      named = labels[0] if len(labels) == 1 else f"{', '.join(labels[:-1])} and {labels[-1]}"
      with ui.row().classes("callout blue").mark("outside"):
        ui.element("span").classes("page-icon icon-info")
        ui.label(f"{' and '.join(whos)} changed {named} while you were editing.")
    if message is not None:
      with ui.row().classes(f"callout {message[0]}"):
        ui.element("span").classes("page-icon " + {"green": "icon-check", "orange": "icon-warning"}.get(message[0], "icon-error"))
        ui.label(message[1])
    if not draft:
      ui.label("None yet.").classes("small muted")
      return
    with ui.element("ul").classes("changes"):
      for field in (f for f in EDITABLE if f in draft):
        with ui.element("li"):
          ui.element("span").classes("page-icon icon-edit")
          was, wanted = (text.field_value(field, v, ctx.groups) for v in (base[field], draft[field]))
          ui.label(f"{text.FIELD_LABEL[field]}: {was} to {wanted}").classes("grow")
          ui.button("Undo", on_click=partial(undo, field)).props("flat dense no-caps").classes("btn text").mark(f"undo-{field}")
    if technical:
      columns = ", ".join(f"{f} = {_stored(draft[f])}" for f in EDITABLE if f in draft)
      ui.label(f"will write: employees {employee} {columns} · {text.count(len(draft), 'change log row')}").classes("det")

  @ui.refreshable
  def actions() -> None:
    if clashing:
      blocker = "Pick theirs or yours for each field marked in red to save."
    elif errors:
      blocker = "Fix the fields marked in red to save."
    else:
      blocker = None if draft else "Nothing to save yet."
    with ui.row().classes("saverow"):
      ui.button("Discard", on_click=discard).props("outline no-caps").classes("btn outline").mark("discard")
      button = ui.button("Saving..." if saving else "Save changes", on_click=save)
      button.props("unelevated no-caps color=primary text-color=black").mark("save")
      if blocker is not None or saving:
        button.disable()
    if blocker is not None:
      ui.label(blocker).classes("blocker small")
    ui.label(text.EMPLOYEE_OFFLINE).classes("offline-note small")

  async def save(*, sure: bool = False) -> None:  # noqa: C901 - one save and each of its answers
    nonlocal message, saving
    if not draft or errors or clashing or saving:
      return
    if outside and not sure:
      confirm.open()
      return
    confirm.close()
    saving = True
    actions.refresh()
    known = len(outside)
    async with busy:
      if clashing or len(outside) != known:  # a live update landed while this waited; it's shown, for Save again
        saving = False
        redraw()
        return
      wanted = {f: draft[f] for f in EDITABLE if f in draft}
      fresh = moved = None
      try:
        async with editor_transaction(pools, gate, storage, session, test_database=signin.test_database) as conn:
          moved = await save_employee(conn, employee_id=employee, base=base, changes=wanted, note=(note.value or "").strip())
          fresh = await _read(conn, employee, default_tz=settings.tz)  # what it saved, or what it lost to
      except SignedOutError:
        ui.navigate.to("/signin")
        return
      except DatabaseChangedError:
        ui.navigate.reload()
        return
      except LookupError:
        message = ("red", text.EMPLOYEE_GONE)
      except psycopg.OperationalError:
        logger.warning("an employee save couldn't reach the database", exc_info=True)
        message = ("red", text.DB_DOWN)
      except psycopg.Error:  # one the database refused: a page bug, since the page checks what it can first
        logger.exception("an employee save failed")
        message = ("red", text.EMPLOYEE_SAVE_FAILED)
      finally:
        saving = False
      if fresh is not None and moved is None:
        draft.clear()
        note.set_value("")
        take(fresh)
        message = ("green", "Saved.")
      elif fresh is not None:  # a changed field moved underneath it: marked to pick, as a live update marks it
        message = ("orange", text.EMPLOYEE_CHANGED) if take(fresh) else ("red", text.EMPLOYEE_GONE)
      redraw()

  async def woken() -> None:
    nonlocal message
    with client:
      async with busy:
        try:
          async with editor_transaction(pools, gate, storage, session, test_database=signin.test_database, touch=False) as conn:
            fresh = await _read(conn, employee, default_tz=settings.tz)  # someone else's change isn't this user's activity
        except SignedOutError:
          ui.navigate.to("/signin")
          return
        except DatabaseChangedError:
          ui.navigate.reload()
          return
        except psycopg.OperationalError:
          logger.warning("the employee editor's live update can't read the database", exc_info=True)  # the next wake tries again
          return
        if not take(fresh):
          message = ("red", text.EMPLOYEE_GONE)
        redraw()

  with body:
    title()
    if own:
      with ui.row().classes("callout grey"):
        ui.element("span").classes("page-icon icon-lock")
        ui.label(text.OWN_RECORD)
    with ui.element("div").classes("ed"):
      with ui.element("div").classes("ed-main"):
        with ui.element("div").classes("card"):
          ui.label("Name").classes("h4")
          with ui.row().classes("fields"):
            for field, label in (("first_name", "First"), ("middle_name", "Middle"), ("last_name", "Last")):
              with ui.column().classes("ef"):
                text_field(field, label, placeholder="optional" if field == "middle_name" else "")
        with ui.element("div").classes("card"):
          ui.label("Contact").classes("h4")
          with ui.row().classes("fields"):
            for field, label in (("phone", "Phone"), ("email", "Email")):
              with ui.column().classes("ef wide"):
                text_field(field, label)
        with ui.element("div").classes("card"):
          ui.label("Work").classes("h4")
          with ui.row().classes("fields"):
            for field, label, options in (("group_id", "Group", groups), ("role", "Role", roles)):
              with ui.column().classes("ef"):
                with ui.row().classes("lockrow"):
                  select = ui.select(options, value=base[field], label=label, on_change=lambda e, field=field: changed(field, e.value))
                  locked(field, select.props("outlined dense options-dense"))
                notes(field)
        with ui.element("div").classes("card"):
          ui.label("Fingerprints").classes("h4")
          fingerprints()
          with ui.row().classes("lockrow"):
            exempt = ui.switch(
              "Exempt from fingerprints: logs in with the PIN only",
              value=bool(base["fingerprint_exempt"]),
              on_change=lambda e: changed("fingerprint_exempt", e.value),
            )
            locked("fingerprint_exempt", exempt)
          notes("fingerprint_exempt")
        with ui.element("div").classes("card"):
          ui.label("Status").classes("h4")
          with ui.row().classes("lockrow"):
            locked("active", ui.switch("Active", value=bool(base["active"]), on_change=lambda e: changed("active", e.value)))
          notes("active")
          switched_off()
        with ui.element("div").classes("card"):
          ui.label("History").classes("h4")
          past()
      with ui.element("div").classes("ed-side"), ui.element("div").classes("card"):
        ui.label("Your changes").classes("h4")
        changes()
        note = ui.textarea(placeholder="Note (optional)").props("outlined autogrow").classes("w-full note").mark("note")
        actions()
  with ui.dialog() as confirm, ui.card().classes("dlg offline-inert"):
    ui.label("Save your changes on top of theirs?").classes("dlg-title")
    ui.label("Someone else changed this employee while you were editing. Saving keeps their changes and adds yours.")
    with ui.row().classes("saverow"):
      ui.button("Cancel", on_click=confirm.close).props("flat no-caps")
      confirm_save = ui.button("Save", on_click=partial(save, sure=True)).mark("confirm-save")
      confirm_save.props("unelevated no-caps color=primary text-color=black")

  client.on_delete(live.subscribe(test=signin.test_database, callback=woken))
