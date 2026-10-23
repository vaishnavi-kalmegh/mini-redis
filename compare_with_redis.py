"""Send the same commands to Mini Redis and to real Redis and show every reply
that differs. Real Redis is the answer key.

Usage:
    redis-server --port 6400 --save "" --appendonly no &
    python compare_with_redis.py            # starts Mini Redis itself, compares with :6400
    python compare_with_redis.py 6400       # same, with a custom port for real Redis

Only deterministic commands are used (no sleeping), so the output is stable.
"""
import asyncio
import sys

from testutil import RespError, call, close, connect, start_server, stop_server

BAD_INTS = ["007", " 5", "5 ", "+5", "-0", "1.5", "", "1_000", "0x10", "- 5", "--5", "00",
            "0", "-1", "123", "9223372036854775807", "-9223372036854775808",
            "9223372036854775808", "-9223372036854775809", "1e3"]

SCRIPT = [
    # connection
    ("PING",), ("PING", "hi"), ("PING", "a", "b"), ("ping",), ("pInG", "x"),
    # SET / GET
    ("SET", "a", "1"), ("GET", "a"), ("GET", "nope"), ("GET",), ("GET", "a", "b"),
    ("SET", "a"), ("SET",), ("sEt", "ci", "1"), ("get", "ci"), ("SET", "empty", ""), ("GET", "empty"),
    ("SET", "bin", "a\r\nb\x00c"), ("GET", "bin"), ("SET", "uni", "नमस्ते"), ("GET", "uni"),
    # SET options
    ("SET", "e", "v", "EX", "0"), ("SET", "e", "v", "EX", "-1"), ("SET", "e", "v", "EX", "abc"),
    ("SET", "e", "v", "EX"), ("SET", "e", "v", "EX", "5", "PX", "5"), ("SET", "e", "v", "FOO"),
    ("SET", "e", "v", "PX", "0"), ("SET", "e", "v", "EX", " 5"), ("SET", "e", "v", "EX", "1.5"),
    ("SET", "e", "v", "EX", "9223372036854775807"), ("SET", "e", "v", "PX", "9223372036854775807"),
    ("SET", "e", "v", "EX", "9223372036854775808"), ("EXISTS", "e"),
    ("SET", "e", "v", "ex", "100"), ("TTL", "e"), ("SET", "e", "v"), ("TTL", "e"),
    ("SET", "e", "v", "px", "100000"), ("TTL", "e"),
    # DEL / EXISTS
    ("DEL",), ("EXISTS",), ("SET", "d1", "x"), ("EXISTS", "d1", "d1", "nope"),
    ("DEL", "d1", "d1", "nope"), ("EXISTS", "d1"), ("DEL", "nope"),
    # EXPIRE / TTL
    ("SET", "t", "v"), ("TTL", "t"), ("EXPIRE", "t", "100"), ("TTL", "t"), ("EXPIRE", "nope", "10"),
    ("EXPIRE", "t", "abc"), ("EXPIRE", "t", "1.5"), ("EXPIRE", "t", " 5"), ("EXPIRE", "t"),
    ("EXPIRE", "t", "0"), ("EXISTS", "t"), ("TTL", "nope"), ("TTL",), ("TTL", "a", "b"),
    ("SET", "t2", "v"), ("EXPIRE", "t2", "-5"), ("EXISTS", "t2"),
    ("SET", "t3", "v"), ("EXPIRE", "t3", "9223372036854775807"), ("EXPIRE", "t3", "9223372036854775808"),
    ("EXPIRE", "t3", "4611686018427387"), ("TTL", "t3"), ("EXPIRE", "nope", "abc"),
    # INCR / DECR
    ("INCR", "c"), ("INCR", "c"), ("DECR", "c"), ("DECR", "c"), ("DECR", "c"), ("DECR", "c2"),
    ("SET", "n", "hello"), ("INCR", "n"), ("GET", "n"), ("INCR",), ("INCR", "a", "b"), ("DECR",),
    *[cmd for i, bad in enumerate(BAD_INTS)
      for cmd in (("SET", "bad%d" % i, bad), ("INCR", "bad%d" % i), ("DECR", "bad%d" % i), ("GET", "bad%d" % i))],
    # lists
    ("RPUSH", "l", "a", "b", "c"), ("LPUSH", "l", "x", "y"), ("LRANGE", "l", "0", "-1"),
    *[("LRANGE", "l", s, e) for s, e in [("0", "0"), ("1", "2"), ("-2", "-1"), ("0", "100"), ("-100", "1"),
                                       ("3", "1"), ("10", "20"), ("0", "-100"), ("-1", "-1"), ("5", "5"),
                                       ("6", "6"), ("x", "1"), ("1", "y"), (" 1", "2"), ("-100", "-50"),
                                       ("9223372036854775807", "-1"), ("0", "9223372036854775807"),
                                       ("-9223372036854775808", "-1")]],
    ("LRANGE", "nope", "0", "-1"), ("LRANGE", "l", "0"), ("LRANGE", "l"), ("LPUSH", "l"), ("RPUSH",),
    ("RPUSH", "l2", ""), ("LRANGE", "l2", "0", "-1"), ("LPUSH", "l3", "a", "b", "c"), ("LRANGE", "l3", "0", "-1"),
    ("RPUSH", "l3", "a\r\nb"), ("LRANGE", "l3", "-1", "-1"),
    # hashes
    ("HSET", "h", "a", "1"), ("HSET", "h", "a", "2", "b", "3"), ("HGET", "h", "a"), ("HGET", "h", "b"),
    ("HGET", "h", "zz"), ("HGET", "nope", "a"), ("HSET", "h", "a"), ("HSET", "h", "a", "1", "b"),
    ("HSET", "h"), ("HSET",), ("HGET", "h"), ("HGET", "h", "a", "b"), ("HSET", "h", "", "empty"),
    ("HGET", "h", ""), ("HSET", "h", "a", "9", "a", "10"), ("HGET", "h", "a"),
    # wrong types
    ("SET", "s", "v"), ("RPUSH", "wl", "a"), ("HSET", "wh", "f", "v"),
    ("GET", "wl"), ("GET", "wh"), ("INCR", "wl"), ("DECR", "wh"), ("LPUSH", "s", "x"), ("RPUSH", "wh", "x"),
    ("LRANGE", "s", "0", "-1"), ("LRANGE", "wh", "0", "-1"), ("HSET", "s", "f", "v"), ("HSET", "wl", "f", "v"),
    ("HGET", "s", "f"), ("HGET", "wl", "f"), ("EXPIRE", "wl", "100"), ("TTL", "wl"), ("TTL", "wh"),
    ("EXISTS", "s", "wl", "wh"), ("SET", "wl", "now string"), ("GET", "wl"), ("DEL", "s", "wl", "wh"),
    ("EXISTS", "s", "wl", "wh"),
    # TTL behaviour of writes
    ("SET", "k1", "10", "EX", "100"), ("INCR", "k1"), ("TTL", "k1"), ("SET", "k1", "v"), ("TTL", "k1"),
    ("RPUSH", "k2", "a"), ("EXPIRE", "k2", "100"), ("RPUSH", "k2", "b"), ("TTL", "k2"),
    ("HSET", "k3", "f", "v"), ("EXPIRE", "k3", "100"), ("HSET", "k3", "g", "v"), ("TTL", "k3"),
    # expiry range checks (a deadline that doesn't fit in 64 bits is an error)
    ("EXPIRE", "nope", "9223372036854775807"), ("EXPIRE", "nope", "-9223372036854775808"),
    ("SET", "x1", "v"), ("EXPIRE", "x1", "-9223372036854775808"), ("EXPIRE", "x1", "-9223372036854776"),
    ("EXPIRE", "x1", "-9223372036854775"), ("EXISTS", "x1"),
    ("SET", "x2", "v"), ("EXPIRE", "x2", "9223372036854776"), ("EXPIRE", "x2", "9223372036854775"),
    ("SET", "x3", "v", "EX", "9223372036854775"), ("SET", "x3", "v", "EX", "9223372036854776"),
    ("SET", "x3", "v", "PX", "9223372036854775807"), ("SET", "x3", "v", "PX", "9223372036854770000"),
    ("SET", "x3", "v", "PX", "-9223372036854775808"),
    # unknown commands: the error text should match real Redis, including odd names
    ("FLY",), ("FLY", "a", "b"), ("",), ("FLY", ""), ("FLY", "x" * 300), ("FLY", "a" * 100, "b" * 100, "c"),
    ("A\r\nB", "line\nbreak"), (b"\xff\xfe",), ("FLY", b"\xff\xfe", "ok"), ("GET\r\n",),
    # the connection must still be in sync after all of that
    ("PING",), ("SET", "after", "ok"), ("GET", "after"),
]

# Known, deliberate differences: (first word of the command) -> why. None right now.
EXPECTED = {}


def show(value):
    if isinstance(value, RespError):
        return "ERR<%s>" % value
    return repr(value)


async def run(script, mini_port, real_port):
    mr, mw = await connect(mini_port)
    rr, rw = await connect(real_port)
    diffs = []
    for command in script:
        mine = await call(mr, mw, *command)
        real = await call(rr, rw, *command)
        if show(mine) != show(real):
            diffs.append((command, mine, real))
    await close(mw)
    await close(rw)
    return diffs


def main():
    real_port = int(sys.argv[1]) if len(sys.argv) > 1 else 6400
    proc, mini_port = start_server()
    try:
        diffs = asyncio.run(run(SCRIPT, mini_port, real_port))
    finally:
        stop_server(proc)

    unexpected = [d for d in diffs if d[0][:1] not in EXPECTED]
    print("%d commands compared, %d differ (%d expected)" % (len(SCRIPT), len(diffs), len(diffs) - len(unexpected)))
    for command, mine, real in unexpected:
        name = " ".join(c if isinstance(c, str) else repr(c) for c in command)[:60]
        print("  %-60s\n      mini: %s\n      real: %s" % (name, show(mine), show(real)))
    return 1 if unexpected else 0


if __name__ == "__main__":
    sys.exit(main())
