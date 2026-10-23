"""Command handlers. Each takes (store, args) where args excludes the command
name, and returns an already-encoded RESP reply."""
import re
from collections import deque
from itertools import islice

import resp

INT_RE = re.compile(rb"-?(0|[1-9][0-9]*)")
INT64_MIN, INT64_MAX = -(2 ** 63), 2 ** 63 - 1

WRONGTYPE = resp.error("WRONGTYPE Operation against a key holding the wrong kind of value")
NOT_INT = resp.error("ERR value is not an integer or out of range")
SYNTAX = resp.error("ERR syntax error")


def parse_int(raw):
    """Strict integer parsing. Returns None for anything Redis would reject
    (Python's int() is too forgiving: it accepts ' 5' and '1_000')."""
    if INT_RE.fullmatch(raw):
        n = int(raw)
        if INT64_MIN <= n <= INT64_MAX:
            return n
    return None


def wrong_args(name):
    return resp.error("ERR wrong number of arguments for '%s' command" % name)


# ---- connection / strings ----

def cmd_ping(store, args):
    if len(args) > 1:
        return wrong_args("ping")
    if args:
        return resp.bulk_string(args[0])
    return resp.simple_string("PONG")


def cmd_set(store, args):
    """SET key value [EX seconds | PX milliseconds]"""
    if len(args) < 2:
        return wrong_args("set")
    key, value = args[0], args[1]

    deadline_ms = None
    i = 2
    while i < len(args):
        option = args[i].upper()
        if option in (b"EX", b"PX") and deadline_ms is None and i + 1 < len(args):
            amount = parse_int(args[i + 1])
            if amount is None:
                return NOT_INT
            if amount <= 0:
                return resp.error("ERR invalid expire time in 'set' command")
            unit = 1000 if option == b"EX" else 1
            deadline_ms = store.now_ms() + amount * unit
            i += 2
        else:
            return SYNTAX

    store.set(key, value, deadline_ms)
    return resp.simple_string("OK")


def cmd_get(store, args):
    if len(args) != 1:
        return wrong_args("get")
    value = store.get(args[0])
    if value is not None and not isinstance(value, bytes):
        return WRONGTYPE
    return resp.bulk_string(value)


def cmd_del(store, args):
    if not args:
        return wrong_args("del")
    # Check each key first so an expired key counts as already gone.
    return resp.integer(sum(1 for key in args if store.get(key) is not None and store.delete(key)))


def cmd_exists(store, args):
    if not args:
        return wrong_args("exists")
    return resp.integer(sum(1 for key in args if store.exists(key)))


# ---- expiry ----

def cmd_expire(store, args):
    """EXPIRE key seconds"""
    if len(args) != 2:
        return wrong_args("expire")
    seconds = parse_int(args[1])
    if seconds is None:
        return NOT_INT
    if not store.exists(args[0]):
        return resp.integer(0)
    if seconds <= 0:
        store.delete(args[0])  # a deadline in the past deletes the key
        return resp.integer(1)
    store.set_expiry(args[0], store.now_ms() + seconds * 1000)
    return resp.integer(1)


def cmd_ttl(store, args):
    """TTL key -> seconds left, -1 = no expiry, -2 = no such key"""
    if len(args) != 1:
        return wrong_args("ttl")
    ms = store.ttl_ms(args[0])
    if ms < 0:
        return resp.integer(ms)
    return resp.integer((ms + 500) // 1000)  # round to nearest second like Redis


# ---- counters ----

def _add_to_counter(store, key, delta):
    current = store.get(key)
    if current is None:
        number = 0
    elif not isinstance(current, bytes):
        return WRONGTYPE
    else:
        number = parse_int(current)
        if number is None:
            return NOT_INT
    number += delta
    if not INT64_MIN <= number <= INT64_MAX:
        return resp.error("ERR increment or decrement would overflow")
    store.put(key, str(number).encode())  # put() keeps any existing TTL
    return resp.integer(number)


def cmd_incr(store, args):
    if len(args) != 1:
        return wrong_args("incr")
    return _add_to_counter(store, args[0], 1)


def cmd_decr(store, args):
    if len(args) != 1:
        return wrong_args("decr")
    return _add_to_counter(store, args[0], -1)


# ---- lists (stored as a deque so pushing on either end is fast) ----

def _get_typed(store, key, kind):
    """Fetch a value that must be of type `kind`. Returns (value, error);
    value is None if the key doesn't exist."""
    value = store.get(key)
    if value is not None and not isinstance(value, kind):
        return None, WRONGTYPE
    return value, None


def _push(store, args, name, left):
    if len(args) < 2:
        return wrong_args(name)
    items, err = _get_typed(store, args[0], deque)
    if err:
        return err
    if items is None:
        items = deque()
        store.set(args[0], items)
    for value in args[1:]:
        if left:
            items.appendleft(value)  # LPUSH a b c  ->  c b a
        else:
            items.append(value)
    return resp.integer(len(items))


def cmd_lpush(store, args):
    return _push(store, args, "lpush", left=True)


def cmd_rpush(store, args):
    return _push(store, args, "rpush", left=False)


def cmd_lrange(store, args):
    """LRANGE key start stop  (both ends included, negative = from the end)"""
    if len(args) != 3:
        return wrong_args("lrange")
    start, stop = parse_int(args[1]), parse_int(args[2])
    if start is None or stop is None:
        return NOT_INT
    items, err = _get_typed(store, args[0], deque)
    if err:
        return err
    if items is None:
        return resp.array([])
    n = len(items)
    if start < 0:
        start = max(n + start, 0)
    if stop < 0:
        stop += n
    stop = min(stop, n - 1)
    if start > stop:
        return resp.array([])
    return resp.array([resp.bulk_string(v) for v in islice(items, start, stop + 1)])


# ---- hashes (a dict inside a key) ----

def cmd_hset(store, args):
    """HSET key field value [field value ...] -> number of NEW fields"""
    if len(args) < 3 or len(args) % 2 == 0:
        return wrong_args("hset")
    fields, err = _get_typed(store, args[0], dict)
    if err:
        return err
    if fields is None:
        fields = {}
        store.set(args[0], fields)
    added = 0
    for i in range(1, len(args), 2):
        if args[i] not in fields:
            added += 1
        fields[args[i]] = args[i + 1]
    return resp.integer(added)


def cmd_hget(store, args):
    if len(args) != 2:
        return wrong_args("hget")
    fields, err = _get_typed(store, args[0], dict)
    if err:
        return err
    if fields is None:
        return resp.bulk_string(None)
    return resp.bulk_string(fields.get(args[1]))


def cmd_command(store, args):
    # redis-cli asks "COMMAND DOCS" when it connects. An empty list keeps it happy.
    return resp.array([])


COMMANDS = {
    b"PING": cmd_ping,
    b"SET": cmd_set,
    b"GET": cmd_get,
    b"DEL": cmd_del,
    b"EXISTS": cmd_exists,
    b"EXPIRE": cmd_expire,
    b"TTL": cmd_ttl,
    b"INCR": cmd_incr,
    b"DECR": cmd_decr,
    b"LPUSH": cmd_lpush,
    b"RPUSH": cmd_rpush,
    b"LRANGE": cmd_lrange,
    b"HSET": cmd_hset,
    b"HGET": cmd_hget,
    b"COMMAND": cmd_command,
}


def execute(store, args):
    handler = COMMANDS.get(args[0].upper())
    if handler is None:
        return resp.error("ERR unknown command '%s'" % args[0].decode(errors="replace"))
    return handler(store, args[1:])
