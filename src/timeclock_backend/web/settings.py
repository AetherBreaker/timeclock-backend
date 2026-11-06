"""Environment-driven settings for the shift corrections page."""

# Standard library imports
from typing import Annotated

# Third party imports
from aeth_ext.settings import BaseSettings
from pydantic import Field, SecretStr, field_validator


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
  postgres_host: Annotated[str, Field(alias="PAGE_POSTGRES_HOST")] = "timeclock-postgres"
  postgres_port: Annotated[int, Field(alias="PAGE_POSTGRES_PORT")] = 5432
  db_password: Annotated[SecretStr, Field(alias="TIMECLOCK_WEB_PASSWORD")] = SecretStr("")
  production_db: Annotated[str, Field(alias="PAGE_PRODUCTION_DB")] = "timeclock"
  test_db: Annotated[str, Field(alias="PAGE_TEST_DB")] = "timeclock_test"
  port: Annotated[int, Field(alias="PAGE_PORT")] = 8080

  @field_validator("public_url", "zoho_accounts_url")
  @classmethod
  def _no_trailing_slash(cls, value: str) -> str:
    # Both are joined with paths, and the Zoho redirect URL must match its registration exactly.
    return value.rstrip("/")
