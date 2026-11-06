"""The page's one sign-in gate (spec 2026-11-05 §3.3, §3.7).

Every page load (the middleware), timer and activity ping, and transaction (`editor_transaction`) goes through
`Gate.check`. The account it returns comes from the server's own record of the sign-in, held in NiceGUI's server-side
storage for that browser, never from anything the request carries, and it's re-read from the production allowlist on
every check, so deleting a row cuts off an open sign-in at once. A token in the browser's signed cookie binds the record
to the browser that completed the sign-in: the storage id outlives sign-ins, so a kept copy of the cookie would otherwise
come back as the next person to sign in there.
"""

# Standard library imports
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
  # Standard library imports
  from collections.abc import Callable, Mapping, MutableMapping

  # Local folder imports
  from .settings import PageSettings


@dataclass(frozen=True, slots=True)
class Account:
  """An allowlist row: the employee it may not edit (None for none) and the developer mark."""

  employee_id: int | None
  developer: bool


class Accounts(Protocol):
  """The production allowlist (§3.2), whichever database the page is showing."""

  async def lookup(self, email: str) -> Account | None:
    """The row for `email` (lowercase), or None when it isn't listed."""
    ...


@dataclass(frozen=True, slots=True)
class SignIn:
  """A checked sign-in; its badge is `email`."""

  email: str
  employee_id: int | None
  developer: bool
  test_database: bool
  started: datetime


class EndReason(StrEnum):
  """Why there's no sign-in; kept in the browser's storage so the sign-in page can say why."""

  NONE = "none"
  IDLE = "idle"
  EXPIRED = "expired"
  REFUSED = "refused"
  FAILED = "failed"
  SIGNED_OUT = "signed_out"


class SignedOutError(Exception):
  """The request has no valid sign-in."""

  def __init__(self, reason: EndReason) -> None:
    """Carry `reason`, which the sign-in page shows."""
    super().__init__(reason.value)
    self.reason = reason


class Gate:
  """Starts, checks and ends sign-ins held in a per-browser `storage` (`app.storage.user`).

  `session` is that browser's signed cookie (`request.session`, or `app.storage.browser` in a page), which carries the
  sign-in's token.

  `general` (`app.storage.general`) holds each developer's database choice, which is per account (§11).
  """

  def __init__(
    self,
    settings: PageSettings,
    accounts: Accounts,
    general: MutableMapping[str, Any],
    *,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
  ) -> None:
    """Read the session limits from `settings`; `now` is the clock, settable for tests."""
    self._session = timedelta(hours=settings.session_hours)
    self._idle = timedelta(minutes=settings.idle_minutes)
    self._accounts = accounts
    self._general = general
    self._now = now

  def start(self, storage: MutableMapping[str, Any], session: MutableMapping[str, Any], email: str) -> None:
    """Record a sign-in Zoho verified and the allowlist accepted, bound to this browser's cookie."""
    now = self._now().isoformat()
    token = secrets.token_urlsafe(16)
    session["signin_token"] = token
    storage.pop("signed_out", None)
    storage["signin"] = {"email": email, "started": now, "active": now, "token": token}

  async def check(self, storage: MutableMapping[str, Any], session: Mapping[str, Any], *, touch: bool) -> SignIn:
    """The sign-in in `storage`, after the session limits and the allowlist; `touch` counts this as activity.

    Raises:
      SignedOutError: There's none, it belongs to another copy of the cookie, or it just ended (then `storage` keeps
        the reason).
    """
    record = storage.get("signin")
    if record is None or session.get("signin_token") != record["token"]:  # a kept copy ends nothing: it just gets no sign-in
      raise SignedOutError(EndReason.NONE)
    now = self._now()
    started = datetime.fromisoformat(record["started"])
    if now - started > self._session:
      self.end(storage, EndReason.EXPIRED)
      raise SignedOutError(EndReason.EXPIRED)
    if now - datetime.fromisoformat(record["active"]) > self._idle:
      self.end(storage, EndReason.IDLE)
      raise SignedOutError(EndReason.IDLE)
    email = record["email"]
    account = await self._accounts.lookup(email)
    if account is None:
      self.end(storage, EndReason.REFUSED)
      raise SignedOutError(EndReason.REFUSED)
    # Re-read after the lookup: a sign-out in another tab meanwhile must not be undone by this touch.
    current = storage.get("signin")
    if current is None or current["token"] != record["token"]:
      raise SignedOutError(EndReason.NONE)
    if touch:
      storage["signin"] = {**current, "active": now.isoformat()}
    test = account.developer and self._general.get("databases", {}).get(email) == "test"
    return SignIn(email, account.employee_id, account.developer, test, started)

  def end(self, storage: MutableMapping[str, Any], reason: EndReason) -> None:
    """End the sign-in in `storage`, keeping `reason` for the sign-in page."""
    storage.pop("signin", None)
    storage["signed_out"] = reason.value

  async def choose_database(self, storage: MutableMapping[str, Any], session: Mapping[str, Any], *, test: bool) -> None:
    """Remember the signed-in account's database choice; `check` honors it only while the account is marked developer.

    Raises:
      SignedOutError: As `check`; nothing changed.
    """
    email = (await self.check(storage, session, touch=True)).email
    databases = {k: v for k, v in self._general.get("databases", {}).items() if k != email}
    if test:
      databases[email] = "test"
    self._general["databases"] = databases  # a new dict, so NiceGUI's storage sees the change
