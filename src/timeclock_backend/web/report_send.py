"""The shift list's manual payroll report send (spec 2026-11-18): a developer picks enrolled addresses for the shown week.

The page only asks: it inserts a `backend.report_sends` row as the developer's badge, which the database checks (schema
0027), and the backend sends it and records the outcome, which the dialog re-reads until it lands.
"""

# Standard library imports
import logging
from typing import TYPE_CHECKING, Any, Final

# Third party imports
import psycopg
from nicegui import ui

# Local folder imports
from . import text
from .db import DatabaseChangedError, editor_transaction
from .gate import SignedOutError

if TYPE_CHECKING:
  # Standard library imports
  from collections.abc import Mapping, MutableMapping
  from datetime import date
  from zoneinfo import ZoneInfo

  # Local folder imports
  from .db import Pools
  from .gate import Gate, SignIn

logger = logging.getLogger(__name__)

POLL_SECS = 2.0  # how often an open dialog re-reads its request; tests shorten it
REPORT: Final = "payroll_report"


async def open_report_send(  # noqa: C901, PLR0915 - one dialog: its read, its drawing and its two actions
  signin: SignIn,
  *,
  monday: date,
  pools: Pools,
  gate: Gate,
  storage: MutableMapping[str, Any],
  session: Mapping[str, Any],
  tz: ZoneInfo,
  h24: bool,
) -> None:
  """Open the dialog for the week starting `monday`, in the database the page shows."""
  emails: list[str] = []
  problem: str | None = None
  try:
    async with editor_transaction(pools, gate, storage, session, test_database=signin.test_database) as conn:
      cursor = await conn.execute("SELECT email FROM backend.report_recipients WHERE report = %s ORDER BY email", (REPORT,))
      emails = [row["email"] for row in await cursor.fetchall()]
  except SignedOutError:
    ui.navigate.to("/signin")
    return
  except DatabaseChangedError:
    ui.navigate.reload()
    return
  except psycopg.Error as e:  # a lost developer mark, or the database down
    problem = e.diag.message_primary or str(e)

  request_id: int | None = None
  waiting = False

  def refresh() -> None:
    ticked = sum(bool(box.value) for box in boxes.values())
    send.set_text(text.send_to(ticked))
    send.set_enabled(ticked > 0 and not waiting)
    select_all.set_text(text.SELECT_NONE if ticked == len(boxes) else text.SELECT_ALL)

  def toggle_all() -> None:
    tick = not all(box.value for box in boxes.values())
    for box in boxes.values():
      box.set_value(tick)  # each fires refresh

  def settle(message: str) -> None:
    nonlocal waiting
    waiting = False
    poll.deactivate()
    outcome.set_text(message)
    refresh()

  async def submit() -> None:
    nonlocal request_id, waiting
    # Checked and set with no await between: NiceGUI runs each click as its own task, so a double click's second task
    # can start before the first has disabled the button.
    if waiting:
      return
    waiting = True
    refresh()
    chosen = [email for email, box in boxes.items() if box.value]
    try:
      async with editor_transaction(pools, gate, storage, session, test_database=signin.test_database) as conn:
        cursor = await conn.execute(
          "INSERT INTO backend.report_sends (report, week_start, recipients) VALUES (%s, %s, %s) RETURNING id",
          (REPORT, monday, chosen),
        )
        row = await cursor.fetchone()
    except SignedOutError:
      ui.navigate.to("/signin")
      return
    except DatabaseChangedError:
      ui.navigate.reload()
      return
    except psycopg.Error as e:  # refused: an address un-enrolled meanwhile, or the developer mark gone
      settle(text.report_failed(e.diag.message_primary or str(e)))
      return
    assert row is not None
    request_id = row["id"]
    outcome.set_text(text.REPORT_WAITING)
    close.set_text(text.CLOSE)  # from here closing abandons the display, never the send
    poll.activate()

  async def check() -> None:
    if request_id is None:
      return
    try:
      async with editor_transaction(pools, gate, storage, session, test_database=signin.test_database, touch=False) as conn:
        cursor = await conn.execute(
          "SELECT status, error, finished_at, cardinality(recipients) AS n FROM backend.report_sends WHERE id = %s", (request_id,)
        )
        row = await cursor.fetchone()
    except SignedOutError:
      ui.navigate.to("/signin")
      return
    except DatabaseChangedError:
      ui.navigate.reload()
      return
    except psycopg.Error:
      logger.warning("a report send's outcome couldn't be read; trying again", exc_info=True)
      return
    if row is None or row["status"] in {"pending", "sending"}:
      return
    if row["status"] == "sent":
      settle(text.report_sent(row["n"], row["finished_at"], tz, h24=h24))
    else:
      settle(text.report_failed(row["error"] or "unknown error"))

  with ui.dialog() as dialog, ui.card().classes("dlg offline-inert").mark("report-send"):
    ui.label(text.REPORT_TITLE).classes("dlg-title")
    ui.label(text.week_label(monday)).classes("h4")
    if signin.test_database:
      ui.label(text.REPORT_TEST_NOTE).classes("small muted")
    select_all = ui.button(text.SELECT_ALL, on_click=toggle_all).props("flat dense no-caps").classes("btn text").mark("select-all")
    boxes = {email: ui.checkbox(email, on_change=refresh).mark(f"recipient-{email}") for email in emails}
    select_all.set_visibility(bool(emails))
    if not emails and problem is None:
      ui.label(text.NO_REPORT_RECIPIENTS)
    outcome = ui.label("" if problem is None else text.report_failed(problem)).classes("small").mark("outcome")
    poll = ui.timer(POLL_SECS, check, active=False)  # dies with the dialog: closing it abandons the display, never the send
    with ui.row().classes("saverow"):
      close = ui.button(text.CANCEL, on_click=dialog.close).props("outline no-caps").classes("btn outline").mark("close")
      send = ui.button(text.send_to(0), on_click=submit).props("unelevated no-caps color=primary text-color=black").mark("send")
      send.set_enabled(False)
      send.set_visibility(bool(emails))
  dialog.on_value_change(lambda e: None if e.value else dialog.delete())
  dialog.open()
