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
  from .run_store import RunStore

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


def _fires(schedule: Cron, first: datetime, last: datetime) -> list[datetime]:
  """`schedule`'s times from its time `first` through `last`."""
  fires: list[datetime] = []
  while first <= last:
    fires.append(first)
    first = schedule.next_after(first)
  return fires


class Runner:
  """Runs the static and dynamic entries; only dynamic ones can be replaced or removed while it runs.

  A run is a task of its own, apart from its entry's waiting task, so replace/remove/stop can cancel a wait without
  cancelling a running job. One lock per name serialises runs across replacements.
  """

  def __init__(
    self, static: Sequence[Entry], dynamic: Sequence[Entry], *, store: RunStore | None = None, clock: Clock = SYSTEM_CLOCK
  ) -> None:
    """Validate the tables: names unique across both, every static entry scheduled, `catch_up` per `_check_catch_up`."""
    names = [e.name for e in (*static, *dynamic)]
    if dupes := sorted({n for n in names if names.count(n) > 1}):
      msg = f"duplicate entry names: {', '.join(dupes)}"
      raise ValueError(msg)
    if idle := [e.name for e in static if e.schedule is None]:
      msg = f"static entries need a schedule: {', '.join(idle)}"
      raise ValueError(msg)
    self._store = store
    for entry in (*static, *dynamic):
      self._check_catch_up(entry)
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
      self._launch(entry, starting=True)
      if (due := self._next[entry.name]) is None or entry.schedule is None:
        LOGGER.info("%s: idle", entry.name)
      else:
        LOGGER.info("%s: %s; next run %s", entry.name, entry.schedule.describe(), due.isoformat())

  def _check_catch_up(self, entry: Entry) -> None:
    """A `catch_up` entry needs the store and a cron schedule: an `At` has no latest past time to catch up."""
    if entry.catch_up and self._store is None:
      msg = f"{entry.name}: catch_up needs a run store"
      raise ValueError(msg)
    if entry.catch_up and not isinstance(entry.schedule, Cron | None):
      msg = f"{entry.name}: catch_up needs a cron schedule"
      raise ValueError(msg)

  def _launch(self, entry: Entry, *, starting: bool = False) -> None:
    """Cancel the entry's wait (never a running job) and, once started, wait on its new schedule.

    At start, a `catch_up` entry's wait first checks its latest past time against the store (spec §5).
    """
    if old := self._waits.pop(entry.name, None):
      old.cancel()
    for run in [r for r in self._resting if self._runs.get(r) == entry.name]:
      run.cancel()  # a superseded definition's retries end now, freeing the lock for the new one
    self._next[entry.name] = None
    if entry.schedule is None or self._loop is None or self._stopping:
      return
    now = self._clock.now()
    due = entry.schedule.next_after(now)
    self._next[entry.name] = due
    check = False
    if starting and entry.catch_up and isinstance(entry.schedule, Cron):
      due, check = entry.schedule.latest_before(now), True
    self._waits[entry.name] = asyncio.create_task(self._wait(entry, due, check=check), name=f"schedule {entry.name}")

  def replace(self, name: str, entry: Entry) -> None:
    """Replace a dynamic entry's definition; a run in progress finishes first."""
    if name not in self._dynamic:
      raise KeyError(name)
    if entry.name != name:
      msg = f"entry name {entry.name!r} doesn't match {name!r}"
      raise ValueError(msg)
    self._check_catch_up(entry)
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

  async def _wait(self, entry: Entry, due: datetime, *, check: bool = False) -> None:
    """Run the entry at each time from `due`; with `check`, `due` is start's past catch-up time.

    That first `due` needs no branch here: it doesn't sleep, and as a `catch_up` entry's latest time it runs however late.
    """
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
        # A late wake may span several times: run the latest if it's within LATE (any lateness with catch_up), log the rest.
        # max(): the spring-forward gap's time runs at the gap's end, which latest_before may place before it.
        latest = max(due, schedule.latest_before(now))
        run_latest = entry.catch_up or now - latest <= LATE
        missed = _fires(schedule, due, latest)[: -1 if run_latest else None]
        if missed:
          LOGGER.warning(
            "skipped %s's %d run(s) for %s through %s: woke at %s",
            name,
            len(missed),
            missed[0].isoformat(),
            missed[-1].isoformat(),
            now.isoformat(),
          )
        if not run_latest:
          due = schedule.next_after(now)
          continue
        due = latest
        if lock.locked():  # a replaced definition's run is still going
          LOGGER.warning("skipped %s's run for %s: its previous run is still going", name, due.isoformat())
          due = schedule.next_after(due)
          continue
      run = asyncio.create_task(self._run(entry, due, check=check), name=f"run {name}")
      check = False
      self._runs[run] = name
      run.add_done_callback(self._runs.pop)
      # Not `await run`: cancelling this wait (replace/remove/stop) must leave the run going, and a run's exception
      # (a KeyboardInterrupt from aeth_ext's exit nudge) must not re-raise here.
      await asyncio.wait((run,))
      if isinstance(schedule, At):
        break
      now = self._clock.now()
      if missed := _fires(schedule, schedule.next_after(due), now):
        LOGGER.warning(
          "skipped %s's %d run(s) for %s through %s: the run for %s was still going",
          name,
          len(missed),
          missed[0].isoformat(),
          missed[-1].isoformat(),
          due.isoformat(),
        )
      due = schedule.next_after(max(now, due))  # max(): a clock stepped back past `due` mustn't run it again
    self._next[name] = None
    if self._waits.get(name) is asyncio.current_task():
      del self._waits[name]

  async def _run(self, entry: Entry, due: datetime, *, check: bool = False) -> None:
    """Run the job, retrying per `entry.retry`; an `Exception` goes to `on_failure`, never out of here.

    A `catch_up` entry's run is recorded once the job returns; failing to record fails the run. With `check` (start's
    catch-up), it runs only when the store's period differs from `due`'s; with no record, `due` is recorded unrun so a
    deploy never sends a past period on the spot.

    `CancelledError`/`KeyboardInterrupt` aren't `Exception`s: they end the run as a stop, never a failure. A superseded
    definition (replaced or removed while queued on the lock or resting) never runs or retries again.
    """
    job = entry.job
    retry = entry.retry
    # Retries stop at the entry's next time, which then runs on its own.
    next_time = entry.schedule.next_after(due) if isinstance(entry.schedule, Cron) else None
    async with self._locks[entry.name]:
      attempt = 1
      while not self._stopping and self._entries[entry.name] is entry:
        try:
          await self._attempt(entry, due, check=check)
        except Exception as error:  # noqa: BLE001  # a job's error must never escape the runner
          if retry is None:
            retry_at, giving_up = None, False
          else:
            retry_at = self._clock.now() + retry.interval
            # A shutdown or a superseded definition ends the retries too: no rest holding the lock for nothing.
            giving_up = (
              attempt > retry.max_retries
              or (next_time is not None and retry_at >= next_time)
              or self._stopping
              or self._entries[entry.name] is not entry
            )
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

  async def _attempt(self, entry: Entry, due: datetime, *, check: bool) -> None:
    job, name, store = entry.job, entry.name, self._store
    if check:
      assert store is not None
      if (last := await store.get(name)) is None:
        LOGGER.info("%s: no recorded run; recording %s without running it", name, due.isoformat())
        await store.record(name, job.period(due), due)
        return
      if last.period == job.period(due):
        return
      LOGGER.info("%s: catching up its run for %s (last run for %s)", name, due.isoformat(), last.scheduled_for.isoformat())
    await job.main(due)
    if entry.catch_up:
      assert store is not None
      await store.record(name, job.period(due), due)

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
