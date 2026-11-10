"""Tests for catching up a missed run at start, and the run stores."""

# Standard library imports
import asyncio
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, override

# Third party imports
import pytest
from scheduling_sim import NY, FakeClock, RecordingJob, ny, until

# First party imports
# Local application imports
from timeclock_backend.bootstrap import bootstrap
from timeclock_backend.databases import connect
from timeclock_backend.scheduling import At, Cron, Entry, MemoryRunStore, PgRunStore, Retry, Runner, RunRecord, jobs

if TYPE_CHECKING:
  # Standard library imports
  from collections.abc import AsyncIterator

  # First party imports
  from timeclock_backend.databases import Database

MONDAYS = Cron("0 6 * * MON", NY)


class WeeklyJob(RecordingJob):
  """A run's period is its week's Monday, as the payroll report's is."""

  @override
  def period(self, scheduled_for: datetime) -> str:
    day = scheduled_for.astimezone(NY).date()
    return (day - timedelta(days=day.weekday())).isoformat()


class FailingStore(MemoryRunStore):
  @override
  async def record(self, name: str, period: str, scheduled_for: datetime) -> None:
    msg = "database down"
    raise RuntimeError(msg)


@pytest.fixture
def clock() -> FakeClock:
  return FakeClock(ny(5, 30))  # Tuesday 2026-11-10; the latest Monday time is 2026-11-09 06:00


@pytest.fixture
async def runners() -> AsyncIterator[list[Runner]]:
  made: list[Runner] = []
  yield made
  for runner in made:
    await runner.stop(0)


async def started(runners: list[Runner], clock: FakeClock, store: MemoryRunStore, *entries: Entry) -> Runner:
  runner = Runner(entries, [], store=store, clock=clock)
  runners.append(runner)
  await runner.start()
  return runner


async def settle() -> None:
  for _ in range(1000):  # ~1000 fake minutes: well short of the next Monday
    await asyncio.sleep(0)


async def test_missed_week_runs_once_at_start(clock: FakeClock, runners: list[Runner]) -> None:
  store = MemoryRunStore()
  store.records["weekly"] = RunRecord("2026-11-02", ny(6, day=2))
  job = WeeklyJob(clock)
  runner = await started(runners, clock, store, Entry("weekly", MONDAYS, job, catch_up=True))
  await until(lambda: job.seen != [])
  await settle()
  assert job.seen == [ny(6, day=9)]  # told the scheduled time, not the wall-clock time
  assert job.ran_at == [ny(5, 30)]
  assert store.records["weekly"] == RunRecord("2026-11-09", ny(6, day=9))
  assert runner.next_run("weekly") == ny(6, day=16)


async def test_two_missed_weeks_send_one(clock: FakeClock, runners: list[Runner]) -> None:
  store = MemoryRunStore()
  store.records["weekly"] = RunRecord("2026-10-26", datetime(2026, 10, 26, 6, tzinfo=NY))
  job = WeeklyJob(clock)
  await started(runners, clock, store, Entry("weekly", MONDAYS, job, catch_up=True))
  await until(lambda: job.seen != [])
  await settle()
  assert job.seen == [ny(6, day=9)]
  assert store.records["weekly"] == RunRecord("2026-11-09", ny(6, day=9))


async def test_no_record_records_without_running(clock: FakeClock, runners: list[Runner]) -> None:
  store = MemoryRunStore()
  job = WeeklyJob(clock)
  await started(runners, clock, store, Entry("weekly", MONDAYS, job, catch_up=True))
  await until(lambda: "weekly" in store.records)
  await settle()
  assert job.seen == []
  assert store.records["weekly"] == RunRecord(job.period(ny(6, day=9)), ny(6, day=9))


async def test_cron_change_within_a_week_does_not_resend(clock: FakeClock, runners: list[Runner]) -> None:
  store = MemoryRunStore()
  done = RunRecord("2026-11-09", ny(6, day=9))  # this week's ran at 06:00; the cron is now 09:00
  store.records["weekly"] = done
  job = WeeklyJob(clock)
  await started(runners, clock, store, Entry("weekly", Cron("0 9 * * MON", NY), job, catch_up=True))
  await settle()
  assert job.seen == []
  assert store.records["weekly"] == done


async def test_cron_moved_later_on_the_day_of_its_run_does_not_resend(runners: list[Runner]) -> None:
  clock = FakeClock(ny(7, day=9))  # Monday, between this week's 06:00 run and the new 09:00
  store = MemoryRunStore()
  store.records["weekly"] = RunRecord("2026-11-09", ny(6, day=9))
  job = WeeklyJob(clock)
  runner = await started(runners, clock, store, Entry("weekly", Cron("0 9 * * MON", NY), job, catch_up=True))
  await until(lambda: job.seen != [])
  assert job.seen == [ny(9, day=16)]  # not last week's at start, not this week's again at 09:00
  assert store.records["weekly"] == RunRecord("2026-11-16", ny(9, day=16))
  await until(lambda: runner.next_run("weekly") == ny(9, day=23))


async def test_cron_moved_to_a_later_day_midweek_does_not_resend(runners: list[Runner]) -> None:
  clock = FakeClock(ny(12, day=11))  # Wednesday; Monday's ran, the cron is now Fridays
  store = MemoryRunStore()
  store.records["weekly"] = RunRecord("2026-11-09", ny(6, day=9))
  job = WeeklyJob(clock)
  await started(runners, clock, store, Entry("weekly", Cron("0 6 * * FRI", NY), job, catch_up=True))
  await until(lambda: job.seen != [])
  assert job.seen == [ny(6, day=20)]  # Friday the 13th is this week's: skipped
  assert store.records["weekly"] == RunRecord("2026-11-16", ny(6, day=20))


async def test_a_catch_up_overrunning_the_next_time_runs_it_late(runners: list[Runner]) -> None:
  clock = FakeClock(ny(5, 59, 30, day=16))  # two weeks unsent; the catch-up for the 9th takes 2 minutes
  store = MemoryRunStore()
  store.records["weekly"] = RunRecord("2026-11-02", ny(6, day=2))
  job = WeeklyJob(clock)
  job.during = lambda: setattr(clock, "t", clock.t + timedelta(minutes=2))
  runner = await started(runners, clock, store, Entry("weekly", MONDAYS, job, catch_up=True))
  await until(lambda: job.seen[1:] != [])
  assert job.seen == [ny(6, day=9), ny(6, day=16)]
  assert job.ran_at == [ny(5, 59, 30, day=16), ny(6, 1, 30, day=16)]
  assert store.records["weekly"] == RunRecord("2026-11-16", ny(6, day=16))
  await until(lambda: runner.next_run("weekly") == ny(6, day=23))


async def test_restart_just_after_the_time_sends_once(runners: list[Runner]) -> None:
  clock = FakeClock(ny(6, 0, 30, day=16))  # Monday, 30 s past the time; last week's is recorded
  store = MemoryRunStore()
  store.records["weekly"] = RunRecord("2026-11-09", ny(6, day=9))
  job = WeeklyJob(clock)
  runner = await started(runners, clock, store, Entry("weekly", MONDAYS, job, catch_up=True))
  await until(lambda: job.seen != [])
  await settle()
  assert job.seen == [ny(6, day=16)]
  assert job.ran_at == [ny(6, 0, 30, day=16)]
  assert store.records["weekly"] == RunRecord("2026-11-16", ny(6, day=16))
  assert runner.next_run("weekly") == ny(6, day=23)


async def test_failed_run_is_not_recorded(clock: FakeClock, runners: list[Runner]) -> None:
  store = MemoryRunStore()
  before = RunRecord("2026-11-02", ny(6, day=2))
  store.records["weekly"] = before
  job = WeeklyJob(clock, fails=-1)
  await started(runners, clock, store, Entry("weekly", MONDAYS, job, catch_up=True))
  await until(lambda: job.failures != [])
  await settle()
  assert job.seen == [ny(6, day=9)]
  assert job.failures == [(1, False)]
  assert store.records["weekly"] == before


async def test_a_catch_up_failing_once_succeeds_on_retry(
  clock: FakeClock, runners: list[Runner], monkeypatch: pytest.MonkeyPatch
) -> None:
  alerts: list[str] = []
  monkeypatch.setattr(jobs, "alert", lambda reason, *_, **__: alerts.append(reason))
  store = MemoryRunStore()
  store.records["weekly"] = RunRecord("2026-11-02", ny(6, day=2))
  job = WeeklyJob(clock, fails=1)
  await started(runners, clock, store, Entry("weekly", MONDAYS, job, catch_up=True, retry=Retry(timedelta(minutes=15), 4)))
  await until(lambda: job.ended[1:] != [])
  await settle()
  assert job.seen == [ny(6, day=9)] * 2
  assert job.ran_at == [ny(5, 30), ny(5, 45)]
  assert job.failures == [(1, False)]
  assert alerts == ["Scheduled job weekly failed"]
  assert store.records["weekly"] == RunRecord("2026-11-09", ny(6, day=9))


async def test_a_failure_to_record_is_a_failed_run(clock: FakeClock, runners: list[Runner]) -> None:
  store = FailingStore()
  store.records["weekly"] = RunRecord("2026-11-02", ny(6, day=2))
  job = WeeklyJob(clock)
  await started(runners, clock, store, Entry("weekly", MONDAYS, job, catch_up=True))
  await until(lambda: job.failures != [])
  assert job.ended == [ny(6, day=9)]  # the job itself returned
  assert job.failures == [(1, False)]


async def test_a_scheduled_run_of_a_catch_up_entry_is_recorded(clock: FakeClock, runners: list[Runner]) -> None:
  store = MemoryRunStore()
  store.records["daily"] = RunRecord(ny(6, day=9).isoformat(), ny(6, day=9))
  job = RecordingJob(clock)
  await started(runners, clock, store, Entry("daily", Cron("0 6 * * *", NY), job, catch_up=True))
  await until(lambda: job.seen != [])
  await settle()
  assert job.seen == [ny(6)]  # the loop's own run, nothing caught up
  assert store.records["daily"] == RunRecord(ny(6).isoformat(), ny(6))


async def test_catch_up_needs_a_store_and_a_cron(clock: FakeClock) -> None:
  job = RecordingJob(clock)
  with pytest.raises(ValueError, match="store"):
    Runner([Entry("weekly", MONDAYS, job, catch_up=True)], [], clock=clock)
  with pytest.raises(ValueError, match="cron"):
    Runner([Entry("once", At(ny(6)), job, catch_up=True)], [], store=MemoryRunStore(), clock=clock)
  runner = Runner([], [Entry("d", None, job, catch_up=True)], store=MemoryRunStore(), clock=clock)  # idle is fine
  with pytest.raises(ValueError, match="cron"):
    runner.replace("d", Entry("d", At(ny(6)), job, catch_up=True))


async def test_pg_run_store_round_trips(fresh_database: Database) -> None:
  await bootstrap([fresh_database])
  store = PgRunStore(fresh_database)
  assert await store.get("weekly") is None
  sql = "SELECT completed_at FROM backend.scheduled_runs WHERE name = 'weekly'"
  await store.record("weekly", "2026-11-02", ny(6, day=2))
  async with await connect(fresh_database.name) as conn:
    [first] = await (await conn.execute(sql)).fetchall()
  await store.record("weekly", "2026-11-09", ny(6, day=9))
  assert await store.get("weekly") == RunRecord("2026-11-09", ny(6, day=9))
  async with await connect(fresh_database.name) as conn:
    [second] = await (await conn.execute(sql)).fetchall()
  assert first["completed_at"] is not None
  assert second["completed_at"] > first["completed_at"]
  assert await store.get("other") is None


@pytest.mark.parametrize(
  ("retry", "catch_up", "stopping", "line"),
  [
    (Retry(timedelta(minutes=15), 1), True, False, True),
    (None, True, False, True),
    (Retry(timedelta(minutes=15), 1), False, False, False),
    (None, False, False, False),
    (None, True, True, False),
    (Retry(timedelta(minutes=15), 1), True, True, False),
  ],
)
async def test_final_failure_of_a_catch_up_entry_says_a_restart_catches_it_up(  # noqa: PLR0917  # parametrized
  clock: FakeClock,
  runners: list[Runner],
  monkeypatch: pytest.MonkeyPatch,
  retry: Retry | None,
  catch_up: bool,
  stopping: bool,
  line: bool,
) -> None:
  alerts: list[str] = []
  monkeypatch.setattr(jobs, "alert", lambda _reason, details, **_: alerts.append(details))
  store = MemoryRunStore()
  store.records["weekly"] = RunRecord("2026-11-02", ny(6, day=2))
  block = asyncio.Event() if stopping else None
  job = WeeklyJob(clock, fails=-1, block=block)
  runner = await started(runners, clock, store, Entry("weekly", MONDAYS, job, catch_up=catch_up, retry=retry))
  if stopping:
    await until(lambda: job.seen != [])
    runner.request_stop()
    for _ in range(10):
      await asyncio.sleep(0)
    assert block is not None
    block.set()
  await until(lambda: any(f[1] or retry is None for f in job.failures))
  await settle()
  assert alerts
  expected = (
    f"Not recorded: a restart before {MONDAYS.next_after(ny(6, day=9)).isoformat()} runs it once; after that this run is skipped."
  )
  assert any(expected in a for a in alerts) is line


async def test_a_non_final_failure_of_a_catch_up_entry_does_not_say_it(
  clock: FakeClock, runners: list[Runner], monkeypatch: pytest.MonkeyPatch
) -> None:
  alerts: list[str] = []
  monkeypatch.setattr(jobs, "alert", lambda _reason, details, **_: alerts.append(details))
  store = MemoryRunStore()
  store.records["weekly"] = RunRecord("2026-11-02", ny(6, day=2))
  job = WeeklyJob(clock, fails=-1)
  await started(runners, clock, store, Entry("weekly", MONDAYS, job, catch_up=True, retry=Retry(timedelta(minutes=15), 4)))
  await until(lambda: alerts != [])
  assert job.failures == [(1, False)]
  assert "Not recorded" not in alerts[0]
