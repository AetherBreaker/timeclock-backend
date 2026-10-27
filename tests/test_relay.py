"""The relay carries bytes to an upstream echo server and refuses peers outside the allowed network."""

# Standard library imports
import asyncio
import socket
from dataclasses import dataclass, field
from ipaddress import IPv4Network
from typing import TYPE_CHECKING

# Third party imports
import pytest

# First party imports
from timeclock_backend.relay import start_relay

if TYPE_CHECKING:
  # Standard library imports
  from collections.abc import AsyncIterator


@dataclass
class Upstream:
  """An echo server that counts connections and notes when one reaches EOF."""

  port: int = 0
  accepted: int = 0
  eof: asyncio.Event = field(default_factory=asyncio.Event)


@pytest.fixture
async def upstream() -> AsyncIterator[Upstream]:
  state = Upstream()

  async def echo(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    state.accepted += 1
    try:
      while data := await reader.read(65536):
        writer.write(data)
        await writer.drain()
    except OSError:
      pass
    state.eof.set()
    writer.close()

  server = await asyncio.start_server(echo, "127.0.0.1", 0)
  state.port = server.sockets[0].getsockname()[1]
  yield state
  server.close()
  server.close_clients()


@pytest.fixture
async def loop_errors() -> list[dict[str, object]]:
  errors: list[dict[str, object]] = []
  asyncio.get_running_loop().set_exception_handler(lambda _loop, ctx: errors.append(ctx))
  return errors


def port_of(server: asyncio.Server) -> int:
  return server.sockets[0].getsockname()[1]


async def test_relay_pipes_bytes_both_ways(upstream: Upstream, loop_errors: list[dict[str, object]]) -> None:
  relay = await start_relay(
    port=0, host="127.0.0.1", upstream_host="127.0.0.1", upstream_port=upstream.port, allowed=IPv4Network("127.0.0.0/8")
  )
  reader, writer = await asyncio.open_connection("127.0.0.1", port_of(relay))
  writer.write(b"ping")
  await writer.drain()
  assert await asyncio.wait_for(reader.readexactly(4), 2) == b"ping"
  writer.close()
  await writer.wait_closed()
  await asyncio.wait_for(upstream.eof.wait(), 2)
  relay.close()
  relay.close_clients()
  assert loop_errors == []


async def test_relay_rejects_outside_cidr(upstream: Upstream, loop_errors: list[dict[str, object]]) -> None:
  relay = await start_relay(
    port=0, host="127.0.0.1", upstream_host="127.0.0.1", upstream_port=upstream.port, allowed=IPv4Network("10.8.0.0/24")
  )
  reader, writer = await asyncio.open_connection("127.0.0.1", port_of(relay))
  assert await asyncio.wait_for(reader.read(), 2) == b""
  assert upstream.accepted == 0
  writer.close()
  relay.close()
  relay.close_clients()
  assert loop_errors == []


async def test_relay_closes_client_when_upstream_unreachable(loop_errors: list[dict[str, object]]) -> None:
  with socket.socket() as probe:
    probe.bind(("127.0.0.1", 0))
    dead_port = probe.getsockname()[1]
  relay = await start_relay(
    port=0, host="127.0.0.1", upstream_host="127.0.0.1", upstream_port=dead_port, allowed=IPv4Network("127.0.0.0/8")
  )
  reader, writer = await asyncio.open_connection("127.0.0.1", port_of(relay))
  assert await asyncio.wait_for(reader.read(), 7) == b""
  writer.close()
  relay.close()
  relay.close_clients()
  assert loop_errors == []


async def test_relay_closes_upstream_when_client_disconnects(upstream: Upstream, loop_errors: list[dict[str, object]]) -> None:
  relay = await start_relay(
    port=0, host="127.0.0.1", upstream_host="127.0.0.1", upstream_port=upstream.port, allowed=IPv4Network("127.0.0.0/8")
  )
  reader, writer = await asyncio.open_connection("127.0.0.1", port_of(relay))
  writer.write(b"x")
  await writer.drain()
  assert await asyncio.wait_for(reader.readexactly(1), 2) == b"x"
  writer.close()
  await writer.wait_closed()
  await asyncio.wait_for(upstream.eof.wait(), 2)
  relay.close()
  relay.close_clients()
  assert loop_errors == []
