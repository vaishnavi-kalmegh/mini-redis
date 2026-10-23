"""Mini Redis: a small Redis-compatible server.

Run:   python server.py --port 6380
Try:   redis-cli -p 6380 PING
"""
import argparse
import asyncio
import sys
import traceback

import commands
import resp
from store import Store

store = Store()

# If a client has sent this many bytes of one unfinished command, give up on it.
# (Real Redis uses 1 GB too.) Set by --max-query-buffer; tests use a tiny value.
max_query_buffer = 1024 ** 3


async def handle_client(reader, writer):
    """One of these runs per connected client. Because they are all coroutines
    on a single event loop, a client that is slow or idle only pauses its own
    coroutine, and commands from different clients never run at the same time."""
    buf = bytearray()
    try:
        while True:
            data = await reader.read(65536)
            if not data:
                break  # client disconnected
            buf.extend(data)

            # Parse as many complete commands as the buffer holds.
            while True:
                try:
                    parsed = resp.parse_command(buf)
                except resp.ProtocolError as e:
                    writer.write(resp.error("ERR Protocol error: %s" % e))
                    await writer.drain()
                    return  # real Redis also closes the connection here
                if parsed is None:
                    break  # incomplete: wait for more data
                args, consumed = parsed
                del buf[:consumed]
                if args:  # ignore empty lines
                    try:
                        writer.write(commands.execute(store, args))
                    except Exception:
                        traceback.print_exc(file=sys.stderr)
                        writer.write(resp.error("ERR internal error"))

            if len(buf) > max_query_buffer:
                writer.write(resp.error("ERR Protocol error: query buffer limit exceeded"))
                await writer.drain()
                return

            # drain() waits if this client isn't reading its replies fast enough.
            # Only this client's coroutine waits; everyone else keeps being served.
            await writer.drain()
    except ConnectionError:
        pass
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass  # the client is already gone, nothing left to clean up


async def sweeper():
    """Active expiry: about once a second, clean out expired keys."""
    while True:
        await asyncio.sleep(1)
        store.sweep()


async def main(host, port):
    server = await asyncio.start_server(handle_client, host, port)
    sweep_task = asyncio.create_task(sweeper())  # keep a reference so it isn't garbage collected
    print("Mini Redis listening on %s:%d" % (host, port), flush=True)
    try:
        async with server:
            await server.serve_forever()
    finally:
        sweep_task.cancel()


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=6380)
    p.add_argument("--max-query-buffer", type=int, default=max_query_buffer,
                   help="bytes of one unfinished command before the client is dropped")
    a = p.parse_args()
    max_query_buffer = a.max_query_buffer
    try:
        asyncio.run(main(a.host, a.port))
    except KeyboardInterrupt:
        pass
