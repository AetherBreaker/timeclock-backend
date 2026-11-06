"""The frame every signed-in page draws first: top bar, settings bubble, test database banner, and the sign-in checks."""

# Standard library imports
import logging
from typing import TYPE_CHECKING, Any, Final

# Third party imports
import psycopg
from nicegui import app, ui

# Local folder imports
from .gate import EndReason, SignedOutError

if TYPE_CHECKING:
  # Third party imports
  from nicegui.events import ValueChangeEventArguments

  # Local folder imports
  from .gate import Gate, SignIn
  from .settings import PageSettings

logger = logging.getLogger(__name__)

# Per browser, in app.storage.user (§11). Dark by default (§2); the others take effect in the shift and employee pages,
# which draw with them, so changing one reloads the page.
DEFAULT_PREFS: Final = {"dark": True, "h24": False, "technical": False}
# How often an open page checks its sign-in without counting as activity, so it leaves soon after the sign-in ends.
SESSION_CHECK_SECS = 30


def prefs() -> dict[str, Any]:
  """This browser's display preferences over the defaults."""
  return {**DEFAULT_PREFS, **app.storage.user.get("prefs", {})}


def frame(signin: SignIn, settings: PageSettings, gate: Gate) -> None:  # noqa: C901, PLR0915 - one page frame, drawn top to bottom
  """Draw the frame and start the page's sign-in checks: a timer, and the browser's activity pings."""
  chosen = prefs()
  dark = ui.dark_mode(chosen["dark"])
  leaving = False

  async def leave_if_ended(*, touch: bool) -> None:
    nonlocal leaving
    if leaving:  # a second navigation would land after the first and lose the reason the sign-in page shows
      return
    try:
      current = await gate.check(app.storage.user, app.storage.browser, touch=touch)
    except SignedOutError:
      leaving = True
      ui.navigate.to("/signin")
    except psycopg.OperationalError:
      logger.warning("the allowlist can't be read", exc_info=True)  # keep the page; the next check tries again
    else:
      if current.test_database != signin.test_database:  # switched in another tab, or the developer mark went
        leaving = True
        ui.navigate.reload()

  def sign_out() -> None:
    gate.end(app.storage.user, EndReason.SIGNED_OUT)
    ui.navigate.to("/signin")

  async def choose_database(*, test: bool) -> None:
    nonlocal leaving
    leaving = True
    try:
      await gate.choose_database(app.storage.user, app.storage.browser, test=test)
    except SignedOutError:
      ui.navigate.to("/signin")
      return
    except psycopg.OperationalError:
      logger.warning("the allowlist can't be read", exc_info=True)  # the reload shows it, then the choice as it stands
    ui.navigate.reload()

  def set_pref(name: str, e: ValueChangeEventArguments) -> None:
    app.storage.user["prefs"] = {**prefs(), name: e.value}  # a new dict, so the storage sees the change
    if name == "dark":
      dark.set_value(e.value)
    else:
      ui.navigate.reload()

  if signin.test_database:
    ui.query("body").classes(add="test-database")
    with ui.row().classes("test-banner").mark("test-banner"):
      ui.label("TEST DATABASE · Nothing here touches real hours or payroll")
      ui.button("Back to the real database", on_click=lambda: choose_database(test=False)).props("flat dense no-caps")

  with ui.row().classes("page-header"):
    ui.element("div").classes("page-logo")
    ui.label(settings.page_name).classes("page-name")
    with ui.row().classes("page-nav"):
      ui.link("Shifts", "/").classes("page-nav-link on")
    ui.space()
    ui.label(signin.email).classes("page-account").mark("page-account")
    with ui.button().props("flat round").classes("page-gear").mark("gear"):
      ui.element("span").classes("page-icon icon-gear")  # the glyph comes from the theme (§2)
      with ui.menu().classes("page-bubble"):  # opens on the same page, no page load (§11)
        ui.label("Display").classes("bubble-heading")
        ui.switch("Dark mode", value=chosen["dark"], on_change=lambda e: set_pref("dark", e)).mark("pref-dark")
        ui.switch("24-hour time", value=chosen["h24"], on_change=lambda e: set_pref("h24", e)).mark("pref-h24")
        ui.switch("Show technical details", value=chosen["technical"], on_change=lambda e: set_pref("technical", e)).mark(
          "pref-technical"
        )
        ui.label("Remembered in this browser.").classes("bubble-note")
        if signin.developer:
          ui.separator()
          ui.label("Database (developers only)").classes("bubble-heading")
          ui.switch("Use the test database", value=signin.test_database, on_change=lambda e: choose_database(test=bool(e.value))).mark(
            "pref-test-db"
          )
          ui.label("Remembered for your account.").classes("bubble-note")
    with ui.button(on_click=sign_out).props("flat no-caps").classes("page-signout").mark("signout"):
      ui.element("span").classes("page-icon icon-signout")
      ui.label("Sign out")

  ui.timer(SESSION_CHECK_SECS, lambda: leave_if_ended(touch=False), immediate=False)  # the page load just checked
  ui.on("page_activity", lambda: leave_if_ended(touch=True))
