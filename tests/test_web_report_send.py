"""Tests for the shift list's manual payroll report send: a developer picks enrolled addresses for the shown week."""

# Standard library imports
from datetime import date
from typing import TYPE_CHECKING, Any

# Third party imports
import pytest
from nicegui import ui

# First party imports
from timeclock_backend.web import report_send, text

if TYPE_CHECKING:
  # Third party imports
  from page_sim import DbPage

  # First party imports
  from timeclock_backend.databases import Database

SEEDED = "jacob.ogden@sweetfiretobacco.com"  # timeclock-schema 0026's payroll_report recipient


@pytest.fixture(autouse=True)
def quick_poll(monkeypatch: pytest.MonkeyPatch) -> None:
  monkeypatch.setattr(report_send, "POLL_SECS", 0.05)


def make_developer(page: DbPage) -> None:
  with page.db.admin() as conn:
    conn.execute("UPDATE backend.editors SET developer = true WHERE email = %s", (page.db.email,))


def enrol(page: DbPage, *emails: str) -> None:
  with page.db.admin() as conn:
    for email in emails:
      conn.execute("INSERT INTO backend.report_recipients VALUES ('payroll_report', %s)", (email,))


def requests(page: DbPage, database: Database | None = None) -> list[tuple[Any, ...]]:
  with page.db.admin(database) as conn:
    return conn.execute("SELECT report, week_start, recipients, requested_by, status FROM backend.report_sends ORDER BY id").fetchall()


def one[T](page: DbPage, marker: str, kind: type[T]) -> T:
  [element] = page.user.find(marker=marker).elements
  assert isinstance(element, kind)
  return element


async def open_dialog(page: DbPage, path: str = "/") -> None:
  make_developer(page)
  await page.user.open(path)
  page.user.find(marker="send-report").click()
  await page.user.should_see(marker="report-send")


async def test_the_button_shows_only_for_developers(db_page: DbPage) -> None:
  await db_page.user.open("/")
  await db_page.user.should_not_see(marker="send-report")
  make_developer(db_page)
  await db_page.user.open("/")
  await db_page.user.should_see(marker="send-report")


async def test_the_dialog_lists_enrolled_addresses_unticked(db_page: DbPage) -> None:
  enrol(db_page, "a@example.com")
  await open_dialog(db_page)
  await db_page.user.should_see("Nov 2 to 8, 2026")
  for email in ("a@example.com", SEEDED):
    assert one(db_page, f"recipient-{email}", ui.checkbox).value is False
  send = one(db_page, "send", ui.button)
  assert (send.text, send.enabled) == ("Send to 0", False)


async def test_select_all_ticks_every_box_then_reads_select_none(db_page: DbPage) -> None:
  enrol(db_page, "a@example.com")
  await open_dialog(db_page)
  db_page.user.find(marker="select-all").click()
  await db_page.user.should_see("Send to 2")
  assert all(one(db_page, f"recipient-{e}", ui.checkbox).value for e in ("a@example.com", SEEDED))
  assert one(db_page, "send", ui.button).enabled
  assert one(db_page, "select-all", ui.button).text == "Select none"
  db_page.user.find(marker="select-all").click()
  await db_page.user.should_see("Send to 0")
  assert one(db_page, "select-all", ui.button).text == "Select all"


async def test_sending_requests_the_shown_week(db_page: DbPage) -> None:
  enrol(db_page, "a@example.com")
  await open_dialog(db_page, "/?week=2026-11-09")
  db_page.user.find(marker="recipient-a@example.com").click()
  db_page.user.find(marker="send").click()
  await db_page.user.should_see(text.REPORT_WAITING)
  assert requests(db_page) == [("payroll_report", date(2026, 11, 9), ["a@example.com"], db_page.db.email, "pending")]
  assert not one(db_page, "send", ui.button).enabled  # until the outcome arrives


async def test_the_current_week_sends_its_monday(db_page: DbPage) -> None:
  await open_dialog(db_page)  # the list opens on this week (the clock: Thu Nov 5), its `week` unset
  db_page.user.find(marker=f"recipient-{SEEDED}").click()
  db_page.user.find(marker="send").click()
  await db_page.user.should_see(text.REPORT_WAITING)
  assert [r[1] for r in requests(db_page)] == [date(2026, 11, 2)]


async def test_the_outcome_shows_when_the_backend_records_it(db_page: DbPage) -> None:
  await open_dialog(db_page)
  db_page.user.find(marker=f"recipient-{SEEDED}").click()
  db_page.user.find(marker="send").click()
  await db_page.user.should_see(text.REPORT_WAITING)
  with db_page.db.admin() as conn:
    conn.execute("UPDATE backend.report_sends SET status = 'sent', finished_at = now()")
  await db_page.user.should_see("Sent to 1 address at")
  assert one(db_page, "send", ui.button).enabled
  db_page.user.find(marker="send").click()
  await db_page.user.should_see(text.REPORT_WAITING)
  with db_page.db.admin() as conn:
    conn.execute(
      "UPDATE backend.report_sends SET status = 'failed', error = 'smtp down', finished_at = now() WHERE status = 'pending'"
    )
  await db_page.user.should_see("Failed: smtp down")


async def test_a_refused_request_shows_the_database_message(db_page: DbPage) -> None:
  enrol(db_page, "a@example.com")
  await open_dialog(db_page)
  db_page.user.find(marker="recipient-a@example.com").click()
  with db_page.db.admin() as conn:
    conn.execute("DELETE FROM backend.report_recipients WHERE email = 'a@example.com'")
  db_page.user.find(marker="send").click()
  await db_page.user.should_see("Failed: a@example.com is not enrolled for payroll_report")
  assert requests(db_page) == []


async def test_no_enrolled_addresses_offers_no_send(db_page: DbPage) -> None:
  with db_page.db.admin() as conn:
    conn.execute("DELETE FROM backend.report_recipients WHERE report = 'payroll_report'")
  await open_dialog(db_page)
  await db_page.user.should_see(text.NO_REPORT_RECIPIENTS)
  await db_page.user.should_not_see(marker="send")


async def test_the_test_database_dialog_says_so(db_page: DbPage) -> None:
  make_developer(db_page)
  await db_page.user.open("/")
  db_page.user.find(marker="pref-test-db").click()
  await db_page.user.should_see(marker="test-banner")
  db_page.user.find(marker="send-report").click()
  await db_page.user.should_see(text.REPORT_TEST_NOTE)
  db_page.user.find(marker=f"recipient-{SEEDED}").click()
  db_page.user.find(marker="send").click()
  await db_page.user.should_see(text.REPORT_WAITING)
  assert len(requests(db_page, db_page.db.test)) == 1
  assert requests(db_page) == []
