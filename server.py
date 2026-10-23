"""Mini Redis: a small Redis-compatible server. Stage 1 (basic commands).

Run:   python server.py --port 6380
Try:   redis-cli -p 6380 PING
"""
import argparse
import asyncio

import resp

# The whole database: key -> value (both bytes).
store = {}


# ---- Commands. Each takes the argument list (without the command name)
# ---- and returns an already-encoded RESP reply.

def cmd_ping(args):
    if len(args) > 1:
        return wrong_args("ping")
    if args:
        return resp.bulk_string(args[0])
    return resp.simple_string("PONG")


def cmd_set(args):
    if len(args) != 2:
        # Options like EX/NX come in Stage 2.
        return wrong_args("set")
    store[args[0]] = args[1]
    return resp.simple_string("OK")


def cmd_get(args):
    if len(args) != 1:
        return wrong_args("get")
    return resp.bulk_string(store.get(args[0]))


def cmd_del(args):
    if not args:
        return wrong_args("del")
    removed = 0
    for key in args:
        if store.pop(key, None) is not None:
            removed += 1
    return resp.integer(removed)


def cmd_exists(args):
    if not args:
        return wrong_args("exists")
    return resp.integer(sum(1 for key in args if key in store))


def cmd_command(args):
    # redis-cli asks "COMMAND DOCS" when it connects. An empty list keeps it happy.
    return resp.array([])


def wrong_args(name):
    return resp.error("ERR wrong number of arguments for '%s' command" % name)


COMMANDS = {
    b"PING": cmd_ping,
    b"SET": cmd_set,
    b"GET": cmd_get,
    b"DEL": cmd_del,
    b"EXISTS": cmd_exists,
    b"COMMAND": cmd_command,
}


def execute(args):
    name = args[0].upper()
    handler = COMMANDS.get(name)
    if handler is None:
        return resp.error("ERR unknown command '%s'" % args[0].decode(errors="replace"))
    return handler(args[1:])


# ---- Networking ----

async def handle_client(reader, writer):
    buf = bytearray()
    try:
        while True:
            data = await reader.read(4096)
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
                    writer.write(execute(args))

            await writer.drain()
    except ConnectionError:
        pass
    finally:
        writer.close()


async def main(host, port):
    server = await asyncio.start_server(handle_client, host, port)
    print("Mini Redis listening on %s:%d" % (host, port), flush=True)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=6380)
    a = p.parse_args()
    try:
        asyncio.run(main(a.host, a.port))
    except KeyboardInterrupt:
        pass
