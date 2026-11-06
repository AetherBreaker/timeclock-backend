"""Tests for the compose file: the page's container, its network, and Postgres's client access rules."""

# Standard library imports
import ipaddress
import os
import re
import socket
import subprocess
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

# Third party imports
import psycopg
import pytest
import yaml

COMPOSE: dict[str, Any] = yaml.safe_load((Path(__file__).parents[1] / "docker" / "compose.yaml").read_text(encoding="utf-8"))
SERVICES: dict[str, Any] = COMPOSE["services"]
PAGE_NETWORK = "timeclock-web-db"
IMAGE_DEFAULTS = [
  ["local", "all", "all", "trust"],
  ["host", "all", "all", "127.0.0.1/32", "trust"],
  ["host", "all", "all", "::1/128", "trust"],
  ["local", "replication", "all", "trust"],
  ["host", "replication", "all", "127.0.0.1/32", "trust"],
  ["host", "replication", "all", "::1/128", "trust"],
  ["host", "all", "all", "all", "scram-sha-256"],
]


def _networks(service: str) -> dict[str, dict[str, Any]]:
  networks = SERVICES[service].get("networks", {})
  return {name: {} for name in networks} if isinstance(networks, list) else {k: v or {} for k, v in networks.items()}


def _environment(service: str) -> dict[str, str]:
  return dict(entry.split("=", 1) for entry in SERVICES[service]["environment"])


def _hba_rules() -> list[list[str]]:
  script = SERVICES["timeclock-postgres"]["entrypoint"][-1]
  lines = script.splitlines()
  start = next(i for i, line in enumerate(lines) if "<<'EOF'" in line)
  end = next(i for i in range(start + 1, len(lines)) if lines[i].strip() == "EOF")
  return [line.split() for line in lines[start + 1 : end] if line.strip() and not line.lstrip().startswith("#")]


def _subnet() -> ipaddress.IPv4Network:
  return ipaddress.IPv4Network(COMPOSE["networks"][PAGE_NETWORK]["ipam"]["config"][0]["subnet"])


def _address(service: str) -> ipaddress.IPv4Address:
  return ipaddress.IPv4Address(_networks(service)[PAGE_NETWORK]["ipv4_address"])


def test_the_page_builds_the_backends_image() -> None:
  assert SERVICES["timeclock-web"]["build"] == SERVICES["timeclock-backend"]["build"]


def test_only_the_page_shares_the_page_network_with_postgres() -> None:
  assert {name for name in SERVICES if PAGE_NETWORK in _networks(name)} == {"timeclock-web", "timeclock-postgres"}
  assert "timeclock-db" not in _networks("timeclock-web")


def test_the_page_reaches_postgres_at_its_address_on_the_page_network() -> None:
  postgres = _address("timeclock-postgres")
  assert ipaddress.IPv4Address(_environment("timeclock-web")["PAGE_POSTGRES_HOST"]) == postgres
  assert postgres in _subnet()


def test_the_page_login_is_allowed_only_from_the_page_network() -> None:
  page = _address("timeclock-web")
  assert page in _subnet()
  assert page != next(_subnet().hosts())  # Docker gives the first address to the gateway
  assert _hba_rules() == [
    ["host", "all", "timeclock_web", f"{page}/32", "scram-sha-256"],
    ["host", "all", "all", f"{page}/32", "reject"],
    ["host", "all", "timeclock_web", "all", "reject"],
    ["local", "all", "timeclock_web", "reject"],
    *IMAGE_DEFAULTS,
  ]


def _docker(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
  return subprocess.run(["docker", *args], capture_output=True, text=True, check=check, timeout=300)


@pytest.mark.skipif(os.environ.get("TIMECLOCK_BACKEND_DOCKER_TESTS") != "1", reason="TIMECLOCK_BACKEND_DOCKER_TESTS is not set")
def test_postgres_takes_the_page_login_only_from_the_page_network() -> None:
  service = SERVICES["timeclock-postgres"]
  network, container = f"tcb-hba-{uuid4().hex[:8]}", f"tcb-hba-pg-{uuid4().hex[:8]}"
  postgres, page = str(_address("timeclock-postgres")), str(_address("timeclock-web"))
  stranger = str(next(a for a in _subnet().hosts() if str(a) not in {postgres, page} and a != next(_subnet().hosts())))
  with socket.socket() as s:
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
  entrypoint, *script = service["entrypoint"]
  _docker("network", "create", "--subnet", str(_subnet()), network)
  try:
    _docker(
      "run", "-d", "--name", container, "--network", network, "--ip", postgres, "-e", "POSTGRES_PASSWORD=pw",
      "-p", f"127.0.0.1:{port}:5432", "--entrypoint", entrypoint, service["image"], *script,
    )  # fmt: skip
    as_postgres = {"host": "127.0.0.1", "port": port, "user": "postgres", "password": "pw", "dbname": "postgres", "connect_timeout": 3}
    deadline = time.monotonic() + 120
    while True:  # the image's first start runs a temporary server first, so wait for the real one on the port
      try:
        with psycopg.connect(**as_postgres) as conn:
          conn.execute("SELECT 1")
        break
      except psycopg.OperationalError:
        if time.monotonic() > deadline:
          raise
        time.sleep(1)
    with psycopg.connect(**as_postgres, autocommit=True) as conn:
      conn.execute("CREATE ROLE timeclock_web LOGIN PASSWORD 'pw'")
      assert conn.execute("SELECT count(*) FROM pg_hba_file_rules WHERE error IS NOT NULL").fetchone() == (0,)

    def psql_from(address: str) -> subprocess.CompletedProcess[str]:
      return _docker(
        "run", "--rm", "--network", network, "--ip", address, "-e", "PGPASSWORD=pw", "--entrypoint", "psql", service["image"],
        "-h", postgres, "-U", "timeclock_web", "-d", "postgres", "-c", "SELECT 1", check=False,
      )  # fmt: skip

    assert psql_from(page).returncode == 0
    refused = psql_from(stranger)
    assert refused.returncode != 0
    assert "pg_hba.conf rejects connection" in refused.stderr
    with pytest.raises(psycopg.OperationalError, match=r"pg_hba\.conf rejects connection"):
      psycopg.connect(**{**as_postgres, "user": "timeclock_web"})
  finally:
    _docker("rm", "-f", container, check=False)
    _docker("network", "rm", network, check=False)


def test_a_missing_page_variable_holds_back_only_the_page() -> None:
  # `${X:?}` refuses the whole file, relay and backend included; the page refuses to start on its own without its secrets.
  required = set(re.findall(r"\$\{(\w+):\?", "\n".join(SERVICES["timeclock-web"]["environment"])))
  others = {
    m for name, s in SERVICES.items() if name != "timeclock-web" for m in re.findall(r"\$\{(\w+)", "\n".join(s["environment"]))
  }
  assert required <= others


def test_the_page_gets_each_of_its_variables_from_coolify() -> None:
  names = (
    "PAGE_PUBLIC_URL",
    "PAGE_SESSION_SECRET",
    "TIMECLOCK_WEB_PASSWORD",
    "ZOHO_CLIENT_ID",
    "ZOHO_CLIENT_SECRET",
    "PAGE_TEST_ACCOUNTS",
  )
  environment = _environment("timeclock-web")
  assert {name: environment.get(name) for name in names} == {name: f"${{{name}:-}}" for name in names}


def test_the_proxy_can_find_the_pages_port() -> None:
  # The image declares no port, and Coolify keeps a domain's port apart from the compose file, so the page names its own.
  assert SERVICES["timeclock-web"]["expose"] == ["8080"]  # PAGE_PORT's default
