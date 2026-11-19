"""Tests for scheduling reports from production's `backend.report_schedules`."""

# Standard library imports
import asyncio
from contextlib import suppress
from datetime import UTC, datetime
from typing import TYPE_CHECKING

# Third party imports
import pytest
from scheduling_sim import FakeClock, RecordingJob

# First party imports
from timeclock_backend.bootstrap import bootstrap
from timeclock_backend.databases import Database, connect
from timeclock_backend.jobs import report_schedules
from timeclock_backend.jobs.report_schedules import apply_report_schedules, listen_for_report_schedules
from timeclock_backend.scheduling import Cron, Entry, MemoryRunStore, Runner

if TYPE_CHECKING:
  # Standard library imports
  from collections.abc import AsyncIterator

NAME = "payroll_report"
SEEDED = "0 13 * * TUE"  # 0029's line


@pytest.fixture
async def migrated(fresh_database: Database) -> Database:
  await bootstrap([fresh_database])
  return fresh_database


@pytest.fixture
def alerts(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
  """The (subject, details) of each alert sent."""
  captured: list[tuple[str, str]] = []
  monkeypatch.setattr(report_schedules, "alert", lambda subject, details, **_: captured.append((subject, details)))
  return captured


@pytest.fixture
def entry() -> Entry:
  """Idle and catch_up, as the registry's payroll entry; with no record, its first catch-up only records."""
  return Entry(NAME, None, RecordingJob(FakeClock(datetime.now(UTC))), catch_up=True)


@pytest.fixture
async def runner(entry: Entry) -> AsyncIterator[Runner]:
  made = Runner([], [entry], store=MemoryRunStore())
  await made.start()
  yield made
  await made.stop(0)


async def change(db: Database, statement: str, *params: object) -> None:
  async with await connect(db.name, autocommit=True) as conn:
    await conn.execute(statement, params)  # pyright: ignore[reportArgumentType]


def upcoming(line: str) -> datetime:
  return Cron(line).next_after(datetime.now(UTC))


async def scheduled(runner: Runner, line: str) -> None:
  """Wait until the entry's next run is `line`'s next time: a replace's catch-up check sets it once that check ends."""
  async with asyncio.timeout(5):
    while runner.next_run(NAME) != upcoming(line):  # noqa: ASYNC110  # runner state, no event to wait on
      await asyncio.sleep(0.01)


async def test_the_seeded_line_schedules_the_entry(
  migrated: Database, runner: Runner, entry: Entry, alerts: list[tuple[str, str]]
) -> None:
  await apply_report_schedules(runner, [entry], migrated, {})
  await scheduled(runner, SEEDED)
  assert alerts == []


async def test_a_change_reschedules_at_once(migrated: Database, runner: Runner, entry: Entry, alerts: list[tuple[str, str]]) -> None:
  task = asyncio.create_task(listen_for_report_schedules(runner, [entry], migrated))
  try:
    await scheduled(runner, SEEDED)  # the connect's pass
    await change(migrated, "UPDATE backend.report_schedules SET cron = '0 6 * * MON' WHERE report = %s", NAME)
    await scheduled(runner, "0 6 * * MON")
    assert not task.done()
  finally:
    task.cancel()
    with suppress(asyncio.CancelledError):
      await task
  assert alerts == []


async def test_an_unchanged_line_is_left_alone(
  migrated: Database, runner: Runner, entry: Entry, alerts: list[tuple[str, str]], monkeypatch: pytest.MonkeyPatch
) -> None:
  # A replace would cancel a retry resting between attempts, so the same line read again must not make one.
  replaced: list[str] = []
  real = runner.replace

  def counting(name: str, new: Entry) -> None:
    replaced.append(name)
    real(name, new)

  monkeypatch.setattr(runner, "replace", counting)
  seen: dict[str, str | None] = {}
  await apply_report_schedules(runner, [entry], migrated, seen)
  await apply_report_schedules(runner, [entry], migrated, seen)  # a reconnect's pass
  await change(migrated, "UPDATE backend.report_schedules SET cron = ' 0  13 * * TUE ' WHERE report = %s", NAME)
  await apply_report_schedules(runner, [entry], migrated, seen)
  await change(migrated, "INSERT INTO backend.report_schedules VALUES ('other_report', '0 6 * * MON')")
  await apply_report_schedules(runner, [entry], migrated, seen)
  assert replaced == [NAME]
  assert alerts == []


async def test_a_bad_line_alerts_once_and_keeps_the_schedule(
  migrated: Database, runner: Runner, entry: Entry, alerts: list[tuple[str, str]]
) -> None:
  seen: dict[str, str | None] = {}
  await apply_report_schedules(runner, [entry], migrated, seen)
  await scheduled(runner, SEEDED)
  await change(migrated, "UPDATE backend.report_schedules SET cron = 'not a cron' WHERE report = %s", NAME)
  await apply_report_schedules(runner, [entry], migrated, seen)
  await apply_report_schedules(runner, [entry], migrated, seen)  # a reconnect: no second alert
  [(subject, details)] = alerts
  assert subject == f"Bad schedule for {NAME}"
  assert "not a cron" in details
  assert "until the backend restarts" in details  # kept for now, but a restart idles it
  assert runner.next_run(NAME) == upcoming(SEEDED)


async def test_a_line_that_never_fires_alerts_and_keeps_the_schedule(
  migrated: Database, runner: Runner, entry: Entry, alerts: list[tuple[str, str]]
) -> None:
  # cronsim parses it, but no date matches: scheduling it would leave nothing to wait for.
  seen: dict[str, str | None] = {}
  await apply_report_schedules(runner, [entry], migrated, seen)
  await scheduled(runner, SEEDED)
  await change(migrated, "UPDATE backend.report_schedules SET cron = '0 13 */14 * TUE#2' WHERE report = %s", NAME)
  await apply_report_schedules(runner, [entry], migrated, seen)
  assert [subject for subject, _ in alerts] == [f"Bad schedule for {NAME}"]
  assert runner.next_run(NAME) == upcoming(SEEDED)


async def test_a_bad_line_at_start_leaves_the_entry_idle(
  migrated: Database, runner: Runner, entry: Entry, alerts: list[tuple[str, str]]
) -> None:
  await change(migrated, "UPDATE backend.report_schedules SET cron = '0 25 * * *' WHERE report = %s", NAME)
  await apply_report_schedules(runner, [entry], migrated, {})
  [(subject, details)] = alerts
  assert subject == f"Bad schedule for {NAME}"
  assert "won't run until the line is fixed" in details  # nothing to keep: no "keeps its schedule" comfort
  assert runner.next_run(NAME) is None


async def test_no_row_alerts_and_idles_the_entry(
  migrated: Database, runner: Runner, entry: Entry, alerts: list[tuple[str, str]]
) -> None:
  seen: dict[str, str | None] = {}
  await apply_report_schedules(runner, [entry], migrated, seen)
  await scheduled(runner, SEEDED)
  await change(migrated, "DELETE FROM backend.report_schedules WHERE report = %s", NAME)
  await apply_report_schedules(runner, [entry], migrated, seen)
  assert [subject for subject, _ in alerts] == [f"No schedule for {NAME}"]
  assert runner.next_run(NAME) is None


async def test_a_fixed_line_after_a_bad_one_schedules(
  migrated: Database, runner: Runner, entry: Entry, alerts: list[tuple[str, str]]
) -> None:
  seen: dict[str, str | None] = {}
  await change(migrated, "UPDATE backend.report_schedules SET cron = 'not a cron' WHERE report = %s", NAME)
  await apply_report_schedules(runner, [entry], migrated, seen)
  await change(migrated, "UPDATE backend.report_schedules SET cron = '0 6 * * MON' WHERE report = %s", NAME)
  await apply_report_schedules(runner, [entry], migrated, seen)
  await scheduled(runner, "0 6 * * MON")
  assert [subject for subject, _ in alerts] == [f"Bad schedule for {NAME}"]
