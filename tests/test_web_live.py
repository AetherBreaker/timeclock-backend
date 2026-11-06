"""Tests for live updates (spec 2026-11-05 §6.1): the database's `timeclock` notifications wake the open pages."""

# Standard library imports
import asyncio
import contextlib
from datetime import UTC, datetime
from typing import TYPE_CHECKING

# Third party imports
import pytest

# First party imports
from timeclock_backend.web.live import Live

if TYPE_CHECKING:
  # Standard library imports
  from collections.abc import AsyncIterator

  # Third party imports
  from page_sim import PagesDb

QUIET = 0.2


class Calls:
  """A subscriber that counts its calls."""

  def __init__(self) -> None:
    self.count = 0
    self.called = asyncio.Event()

  async def __call__(self) -> None:
    self.count += 1
    self.called.set()


@pytest.fixture
async def live(pages_db: PagesDb) -> AsyncIterator[Live]:
  live = Live(pages_db.pools, quiet=QUIET, retry=0.1)
  task = asyncio.create_task(live.run())
  await asyncio.wait_for(asyncio.gather(live.listening[False].wait(), live.listening[True].wait()), 10)
  try:
    yield live
  finally:
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
      await task


async def test_a_burst_reaches_each_subscriber_once(pages_db: PagesDb, live: Live) -> None:
  first, second = Calls(), Calls()
  live.subscribe(test=False, callback=first)
  live.subscribe(test=False, callback=second)
  for _ in range(3):
    pages_db.punch(pages_db.employees[1], "clock_in", datetime.now(UTC))
  await asyncio.wait_for(first.called.wait(), 5)
  await asyncio.sleep(QUIET * 3)
  assert (first.count, second.count) == (1, 1)


async def test_the_other_database_wakes_only_its_own_subscribers(pages_db: PagesDb, live: Live) -> None:
  production, test = Calls(), Calls()
  live.subscribe(test=False, callback=production)
  live.subscribe(test=True, callback=test)
  pages_db.punch(pages_db.employees[1], "clock_in", datetime.now(UTC), database=pages_db.test)
  await asyncio.wait_for(test.called.wait(), 5)
  await asyncio.sleep(QUIET * 3)
  assert (production.count, test.count) == (0, 1)


async def test_unsubscribing_stops_the_calls(pages_db: PagesDb, live: Live) -> None:
  gone, kept = Calls(), Calls()
  unsubscribe = live.subscribe(test=False, callback=gone)
  live.subscribe(test=False, callback=kept)
  unsubscribe()
  pages_db.punch(pages_db.employees[1], "clock_in", datetime.now(UTC))
  await asyncio.wait_for(kept.called.wait(), 5)
  assert gone.count == 0


async def test_a_failing_subscriber_does_not_stop_the_others(pages_db: PagesDb, live: Live) -> None:
  async def broken() -> None:
    raise RuntimeError

  kept = Calls()
  live.subscribe(test=False, callback=broken)
  live.subscribe(test=False, callback=kept)
  pages_db.punch(pages_db.employees[1], "clock_in", datetime.now(UTC))
  await asyncio.wait_for(kept.called.wait(), 5)


async def test_a_dropped_listener_reconnects_and_wakes_everyone(pages_db: PagesDb, live: Live) -> None:
  woken = Calls()
  live.subscribe(test=False, callback=woken)
  with pages_db.admin() as conn:
    dropped = conn.execute(
      "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s AND query = 'LISTEN timeclock'",
      (pages_db.prod.name,),
    ).fetchall()
  assert dropped == [(True,)]
  await asyncio.wait_for(woken.called.wait(), 5)  # changes may have been missed while it was down
  woken.called.clear()
  pages_db.punch(pages_db.employees[1], "clock_in", datetime.now(UTC))
  await asyncio.wait_for(woken.called.wait(), 5)
