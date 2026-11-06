"""Environment-driven settings for the shift corrections page."""

# Standard library imports
import json
from typing import Annotated, Final

# Third party imports
from aeth_ext.settings import BaseSettings
from pydantic import Field, SecretStr, field_validator

# Long enough that guessing one through the public form is hopeless, so the form needs no lockout.
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
  # `test_logins`, so a malformed value's error never carries the passwords into the logs.
  test_accounts: Annotated[SecretStr, Field(alias="PAGE_TEST_ACCOUNTS")] = SecretStr("")
  postgres_host: Annotated[str, Field(alias="PAGE_POSTGRES_HOST")] = "timeclock-postgres"
  postgres_port: Annotated[int, Field(alias="PAGE_POSTGRES_PORT")] = 5432
  db_password: Annotated[SecretStr, Field(alias="TIMECLOCK_WEB_PASSWORD")] = SecretStr("")
  production_db: Annotated[str, Field(alias="PAGE_PRODUCTION_DB")] = "timeclock"
  test_db: Annotated[str, Field(alias="PAGE_TEST_DB")] = "timeclock_test"
  port: Annotated[int, Field(alias="PAGE_PORT")] = 8080

  def test_logins(self) -> dict[str, str]:
    """The test sign-in's passwords by lowercase email (badges are lowercase, 0014); empty when it's off.

    Raises:
      ValueError: PAGE_TEST_ACCOUNTS isn't a JSON object of email to a long enough password; the message never quotes it.
    """
    raw = self.test_accounts.get_secret_value().strip()
    if not raw:
      return {}
    try:
      accounts = json.loads(raw)
    except json.JSONDecodeError:
      raise ValueError("PAGE_TEST_ACCOUNTS isn't valid JSON") from None
    if not isinstance(accounts, dict) or not all(isinstance(password, str) for password in accounts.values()):
      raise ValueError('PAGE_TEST_ACCOUNTS must be a JSON object of email to password: {"name@example.com": "..."}')
    if short := sorted(email for email, password in accounts.items() if len(password) < MIN_TEST_PASSWORD):
      raise ValueError(f"PAGE_TEST_ACCOUNTS passwords need {MIN_TEST_PASSWORD} characters or more: {', '.join(short)}")
    return {email.strip().lower(): password for email, password in accounts.items()}

  @field_validator("public_url", "zoho_accounts_url")
  @classmethod
  def _no_trailing_slash(cls, value: str) -> str:
    # Both are joined with paths, and the Zoho redirect URL must match its registration exactly.
    return value.rstrip("/")
