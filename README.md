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

## What works (Stage 1)

| Command  | Notes                          |
|----------|--------------------------------|
| `PING`   | with or without a message      |
| `SET`    | `SET key value` only (no options yet) |
| `GET`    | returns nil for missing keys   |
| `DEL`    | multiple keys, returns count   |
| `EXISTS` | multiple keys, returns count   |

The parser copes with commands that arrive in pieces, several commands arriving
at once, and inline commands (e.g. from telnet). Each of these has a test.

## Not built yet

- Key expiry (`EXPIRE`, `TTL`, `SET ... EX`)
- Counters, lists, hashes
- Saving to disk
- Comparison tests and benchmarks against real Redis

## Roadmap

1. Basics (done)
2. More commands: counters, expiry, lists, hashes
3. Many clients at once (already using `asyncio`; verify under load)
4. Persistence with an append-only log
5. Compare with real Redis in Docker, benchmark, write up results
