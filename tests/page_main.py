"""Run by `user_simulation(main_file=…)`, which resets NiceGUI first: registers the page with the test's arguments."""

# Third party imports
import page_sim
from nicegui import ui

# First party imports
from timeclock_backend.web.app import build

build(**page_sim.BUILD_ARGS)
ui.run(storage_secret="test")
