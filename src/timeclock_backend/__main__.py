"""The app `run-app-timeclock-backend`: for now, only the heartbeat.

The database relay and the scheduler land here with the design spec. Until then the app writes the
heartbeat the standard healthcheck reads, and pings healthchecks.io itself (no supervisor yet).
"""

# Standard library imports
import os
import signal
import threading
from pathlib import Path

# Third party imports
from pydantic import SecretStr

# First party imports
from aeth_ext.monitoring.heartbeat import send_heartbeat

BEAT_SECS = 60
HEARTBEAT_FILE = Path("/app/persisted_data/logs/heartbeat.txt")


def run_app() -> None:
  """Beat every `BEAT_SECS` until SIGINT or SIGTERM."""
  stop = threading.Event()
  signal.signal(signal.SIGINT, lambda *_: stop.set())
  signal.signal(signal.SIGTERM, lambda *_: stop.set())
  HEARTBEAT_FILE.parent.mkdir(parents=True, exist_ok=True)
  url = os.environ.get("ALERTS_HEALTHCHECK_PING_URL", "").strip()
  key = os.environ.get("PINGKEY", "").strip()
  while not stop.is_set():
    send_heartbeat(
      HEARTBEAT_FILE,
      ping_url=SecretStr(url) if url else None,
      pingkey=SecretStr(key) if key else None,
      slug=os.environ.get("HEARTBEAT_SLUG"),
    )
    stop.wait(BEAT_SECS)


if __name__ == "__main__":
  run_app()
