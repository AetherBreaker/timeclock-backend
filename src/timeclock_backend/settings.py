"""Environment-driven settings for the backend."""

# Standard library imports
from ipaddress import IPv4Network
from typing import Annotated, Final

# Third party imports
from aeth_ext.settings import BaseSettings
from pydantic import Field, SecretStr


class Settings(BaseSettings):
  """The backend's settings; `tz`, the log folder and the alert settings come from aeth_ext's base."""

  # The sidecar's compose service; not `postgres`, which is Coolify's own database's alias on the shared coolify network.
  postgres_host: Annotated[str, Field(alias="POSTGRES_HOST")] = "timeclock-postgres"
  postgres_port: Annotated[int, Field(alias="POSTGRES_PORT")] = 5432
  # Compose's `${POSTGRES_PASSWORD:?}` guarantees a value in production; the empty default lets tests import this module.
  postgres_password: Annotated[SecretStr, Field(alias="POSTGRES_PASSWORD")] = SecretStr("")
  relay_port: Annotated[int, Field(alias="RELAY_PORT")] = 5432
  relay_allowed_cidr: Annotated[IPv4Network, Field(alias="RELAY_ALLOWED_CIDR")] = IPv4Network("10.8.0.0/24")


SETTINGS: Final = Settings.get_settings()
