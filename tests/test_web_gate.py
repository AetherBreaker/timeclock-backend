"""Tests for the page's sign-in gate."""

# Standard library imports
from typing import Any, override

# Third party imports
import pytest
from page_sim import FakeAccounts, FakeClock, page_settings

# First party imports
from timeclock_backend.web.gate import Account, EndReason, Gate, SignedOutError, SignIn

ANN, BOB, DEV = "ann@example.com", "bob@example.com", "dev@example.com"


@pytest.fixture
def clock() -> FakeClock:
  return FakeClock()


@pytest.fixture
def accounts() -> FakeAccounts:
  return FakeAccounts({ANN: Account(7, developer=False), BOB: Account(8, developer=True), DEV: Account(None, developer=True)})


@pytest.fixture
def gate(accounts: FakeAccounts, clock: FakeClock) -> Gate:
  return Gate(page_settings(), accounts, {}, now=clock)


async def _ended(gate: Gate, storage: dict[str, Any], session: dict[str, Any], *, touch: bool = True) -> EndReason:
  with pytest.raises(SignedOutError) as raised:
    await gate.check(storage, session, touch=touch)
  return raised.value.reason


async def test_check_refuses_without_a_sign_in(gate: Gate) -> None:
  storage: dict[str, Any] = {}
  session: dict[str, Any] = {}
  assert await _ended(gate, storage, session) is EndReason.NONE
  assert storage == {}


async def test_check_returns_the_records_account(gate: Gate, clock: FakeClock) -> None:
  storage: dict[str, Any] = {}
  session: dict[str, Any] = {}
  gate.start(storage, session, ANN)
  assert await gate.check(storage, session, touch=True) == SignIn(ANN, 7, developer=False, test_database=False, started=clock.now)


async def test_check_ends_an_idle_sign_in(gate: Gate, clock: FakeClock) -> None:
  storage: dict[str, Any] = {}
  session: dict[str, Any] = {}
  gate.start(storage, session, ANN)
  clock.advance(minutes=30, seconds=1)
  assert await _ended(gate, storage, session) is EndReason.IDLE
  assert "signin" not in storage
  assert storage["signed_out"] == "idle"


async def test_activity_extends_only_the_idle_limit(gate: Gate, clock: FakeClock) -> None:
  storage: dict[str, Any] = {}
  session: dict[str, Any] = {}
  gate.start(storage, session, ANN)
  for _ in range(2):
    clock.advance(minutes=29)
    await gate.check(storage, session, touch=True)
  clock.advance(minutes=29)
  await gate.check(storage, session, touch=False)
  clock.advance(minutes=2)
  assert await _ended(gate, storage, session, touch=False) is EndReason.IDLE

  gate.start(storage, session, ANN)
  for _ in range(24):
    clock.advance(minutes=20)
    await gate.check(storage, session, touch=True)
  clock.advance(seconds=1)
  assert await _ended(gate, storage, session) is EndReason.EXPIRED


async def test_check_ends_a_sign_in_whose_allowlist_row_is_gone(gate: Gate, accounts: FakeAccounts) -> None:
  storage: dict[str, Any] = {}
  session: dict[str, Any] = {}
  gate.start(storage, session, ANN)
  del accounts.accounts[ANN]
  assert await _ended(gate, storage, session) is EndReason.REFUSED
  assert "signin" not in storage


async def test_check_looks_the_account_up_every_time(gate: Gate, accounts: FakeAccounts) -> None:
  storage: dict[str, Any] = {}
  session: dict[str, Any] = {}
  gate.start(storage, session, ANN)
  checks = 3
  for _ in range(checks):
    await gate.check(storage, session, touch=True)
  assert accounts.lookups == checks


async def test_a_developer_gets_their_chosen_database(gate: Gate) -> None:
  storage: dict[str, Any] = {}
  session: dict[str, Any] = {}
  gate.start(storage, session, DEV)
  gate.choose_database(DEV, test=True)
  assert (await gate.check(storage, session, touch=True)).test_database
  gate.choose_database(DEV, test=False)
  assert not (await gate.check(storage, session, touch=True)).test_database


async def test_losing_the_developer_mark_returns_to_production(gate: Gate, accounts: FakeAccounts) -> None:
  storage: dict[str, Any] = {}
  session: dict[str, Any] = {}
  gate.start(storage, session, DEV)
  gate.choose_database(DEV, test=True)
  accounts.accounts[DEV] = Account(None, developer=False)
  assert not (await gate.check(storage, session, touch=True)).test_database


async def test_another_accounts_choice_does_not_apply(gate: Gate) -> None:
  storage: dict[str, Any] = {}
  session: dict[str, Any] = {}
  gate.choose_database(BOB, test=True)
  gate.start(storage, session, DEV)
  assert not (await gate.check(storage, session, touch=True)).test_database


async def test_end_keeps_the_reason_for_the_sign_in_page(gate: Gate) -> None:
  storage: dict[str, Any] = {}
  session: dict[str, Any] = {}
  gate.start(storage, session, ANN)
  gate.end(storage, EndReason.SIGNED_OUT)
  assert storage == {"signed_out": "signed_out"}


async def test_a_sign_out_during_a_check_is_not_undone(accounts: FakeAccounts, clock: FakeClock) -> None:
  storage: dict[str, Any] = {}
  session: dict[str, Any] = {}

  class SignsOutMidLookup(FakeAccounts):
    @override
    async def lookup(self, email: str) -> Account | None:
      gate.end(storage, EndReason.SIGNED_OUT)  # another tab signs out while this check waits on the database
      return await super().lookup(email)

  gate = Gate(page_settings(), SignsOutMidLookup(accounts.accounts), {}, now=clock)
  gate.start(storage, session, ANN)
  with pytest.raises(SignedOutError):
    await gate.check(storage, session, touch=True)
  assert "signin" not in storage


async def test_a_sign_in_belongs_to_the_browser_that_completed_it(gate: Gate) -> None:
  storage: dict[str, Any] = {}
  session: dict[str, Any] = {}
  gate.start(storage, session, ANN)
  kept = dict(session)  # a copy of this browser's cookie
  gate.end(storage, EndReason.SIGNED_OUT)
  gate.start(storage, session, DEV)  # the next sign-in in the same browser
  assert await _ended(gate, storage, kept) is EndReason.NONE
  assert (await gate.check(storage, session, touch=True)).email == DEV  # the copy ended nothing
