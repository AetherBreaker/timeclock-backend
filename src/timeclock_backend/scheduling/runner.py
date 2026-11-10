"""The runner: one asyncio task per scheduled entry, sleeping until its next time and running its job."""

# Standard library imports
import asyncio
import dataclasses
import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Final, Protocol

# Local folder imports
from .schedules import At, Cron

if TYPE_CHECKING:
  # Standard library imports
  from collections.abc import Sequence

  # Local folder imports
  from .jobs import Entry

LOGGER: Final = logging.getLogger(__name__)

# Sleeps are capped so a wall-clock jump is noticed within a minute; a cron run later than LATE is skipped.
MAX_SLEEP: Final = 60.0
LATE: Final = timedelta(seconds=60)


class Clock(Protocol):
  """The runner's only source of time."""

  def now(self) -> datetime:
    """The current time, aware UTC."""
    ...

  async def sleep(self, seconds: float) -> None:
    """Sleep `seconds`."""
    ...


class _SystemClock:
  def now(self) -> datetime:
    return datetime.now(UTC)

  async def sleep(self, seconds: float) -> None:
    await asyncio.sleep(seconds)


SYSTEM_CLOCK: Final[Clock] = _SystemClock()


class Runner:
  """Runs the static and dynamic entries; only dynamic ones can be replaced or removed while it runs.

  A run is a task of its own, apart from its entry's waiting task, so replace/remove/stop can cancel a wait without
  cancelling a running job. One lock per name serialises runs across replacements.
  """

  def __init__(self, static: Sequence[Entry], dynamic: Sequence[Entry], *, store: object = None, clock: Clock = SYSTEM_CLOCK) -> None:
    """Validate the tables: names unique across both, every static entry scheduled."""
    names = [e.name for e in (*static, *dynamic)]
    if dupes := sorted({n for n in names if names.count(n) > 1}):
      msg = f"duplicate entry names: {', '.join(dupes)}"
      raise ValueError(msg)
    if idle := [e.name for e in static if e.schedule is None]:
      msg = f"static entries need a schedule: {', '.join(idle)}"
      raise ValueError(msg)
    self._store = store  # the catch-up run store; nothing records into it yet
    self._clock = clock
    self._dynamic = frozenset(e.name for e in dynamic)
    self._entries: dict[str, Entry] = {}
    for entry in (*static, *dynamic):
      self._entries[entry.name] = entry
      entry.job.name, entry.job.runner = entry.name, self
    self._locks = {n: asyncio.Lock() for n in names}
    self._next: dict[str, datetime | None] = dict.fromkeys(names)
    self._waits: dict[str, asyncio.Task[None]] = {}
    self._runs: dict[asyncio.Task[None], str] = {}
    self._resting: set[asyncio.Task[None]] = set()  # runs sleeping between retries
    self._loop: asyncio.AbstractEventLoop | None = None
    self._stopping = False

  async def start(self) -> None:
    """Log every entry with its next run and start waiting on each scheduled one."""
    self._loop = asyncio.get_running_loop()
    for entry in self._entries.values():
      self._launch(entry)
      if (due := self._next[entry.name]) is None or entry.schedule is None:
        LOGGER.info("%s: idle", entry.name)
      else:
        LOGGER.info("%s: %s; next run %s", entry.name, entry.schedule.describe(), due.isoformat())

  def _launch(self, entry: Entry) -> None:
    """Cancel the entry's wait (never its run) and, once started, wait on its new schedule."""
    if old := self._waits.pop(entry.name, None):
      old.cancel()
    self._next[entry.name] = None
    if entry.schedule is None or self._loop is None or self._stopping:
      return
    due = entry.schedule.next_after(self._clock.now())
    self._next[entry.name] = due
    self._waits[entry.name] = asyncio.create_task(self._wait(entry, due), name=f"schedule {entry.name}")

  def replace(self, name: str, entry: Entry) -> None:
    """Replace a dynamic entry's definition; a run in progress finishes first."""
    if name not in self._dynamic:
      raise KeyError(name)
    if entry.name != name:
      msg = f"entry name {entry.name!r} doesn't match {name!r}"
      raise ValueError(msg)
    self._entries[name] = entry
    entry.job.name, entry.job.runner = name, self
    self._launch(entry)

  def remove(self, name: str) -> None:
    """Idle a dynamic entry; its name stays declared, so it can be replaced again."""
    if name not in self._dynamic:
      raise KeyError(name)
    self.replace(name, dataclasses.replace(self._entries[name], schedule=None))

  def next_run(self, name: str) -> datetime | None:
    """The entry's next scheduled time, or None when idle; `KeyError` for an undeclared name."""
    return self._next[name]

  async def _wait(self, entry: Entry, due: datetime) -> None:
    schedule = entry.schedule
    assert schedule is not None
    name = entry.name
    lock = self._locks[name]
    while True:
      self._next[name] = due
      while (left := (due - self._clock.now()).total_seconds()) > 0:
        await self._clock.sleep(min(left, MAX_SLEEP))
      now = self._clock.now()
      if isinstance(schedule, Cron):  # an At always runs, however late, since nothing else would reschedule it
        if not entry.catch_up and now - due > LATE:
          LOGGER.warning("skipped %s's run for %s: woke %s late", name, due.isoformat(), now - due)
          due = schedule.next_after(now)
          continue
        if lock.locked():  # a replaced definition's run is still going
          LOGGER.warning("skipped %s's run for %s: its previous run is still going", name, due.isoformat())
          due = schedule.next_after(due)
          continue
      run = asyncio.create_task(self._run(entry, due), name=f"run {name}")
      self._runs[run] = name
      run.add_done_callback(self._runs.pop)
      # Not `await run`: cancelling this wait (replace/remove/stop) must leave the run going, and a run's exception
      # (a KeyboardInterrupt from aeth_ext's exit nudge) must not re-raise here.
      await asyncio.wait((run,))
      if isinstance(schedule, At):
        break
      ran_for, due = due, schedule.next_after(due)
      if due <= (now := self._clock.now()):
        LOGGER.warning("skipped %s's run for %s: the run for %s was still going", name, due.isoformat(), ran_for.isoformat())
        due = schedule.next_after(now)
    self._next[name] = None
    if self._waits.get(name) is asyncio.current_task():
      del self._waits[name]

  async def _run(self, entry: Entry, due: datetime) -> None:
    """Run the job, retrying per `entry.retry`; an `Exception` goes to `on_failure`, never out of here.

    `CancelledError`/`KeyboardInterrupt` aren't `Exception`s: they end the run as a stop, never a failure.
    """
    job = entry.job
    retry = entry.retry
    # Retries stop at the entry's next time, which then runs on its own.
    next_time = entry.schedule.next_after(due) if isinstance(entry.schedule, Cron) else None
    async with self._locks[entry.name]:
      attempt = 1
      while not self._stopping:
        try:
          await job.main(due)
        except Exception as error:  # noqa: BLE001  # a job's error must never escape the runner
          if retry is None:
            retry_at, giving_up = None, False
          else:
            retry_at = self._clock.now() + retry.interval
            giving_up = attempt > retry.max_retries or (next_time is not None and retry_at >= next_time)
          try:
            await job.on_failure(due, error, attempt, giving_up)
          except Exception:
            LOGGER.exception("on_failure of %s raised", entry.name)
          if retry_at is None or giving_up:
            return
        else:
          return
        task = asyncio.current_task()
        assert task is not None
        self._resting.add(task)  # stop cancels a run resting between retries at once, not after its budget
        try:
          while (left := (retry_at - self._clock.now()).total_seconds()) > 0:
            await self._clock.sleep(min(left, MAX_SLEEP))
        finally:
          self._resting.discard(task)
        attempt += 1

  def _halt(self) -> None:
    self._stopping = True
    for wait in self._waits.values():
      wait.cancel()
    self._waits.clear()
    for run in self._resting:
      run.cancel()

  def request_stop(self) -> None:
    """Stop new runs from starting; safe from any thread. `stop` is the awaited teardown."""
    if self._loop is None:
      self._stopping = True
    else:
      self._loop.call_soon_threadsafe(self._halt)

  async def stop(self, timeout: float) -> None:  # noqa: ASYNC109  # a budget for the running jobs, on the runner's clock
    """Stop new runs, give running jobs up to `timeout` seconds to finish, then cancel them."""
    waits = list(self._waits.values())
    self._halt()
    await asyncio.gather(*waits, return_exceptions=True)
    runs = set(self._runs)
    if not runs:
      return
    finished = asyncio.ensure_future(asyncio.wait(runs))
    budget = asyncio.ensure_future(self._clock.sleep(timeout))
    await asyncio.wait((finished, budget), return_when=asyncio.FIRST_COMPLETED)
    finished.cancel()
    budget.cancel()
    if late := [run for run in runs if not run.done()]:
      LOGGER.warning("cancelling scheduled job(s) still running at shutdown: %s", ", ".join(self._runs[r] for r in late))
      for run in late:
        run.cancel()
    # Retrieves each run's outcome (a stored KeyboardInterrupt included) so none is reported as never retrieved.
    await asyncio.gather(*runs, return_exceptions=True)
