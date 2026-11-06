"""The page's routes and pages (spec 2026-11-05 §3), registered on NiceGUI's app by `build`; `serve` runs them."""

# Standard library imports
import asyncio
import contextlib
import hmac
import logging
import traceback
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, override
from urllib.parse import quote

# Third party imports
import psycopg
import uvicorn
from aeth_ext.errors import install_loop_exception_handler, trigger_shutdown
from aeth_ext.errors.shutdown import ShutdownPhase, register_for_shutdown
from aeth_ext.monitoring import run_heartbeat_async
from authlib.integrations.starlette_client import OAuth
from nicegui import app, ui
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request  # noqa: TC002 - FastAPI reads route annotations at runtime
from starlette.responses import PlainTextResponse, RedirectResponse, Response

# Local folder imports
from . import text
from .db import PoolAccounts, Pools
from .gate import EndReason, Gate, SignedOutError
from .settings import PageSettings
from .shell import frame, prefs

if TYPE_CHECKING:
  # Standard library imports
  from collections.abc import Callable, Coroutine, Generator, Mapping

  # Third party imports
  from starlette.middleware.base import RequestResponseEndpoint

  # Local folder imports
  from .gate import Accounts, SignIn

logger = logging.getLogger(__name__)

STATIC_DIR: Final = Path(__file__).with_name("static")
# The sign-in page and its methods' routes, and the icon, which the sign-in page's tab shows too.
_PUBLIC_PATHS: Final = frozenset({"/signin", "/signin/zoho", "/auth/zoho", "/signin/test", "/favicon.ico"})
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
  """Register the page on NiceGUI's app: its files, the gate, the sign-in routes, and the pages.

  Each sign-in method only proves an email: the allowlist and the gate do the rest, so a method is a route that ends in
  `gate.start` and a way in on the sign-in page. Zoho is on when its client is set, the test sign-in when it has accounts.

  Args:
    settings: The page's settings.
    accounts: The production allowlist.
    zoho_client_kwargs: Extra httpx client arguments for talking to Zoho (tests pass a transport).
    now: The clock the gate uses.
  """
  gate = Gate(settings, accounts, app.storage.general, now=now)
  logins = settings.test_logins()
  zoho = (
    OAuth().register(
      "zoho",
      client_id=settings.zoho_client_id,
      client_secret=settings.zoho_client_secret.get_secret_value(),
      server_metadata_url=f"{settings.zoho_accounts_url}/.well-known/openid-configuration",
      client_kwargs={"scope": "openid email", "code_challenge_method": "S256", **(zoho_client_kwargs or {})},
    )
    if settings.zoho_client_id
    else None
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
          request.state.signin = await gate.check(app.storage.user, request.session, touch=True)
        except SignedOutError:
          return RedirectResponse("/signin", status_code=303)
        except psycopg.OperationalError:
          logger.warning("the allowlist can't be read", exc_info=True)
          return PlainTextResponse(text.DB_DOWN, status_code=503)
      return await call_next(request)

  app.add_middleware(SignInGate)

  if zoho is not None:

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
      email = None if userinfo is None else userinfo.get("email")
      if userinfo is None:
        ended = EndReason.FAILED
      elif userinfo.get("email_verified") is not True or not isinstance(email, str):
        ended = EndReason.REFUSED
      else:
        email = email.lower()  # badges are lowercase (0014), whatever casing Zoho reports
        try:
          account = await accounts.lookup(email)
        except psycopg.OperationalError:
          logger.warning("the allowlist can't be read", exc_info=True)
          return PlainTextResponse(text.DB_DOWN, status_code=503)
        if account is not None:
          gate.start(storage, request.session, email)
          return RedirectResponse("/", status_code=303)
        ended = EndReason.REFUSED
      try:  # a failed reply leaves a valid sign-in alone: Back into Zoho's page replays a used state
        await gate.check(storage, request.session, touch=False)
      except SignedOutError, psycopg.OperationalError:
        # In the cookie: server storage would keep a file for every cookieless request a script sends here.
        request.session["signed_out"] = ended.value
        return RedirectResponse("/signin", status_code=303)
      return RedirectResponse("/", status_code=303)

  if logins:

    @app.post("/signin/test")
    async def test_sign_in(request: Request) -> Response:
      form = await request.form()
      email, password = str(form.get("email") or "").strip().lower(), str(form.get("password") or "")
      expected = logins.get(email, "")
      # Timing-safe, and compared for an unlisted email too, so a wrong guess tells nothing about which emails are listed.
      if not hmac.compare_digest(password.encode(), expected.encode()) or not expected:
        logger.info("a test sign-in failed for %r", email)
        ended = EndReason.WRONG_PASSWORD
      else:
        try:
          account = await accounts.lookup(email)
        except psycopg.OperationalError:
          logger.warning("the allowlist can't be read", exc_info=True)
          return PlainTextResponse(text.DB_DOWN, status_code=503)
        if account is not None:
          gate.start(app.storage.user, request.session, email)
          return RedirectResponse("/", status_code=303)
        ended = EndReason.REFUSED
      request.session["signed_out"] = ended.value  # in the cookie, as the Zoho callback's
      return RedirectResponse("/signin", status_code=303)

  @ui.page("/signin", title=settings.page_name)
  async def signin_page(request: Request) -> Response | None:
    storage = app.storage.user
    # The callback's reason is in the cookie, the gate's in storage. Both read before any await: the cookie can change only
    # until the response starts.
    from_cookie, stored = request.session.pop("signed_out", None), storage.pop("signed_out", None)
    reason = EndReason(from_cookie or stored or EndReason.NONE)
    try:
      await gate.check(storage, request.session, touch=False)
    except SignedOutError, psycopg.OperationalError:
      pass
    else:
      return RedirectResponse("/", status_code=303)
    ui.dark_mode(prefs()["dark"])
    with ui.column().classes("signin-card"):
      ui.element("div").classes("page-logo")
      ui.label(settings.page_name).classes("signin-title")
      if (message := text.ended(reason, settings)) is not None:
        ui.label(message).classes("signin-message").mark("signin-message")
      if zoho is not None and reason is EndReason.REFUSED:  # §3.5: the message and another account, nothing else
        ui.button("Sign in with another account", on_click=lambda: ui.navigate.to(other_account)).classes("zoho-signin")
        ui.label("This signs you out of Zoho in this browser first.").classes("signin-note")
      elif zoho is not None:
        with ui.button(on_click=lambda: ui.navigate.to("/signin/zoho")).classes("zoho-signin").mark("zoho-signin"):
          ui.element("span").classes("zoho-logo")
          ui.label("Sign in using Zoho")
      if logins:  # a plain form: only a response can set the cookie a sign-in is bound to, never a page event
        with ui.element("form").props("method=post action=/signin/test").classes("test-signin").mark("test-signin"):
          ui.label("Test sign-in").classes("bubble-heading")
          ui.input("Email").props("name=email type=email autocomplete=username").classes("w-full")
          ui.input("Password", password=True).props("name=password autocomplete=current-password").classes("w-full")
          ui.button("Sign in").props("type=submit no-caps").classes("w-full")
    return None

  @ui.page("/", title=settings.page_name)
  async def home_page(request: Request) -> None:
    signin: SignIn = request.state.signin
    frame(signin, settings, gate)
    with ui.column().classes("page-body"):
      ui.label(f"Signed in as {signin.email}").mark("signed-in-as")
      ui.label("Shifts and employees will appear here.")


@contextlib.contextmanager
def _no_signal_capture(_server: uvicorn.Server) -> Generator[None]:
  # aeth_ext's signal handlers drive the shutdown; uvicorn's own capture would take the signals from them (as the clock).
  yield


def serve() -> None:
  """Build the page from the environment and run NiceGUI until the shutdown stops it.

  Raises:
    ValueError: A setting the page can't run without is empty.
  """
  settings = PageSettings.get_settings()
  required = {
    "PAGE_PUBLIC_URL": settings.public_url,
    "PAGE_SESSION_SECRET": settings.session_secret.get_secret_value(),
    "TIMECLOCK_WEB_PASSWORD": settings.db_password.get_secret_value(),
  }
  logins = settings.test_logins()  # refuses a malformed PAGE_TEST_ACCOUNTS
  zoho = {"ZOHO_CLIENT_ID": settings.zoho_client_id, "ZOHO_CLIENT_SECRET": settings.zoho_client_secret.get_secret_value()}
  if any(zoho.values()) or not logins:  # half of Zoho is a mistake, and without test accounts Zoho is the only way in
    required |= zoho
  if missing := [name for name, value in required.items() if not value]:
    instead = "" if logins or any(zoho.values()) else " (or PAGE_TEST_ACCOUNTS instead of Zoho's two)"
    raise ValueError(f"the page can't start without {', '.join(missing)}{instead}")
  # Socket logging creates no logs folder, and the page's volume starts empty; the healthcheck reads the heartbeat there.
  settings.log_loc_folder.mkdir(parents=True, exist_ok=True)
  pools = Pools.create(settings)
  # asyncio holds tasks weakly, so the heartbeat is kept here, and its failure shuts the page down (as the clock).
  running: set[asyncio.Future[Any]] = set()

  def keep(name: str, coro: Coroutine[Any, Any, Any]) -> None:
    fut = asyncio.ensure_future(coro)
    running.add(fut)

    def done(f: asyncio.Future[Any]) -> None:
      running.discard(f)
      if not f.cancelled() and (exc := f.exception()) is not None:
        trigger_shutdown(f"{name} stopped", "".join(traceback.format_exception(exc)))

    fut.add_done_callback(done)

  async def startup() -> None:
    install_loop_exception_handler()
    await pools.open()
    heartbeat = run_heartbeat_async(
      settings.log_loc_folder / "heartbeat.txt",
      ping_url=settings.alerts_healthcheck_ping_url,
      pingkey=settings.alerts_healthcheck_pingkey,
      tz=settings.tz,
    )
    keep("Heartbeat", heartbeat)

  app.on_startup(lambda: keep("Startup", startup()))  # through keep: NiceGUI would only log the exception
  app.on_shutdown(pools.close)
  # A plain lambda: aeth_ext holds bound methods weakly. app.shutdown() sets uvicorn's should_exit.
  register_for_shutdown(lambda _trails: app.shutdown(), phase=ShutdownPhase.THREADED)
  uvicorn.Server.capture_signals = _no_signal_capture  # pyright: ignore[reportAttributeAccessIssue]  # as the clock
  build(settings, PoolAccounts(pools))
  ui.run(
    host="0.0.0.0",  # Coolify's proxy reaches it over the coolify network
    port=settings.port,
    title=settings.page_name,
    dark=True,
    reload=False,
    show=False,
    storage_secret=settings.session_secret.get_secret_value(),
    # `__Host-`: no sibling subdomain can set it (session fixation). `lax` still sends it on Zoho's redirect back. It
    # carries the browser's storage id (preferences, why a sign-in ended) and is re-sent only when changed, so it lasts
    # Chrome's 400-day cap; the gate ends the sign-in.
    session_middleware_kwargs={
      "session_cookie": "__Host-timeclock-web",
      "https_only": True,
      "same_site": "lax",
      "max_age": 400 * 24 * 3600,
    },
    favicon=STATIC_DIR / "SFT-Logo-June-2022-color.svg",
    uvicorn_logging_level="warning",
    log_config=None,  # passed to uvicorn.Config; its dictConfig would close aeth_ext's handlers
  )
