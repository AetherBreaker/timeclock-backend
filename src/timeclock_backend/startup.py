"""Boot sequence: bootstrap, relay, job runner and settings listeners, running until a shutdown."""

# Standard library imports
import asyncio
from asyncio import create_task
from logging import INFO, WARNING, getLogger
from typing import TYPE_CHECKING, Any, Final

# Third party imports
from aeth_ext.errors import install_loop_exception_handler
from aeth_ext.errors.shutdown import SHUTDOWN, SHUTDOWN_COMPLETE, ShutdownKind, ShutdownPhase, register_for_shutdown
from aeth_ext.monitoring import run_heartbeat_async

# First party imports
from timeclock_backend.bootstrap import bootstrap
from timeclock_backend.databases import DATABASES
from timeclock_backend.jobs.auto_clock_out import AutoClockOutJob, listen_for_settings
from timeclock_backend.relay import start_relay
from timeclock_backend.schedule import DYNAMIC, STATIC
from timeclock_backend.scheduling import PgRunStore, Runner
from timeclock_backend.settings import SETTINGS

if TYPE_CHECKING:
  # Third party imports
  from aeth_ext.errors.exception_trail import ExceptionTrail

logger = getLogger(__name__)

HEARTBEAT_FILE: Final = SETTINGS.log_loc_folder / "heartbeat.txt"


def _surface_task_failure(task: asyncio.Task[Any]) -> None:
  """Done callback: hand a long-running task's exception to the loop handler, which shuts down fatally."""
  if not task.cancelled() and (exc := task.exception()) is not None:
    task.get_loop().call_exception_handler({"message": f"{task.get_name()} failed", "exception": exc, "task": task})


async def main() -> None:
  """Bootstrap the databases, run the relay, job runner and listeners, and wait for a shutdown."""
  install_loop_exception_handler()  # uvloop's loop exists only now

  # Under devkit-container's supervision this only writes the file; the supervisor owns the pings.
  heartbeat = create_task(
    run_heartbeat_async(
      HEARTBEAT_FILE,
      ping_url=SETTINGS.alerts_healthcheck_ping_url,
      pingkey=SETTINGS.alerts_healthcheck_pingkey,
      tz=SETTINGS.tz,
    ),
    name="heartbeat",
  )

  # A failure raises out of `main()`: aeth_ext's crash hook alerts and shuts down fatally, and
  # Docker restarts the container while the clocks keep buffering.
  await bootstrap()

  relay = await start_relay(
    port=SETTINGS.relay_port,
    upstream_host=SETTINGS.postgres_host,
    upstream_port=SETTINGS.postgres_port,
    allowed=SETTINGS.relay_allowed_cidr,
  )

  runner = Runner(STATIC, DYNAMIC, store=PgRunStore(next(db for db in DATABASES if db.production)))

  def stop_runner(_trails: tuple[ExceptionTrail, ...]) -> None:
    # A plain function is held strongly (a bound method wouldn't be); `request_stop()` is thread-safe. Never `stop()` from
    # here: it's awaited on the loop below.
    runner.request_stop()

  register_for_shutdown(stop_runner, phase=ShutdownPhase.THREADED, priority=-10)
  await runner.start()

  # Each listener's first pass (on connect) schedules its idle entry.
  listeners = [
    create_task(listen_for_settings(e.job), name=f"listen:{e.job.db.name}") for e in DYNAMIC if isinstance(e.job, AutoClockOutJob)
  ]
  for task in (heartbeat, *listeners):
    task.add_done_callback(_surface_task_failure)

  await SHUTDOWN
  # Declares the tail below without blocking: undeclared, aeth_ext's exit nudge fires 0.25 s after the threaded pass and
  # cancels a running job mid-`runner.stop`; declared, it waits out the graceful budget less a margin.
  SHUTDOWN_COMPLETE.wait(timeout=0)

  logger.log(INFO if SHUTDOWN.kind is ShutdownKind.GRACEFUL else WARNING, "Shutdown requested (%s); stopping", SHUTDOWN.kind.name)

  # Stop accepting only: uvloop's and winloop's `Server` have no `close_clients`, and `wait_closed()` would wait on every open clock
  # connection. Once `main()` returns, `asyncio.run` cancels the remaining handler tasks, whose `finally` closes both transports.
  relay.close()

  for task in (heartbeat, *listeners):
    task.cancel()
  await asyncio.gather(heartbeat, *listeners, return_exceptions=True)

  try:
    await runner.stop(5.0)  # a running job's budget: inside the 7 s graceful one, which the declaration above holds the nudge to
  except Exception:
    logger.exception("Shutdown: failed to stop the job runner cleanly")

  await SHUTDOWN_COMPLETE
