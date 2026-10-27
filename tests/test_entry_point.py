"""The entry point's constants and the task-failure bridge into aeth_ext's loop handler."""

# Standard library imports
import asyncio
import importlib
import socket
import subprocess
import sys
from typing import TYPE_CHECKING

# First party imports
from timeclock_backend.startup import _surface_task_failure  # pyright: ignore[reportPrivateUsage]

if TYPE_CHECKING:
  # Standard library imports
  from pathlib import Path


def test_entry_point_constants() -> None:
  m = importlib.import_module("timeclock_backend.__main__")
  assert (m.PROJECT_NAME, m.REQUIRE_LOG_SERVER, m.SHUTDOWN_ON_LOG_REJECTION) == ("timeclock-backend", False, False)


async def test_surface_task_failure_reports_to_the_loop() -> None:
  errors = []
  asyncio.get_running_loop().set_exception_handler(lambda _loop, ctx: errors.append(ctx))

  async def boom() -> None:
    msg = "x"
    raise RuntimeError(msg)

  task = asyncio.create_task(boom(), name="boom")
  await asyncio.gather(task, return_exceptions=True)
  _surface_task_failure(task)
  assert [ctx["message"] for ctx in errors] == ["boom failed"]
  assert isinstance(errors[0]["exception"], RuntimeError)


# Runs the real entry path (SHUTDOWN is one-shot per process): no database, no listeners, a graceful
# shutdown requested once the relay is up. The tail must finish, exit 0 and tear down without errors.
_SHUTDOWN_DRIVER = """
import asyncio, os, socket, sys
os.environ.update(RELAY_PORT=sys.argv[1], LOG_LOC_FOLDER=sys.argv[2], POSTGRES_HOST="127.0.0.1", POSTGRES_PORT="9", RELAY_ALLOWED_CIDR="127.0.0.0/8", POSTGRES_PASSWORD="x", ALERTS_EMAIL_PWD="x")
import timeclock_backend.__main__ as entry
from aeth_ext import initialize

initialize(asyncio=True, logging=False)
import logging
logging.basicConfig(level=logging.INFO)
from aeth_ext.errors.shutdown import SHUTDOWN, ShutdownKind, run_shutdown
from timeclock_backend import startup

async def no_bootstrap():
  pass

async def request_when_relay_up():
  while True:
    try:
      socket.create_connection(("127.0.0.1", int(sys.argv[1])), timeout=1).close()
      break
    except OSError:
      await asyncio.sleep(0.1)
  run_shutdown(ShutdownKind.GRACEFUL)

startup.bootstrap = no_bootstrap
startup.DATABASES = ()
real_start_relay = startup.start_relay

async def start_relay_then_request(**kwargs):
  relay = await real_start_relay(**kwargs)
  asyncio.create_task(request_when_relay_up())
  return relay

startup.start_relay = start_relay_then_request
try:
  asyncio.run(startup.main())
except KeyboardInterrupt:
  pass
raise SystemExit(1 if SHUTDOWN.kind >= ShutdownKind.FATAL else 0)
"""


def test_shutdown_tail_completes_under_the_real_loop(tmp_path: Path) -> None:
  with socket.socket() as s:
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
  result = subprocess.run(
    [sys.executable, "-c", _SHUTDOWN_DRIVER, str(port), str(tmp_path)], capture_output=True, text=True, timeout=60, check=False
  )
  output = result.stdout + result.stderr
  assert "TEARDOWN FAILED" not in output, output
  assert "Traceback" not in output, output
  assert "Shutdown requested" in output, output
  assert result.returncode == 0, output
