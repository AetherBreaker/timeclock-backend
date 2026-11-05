"""The page's fixed copy (spec 2026-11-05 §3.4, §3.5)."""

# Standard library imports
from typing import TYPE_CHECKING

# Local folder imports
from .gate import EndReason

if TYPE_CHECKING:
  # Local folder imports
  from .settings import PageSettings

REFUSED = "This account can't use this page. If it should have access, ask to have it added."
SIGNED_OUT = "You're signed out."
FAILED = "Sign-in with Zoho didn't work. Try again."
DB_DOWN = "The database can't be reached right now. Try again in a minute."


def ended(reason: EndReason, settings: PageSettings) -> str | None:
  """Why the last sign-in ended, for the sign-in page; None when there's nothing to say."""
  match reason:
    case EndReason.NONE:
      return None
    case EndReason.IDLE:
      minutes = settings.idle_minutes
      return f"Signed out after {minutes} minute{'s' * (minutes != 1)} without activity; unsaved changes were discarded."
    case EndReason.EXPIRED:
      hours = settings.session_hours
      return f"Signed out after {hours} hour{'s' * (hours != 1)}; unsaved changes were discarded."
    case EndReason.REFUSED:
      return REFUSED
    case EndReason.FAILED:
      return FAILED
    case EndReason.SIGNED_OUT:
      return SIGNED_OUT
