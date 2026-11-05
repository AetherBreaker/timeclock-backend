"""Test helpers shared by the page's tests: a fake allowlist and a settable clock."""

# Standard library imports
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

# First party imports
from timeclock_backend.web.settings import PageSettings

if TYPE_CHECKING:
  # First party imports
  from timeclock_backend.web.gate import Account


def page_settings(**values: object) -> PageSettings:
  """`PageSettings` from these variables (by name) over the defaults, ignoring the environment and any `.env`."""
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
