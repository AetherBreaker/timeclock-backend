"""Live updates (spec 2026-11-05 §6.1): database changes wake the open pages, which re-read as their own badges.

The database already announces every change on `timeclock` (`notify_change`, which the clocks listen to). The page's
login can read nothing, so a notification only says something changed in that database: every subscriber
of it re-reads. LISTEN needs no privilege.
"""

# Standard library imports
import asyncio
import logging
from typing import TYPE_CHECKING

# Third party imports
import psycopg

if TYPE_CHECKING:
  # Standard library imports
  from collections.abc import Awaitable, Callable

  # Local folder imports
  from .db import Pools

logger = logging.getLogger(__name__)

type Subscriber = Callable[[], Awaitable[None]]


class Live:
  """Listens to both databases and calls each one's subscribers once per burst of changes."""

  def __init__(self, pools: Pools, *, quiet: float = 0.4, retry: float = 5.0) -> None:
    """Listen as the pools' login; a burst ends `quiet` seconds after its last change; a lost listener retries after `retry`."""
    production, test = pools.production.conninfo, pools.test.conninfo
    assert isinstance(production, str) and isinstance(test, str)  # Pools.create passes strings
    self._conninfo = {False: production, True: test}
    self._quiet = quiet
    self._retry = retry
    self._subscribers: dict[bool, set[Subscriber]] = {False: set(), True: set()}
    self._pending: dict[bool, asyncio.TimerHandle] = {}
    self._waking: set[asyncio.Task[None]] = set()  # asyncio holds tasks weakly
    self.listening = {False: asyncio.Event(), True: asyncio.Event()}
    """Set while each database's listener is connected (test: True)."""

  def subscribe(self, *, test: bool, callback: Subscriber) -> Callable[[], None]:
    """Call `callback` after changes in the test (True) or production database; returns the unsubscribe."""
    self._subscribers[test].add(callback)
    return lambda: self._subscribers[test].discard(callback)

  async def run(self) -> None:
    """Listen to both databases until cancelled."""
    async with asyncio.TaskGroup() as group:
      for test in (False, True):
        group.create_task(self._listen(test=test))

  async def _listen(self, *, test: bool) -> None:
    reconnecting = False
    while True:
      try:
        async with await psycopg.AsyncConnection.connect(self._conninfo[test], autocommit=True) as conn:
          await conn.execute("LISTEN timeclock")
          self.listening[test].set()
          if reconnecting:  # changes made while it was down went unannounced
            self._poke(test=test)
          async for _ in conn.notifies():
            self._poke(test=test)
      except psycopg.OperationalError:
        logger.warning("the %s database's change listener dropped; retrying", "test" if test else "production", exc_info=True)
      self.listening[test].clear()
      reconnecting = True
      await asyncio.sleep(self._retry)

  def _poke(self, *, test: bool) -> None:
    # Restart the quiet period, so a burst (a save writes several rows) wakes the pages once.
    if (pending := self._pending.get(test)) is not None:
      pending.cancel()
    self._pending[test] = asyncio.get_running_loop().call_later(self._quiet, self._start_wake, test)

  def _start_wake(self, test: bool) -> None:
    task = asyncio.ensure_future(self._wake(test=test))
    self._waking.add(task)
    task.add_done_callback(self._waking.discard)

  async def _wake(self, *, test: bool) -> None:
    subscribers = list(self._subscribers[test])
    results = await asyncio.gather(*(s() for s in subscribers), return_exceptions=True)
    for subscriber, result in zip(subscribers, results, strict=True):
      if isinstance(result, Exception):  # one page's fault mustn't keep the others stale
        logger.error("a live update failed in %r", subscriber, exc_info=result)
