"""Tests for the cron and one-off schedule types."""

# Standard library imports
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

# Third party imports
import pytest
from cronsim import CronSimError

# First party imports
# Local application imports
from timeclock_backend.scheduling import At, Cron
from timeclock_backend.settings import SETTINGS

NY = ZoneInfo("America/New_York")


def test_cron_next_and_latest_in_its_zone() -> None:
  c = Cron("0 6 * * MON", NY)
  t = datetime(2026, 11, 10, 9, tzinfo=NY)
  assert c.next_after(t) == datetime(2026, 11, 16, 6, tzinfo=NY)
  assert c.latest_before(t) == datetime(2026, 11, 9, 6, tzinfo=NY)
  assert c.describe() == "At 06:00 on Monday"


def test_cron_next_after_is_strict() -> None:
  c = Cron("0 6 * * MON", NY)
  assert c.next_after(datetime(2026, 11, 9, 6, tzinfo=NY)) == datetime(2026, 11, 16, 6, tzinfo=NY)


def test_cron_latest_before_includes_t() -> None:
  c = Cron("0 6 * * MON", NY)
  t = datetime(2026, 11, 9, 6, tzinfo=NY)
  assert c.latest_before(t) == t


def test_cron_spring_forward_gap_runs_at_its_end() -> None:
  assert Cron("30 2 * * *", NY).next_after(datetime(2027, 3, 13, 12, tzinfo=NY)) == datetime(2027, 3, 14, 3, tzinfo=NY)


def test_cron_fall_back_hour_runs_once() -> None:
  c = Cron("30 1 * * *", NY)
  first = c.next_after(datetime(2026, 10, 31, 12, tzinfo=NY))
  assert c.next_after(first).date() == date(2026, 11, 2)


def test_cron_defaults_to_the_settings_zone() -> None:
  assert Cron("0 6 * * MON").tz == SETTINGS.tz


def test_bad_cron_line_raises_at_construction() -> None:
  with pytest.raises(CronSimError):
    Cron("0 6 * * FUNDAY")


def test_at_always_returns_its_time() -> None:
  when = datetime(2026, 11, 10, 23, tzinfo=NY)
  assert At(when).next_after(when + timedelta(days=1)) == when
  assert At(when).describe() == f"once at {when.isoformat()}"


def test_at_needs_an_aware_time() -> None:
  with pytest.raises(ValueError, match="aware"):
    At(datetime(2026, 11, 10, 23))  # noqa: DTZ001  # naive on purpose


def test_latest_before_in_the_repeated_fall_back_hour() -> None:
  t = datetime(2026, 11, 1, 1, 30, fold=1, tzinfo=NY)  # 01:30 EST, 06:30 UTC
  assert Cron("* * * * *", NY).latest_before(t).astimezone(UTC) == datetime(2026, 11, 1, 6, 30, tzinfo=UTC)
  fired = Cron("30 1 * * *", NY).latest_before(datetime(2026, 11, 1, 1, 45, fold=1, tzinfo=NY))
  assert fired.astimezone(UTC) in {datetime(2026, 11, 1, 5, 30, tzinfo=UTC), datetime(2026, 11, 1, 6, 30, tzinfo=UTC)}
  assert fired.date() == date(2026, 11, 1)
  assert fired.hour == 1


def test_next_after_in_the_repeated_fall_back_hour() -> None:
  t = datetime(2026, 11, 1, 1, 30, fold=1, tzinfo=NY)
  assert Cron("* * * * *", NY).next_after(t).astimezone(UTC) == datetime(2026, 11, 1, 6, 31, tzinfo=UTC)


def test_latest_before_just_after_the_spring_forward_gap() -> None:
  c = Cron("30 2 * * *", NY)
  assert c.latest_before(datetime(2027, 3, 14, 3, 10, tzinfo=NY)) == datetime(2027, 3, 14, 3, 0, tzinfo=NY)
