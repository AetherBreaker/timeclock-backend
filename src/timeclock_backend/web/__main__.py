"""The page's entry point (`timeclock-web`); stays import-light so aeth_ext's initialize() runs before anything else."""

PROJECT_NAME = "timeclock-web"
# A log-server outage must not take the page down, as for the backend.
REQUIRE_LOG_SERVER = False
SHUTDOWN_ON_LOG_REJECTION = False


def run_page() -> None:
  """Run the page; the exit code says whether it ended fatally."""
  # Standard library imports
  import contextlib

  # Third party imports
  from aeth_ext import initialize
  from aeth_ext.errors.shutdown import SHUTDOWN, ShutdownKind

  # asyncio stays False: uvicorn brings uvloop itself, as for the clock.
  initialize(logging="socket", install_signal_handlers=True)

  # First party imports
  from timeclock_backend.web.app import serve

  # aeth_ext's exit nudge (a simulated KeyboardInterrupt) isn't an error; the exit code says how the page stopped.
  with contextlib.suppress(KeyboardInterrupt):
    serve()
  raise SystemExit(1 if SHUTDOWN.kind >= ShutdownKind.FATAL else 0)


if __name__ == "__main__":
  run_page()
