"""Tests for manual report sends: claiming a request, re-checking it, sending it and recording the outcome."""

# Standard library imports
import asyncio
import dataclasses
from collections.abc import Sequence
from datetime import date
from typing import TYPE_CHECKING, Any

# Third party imports
import psycopg
import pytest

# First party imports
from timeclock_backend import databases
from timeclock_backend.bootstrap import bootstrap
from timeclock_backend.databases import Database, connect
from timeclock_backend.jobs import payroll_report, report_sends
from timeclock_backend.jobs.report_sends import INTERRUPTED, fail_interrupted, listen_for_report_sends, process_pending

if TYPE_CHECKING:
  # Standard library imports
  from email.message import EmailMessage

WEEK = date(2026, 11, 2)


@pytest.fixture
async def migrated(fresh_database: Database) -> Database:
  """A bootstrapped database with a@ and b@example.com enrolled for the payroll report."""
  await bootstrap([fresh_database])
  async with await connect(fresh_database.name, autocommit=True) as conn:
    for email in ("a@example.com", "b@example.com"):
      await conn.execute("INSERT INTO backend.report_recipients VALUES ('payroll_report', %s)", (email,))
  return fresh_database


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch) -> list[EmailMessage]:
  """The messages the job would have sent."""
  captured: list[EmailMessage] = []

  def capture(msg: EmailMessage | Sequence[EmailMessage]) -> None:
    assert not isinstance(msg, Sequence)
    captured.append(msg)

  monkeypatch.setattr(payroll_report, "batch_send_emails", capture)
  return captured


@pytest.fixture
def alerts(monkeypatch: pytest.MonkeyPatch) -> list[str]:
  """The subjects of the alerts sent."""
  captured: list[str] = []
  monkeypatch.setattr(report_sends, "alert", lambda subject, *_, **__: captured.append(subject))
  return captured


async def request(db: Database, *emails: str, report: str = "payroll_report") -> int:
  """A request inserted by the owner's login, as the page's badge would."""
  async with await connect(db.name, autocommit=True) as conn:
    row = await (
      await conn.execute(
        "INSERT INTO backend.report_sends (report, week_start, recipients) VALUES (%s, %s, %s) RETURNING id",
        (report, WEEK, list(emails)),
      )
    ).fetchone()
  assert row is not None
  return row["id"]


async def outcome(db: Database, request_id: int) -> dict[str, Any]:
  async with await connect(db.name) as conn:
    row = await (
      await conn.execute("SELECT status, error, finished_at FROM backend.report_sends WHERE id = %s", (request_id,))
    ).fetchone()
  assert row is not None
  return row


async def change(db: Database, statement: str, *params: object) -> None:
  async with await connect(db.name, autocommit=True) as conn:
    await conn.execute(statement, params)  # pyright: ignore[reportArgumentType]


async def test_a_pending_request_is_sent_and_marked_sent(migrated: Database, sent: list[EmailMessage], alerts: list[str]) -> None:
  # The owner's login (a superuser) passes the developer check, so a request added by hand sends too.
  request_id = await request(migrated, "a@example.com")
  assert await process_pending(migrated) == 1
  row = await outcome(migrated, request_id)
  assert (row["status"], row["error"]) == ("sent", None)
  assert row["finished_at"] is not None
  [msg] = sent
  assert [a.addr_spec for a in msg["To"].addresses] == ["a@example.com"]
  assert msg["Subject"] == "default Timeclock Report 2026-11-02 to 2026-11-08"
  body = msg.get_body()
  assert body is not None
  assert "Sent by hand by postgres at " in body.get_content()
  assert alerts == []


async def test_an_address_unenrolled_while_waiting_fails_it_and_sends_nothing(
  migrated: Database, sent: list[EmailMessage], alerts: list[str]
) -> None:
  request_id = await request(migrated, "a@example.com", "b@example.com")
  await change(migrated, "DELETE FROM backend.report_recipients WHERE email = %s", "b@example.com")
  await process_pending(migrated)
  row = await outcome(migrated, request_id)
  assert row["status"] == "failed"
  assert "b@example.com" in row["error"]
  assert sent == []
  assert len(alerts) == 1


async def test_a_requester_without_the_role_fails_it(migrated: Database, sent: list[EmailMessage], alerts: list[str]) -> None:
  request_id = await request(migrated, "a@example.com")
  await change(migrated, "UPDATE backend.report_sends SET requested_by = 'gone@example.com' WHERE id = %s", request_id)
  await process_pending(migrated)
  row = await outcome(migrated, request_id)
  assert (row["status"], row["error"]) == ("failed", "gone@example.com is no longer a developer")
  assert sent == []


async def test_an_unknown_report_fails_it(migrated: Database, sent: list[EmailMessage], alerts: list[str]) -> None:
  await change(migrated, "INSERT INTO backend.report_recipients VALUES ('nope', 'a@example.com')")
  request_id = await request(migrated, "a@example.com", report="nope")
  await process_pending(migrated)
  assert (await outcome(migrated, request_id))["error"] == "unknown report nope"
  assert sent == []


async def test_a_send_failure_marks_failed_and_alerts(migrated: Database, alerts: list[str], monkeypatch: pytest.MonkeyPatch) -> None:
  def boom(msg: object) -> None:
    raise OSError("smtp down")

  monkeypatch.setattr(payroll_report, "batch_send_emails", boom)
  request_id = await request(migrated, "a@example.com")
  await process_pending(migrated)
  row = await outcome(migrated, request_id)
  assert (row["status"], row["error"]) == ("failed", "smtp down")
  assert row["finished_at"] is not None
  assert len(alerts) == 1


async def test_queued_requests_send_oldest_first(migrated: Database, sent: list[EmailMessage], alerts: list[str]) -> None:
  await request(migrated, "a@example.com")
  await request(migrated, "b@example.com")
  assert await process_pending(migrated) == 2  # noqa: PLR2004
  assert [m["To"] for m in sent] == ["a@example.com", "b@example.com"]


async def test_interrupted_sends_fail_and_are_not_resent(migrated: Database, sent: list[EmailMessage], alerts: list[str]) -> None:
  request_id = await request(migrated, "a@example.com")
  await change(migrated, "UPDATE backend.report_sends SET status = 'sending' WHERE id = %s", request_id)
  assert await fail_interrupted(migrated) == 1
  assert (await outcome(migrated, request_id))["error"] == INTERRUPTED
  assert await process_pending(migrated) == 0
  assert sent == []


async def test_the_listener_sends_on_a_ping(migrated: Database, sent: list[EmailMessage], alerts: list[str]) -> None:
  listening = asyncio.create_task(listen_for_report_sends(migrated))
  try:
    await asyncio.sleep(0.5)  # connected and caught up, so the send below comes from the ping
    request_id = await request(migrated, "a@example.com")
    async with asyncio.timeout(10):
      while (await outcome(migrated, request_id))["status"] != "sent":  # noqa: ASYNC110 - the row is the only signal
        await asyncio.sleep(0.05)
  finally:
    listening.cancel()
    with pytest.raises(asyncio.CancelledError):
      await listening
  assert len(sent) == 1


async def test_a_connection_lost_mid_send_still_alerts(migrated: Database, alerts: list[str], monkeypatch: pytest.MonkeyPatch) -> None:
  # The claim's connection dies during the send (Postgres restarting), so recording the failure fails too.
  def drop_connections_then_fail(msg: object) -> None:
    with psycopg.connect(databases.SERVER_CONNINFO, dbname=migrated.name, autocommit=True) as admin:
      admin.execute(
        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s AND pid <> pg_backend_pid()", (migrated.name,)
      )
    raise OSError("smtp down")

  monkeypatch.setattr(payroll_report, "batch_send_emails", drop_connections_then_fail)
  await request(migrated, "a@example.com")
  with pytest.raises(psycopg.OperationalError):
    await process_pending(migrated)
  assert len(alerts) == 1


async def test_the_listener_fails_rows_left_sending_when_it_connects(migrated: Database, alerts: list[str]) -> None:
  # The listener is its database's only claimer: a row already `sending` when it (re)connects is one a lost connection
  # or a restart stranded.
  request_id = await request(migrated, "a@example.com")
  await change(migrated, "UPDATE backend.report_sends SET status = 'sending' WHERE id = %s", request_id)
  listening = asyncio.create_task(listen_for_report_sends(migrated))
  try:
    async with asyncio.timeout(10):
      while (await outcome(migrated, request_id))["status"] != "failed":  # noqa: ASYNC110 - the row is the only signal
        await asyncio.sleep(0.05)
  finally:
    listening.cancel()
    with pytest.raises(asyncio.CancelledError):
      await listening
  assert (await outcome(migrated, request_id))["error"] == INTERRUPTED


async def test_fail_interrupted_alerts_only_when_it_fails_rows(migrated: Database, alerts: list[str]) -> None:
  assert await fail_interrupted(migrated) == 0
  assert alerts == []
  request_id = await request(migrated, "a@example.com")
  await change(migrated, "UPDATE backend.report_sends SET status = 'sending' WHERE id = %s", request_id)
  assert await fail_interrupted(migrated) == 1
  assert len(alerts) == 1


async def test_a_test_database_request_is_marked_test(migrated: Database, sent: list[EmailMessage], alerts: list[str]) -> None:
  await request(migrated, "a@example.com")
  await process_pending(dataclasses.replace(migrated, production=False))
  [msg] = sent
  assert msg["Subject"].startswith("TEST — ")
