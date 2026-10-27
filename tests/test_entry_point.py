"""The entry point's constants and the task-failure bridge into aeth_ext's loop handler."""

# Standard library imports
import asyncio
import importlib

# First party imports
from timeclock_backend.startup import _surface_task_failure  # pyright: ignore[reportPrivateUsage]


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
