"""Test helpers shared by the page's tests: a fake allowlist, a settable clock, and signing in through the fake Zoho."""

# Standard library imports
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlsplit

# First party imports
from timeclock_backend.web.settings import PageSettings

if TYPE_CHECKING:
  # Third party imports
  from fake_zoho import FakeZoho
  from nicegui.testing import User

  # First party imports
  from timeclock_backend.web.gate import Account

# What the next `page_main.py` run passes to `build`; the `page` fixture fills it.
BUILD_ARGS: dict[str, Any] = {}


def page_settings(**values: object) -> PageSettings:
  """`PageSettings` from these variables (by name), over the environment and the defaults."""
  return PageSettings.model_validate({"ALERTS_EMAIL_PWD": "test", **values})


@dataclass
class FakeAccounts:
  """The allowlist as a dict; counts lookups, and raises `fail` when set."""

  accounts: dict[str, Account] = field(default_factory=dict)
  lookups: int = 0
  fail: Exception | None = None

  async def lookup(self, email: str) -> Account | None:
    self.lookups += 1
    if self.fail is not None:
      raise self.fail
    return self.accounts.get(email)


@dataclass
class FakeClock:
  now: datetime = field(default_factory=lambda: datetime(2026, 11, 5, 14, 0, tzinfo=UTC))

  def __call__(self) -> datetime:
    return self.now

  def advance(self, **delta: float) -> None:
    self.now += timedelta(**delta)


@dataclass
class PageUnderTest:
  user: User
  zoho: FakeZoho
  accounts: FakeAccounts
  clock: FakeClock


def authorize_query(location: str) -> dict[str, str]:
  return {key: values[0] for key, values in parse_qs(urlsplit(location).query).items()}


async def sign_in(page: PageUnderTest, email: str | None = None) -> None:
  """Sign in through the fake Zoho as `email` (its default, ann@example.com, when None), as a browser would."""
  if email is not None:
    page.zoho.claims["email"] = email
  started = await page.user.http_client.get("/signin/zoho")
  query = authorize_query(started.headers["location"])
  page.zoho.nonce = query["nonce"]
  # Then the page Zoho's reply redirects to, as the browser's address: NiceGUI's simulated reload re-requests the path
  # opened, which for the reply itself would replay a used state.
  reply = await page.user.http_client.get(f"/auth/zoho?code=c&state={query['state']}")
  await page.user.open(reply.headers["location"])
