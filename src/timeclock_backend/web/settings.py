"""Environment-driven settings for the shift corrections page."""

# Standard library imports
import json
from collections import Counter
from typing import Annotated, Final

# Third party imports
from aeth_ext.settings import BaseSettings
from pydantic import Field, SecretStr, field_validator

# A random password this long can't be guessed through the public form, so the form needs no lockout.
MIN_TEST_PASSWORD: Final = 16


class PageSettings(BaseSettings):
  """The page's settings; `tz`, the log folder and the alert settings come from aeth_ext's base.

  The empty defaults let tests import this module; `serve()` refuses to start without them.
  """

  public_url: Annotated[str, Field(alias="PAGE_PUBLIC_URL")] = ""
  page_name: Annotated[str, Field(alias="PAGE_NAME")] = "Shift corrections"
  session_secret: Annotated[SecretStr, Field(alias="PAGE_SESSION_SECRET")] = SecretStr("")
  session_hours: Annotated[int, Field(alias="PAGE_SESSION_HOURS", gt=0)] = 8
  idle_minutes: Annotated[int, Field(alias="PAGE_IDLE_MINUTES", gt=0)] = 30
  zoho_accounts_url: Annotated[str, Field(alias="ZOHO_ACCOUNTS_URL")] = "https://accounts.zoho.com"
  zoho_client_id: Annotated[str, Field(alias="ZOHO_CLIENT_ID")] = ""
  zoho_client_secret: Annotated[SecretStr, Field(alias="ZOHO_CLIENT_SECRET")] = SecretStr("")
  # The test sign-in's accounts as a JSON object of email to password; empty turns it off. A string, parsed by
  # `test_logins`, since a pydantic validation error would quote the passwords.
  test_accounts: Annotated[SecretStr, Field(alias="PAGE_TEST_ACCOUNTS")] = SecretStr("")
  postgres_host: Annotated[str, Field(alias="PAGE_POSTGRES_HOST")] = "timeclock-postgres"
  postgres_port: Annotated[int, Field(alias="PAGE_POSTGRES_PORT")] = 5432
  db_password: Annotated[SecretStr, Field(alias="TIMECLOCK_WEB_PASSWORD")] = SecretStr("")
  production_db: Annotated[str, Field(alias="PAGE_PRODUCTION_DB")] = "timeclock"
  test_db: Annotated[str, Field(alias="PAGE_TEST_DB")] = "timeclock_test"
  port: Annotated[int, Field(alias="PAGE_PORT")] = 8080
  # Who the editor's "found a problem in itself" dialog says to tell (spec §6.9).
  fault_contact: Annotated[str, Field(alias="PAGE_FAULT_CONTACT")] = "Jacob"

  def test_logins(self) -> dict[str, SecretStr]:
    """The test sign-in's passwords by lowercase email (badges are lowercase, 0014); empty when it's off.

    Raises:
      ValueError: PAGE_TEST_ACCOUNTS isn't a JSON object of distinct emails to long enough passwords. Neither the
        message nor a local quotes a password: aeth_ext's crash alert shows every frame's locals.
    """
    try:
      accounts = json.loads(self.test_accounts.get_secret_value().strip() or "{}")
    except json.JSONDecodeError:
      accounts = None  # not raised in here: the decode error would carry the whole value
    if not isinstance(accounts, dict) or not all("@" in email and isinstance(password, str) for email, password in accounts.items()):
      del accounts  # "@" also catches a swapped pair, whose password would otherwise be named as an email below
      raise ValueError('PAGE_TEST_ACCOUNTS must be a JSON object of email to password: {"name@example.com": "..."}')
    twice = sorted(email for email, count in Counter(email.strip().lower() for email in accounts).items() if count > 1)
    logins = {email.strip().lower(): SecretStr(password) for email, password in accounts.items()}
    del accounts
    if twice:
      raise ValueError(f"PAGE_TEST_ACCOUNTS lists an email twice (casing aside): {', '.join(twice)}")
    if short := sorted(email for email, password in logins.items() if len(password.get_secret_value()) < MIN_TEST_PASSWORD):
      raise ValueError(f"PAGE_TEST_ACCOUNTS passwords need {MIN_TEST_PASSWORD} random characters or more: {', '.join(short)}")
    return logins

  @field_validator("public_url", "zoho_accounts_url")
  @classmethod
  def _no_trailing_slash(cls, value: str) -> str:
    # Both are joined with paths, and the Zoho redirect URL must match its registration exactly.
    return value.rstrip("/")
