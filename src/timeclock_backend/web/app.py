"""The page's routes and pages (spec 2026-11-05 §3), registered on NiceGUI's app by `build`."""

# Standard library imports
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, override
from urllib.parse import quote

# Third party imports
import psycopg
from authlib.integrations.starlette_client import OAuth
from nicegui import app, ui
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request  # noqa: TC002 - FastAPI reads route annotations at runtime
from starlette.responses import PlainTextResponse, RedirectResponse, Response

# Local folder imports
from . import text
from .gate import EndReason, Gate, SignedOutError

if TYPE_CHECKING:
  # Standard library imports
  from collections.abc import Callable, Mapping

  # Third party imports
  from starlette.middleware.base import RequestResponseEndpoint

  # Local folder imports
  from .gate import Accounts, SignIn
  from .settings import PageSettings

logger = logging.getLogger(__name__)

STATIC_DIR: Final = Path(__file__).with_name("static")
# How often an open page checks its sign-in without counting as activity, so it leaves soon after the sign-in ends.
SESSION_CHECK_SECS = 30
_PUBLIC_PATHS: Final = frozenset({"/signin", "/signin/zoho", "/auth/zoho"})
_PUBLIC_PREFIXES: Final = ("/static/", "/_nicegui")  # the page's files and NiceGUI's (its assets and socket)
# Activity in the browser, at most once a minute, so working in a page without loading another keeps the sign-in.
_ACTIVITY_SCRIPT: Final = """<script>
(() => {
  let last = 0;
  const ping = () => { if (Date.now() - last > 60000) { last = Date.now(); emitEvent("page_activity"); } };
  addEventListener("pointerdown", ping, true);
  addEventListener("keydown", ping, true);
})();
</script>"""


def build(  # noqa: C901, PLR0915 - one linear registration of every route and page; splitting it would only scatter it
  settings: PageSettings,
  accounts: Accounts,
  *,
  zoho_client_kwargs: Mapping[str, Any] | None = None,
  now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> None:
  """Register the page on NiceGUI's app: its files, the gate, the Zoho sign-in routes, and the pages.

  Args:
    settings: The page's settings.
    accounts: The production allowlist.
    zoho_client_kwargs: Extra httpx client arguments for talking to Zoho (tests pass a transport).
    now: The clock the gate uses.
  """
  gate = Gate(settings, accounts, app.storage.general, now=now)
  zoho = OAuth().register(
    "zoho",
    client_id=settings.zoho_client_id,
    client_secret=settings.zoho_client_secret.get_secret_value(),
    server_metadata_url=f"{settings.zoho_accounts_url}/.well-known/openid-configuration",
    client_kwargs={"scope": "openid email", "code_challenge_method": "S256", **(zoho_client_kwargs or {})},
  )
  redirect_uri = f"{settings.public_url}/auth/zoho"
  # Zoho takes no prompt=select_account, so another account means signing out of Zoho first.
  other_account = f"{settings.zoho_accounts_url}/logout?serviceurl={quote(f'{settings.public_url}/signin', safe='')}"

  app.add_static_files("/static", STATIC_DIR)
  ui.add_head_html(f'<link rel="stylesheet" href="/static/theme.css">\n{_ACTIVITY_SCRIPT}', shared=True)

  class SignInGate(BaseHTTPMiddleware):
    """Every page load but the sign-in's own passes the gate.

    Added here, so it runs inside the session and request-tracking middleware `ui.run` adds later (outermost), where
    `app.storage.user` is this browser's.
    """

    @override
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
      path = request.url.path
      if path not in _PUBLIC_PATHS and not path.startswith(_PUBLIC_PREFIXES):
        try:
          request.state.signin = await gate.check(app.storage.user, touch=True)
        except SignedOutError:
          return RedirectResponse("/signin", status_code=303)
        except psycopg.OperationalError:
          logger.warning("the allowlist can't be read", exc_info=True)
          return PlainTextResponse(text.DB_DOWN, status_code=503)
      return await call_next(request)

  app.add_middleware(SignInGate)

  @app.get("/signin/zoho")
  async def start_zoho_sign_in(request: Request) -> Response:
    return await zoho.authorize_redirect(request, redirect_uri)

  @app.get("/auth/zoho")
  async def finish_zoho_sign_in(request: Request) -> Response:
    storage = app.storage.user
    try:
      # Authlib checks the audience only when asked to, and skips every id_token check when the reply has no id_token.
      token = await zoho.authorize_access_token(
        request,
        claims_options={
          "iss": {"essential": True, "value": settings.zoho_accounts_url},
          "aud": {"essential": True, "value": settings.zoho_client_id},
        },
      )
    except Exception:  # any fault in Zoho's reply, or in reaching Zoho, is a failed sign-in, never a signed-in one
      logger.warning("a Zoho sign-in failed", exc_info=True)
      token = {}
    userinfo = token.get("userinfo")
    if userinfo is None:
      gate.end(storage, EndReason.FAILED)
      return RedirectResponse("/signin", status_code=303)
    email = userinfo.get("email")
    if userinfo.get("email_verified") is not True or not isinstance(email, str):
      gate.end(storage, EndReason.REFUSED)
      return RedirectResponse("/signin", status_code=303)
    email = email.lower()  # badges are lowercase (0014), whatever casing Zoho reports
    try:
      account = await accounts.lookup(email)
    except psycopg.OperationalError:
      logger.warning("the allowlist can't be read", exc_info=True)
      return PlainTextResponse(text.DB_DOWN, status_code=503)
    if account is None:
      gate.end(storage, EndReason.REFUSED)
      return RedirectResponse("/signin", status_code=303)
    gate.start(storage, email)
    return RedirectResponse("/", status_code=303)

  @ui.page("/signin", title=settings.page_name)
  async def signin_page() -> Response | None:
    storage = app.storage.user
    try:
      await gate.check(storage, touch=False)
    except SignedOutError, psycopg.OperationalError:
      pass
    else:
      return RedirectResponse("/", status_code=303)
    reason = EndReason(storage.pop("signed_out", EndReason.NONE))
    with ui.column().classes("signin-card"):
      ui.element("div").classes("page-logo")
      ui.label(settings.page_name).classes("signin-title")
      if (message := text.ended(reason, settings)) is not None:
        ui.label(message).classes("signin-message").mark("signin-message")
      if reason is EndReason.REFUSED:  # §3.5: the message and another account, nothing else
        ui.button("Sign in with another account", on_click=lambda: ui.navigate.to(other_account)).classes("zoho-signin")
        ui.label("This signs you out of Zoho in this browser first.").classes("signin-note")
      else:
        with ui.button(on_click=lambda: ui.navigate.to("/signin/zoho")).classes("zoho-signin").mark("zoho-signin"):
          ui.element("span").classes("zoho-logo")
          ui.label("Sign in using Zoho")
    return None

  @ui.page("/", title=settings.page_name)
  async def home_page(request: Request) -> None:
    signin: SignIn = request.state.signin

    def sign_out() -> None:
      gate.end(app.storage.user, EndReason.SIGNED_OUT)
      ui.navigate.to("/signin")

    leaving = False

    async def leave_if_ended(*, touch: bool) -> None:
      nonlocal leaving
      if leaving:  # a second navigation would land after the first and lose the reason the sign-in page shows
        return
      try:
        await gate.check(app.storage.user, touch=touch)
      except SignedOutError:
        leaving = True
        ui.navigate.to("/signin")
      except psycopg.OperationalError:
        logger.warning("the allowlist can't be read", exc_info=True)  # keep the page; the next check tries again

    ui.label(f"Signed in as {signin.email}").mark("signed-in-as")
    ui.label("Shifts and employees will appear here.")
    ui.button("Sign out", on_click=sign_out).mark("signout")
    ui.timer(SESSION_CHECK_SECS, lambda: leave_if_ended(touch=False), immediate=False)  # the page load just checked
    ui.on("page_activity", lambda: leave_if_ended(touch=True))
