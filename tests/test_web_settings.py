"""Tests for the page's settings and sign-out messages."""

# Third party imports
from page_sim import page_settings

# First party imports
from timeclock_backend.web import text
from timeclock_backend.web.gate import EndReason


def test_the_public_url_loses_its_trailing_slash() -> None:
  assert page_settings(PAGE_PUBLIC_URL="https://x.example/").public_url == "https://x.example"


def test_the_session_limits_default_to_8_hours_and_30_minutes() -> None:
  settings = page_settings()
  assert (settings.session_hours, settings.idle_minutes) == (8, 30)


def test_the_sign_out_messages_name_the_limits() -> None:
  settings = page_settings()
  assert text.ended(EndReason.IDLE, settings) == "Signed out after 30 minutes without activity; unsaved changes were discarded."
  assert text.ended(EndReason.EXPIRED, settings) == "Signed out after 8 hours; unsaved changes were discarded."
  assert text.ended(EndReason.REFUSED, settings) == text.REFUSED
  assert text.REFUSED == "This account can't use this page. If it should have access, ask to have it added."
  assert text.ended(EndReason.NONE, settings) is None
