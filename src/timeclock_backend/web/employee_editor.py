"""The employee editor (spec 2026-11-05 §9; mockups K to M): NiceGUI widgets over a draft the page keeps server-side."""

# Standard library imports
import re
from typing import TYPE_CHECKING

# Third party imports
import bcrypt

if TYPE_CHECKING:
  # Third party imports
  from pydantic import SecretStr

_PHONE_PUNCTUATION = re.compile(r"[\s().-]")


def normalize_phone(raw: str) -> str:
  """A US phone typed in any common format as `+1` and ten digits, the one stored format.

  The clock's `timeclock_system.clock.normalize_phone`, copied: the two must agree.

  Raises:
    ValueError: It isn't a ten-digit US number.
  """
  digits = _PHONE_PUNCTUATION.sub("", raw)
  digits = digits.removeprefix("+1") if digits.startswith("+") else digits
  if len(digits) == 11 and digits.startswith("1"):  # noqa: PLR2004  # the country code without its plus
    digits = digits[1:]
  if not (len(digits) == 10 and digits.isascii() and digits.isdecimal()):  # noqa: PLR2004
    raise ValueError("not a ten-digit US phone number")
  return f"+1{digits}"


def hash_pin(pin: SecretStr) -> str:
  """`pin`'s bcrypt hash with the `$2a$` prefix the clock uses: pgcrypto's `crypt()` can't check bcrypt's default `$2b$`."""
  return bcrypt.hashpw(pin.get_secret_value().encode(), bcrypt.gensalt(prefix=b"2a")).decode()
