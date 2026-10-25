"""Command handlers. Each takes (store, args) where args excludes the command
name, and returns an already-encoded RESP reply."""
import re
from collections import deque
from itertools import islice

import resp

# Redis's rule: "0", or an optional minus sign then a number with no leading zero.
# That makes "-0", "007", "+5" and " 5" all invalid.
INT_RE = re.compile(rb"0|-?[1-9][0-9]*")
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


def deadline_ms(store, amount, unit_ms):
    """now + amount * unit, or None if the result doesn't fit in 64 bits.
    Real Redis rejects those with 'invalid expire time'. The lower bound uses
    C-style rounding toward zero so the boundary matches exactly."""
    if amount > INT64_MAX // unit_ms or amount < -(2 ** 63 // unit_ms):
        return None
    deadline = store.now_ms() + amount * unit_ms
    return deadline if deadline <= INT64_MAX else None


def invalid_expire(command):
    return resp.error("ERR invalid expire time in '%s' command" % command)


# ---- connection / strings ----

def cmd_ping(store, args):
    if len(args) > 1:
        return wrong_args("ping")
    if args:
        return resp.bulk_string(args[0])
    return resp.simple_string("PONG")


def cmd_set(store, args):
    """SET key value [EX seconds | PX milliseconds | PXAT unix-milliseconds]"""
    if len(args) < 2:
        return wrong_args("set")
    key, value = args[0], args[1]

    deadline = None
    i = 2
    while i < len(args):
        option = args[i].upper()
        if option in (b"EX", b"PX", b"PXAT") and deadline is None and i + 1 < len(args):
            amount = parse_int(args[i + 1])
            if amount is None:
                return NOT_INT
            if amount <= 0:
                return invalid_expire("set")
            if option == b"PXAT":
                deadline = amount  # already an absolute time
            else:
                deadline = deadline_ms(store, amount, 1000 if option == b"EX" else 1)
                if deadline is None:
                    return invalid_expire("set")
            i += 2
        else:
            return SYNTAX

    store.set(key, value, deadline)
    # Logged in absolute form, as ONE command, so a crash can't separate the
    # value from its deadline.
    if deadline is None:
        store.emit(b"SET", key, value)
    else:
        store.emit(b"SET", key, value, b"PXAT", str(deadline).encode())
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
    removed = []
    for key in args:
        # get() first, so an expired key counts as already gone
        if store.get(key) is not None and store.delete(key):
            removed.append(key)
    if removed:
        store.emit(b"DEL", *removed)
    return resp.integer(len(removed))


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
    deadline = deadline_ms(store, seconds, 1000)
    if deadline is None:  # checked even if the key is missing, like real Redis
        return invalid_expire("expire")
    if not store.exists(args[0]):
        return resp.integer(0)
    if seconds <= 0:
        store.delete(args[0])  # a deadline in the past deletes the key
        store.emit(b"DEL", args[0])
        return resp.integer(1)
    store.set_expiry(args[0], deadline)
    store.emit(b"PEXPIREAT", args[0], str(deadline).encode())  # logged as an absolute time
    return resp.integer(1)


def cmd_pexpireat(store, args):
    """PEXPIREAT key unix-milliseconds  (an absolute deadline; this is the form
    EXPIRE is written to the log in)"""
    if len(args) != 2:
        return wrong_args("pexpireat")
    when = parse_int(args[1])
    if when is None:
        return NOT_INT
    key = args[0]
    if not store.exists(key):
        return resp.integer(0)
    if when <= store.now_ms() and not store.loading:
        store.delete(key)  # a deadline that has already passed deletes the key
        store.emit(b"DEL", key)
        return resp.integer(1)
    store.set_expiry(key, when)
    store.emit(b"PEXPIREAT", key, str(when).encode())
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
    store.emit(b"INCR" if delta > 0 else b"DECR", key)
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
    store.emit(b"LPUSH" if left else b"RPUSH", *args)
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
    store.emit(b"HSET", *args)
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
    b"PEXPIREAT": cmd_pexpireat,
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
        return unknown_command(args)
    return handler(store, args[1:])


def unknown_command(args):
    """Same wording as real Redis 7: the name, then the start of the arguments
    (cut off at about 128 characters so a huge argument can't fill the reply)."""
    shown = b""
    for arg in args[1:]:
        if len(shown) >= 128:
            break
        shown += b"'" + arg[:128 - len(shown)] + b"' "
    return resp.error(b"ERR unknown command '" + args[0][:128] + b"', with args beginning with: " + shown)
