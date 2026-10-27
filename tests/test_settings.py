# Standard library imports
from ipaddress import IPv4Network

# First party imports
from timeclock_backend import databases
from timeclock_backend.databases import DATABASES, Database
from timeclock_backend.settings import Settings


def test_settings_defaults():
  s = Settings.get_settings()
  assert (s.postgres_host, s.postgres_port, s.relay_port, s.relay_allowed_cidr) == ("postgres", 5432, 5432, IPv4Network("10.8.0.0/24"))


def test_databases_and_their_groups():
  assert DATABASES == (Database("timeclock", "timeclock_clocks"), Database("timeclock_test", "timeclock_test_clocks"))


def test_server_conninfo_never_names_a_database():
  assert "dbname" not in databases.SERVER_CONNINFO
  assert "user=postgres" in databases.SERVER_CONNINFO
