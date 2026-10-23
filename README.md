# Mini Redis

A small Redis-compatible server written in Python, built to learn how Redis works.
Real tools such as `redis-cli` can talk to it because it speaks the RESP protocol.

## Run it

```
python server.py --port 6380
redis-cli -p 6380 PING        # -> PONG
```

## Run the tests

```
python -m unittest -v        # all tests, takes a few seconds
```

## Commands

| Command  | Notes |
|----------|-------|
| `PING [msg]` | |
| `SET key value [EX seconds \| PX ms]` | wipes any old expiry, like Redis |
| `GET key` | nil if missing |
| `DEL key [key ...]` | returns how many were removed |
| `EXISTS key [key ...]` | returns how many exist |
| `EXPIRE key seconds` | zero or negative deletes the key |
| `TTL key` | seconds left, `-1` no expiry, `-2` no such key |
| `INCR key` / `DECR key` | strict 64-bit integers, keeps the key's TTL |
| `LPUSH` / `RPUSH key value [value ...]` | returns the new length |
| `LRANGE key start stop` | negative indexes count from the end |
| `HSET key field value [field value ...]` | returns the number of new fields |
| `HGET key field` | |

Using a command on the wrong kind of value (for example `GET` on a list) returns
a `WRONGTYPE` error, as in real Redis.

## How it works

- `resp.py`: parses requests and encodes replies. Copes with commands that arrive
  in pieces, several commands arriving at once, and inline commands.
- `store.py`: the data and expiry logic.
- `commands.py`: one handler per command.
- `server.py`: the asyncio networking loop.

### Expiring keys

Deadlines are stored next to the keys, not as one timer per key. Two things remove
expired keys, the same way real Redis does it:

1. **Lazy:** every access checks the key's deadline first. An expired key is deleted
   and treated as if it never existed.
2. **Active:** once a second a background task samples keys that have deadlines and
   removes the expired ones. If more than a quarter of a sample was expired it
   samples again (up to 16 rounds), so one sweep can't hog the server.

Edge case decisions, each covered by a test:

- A key is expired when `now >= deadline`, and the check runs before the command acts.
  So a `SET` at the exact moment of expiry creates a fresh key with no TTL.
- `SET` clears the old TTL. `INCR` and `HSET` keep it.
- `DEL` and `EXISTS` never count an expired key.

Time-based tests use a fake clock, so they are exact and never sleep.

### Many clients at once

Every connected client gets its own coroutine on one `asyncio` event loop, the same
idea as Redis's single loop serving everyone in turn. This gives some useful
properties, each covered by a test in `test_concurrency.py`:

- **Commands never overlap.** Only one command runs at a time, so `INCR` from 50
  clients at once returns exactly 1..N with no lost or repeated numbers, and no
  locks are needed.
- **A slow client only slows itself.** A client that never reads its replies makes
  only its own coroutine wait (`drain()`); other clients are still answered.
- **Broken clients are survivable.** Clients that disconnect halfway through a
  command, send garbage, or promise a huge value and never finish are cleaned up
  without affecting anyone else. A client whose unfinished command grows past
  `--max-query-buffer` (default 1 GB) is dropped.
- Also tested: 150 idle connections, a 5 MB value, and 10,000 pipelined commands in
  one write.

The parser has its own tests (`test_resp.py`), including one that feeds every
possible partial prefix of a command and checks it reports "not complete yet".

I also pointed the real `redis-benchmark` tool at the server with 50 and 200
clients (including pipelining) as a sanity check. It completed without errors.
Proper speed measurements against real Redis come in Stage 5.

**Trade-off:** because one thread runs every command, one slow command delays all
clients. Real Redis has the same property, which is why its docs warn against
slow commands on big data.

## Not built yet

- Saving to disk (a restart wipes all data)
- Many commands real Redis has (this is deliberately about 15)
- `SET` options other than `EX` and `PX`
- A limit on the number of connections (`maxclients`)
- The sweep builds a list of all keys with deadlines each time, which is fine here
  but would be too slow for millions of keys
- Comparison tests and benchmarks against real Redis

## Roadmap

1. Basics (done)
2. Counters, expiry, lists, hashes (done)
3. Many clients at once (done)
4. Persistence with an append-only log
5. Compare with real Redis in Docker, benchmark, write up results
