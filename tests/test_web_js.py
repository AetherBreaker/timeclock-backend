"""The shift editor's JavaScript tests (tests/js), run under Node's own test runner."""

# Standard library imports
import shutil
import subprocess
from pathlib import Path

# Third party imports
import pytest

JS_TESTS = Path(__file__).with_name("js")


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_the_editor_logic_passes_its_node_tests() -> None:
  node = shutil.which("node")
  assert node is not None
  files = sorted(str(f) for f in JS_TESTS.glob("*.test.mjs"))
  assert files
  done = subprocess.run([node, "--test", *files], capture_output=True, text=True, check=False, timeout=120)
  assert done.returncode == 0, done.stdout + done.stderr
