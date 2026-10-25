"""Mini Redis: a small Redis-compatible server.

Run:   python server.py --port 6380
       python server.py --port 6380 --aof-file mini.aof     (keep data across restarts)
Try:   redis-cli -p 6380 PING
"""
import argparse
import asyncio
import os
import signal
import sys
import traceback

import aof as aof_module
import commands
import resp
from store import Store

store = Store()
log = None  # the AppendOnlyLog, when --aof-file is given

# If a client has sent this many bytes of one unfinished command, give up on it.
# (Real Redis uses 1 GB too.) Set by --max-query-buffer; tests use a tiny value.
max_query_buffer = 1024 ** 3


def log_writes():
    """Append the changes the last command made to the log. Called BEFORE the
    client is sent its reply, so no write is ever acknowledged unless it has
    been handed to the log."""
    if not store.pending:
        return
    try:
        log.append(store.take_pending())
    except OSError as e:
        # The data in memory now has changes the log doesn't. Carrying on would
        # mean promising durability we can't give, so stop (Redis does the same
        # when "appendfsync always" fails).
        print("FATAL: cannot write to the append-only log (%s). Stopping." % e,
              file=sys.stderr, flush=True)
        os._exit(1)


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

            # Run every complete command the buffer holds, collecting the replies.
            # Doing the writes once per batch, not once per command, matters a lot:
            # a socket write is a system call, and profiling showed that making one
            # per command was about a third of all the server's time when a client
            # pipelines (sends many commands without waiting for replies).
            replies = []
            protocol_error = None
            while True:
                try:
                    parsed = resp.parse_command(buf)
                except resp.ProtocolError as e:
                    protocol_error = e
                    break
                if parsed is None:
                    break  # incomplete: wait for more data
                args, consumed = parsed
                del buf[:consumed]
                if args:  # ignore empty lines
                    try:
                        replies.append(commands.execute(store, args))
                    except Exception:
                        traceback.print_exc(file=sys.stderr)
                        replies.append(resp.error("ERR internal error"))

            # The batch's changes go to the log BEFORE any reply is sent, so a
            # write is still never acknowledged unless it has been logged.
            log_writes()
            if protocol_error is not None:
                # Replies to the commands before the bad data still go out first.
                replies.append(resp.error("ERR Protocol error: %s" % protocol_error))
            if replies:
                writer.write(b"".join(replies))
            if protocol_error is not None:
                await writer.drain()
                return  # real Redis also closes the connection here

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
        log_writes()  # expiries are logged as DEL


async def main(host, port, aof_path=None, fsync_policy="everysec"):
    global log
    if aof_path:
        replayed, ignored = aof_module.load(aof_path, store)  # may raise AOFError
        print("Loaded %d commands from %s" % (replayed, aof_path), flush=True)
        if ignored:
            print("WARNING: ignored %d bytes of an incomplete command at the end of %s "
                  "(the server was probably stopped in the middle of a write); removed them."
                  % (ignored, aof_path), file=sys.stderr, flush=True)
        log = aof_module.AppendOnlyLog(aof_path, fsync_policy)
        store.record_writes = True

    server = await asyncio.start_server(handle_client, host, port)
    tasks = [asyncio.create_task(sweeper())]  # keep references so they aren't garbage collected
    if log and fsync_policy == "everysec":
        tasks.append(asyncio.create_task(log.sync_forever()))

    # Stop cleanly on Ctrl+C / SIGTERM so the log gets a final fsync.
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass  # Windows: Ctrl+C still stops the program, just without the final fsync

    print("Mini Redis listening on %s:%d" % (host, port), flush=True)
    try:
        await stop.wait()
    finally:
        server.close()
        for task in tasks:
            task.cancel()
        if log:
            log.close()


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=6380)
    p.add_argument("--max-query-buffer", type=int, default=max_query_buffer,
                   help="bytes of one unfinished command before the client is dropped")
    p.add_argument("--aof-file", help="append-only log file; enables saving data to disk")
    p.add_argument("--appendfsync", choices=["always", "everysec", "no"], default="everysec",
                   help="how often to fsync the log (default: everysec)")
    a = p.parse_args()
    max_query_buffer = a.max_query_buffer
    try:
        asyncio.run(main(a.host, a.port, a.aof_file, a.appendfsync))
    except aof_module.AOFError as e:
        print("FATAL: the append-only log %s is damaged and the server will not start: %s\n"
              "Nothing was changed. Fix or move the file, then start again." % (a.aof_file, e),
              file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        pass
