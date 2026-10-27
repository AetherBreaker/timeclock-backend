"""Boot sequence: bootstrap, relay, scheduler and settings listeners, running until a shutdown."""

# Standard library imports
import asyncio
from asyncio import create_task
from contextlib import suppress
from logging import INFO, WARNING, getLogger
from typing import TYPE_CHECKING, Any, Final

# Third party imports
from aeth_ext.errors import install_loop_exception_handler
from aeth_ext.errors.shutdown import SHUTDOWN, SHUTDOWN_COMPLETE, ShutdownKind, ShutdownPhase, register_for_shutdown
from aeth_ext.monitoring import run_heartbeat_async
from apscheduler.schedulers import SchedulerNotRunningError

# First party imports
from timeclock_backend.auto_clock_out import listen_for_settings
from timeclock_backend.bootstrap import bootstrap
from timeclock_backend.databases import DATABASES
from timeclock_backend.relay import start_relay
from timeclock_backend.scheduler_config import Scheduler
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
  """Bootstrap the databases, run the relay, scheduler and listeners, and wait for a shutdown."""
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

  scheduler = Scheduler.init_scheduler()

  def pause_scheduler(_trails: tuple[ExceptionTrail, ...]) -> None:
    # A plain function is held strongly (a bound method wouldn't be); `pause()` is thread-safe. Never `shutdown()` from here.
    # The loop's own `shutdown()` below may win the race to wake first, and `pause()` raises once stopped.
    with suppress(SchedulerNotRunningError):
      scheduler.pause()

  register_for_shutdown(pause_scheduler, phase=ShutdownPhase.THREADED, priority=-10)
  scheduler.start()

  listeners = [create_task(listen_for_settings(scheduler, db), name=f"listen:{db.name}") for db in DATABASES]
  for task in (heartbeat, *listeners):
    task.add_done_callback(_surface_task_failure)

  await SHUTDOWN

  logger.log(INFO if SHUTDOWN.kind is ShutdownKind.GRACEFUL else WARNING, "Shutdown requested (%s); stopping", SHUTDOWN.kind.name)

  # Stop accepting only: the library's `Server` has no `close_clients`, and `wait_closed()` would wait on every open clock
  # connection. Once `main()` returns, `asyncio.run` cancels the remaining handler tasks, whose `finally` closes both transports.
  relay.close()

  for task in (heartbeat, *listeners):
    task.cancel()
  await asyncio.gather(heartbeat, *listeners, return_exceptions=True)

  try:
    scheduler.pause()
    scheduler.shutdown(wait=False)
  except Exception:
    logger.exception("Shutdown: failed to stop the scheduler cleanly")

  await SHUTDOWN_COMPLETE
