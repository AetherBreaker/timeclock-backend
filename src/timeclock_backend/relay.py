"""TCP relay from the clocks' WireGuard addresses to the PostgreSQL sidecar (spec §8.1).

Adds reach only: PostgreSQL's own password authentication still applies to every relayed connection.
"""

# Standard library imports
import asyncio
import logging
import socket
from ipaddress import IPv4Address, IPv4Network
from typing import Final

LOGGER = logging.getLogger(__name__)

# Keepalive so a half-dead tunnel peer is noticed instead of holding a Postgres backend forever; the TCP_* knobs are platform-dependent.
_KEEPALIVE: Final = [(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)] + [
  (socket.IPPROTO_TCP, getattr(socket, name), value)
  for name, value in (("TCP_KEEPIDLE", 30), ("TCP_KEEPINTVL", 10), ("TCP_KEEPCNT", 3))
  if hasattr(socket, name)
]


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
  """Copy bytes until EOF, without parsing them."""
  while data := await reader.read(65536):
    # uvloop/winloop's write() raises RuntimeError on a closing handle, where the stdlib loop drops the data.
    if writer.is_closing():
      return
    writer.write(data)
    await writer.drain()


async def start_relay(
  *, port: int, upstream_host: str, upstream_port: int, allowed: IPv4Network, host: str = "0.0.0.0"
) -> asyncio.Server:
  """Start the relay; the caller closes the returned server.

  Args:
    port: Port to listen on.
    upstream_host: PostgreSQL host.
    upstream_port: PostgreSQL port.
    allowed: Only peers inside this network are relayed; others are closed before the upstream is contacted.
    host: Address to listen on.

  Returns:
    The started server.
  """

  async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    peer = writer.get_extra_info("peername")[0]
    upstream_writer: asyncio.StreamWriter | None = None
    tasks: list[asyncio.Task[None]] = []
    try:
      if IPv4Address(peer) not in allowed:
        LOGGER.debug("Relay refused %s: outside %s", peer, allowed)
        return
      async with asyncio.timeout(5):
        upstream_reader, upstream_writer = await asyncio.open_connection(upstream_host, upstream_port)
      for stream in (writer, upstream_writer):
        sock = stream.get_extra_info("socket")
        if sock is None:
          # uvloop/winloop report no socket once the peer's reset has closed the transport; `finally` cleans up.
          return
        for level, option, value in _KEEPALIVE:
          sock.setsockopt(level, option, value)
      tasks = [asyncio.create_task(_pipe(reader, upstream_writer)), asyncio.create_task(_pipe(upstream_reader, writer))]
      done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
      for task in done:
        task.result()
    except OSError as exc:
      LOGGER.warning("Relay connection from %s failed: %r", peer, exc)
    finally:
      # Also reached on cancellation (server shutdown): no orphaned pipe tasks or open transports.
      for task in tasks:
        task.cancel()
      await asyncio.gather(*tasks, return_exceptions=True)
      writer.close()
      if upstream_writer is not None:
        upstream_writer.close()

  return await asyncio.start_server(handle, host, port)
