"""The registry's tables."""

# First party imports
from timeclock_backend.databases import DATABASES
from timeclock_backend.schedule import DYNAMIC, STATIC


def test_registry_names_are_unique_and_cover_every_database() -> None:
  names = [e.name for e in (*STATIC, *DYNAMIC)]
  assert len(names) == len(set(names))
  assert {f"auto_clock_out:{db.name}" for db in DATABASES} <= set(names)
