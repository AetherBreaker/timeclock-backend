"""Tests for the test sign-in: email and password accounts from PAGE_TEST_ACCOUNTS, still held to the allowlist."""

# Standard library imports
import asyncio
import json
from typing import TYPE_CHECKING

# Third party imports
import psycopg
import pytest
from nicegui import app, background_tasks

# First party imports
from timeclock_backend.web import text

if TYPE_CHECKING:
  # Third party imports
  import httpx
  from page_sim import PageUnderTest

PASSWORD = "correct-horse-battery"
ACCOUNTS = json.dumps({"Ann@Example.com": PASSWORD, "stranger@example.com": PASSWORD})  # stranger isn't on the allowlist


@pytest.fixture
def page_values() -> dict[str, str]:
  return {"PAGE_TEST_ACCOUNTS": ACCOUNTS}


async def _post(page: PageUnderTest, email: str, password: str) -> httpx.Response:
  return await page.user.http_client.post("/signin/test", data={"email": email, "password": password})


async def test_a_test_account_signs_in_with_its_password(page: PageUnderTest) -> None:
  reply = await _post(page, "ANN@example.com ", PASSWORD)  # matched whatever the casing, as badges are lowercase
  assert (reply.status_code, reply.headers["location"]) == (303, "/")
  await page.user.open("/")
  await page.user.should_see("Signed in as ann@example.com")


async def test_a_wrong_password_and_an_unlisted_email_get_the_same_message(page: PageUnderTest) -> None:
  for email, password in (("ann@example.com", "wrong-horse-battery"), ("dev@example.com", PASSWORD)):  # dev: Zoho only
    reply = await _post(page, email, password)
    assert reply.headers["location"] == "/signin"
    await page.user.open("/signin")
    await page.user.should_see(text.WRONG_PASSWORD)
  assert (await page.user.http_client.get("/")).headers["location"] == "/signin"


async def test_a_test_account_must_be_on_the_allowlist(page: PageUnderTest) -> None:
  reply = await _post(page, "stranger@example.com", PASSWORD)
  assert reply.headers["location"] == "/signin"
  await page.user.open("/signin")
  await page.user.should_see(text.REFUSED)
  await page.user.should_see(marker="test-signin")  # the way to try another account


async def test_a_test_sign_in_waits_for_the_database(page: PageUnderTest) -> None:
  page.accounts.fail = psycopg.OperationalError("down")
  reply = await _post(page, "ann@example.com", PASSWORD)
  assert (reply.status_code, reply.text) == (503, text.DB_DOWN)


async def test_the_sign_in_page_offers_both_methods(page: PageUnderTest) -> None:
  await page.user.open("/signin")
  await page.user.should_see(marker="zoho-signin")
  await page.user.should_see(marker="test-signin")


@pytest.mark.parametrize("page_values", [{"PAGE_TEST_ACCOUNTS": ACCOUNTS, "ZOHO_CLIENT_ID": ""}])
async def test_without_zoho_the_sign_in_page_offers_only_the_test_form(page: PageUnderTest) -> None:
  await page.user.open("/signin")
  await page.user.should_see(marker="test-signin")
  await page.user.should_not_see(marker="zoho-signin")
  assert (await page.user.http_client.get("/signin/zoho")).is_client_error


async def test_a_failed_test_sign_in_stores_nothing_on_the_server(page: PageUnderTest) -> None:
  before = set(app.storage.path.glob("storage-user-*.json"))
  for _ in range(3):
    page.user.http_client.cookies.clear()  # each a new browser, as a script's requests are
    assert (await _post(page, "ann@example.com", "wrong-horse-battery")).headers["location"] == "/signin"
  await asyncio.gather(*background_tasks.lazy_tasks_running.values())  # NiceGUI's storage file writes
  assert set(app.storage.path.glob("storage-user-*.json")) == before
