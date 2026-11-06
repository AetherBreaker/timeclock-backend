"""Tests for the page's settings and sign-out messages."""

# Standard library imports
import json

# Third party imports
import pytest
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


def test_the_test_sign_in_is_off_without_accounts() -> None:
  assert page_settings().test_logins() == {}
  assert page_settings(PAGE_TEST_ACCOUNTS="").test_logins() == {}  # compose's ${PAGE_TEST_ACCOUNTS:-}


def test_test_accounts_are_kept_by_lowercase_email() -> None:
  accounts = json.dumps({" Ann@Example.com ": "p" * 16})
  assert page_settings(PAGE_TEST_ACCOUNTS=accounts).test_logins() == {"ann@example.com": "p" * 16}


@pytest.mark.parametrize(
  "raw",
  ['{"ann@example.com": "short-secret"', '["ann@example.com", "short-secret"]', '{"ann@example.com": "short-secret"}'],
  ids=["not-json", "not-an-object", "short-password"],
)
def test_bad_test_accounts_are_refused_without_quoting_them(raw: str) -> None:
  with pytest.raises(ValueError, match="PAGE_TEST_ACCOUNTS") as raised:
    page_settings(PAGE_TEST_ACCOUNTS=raw).test_logins()
  assert "short-secret" not in str(raised.value)  # it reaches the logs


def test_a_short_test_password_names_its_account() -> None:
  with pytest.raises(ValueError, match=r"ann@example.com"):
    page_settings(PAGE_TEST_ACCOUNTS=json.dumps({"ann@example.com": "p" * 15})).test_logins()


def test_the_wrong_password_message() -> None:
  assert text.ended(EndReason.WRONG_PASSWORD, page_settings()) == text.WRONG_PASSWORD
  assert text.WRONG_PASSWORD == "That email and password don't match a test account."
