"""Tests for the job runner, on a fake clock."""

# Standard library imports
import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, override

# Third party imports
import pytest
from scheduling_sim import HOURLY, NY, FakeClock, RecordingJob, ny, until

# First party imports
# Local application imports
from timeclock_backend.scheduling import At, Cron, Entry, MemoryRunStore, Retry, Runner, RunRecord, jobs

if TYPE_CHECKING:
  # Standard library imports
  from collections.abc import AsyncIterator


@pytest.fixture(autouse=True)
def alerts(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str, dict[str, Any]]]:
  """Record `alert` calls (aeth_ext's is a no-op outside `-O`); every call must pass `wait=False` so it can't block the loop."""
  recorded: list[tuple[str, str, dict[str, Any]]] = []

  def record(reason: str, details: str, **kwargs: Any) -> None:
    assert kwargs.get("wait") is False
    recorded.append((reason, details, kwargs))

  monkeypatch.setattr(jobs, "alert", record)
  return recorded


@pytest.fixture
def clock() -> FakeClock:
  return FakeClock(ny(5, 30))


@pytest.fixture
async def runners() -> AsyncIterator[list[Runner]]:
  """Runners a test appends here are stopped afterwards."""
  made: list[Runner] = []
  yield made
  for runner in made:
    await runner.stop(0)


async def started(
  runners: list[Runner], clock: FakeClock, static: list[Entry], dynamic: list[Entry] | None = None, store: MemoryRunStore | None = None
) -> Runner:
  runner = Runner(static, dynamic or [], store=store, clock=clock)
  runners.append(runner)
  await runner.start()
  return runner


def done_at_5(job: RecordingJob) -> MemoryRunStore:
  """A store with the 05:00 run recorded, so starting at 05:xx has nothing to catch up."""
  store = MemoryRunStore()
  store.records["hourly"] = RunRecord(job.period(ny(5)), ny(5))
  return store


async def test_cron_entry_runs_at_each_time(clock: FakeClock, runners: list[Runner]) -> None:
  job = RecordingJob(clock)
  await started(runners, clock, [Entry("hourly", HOURLY, job)])
  await until(lambda: job.seen[1:] != [])
  assert job.seen == [ny(6), ny(7)]
  assert job.ran_at == [ny(6), ny(7)]


async def test_late_cron_run_is_skipped_and_logged(clock: FakeClock, runners: list[Runner], caplog: pytest.LogCaptureFixture) -> None:
  clock.t = ny(5, 59, 30)
  clock.lag = timedelta(seconds=61)  # the first sleep wakes at 06:01:01
  job = RecordingJob(clock)
  await started(runners, clock, [Entry("hourly", HOURLY, job)])
  await until(lambda: len(job.seen) == 1)
  assert job.seen == [ny(7)]
  assert [r.levelno for r in caplog.records if "skipped" in r.getMessage()] == [logging.WARNING]


async def test_late_run_of_a_catch_up_entry_still_runs(clock: FakeClock, runners: list[Runner]) -> None:
  clock.t = ny(5, 59, 30)
  clock.lag = timedelta(seconds=61)
  job = RecordingJob(clock)
  await started(runners, clock, [Entry("hourly", HOURLY, job, catch_up=True)], store=done_at_5(job))
  await until(lambda: len(job.seen) == 1)
  assert job.seen == [ny(6)]
  assert job.ran_at == [ny(6, 1, 1)]


async def test_overrunning_run_skips_the_missed_time_once(
  clock: FakeClock, runners: list[Runner], caplog: pytest.LogCaptureFixture
) -> None:
  block = asyncio.Event()
  job = RecordingJob(clock, block=block)
  await started(runners, clock, [Entry("hourly", HOURLY, job)])
  await until(lambda: len(job.seen) == 1)
  clock.t += timedelta(minutes=70)  # the run is still going at 07:00
  block.set()
  await until(lambda: job.seen[1:] != [])
  assert job.seen == [ny(6), ny(8)]
  skips = [r.getMessage() for r in caplog.records if "skipped" in r.getMessage()]
  assert len(skips) == 1
  assert ny(7).isoformat() in skips[0]


async def test_at_replaced_during_its_run_runs_after_it(clock: FakeClock, runners: list[Runner]) -> None:
  block = asyncio.Event()
  job = RecordingJob(clock, block=block)
  runner = await started(runners, clock, [], [Entry("once", At(ny(6)), job)])
  job.during = lambda: runner.replace("once", Entry("once", At(clock.now()), job))
  await until(lambda: len(job.seen) == 1)
  for _ in range(100):
    await asyncio.sleep(0)
  assert job.seen == [ny(6)]  # the replacement waits for the run it was made in
  block.set()
  await until(lambda: job.seen[1:] != [])
  assert job.seen == [ny(6), ny(6)]
  assert job.ended[0] == ny(6)
  await until(lambda: runner.next_run("once") is None)


async def test_failure_alerts_and_keeps_later_runs(
  clock: FakeClock, runners: list[Runner], alerts: list[tuple[str, str, dict[str, Any]]]
) -> None:
  job = RecordingJob(clock, fails=1)
  await started(runners, clock, [Entry("hourly", HOURLY, job)])
  await until(lambda: job.seen[1:] != [])
  assert job.failures == [(1, False)]
  assert [a[0] for a in alerts] == ["Scheduled job hourly failed"]
  assert "RuntimeError: boom" in alerts[0][1]
  assert job.seen == [ny(6), ny(7)]


async def test_retry_runs_every_interval_up_to_the_cap(
  clock: FakeClock, runners: list[Runner], alerts: list[tuple[str, str, dict[str, Any]]]
) -> None:
  job = RecordingJob(clock, fails=-1)
  await started(runners, clock, [Entry("daily", Cron("0 6 * * *", NY), job, retry=Retry(timedelta(minutes=15), 4))])
  await until(lambda: job.failures[4:] != [])
  for _ in range(1000):
    await asyncio.sleep(0)
  assert job.ran_at == [ny(6), ny(6, 15), ny(6, 30), ny(6, 45), ny(7)]
  assert job.seen == [ny(6)] * 5
  assert job.failures == [(1, False), (2, False), (3, False), (4, False), (5, True)]
  assert [a[0] for a in alerts] == ["Scheduled job daily failed"] * 2


async def test_retry_stops_at_the_next_scheduled_time(clock: FakeClock, runners: list[Runner]) -> None:
  job = RecordingJob(clock, fails=-1)
  await started(runners, clock, [Entry("hourly", HOURLY, job, retry=Retry(timedelta(minutes=15), 4))])
  await until(lambda: job.seen[4:] != [])
  assert job.ran_at == [ny(6), ny(6, 15), ny(6, 30), ny(6, 45), ny(7)]
  assert job.seen == [ny(6)] * 4 + [ny(7)]
  assert job.failures[:5] == [(1, False), (2, False), (3, False), (4, True), (1, False)]


async def test_replace_and_remove_dynamic_entries(clock: FakeClock, runners: list[Runner]) -> None:
  job = RecordingJob(clock)
  runner = await started(runners, clock, [], [Entry("d", Cron("0 6 * * *", NY), job)])
  assert runner.next_run("d") == ny(6)
  runner.replace("d", Entry("d", Cron("0 9 * * *", NY), job))
  assert runner.next_run("d") == ny(9)
  runner.remove("d")
  assert runner.next_run("d") is None
  for _ in range(1000):
    await asyncio.sleep(0)
  assert job.seen == []
  assert clock.now() == ny(5, 30)  # nothing is waiting
  runner.replace("d", Entry("d", At(ny(8)), job))
  assert runner.next_run("d") == ny(8)
  await until(lambda: job.seen == [ny(8)])


async def test_replace_rejects_static_and_undeclared_names(clock: FakeClock, runners: list[Runner]) -> None:
  job = RecordingJob(clock)
  runner = await started(runners, clock, [Entry("s", HOURLY, job)], [Entry("d", None, job)])
  with pytest.raises(KeyError):
    runner.replace("s", Entry("s", HOURLY, job))
  with pytest.raises(KeyError):
    runner.replace("x", Entry("x", HOURLY, job))
  with pytest.raises(KeyError):
    runner.remove("s")
  with pytest.raises(ValueError, match="name"):
    runner.replace("d", Entry("e", HOURLY, job))


async def test_duplicate_names_are_rejected(clock: FakeClock) -> None:
  job = RecordingJob(clock)
  with pytest.raises(ValueError, match="a"):
    Runner([Entry("a", HOURLY, job)], [Entry("a", None, job)], clock=clock)


async def test_stop_mid_run_neither_alerts_nor_records(clock: FakeClock, alerts: list[tuple[str, str, dict[str, Any]]]) -> None:
  job = RecordingJob(clock, block=asyncio.Event())  # never set
  store = done_at_5(job)
  before = store.records["hourly"]
  runner = Runner([Entry("hourly", HOURLY, job, catch_up=True)], [], store=store, clock=clock)
  await runner.start()
  await until(lambda: len(job.seen) == 1)
  await runner.stop(5)
  assert job.seen == [ny(6)]
  assert job.cancelled
  assert job.failures == []
  assert alerts == []
  assert store.records == {"hourly": before}


async def test_request_stop_from_another_thread_stops_new_runs(clock: FakeClock, runners: list[Runner]) -> None:
  job = RecordingJob(clock)
  runner = await started(runners, clock, [Entry("hourly", HOURLY, job)])
  await asyncio.to_thread(runner.request_stop)
  for _ in range(5000):  # without the stop, the 06:00 run comes within ~30 sleeps
    await asyncio.sleep(0)
  assert job.seen == []


async def test_start_logs_every_entry_with_its_next_run(
  clock: FakeClock, runners: list[Runner], caplog: pytest.LogCaptureFixture
) -> None:
  caplog.set_level(logging.INFO, logger="timeclock_backend.scheduling")
  job = RecordingJob(clock)
  await started(runners, clock, [Entry("weekly", Cron("0 6 * * MON", NY), job)], [Entry("idle", None, job)])
  assert "At 06:00 on Monday" in caplog.text
  assert ny(6, day=16).isoformat() in caplog.text
  assert "idle" in caplog.text


async def test_late_wake_runs_the_latest_time_within_late(
  clock: FakeClock, runners: list[Runner], caplog: pytest.LogCaptureFixture
) -> None:
  clock.t = ny(5, 59, 30)
  clock.lag = timedelta(hours=1, seconds=30)  # the first sleep wakes at 07:00:30
  job = RecordingJob(clock)
  await started(runners, clock, [Entry("hourly", HOURLY, job)])
  await until(lambda: job.seen != [])
  assert job.seen == [ny(7)]
  assert job.ran_at == [ny(7, 0, 30)]
  skips = [r.getMessage() for r in caplog.records if "skipped" in r.getMessage()]
  assert len(skips) == 1
  assert "1 run(s)" in skips[0]
  assert ny(6).isoformat() in skips[0]


async def test_late_wake_of_a_catch_up_entry_runs_the_latest_missed_time(
  clock: FakeClock, runners: list[Runner], caplog: pytest.LogCaptureFixture
) -> None:
  clock.t = ny(5, 59, 30)
  clock.lag = timedelta(hours=2, minutes=30)  # wakes at 08:30
  job = RecordingJob(clock)
  await started(runners, clock, [Entry("hourly", HOURLY, job, catch_up=True)], store=done_at_5(job))
  await until(lambda: job.seen != [])
  assert job.seen == [ny(8)]
  assert job.ran_at == [ny(8, 30)]
  skips = [r.getMessage() for r in caplog.records if "skipped" in r.getMessage()]
  assert len(skips) == 1
  assert "2 run(s)" in skips[0]
  assert ny(6).isoformat() in skips[0]
  assert ny(7).isoformat() in skips[0]


async def test_overrun_spanning_two_times_logs_both(clock: FakeClock, runners: list[Runner], caplog: pytest.LogCaptureFixture) -> None:
  block = asyncio.Event()
  job = RecordingJob(clock, block=block)
  await started(runners, clock, [Entry("hourly", HOURLY, job)])
  await until(lambda: job.seen != [])
  clock.t += timedelta(hours=2, minutes=10)  # 07:00 and 08:00 pass during the run
  block.set()
  await until(lambda: job.seen[1:] != [])
  assert job.seen == [ny(6), ny(9)]
  skips = [r.getMessage() for r in caplog.records if "skipped" in r.getMessage()]
  assert len(skips) == 1
  assert "2 run(s)" in skips[0]
  assert ny(7).isoformat() in skips[0]
  assert ny(8).isoformat() in skips[0]


async def test_removing_an_at_queued_behind_a_run_drops_it(clock: FakeClock, runners: list[Runner]) -> None:
  block = asyncio.Event()
  job = RecordingJob(clock, block=block)
  runner = await started(runners, clock, [], [Entry("d", At(ny(6)), job)])
  await until(lambda: job.seen != [])
  runner.replace("d", Entry("d", At(ny(6, 0, 1)), job))
  for _ in range(100):  # its run is created and queues on the lock
    await asyncio.sleep(0)
  runner.remove("d")
  block.set()
  for _ in range(1000):
    await asyncio.sleep(0)
  assert job.seen == [ny(6)]
  assert runner.next_run("d") is None


async def test_replacing_an_at_queued_behind_a_run_runs_only_the_last(clock: FakeClock, runners: list[Runner]) -> None:
  block = asyncio.Event()
  job = RecordingJob(clock, block=block)
  runner = await started(runners, clock, [], [Entry("d", At(ny(6)), job)])
  await until(lambda: job.seen != [])
  runner.replace("d", Entry("d", At(ny(6, 0, 1)), job))
  for _ in range(100):
    await asyncio.sleep(0)
  runner.replace("d", Entry("d", At(ny(6, 0, 2)), job))
  for _ in range(100):
    await asyncio.sleep(0)
  block.set()
  for _ in range(1000):
    await asyncio.sleep(0)
  assert job.seen == [ny(6), ny(6, 0, 2)]


async def test_removing_an_entry_ends_its_pending_retries(clock: FakeClock, runners: list[Runner]) -> None:
  job = RecordingJob(clock, fails=-1)
  runner = await started(runners, clock, [], [Entry("d", Cron("0 6 * * *", NY), job, retry=Retry(timedelta(minutes=15), 4))])
  await until(lambda: job.failures != [])  # now resting before its first retry
  runner.remove("d")
  removed_at = clock.now()
  other = RecordingJob(clock)
  runner.replace("d", Entry("d", At(removed_at), other))
  await until(lambda: other.seen != [])
  assert other.ran_at == [removed_at]  # not held behind the removed entry's rest
  for _ in range(1000):
    await asyncio.sleep(0)
  assert job.ran_at == [ny(6)]
  assert job.failures == [(1, False)]


async def test_a_failure_after_request_stop_does_not_rest_for_a_retry(clock: FakeClock, runners: list[Runner]) -> None:
  block = asyncio.Event()
  job = RecordingJob(clock, fails=-1, block=block)
  runner = await started(runners, clock, [Entry("hourly", HOURLY, job, retry=Retry(timedelta(minutes=15), 4))])
  await until(lambda: job.seen != [])
  runner.request_stop()
  for _ in range(10):
    await asyncio.sleep(0)
  stopped_at = clock.now()
  block.set()
  await until(lambda: job.failures != [])
  for _ in range(1000):
    await asyncio.sleep(0)
  assert clock.now() == stopped_at  # a rest would have advanced the fake clock
  assert job.ran_at == [ny(6)]


async def test_a_run_superseded_mid_main_does_not_rest_after_failing(clock: FakeClock, runners: list[Runner]) -> None:
  block = asyncio.Event()
  job = RecordingJob(clock, fails=-1, block=block)
  runner = await started(runners, clock, [], [Entry("d", Cron("0 6 * * *", NY), job, retry=Retry(timedelta(minutes=15), 4))])
  await until(lambda: job.seen != [])
  other = RecordingJob(clock)
  runner.replace("d", Entry("d", At(ny(6)), other))
  block.set()
  await until(lambda: other.seen != [])
  assert other.ran_at == [ny(6)]  # not held behind a retry rest
  assert job.ran_at == [ny(6)]
  assert job.failures == [(1, True)]  # no retry follows


async def test_clock_stepping_back_during_a_run_does_not_repeat_it(clock: FakeClock, runners: list[Runner]) -> None:
  block = asyncio.Event()
  job = RecordingJob(clock, block=block)
  await started(runners, clock, [Entry("hourly", HOURLY, job)])
  await until(lambda: job.seen != [])
  clock.t = ny(5, 59, 40).astimezone(UTC)  # stepped back 30 s, 10 s into the run
  block.set()
  await until(lambda: job.seen[1:] != [])
  assert job.seen == [ny(6), ny(7)]


async def test_a_successful_run_is_logged(clock: FakeClock, runners: list[Runner], caplog: pytest.LogCaptureFixture) -> None:
  caplog.set_level(logging.INFO)
  job = RecordingJob(clock)
  await started(runners, clock, [Entry("hourly", HOURLY, job)])
  await until(lambda: job.seen != [])
  await until(lambda: "hourly: run for 2026-11-10T06:00:00-05:00 done" in caplog.messages)


async def test_an_error_in_a_wait_task_reaches_the_loop_handler(clock: FakeClock, runners: list[Runner]) -> None:
  """A wait that dies silently would end its entry's schedule, so its exception goes to the loop's handler."""

  class Flaky(Cron):
    @override
    def next_after(self, t: datetime) -> datetime:
      if t >= ny(6):  # the first call plans the 06:00 run; the call after that run is the one that fails
        msg = "bad schedule"
        raise RuntimeError(msg)
      return super().next_after(t)

  caught: list[dict[str, Any]] = []
  asyncio.get_running_loop().set_exception_handler(lambda _loop, context: caught.append(context))
  job = RecordingJob(clock)
  await started(runners, clock, [Entry("hourly", Flaky("0 * * * *", NY), job)])
  await until(lambda: caught != [])
  assert isinstance(caught[0]["exception"], RuntimeError)
  assert caught[0]["message"] == "schedule hourly failed"
