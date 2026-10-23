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
python -m unittest -v
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

## Not built yet

- Saving to disk (a restart wipes all data)
- Many commands real Redis has (this is deliberately about 15)
- `SET` options other than `EX` and `PX`
- The sweep builds a list of all keys with deadlines each time, which is fine here
  but would be too slow for millions of keys
- Comparison tests and benchmarks against real Redis

## Roadmap

1. Basics (done)
2. Counters, expiry, lists, hashes (done)
3. Many clients at once (already on `asyncio`; verify under load)
4. Persistence with an append-only log
5. Compare with real Redis in Docker, benchmark, write up results
