"""Helpers shared by the socket-based tests."""
import asyncio
import socket
import subprocess
import sys
import time


def cmd(*parts):
    """Encode a command in RESP, e.g. cmd('SET', 'a', '1')."""
    out = b"*%d\r\n" % len(parts)
    for p in parts:
        p = p.encode() if isinstance(p, str) else p
        out += b"$%d\r\n%s\r\n" % (len(p), p)
    return out


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def start_server(*extra_args):
    """Start server.py as a separate process. Returns (process, port)."""
    port = free_port()
    proc = subprocess.Popen(
        [sys.executable, "server.py", "--port", str(port), *extra_args],
        stdout=subprocess.DEVNULL,
    )
    for _ in range(50):
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            return proc, port
        except OSError:
            time.sleep(0.1)
    proc.kill()
    raise RuntimeError("server did not start")


def stop_server(proc):
    proc.terminate()
    proc.wait()


# ---- a tiny asyncio client ----

class RespError(Exception):
    """An error reply (-ERR ...) from the server."""


async def connect(port):
    return await asyncio.open_connection("127.0.0.1", port)


async def close(writer):
    writer.close()
    try:
        await writer.wait_closed()
    except Exception:
        pass


async def read_reply(reader):
    """Read one reply: str -> bytes, error -> RespError, int -> int,
    bulk -> bytes or None, array -> list."""
    line = await reader.readline()
    if not line:
        raise ConnectionError("server closed the connection")
    kind, rest = line[:1], line[1:-2]
    if kind == b"+":
        return rest
    if kind == b"-":
        return RespError(rest.decode())
    if kind == b":":
        return int(rest)
    if kind == b"$":
        n = int(rest)
        if n < 0:
            return None
        return (await reader.readexactly(n + 2))[:-2]
    if kind == b"*":
        return [await read_reply(reader) for _ in range(int(rest))]
    raise ValueError("unexpected reply %r" % line)


async def call(reader, writer, *parts, timeout=10):
    writer.write(cmd(*parts))
    await writer.drain()
    return await asyncio.wait_for(read_reply(reader), timeout)
