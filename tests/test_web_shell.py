"""Tests for the page's shell: the top bar, the settings bubble, the test database banner, and the theme (spec §2, §11, §12)."""

# Standard library imports
import re
import sys
from pathlib import Path
from typing import TYPE_CHECKING

# Third party imports
from nicegui import app, ui
from page_sim import PageUnderTest, sign_in

# First party imports
from timeclock_backend.web.gate import Account

if TYPE_CHECKING:
  # Third party imports
  import pytest

WEB = Path(__file__).parents[1] / "src" / "timeclock_backend" / "web"
DEVELOPER = "dev@example.com"


async def test_the_gear_opens_the_settings_bubble(page: PageUnderTest) -> None:
  await sign_in(page)
  page.user.find(marker="gear").click()
  for label in ("Dark mode", "24-hour time", "Show technical details"):
    await page.user.should_see(label)
  await page.user.should_not_see(marker="pref-test-db")


async def test_preferences_are_remembered_per_browser(page: PageUnderTest) -> None:
  await sign_in(page)
  assert page.user.find(kind=ui.switch, marker="pref-dark").elements.pop().value is True  # dark by default
  page.user.find(marker="pref-dark").click()
  page.user.find(marker="pref-h24").click()
  await page.user.open("/")
  assert page.user.find(kind=ui.switch, marker="pref-dark").elements.pop().value is False
  assert page.user.find(kind=ui.switch, marker="pref-h24").elements.pop().value is True
  assert page.user.find(kind=ui.switch, marker="pref-technical").elements.pop().value is False
  assert page.user.find(ui.dark_mode).elements.pop().value is False


async def test_a_developer_can_switch_to_the_test_database(page: PageUnderTest) -> None:
  await sign_in(page, DEVELOPER)
  await page.user.should_not_see(marker="test-banner")
  page.user.find(marker="pref-test-db").click()
  await page.user.should_see(marker="test-banner")
  await page.user.should_see("TEST DATABASE")
  page.user.find("Back to the real database").click()
  await page.user.should_not_see(marker="test-banner")


async def test_the_test_database_choice_belongs_to_the_account(page: PageUnderTest) -> None:
  await sign_in(page, DEVELOPER)
  page.user.find(marker="pref-test-db").click()
  await page.user.should_see(marker="test-banner")
  page.user.http_client.cookies.clear()  # another browser
  await sign_in(page, DEVELOPER)
  await page.user.should_see(marker="test-banner")


async def test_an_open_page_follows_a_database_switch_from_another_tab(page: PageUnderTest, monkeypatch: pytest.MonkeyPatch) -> None:
  monkeypatch.setattr(sys.modules["timeclock_backend.web.shell"], "SESSION_CHECK_SECS", 0.05)  # the live module
  await sign_in(page, DEVELOPER)
  app.storage.general["databases"] = {DEVELOPER: "test"}  # as Gate.choose_database in the other tab
  await page.user.should_see(marker="test-banner")


async def test_an_open_page_leaves_the_test_database_with_the_developer_mark(
  page: PageUnderTest, monkeypatch: pytest.MonkeyPatch
) -> None:
  monkeypatch.setattr(sys.modules["timeclock_backend.web.shell"], "SESSION_CHECK_SECS", 0.05)
  await sign_in(page, DEVELOPER)
  page.user.find(marker="pref-test-db").click()
  await page.user.should_see(marker="test-banner")
  page.accounts.accounts[DEVELOPER] = Account(None, developer=False)
  await page.user.should_not_see(marker="test-banner")


async def test_signing_out_from_the_top_bar(page: PageUnderTest) -> None:
  await sign_in(page)
  await page.user.should_see("ann@example.com")
  page.user.find(marker="signout").click()
  await page.user.should_see("You're signed out.")


def test_page_code_has_no_colors_or_glyphs() -> None:
  banned = re.compile(r"#[0-9A-Fa-f]{3,8}\b|\brgba?\(|\bhsla?\(|ui\.colors\(|ui\.icon\(|\bicon=")
  found = [
    f"{path.relative_to(WEB)}:{n}: {line.strip()}"
    for path in sorted(WEB.rglob("*"))
    if path.suffix in {".py", ".js", ".vue"}
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
    if banned.search(line)
  ]
  assert found == []


def test_the_theme_sets_quasars_colors_for_both_modes() -> None:
  css = (WEB / "static" / "theme.css").read_text(encoding="utf-8")
  for selector in ("body.body--dark", "body.body--light", "body.test-database"):
    assert selector in css
  for token in ("--q-primary", "--q-negative", "--q-dark-page"):
    assert f"{token}:" in css
  assert 'url("/static/SFT-Logo-June-2022-color.svg")' in css
  assert 'url("/static/zoho-logo.svg")' in css
