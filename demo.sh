#!/usr/bin/env bash
# A short scripted demo of Mini Redis, meant to be recorded as the GIF for the README.
#
#   ./demo.sh               # run it at watching speed
#   DEMO_FAST=1 ./demo.sh   # no pauses (to check that it works)
#
# To record it, use any screen recorder, for example:
#   asciinema rec demo.cast -c ./demo.sh     (then convert with agg: agg demo.cast demo.gif)
# or ScreenToGif (Windows) / Peek (Linux) / the macOS screen recorder.
# Needs redis-cli (it comes with Redis).
set -u
cd "$(dirname "$0")"

PORT=${PORT:-6399}
DIR=$(mktemp -d)
AOF="$DIR/demo.aof"
SERVER_PID=""

pause() { [ -n "${DEMO_FAST:-}" ] || sleep "$1"; }
say()   { printf '\n\033[1;36m# %s\033[0m\n' "$*"; pause 1.2; }
cli()   { printf '\033[1;32m$\033[0m redis-cli -p %s %s\n' "$PORT" "$*"; pause 0.7; redis-cli -p "$PORT" "$@"; pause 0.9; }
start() { python3 server.py --port "$PORT" --aof-file "$AOF" >/dev/null 2>&1 & SERVER_PID=$!; sleep 0.8; }
cleanup() { [ -n "$SERVER_PID" ] && kill "$SERVER_PID" 2>/dev/null; rm -rf "$DIR"; }
trap cleanup EXIT

command -v redis-cli >/dev/null || { echo "redis-cli is not installed"; exit 1; }

say "Mini Redis: a Redis-compatible server in Python. The real redis-cli talks to it."
printf '\033[1;32m$\033[0m python server.py --port %s --aof-file demo.aof\n' "$PORT"
start; pause 1

say "Strings"
cli PING
cli SET name Vi
cli GET name

say "Keys that expire"
cli SET session abc123 EX 30
cli TTL session

say "Counters"
cli INCR visits
cli INCR visits

say "Lists and hashes"
cli RPUSH queue first second third
cli LRANGE queue 0 -1
cli HSET user:1 name Vi city Nagpur
cli HGET user:1 city

say "Using the wrong type gives the same error real Redis gives"
cli GET queue

say "Now kill the server with kill -9, no clean shutdown..."
printf '\033[1;32m$\033[0m kill -9 <server>\n'; pause 0.7
kill -9 "$SERVER_PID"; wait "$SERVER_PID" 2>/dev/null; SERVER_PID=""; pause 1

say "...and start it again. The data was saved in the append-only log."
printf '\033[1;32m$\033[0m python server.py --port %s --aof-file demo.aof\n' "$PORT"
start; pause 1
cli GET name
cli LRANGE queue 0 -1
cli INCR visits
cli TTL session

say "The log is just the commands, in the same format clients send:"
printf '\033[1;32m$\033[0m head -c 200 demo.aof | tr "\\r" " " | head -12\n'; pause 0.7
head -c 200 "$AOF" | tr '\r' ' ' | head -12
pause 1.5
