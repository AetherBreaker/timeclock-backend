"""Tests for signing in with Zoho, and the gate on every page load (spec 2026-11-05 §3)."""

# Standard library imports
import sys
import time

# Third party imports
import psycopg
import pytest
from page_sim import PageUnderTest, authorize_query, sign_in

# First party imports
from timeclock_backend.web import text
from timeclock_backend.web.gate import Account

SIGN_IN_BUTTON = "Sign in using Zoho"
IDLE = "Signed out after 30 minutes without activity; unsaved changes were discarded."


async def test_every_page_needs_a_sign_in(page: PageUnderTest) -> None:
  await page.user.open("/")
  await page.user.should_see(SIGN_IN_BUTTON)
  await page.user.should_not_see("Signed in as")


async def test_signing_in_with_a_listed_verified_email_opens_the_page(page: PageUnderTest) -> None:
  await sign_in(page)
  await page.user.should_see("Signed in as ann@example.com")


async def test_the_sign_in_asks_zoho_for_pkce_and_a_nonce(page: PageUnderTest) -> None:
  started = await page.user.http_client.get("/signin/zoho")
  location = started.headers["location"]
  query = authorize_query(location)
  assert location.startswith(f"{page.zoho.ISSUER}/oauth/v2/auth?")
  assert query["code_challenge_method"] == "S256"
  assert query["code_challenge"]
  assert query["nonce"]
  assert query["state"]
  assert {"openid", "email"} <= set(query["scope"].split())
  assert query["redirect_uri"] == "http://test/auth/zoho"
  assert query["client_id"] == "page"


async def test_a_capitalized_email_signs_in_as_its_lowercase_badge(page: PageUnderTest) -> None:
  await sign_in(page, "Ann@Example.com")
  await page.user.should_see("Signed in as ann@example.com")


async def test_an_unlisted_email_is_refused(page: PageUnderTest) -> None:
  await sign_in(page, "eve@example.com")
  await page.user.should_see(text.REFUSED)
  await page.user.should_see("Sign in with another account")
  await page.user.should_not_see(SIGN_IN_BUTTON)
  await page.user.open("/")
  await page.user.should_see(SIGN_IN_BUTTON)


@pytest.mark.parametrize("verified", [False, None, "true"])
async def test_an_unverified_email_is_refused(page: PageUnderTest, verified: object) -> None:
  page.zoho.claims["email_verified"] = verified
  await sign_in(page)
  await page.user.should_see(text.REFUSED)


@pytest.mark.parametrize("bad", ["other_key", "other_audience", "other_issuer", "expired", "other_nonce", "no_id_token"])
async def test_a_reply_that_fails_a_check_does_not_sign_in(page: PageUnderTest, bad: str) -> None:
  now = int(time.time())
  match bad:
    case "other_key":
      page.zoho.sign_with_other_key = True
    case "other_audience":
      page.zoho.claims["aud"] = "another-app"
    case "other_issuer":
      page.zoho.claims["iss"] = "https://accounts.zoho.evil"
    case "expired":
      page.zoho.claims.update(iat=now - 7200, exp=now - 600)
    case "other_nonce":
      page.zoho.claims["nonce"] = "not-the-one-sent"
    case _:
      page.zoho.send_id_token = False
  await sign_in(page)
  await page.user.should_see(text.FAILED)
  await page.user.open("/")
  await page.user.should_see(SIGN_IN_BUTTON)


async def test_a_callback_this_page_did_not_start_is_refused(page: PageUnderTest) -> None:
  await page.user.open("/auth/zoho?code=c&state=forged")
  await page.user.should_see(text.FAILED)


async def test_an_idle_sign_in_says_why_it_ended(page: PageUnderTest) -> None:
  await sign_in(page)
  page.clock.advance(minutes=31)
  await page.user.open("/")
  await page.user.should_see(IDLE)


async def test_a_deleted_allowlist_row_cuts_off_an_open_sign_in(page: PageUnderTest) -> None:
  await sign_in(page)
  del page.accounts.accounts["ann@example.com"]
  await page.user.open("/")
  await page.user.should_see(text.REFUSED)


async def test_signing_out_ends_the_sign_in_for_every_tab(page: PageUnderTest) -> None:
  await sign_in(page)
  page.user.find(marker="signout").click()
  await page.user.should_see(text.SIGNED_OUT)
  await page.user.open("/")  # the same cookies, as another tab would send
  await page.user.should_see(SIGN_IN_BUTTON)


async def test_an_open_page_leaves_when_its_sign_in_ends(page: PageUnderTest, monkeypatch: pytest.MonkeyPatch) -> None:
  # The live module: NiceGUI's test reset drops the page's modules after each simulation, and page_main.py re-imports them.
  monkeypatch.setattr(sys.modules["timeclock_backend.web.app"], "SESSION_CHECK_SECS", 0.05)
  await sign_in(page)
  page.clock.advance(minutes=31)
  await page.user.should_see(IDLE)


async def test_the_database_down_shows_a_plain_message(page: PageUnderTest) -> None:
  await sign_in(page)
  page.accounts.fail = psycopg.OperationalError("connection refused")
  response = await page.user.http_client.get("/")
  assert (response.status_code, response.text) == (503, text.DB_DOWN)
  page.accounts.fail = None
  await page.user.open("/")
  await page.user.should_see("Signed in as ann@example.com")


async def test_a_signed_in_visit_to_the_sign_in_page_goes_home(page: PageUnderTest) -> None:
  await sign_in(page)
  await page.user.open("/signin")
  await page.user.should_see("Signed in as ann@example.com")


async def test_a_listed_email_needs_no_employee_number(page: PageUnderTest) -> None:
  page.accounts.accounts["ann@example.com"] = Account(None, developer=False)
  await sign_in(page)
  await page.user.should_see("Signed in as ann@example.com")
