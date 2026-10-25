# Benchmarks: Mini Redis vs real Redis

All numbers below were measured with the real `redis-benchmark` tool by `bench.py`
(raw data in `benchmark.json`). They are **requests per second** (higher is better).

![Mini Redis speed as a percentage of real Redis](benchmark.png)

## How it was measured

| | |
|---|---|
| Machine | Intel(R) Xeon(R) Processor @ 2.10GHz, 2 cores (a shared cloud VM, so numbers are noisy) |
| Real Redis | v=7.0.15, default settings except no snapshots (`--save ""`) |
| Mini Redis | Python 3.13.17, standard `asyncio` event loop, no extra libraries |
| Tool | redis-benchmark 7.0.15, 50 clients, random keys (`-r 100000`) |
| Requests per test | 50,000 (one at a time) or 200,000 (16 at a time) |
| Runs | 3 per setup; the **median** is reported |
| Fairness | each server is started fresh for every run, pinned to CPU core 0; the benchmark tool is pinned to core 1; the two servers alternate who goes first; a short warm-up is thrown away |
| Two setups | no persistence, and an append-only log with fsync once a second (the same on both servers) |
| Two workloads | **P=1**: every client waits for each reply before sending the next command. **P=16**: every client sends 16 commands at a time (pipelining) |

Reproduce it with `python bench.py` (takes about 5 minutes; needs `redis-server` and
`redis-benchmark`). `python bench.py --redraw docs/benchmark.json` redraws the chart
and tables from the saved data.

## Results

**No persistence, one command at a time per client (P=1)**

| Command | Real Redis | Mini Redis | Mini as % of real | p50 latency real / mini | Run-to-run spread real / mini |
|---|---:|---:|---:|---:|---:|
| PING | 96,525 | 48,356 | 50% | 0.27 / 0.92 ms | 22% / 31% |
| SET | 112,867 | 31,309 | 28% | 0.24 / 1.64 ms | 10% / 8% |
| GET | 102,459 | 32,425 | 32% | 0.25 / 1.56 ms | 17% / 7% |
| INCR | 108,460 | 29,869 | 28% | 0.29 / 1.66 ms | 5% / 8% |
| LPUSH | 93,985 | 33,003 | 35% | 0.27 / 1.53 ms | 20% / 14% |
| RPUSH | 98,232 | 28,986 | 30% | 0.29 / 1.72 ms | 14% / 10% |
| LRANGE 0 99 | 71,327 | 14,646 | 21% | 0.36 / 3.20 ms | 11% / 14% |
| HSET | 102,459 | 27,518 | 27% | 0.27 / 1.78 ms | 7% / 19% |

**No persistence, 16 commands sent together (P=16)**

| Command | Real Redis | Mini Redis | Mini as % of real | p50 latency real / mini | Run-to-run spread real / mini |
|---|---:|---:|---:|---:|---:|
| PING | 1,503,759 | 249,688 | 17% | 0.37 / 2.96 ms | 26% / 17% |
| SET | 754,717 | 151,976 | 20% | 0.93 / 4.86 ms | 31% / 6% |
| GET | 854,701 | 160,256 | 19% | 0.80 / 4.48 ms | 31% / 29% |
| INCR | 749,064 | 151,515 | 20% | 0.85 / 4.88 ms | 39% / 13% |
| LPUSH | 1,030,928 | 161,420 | 16% | 0.49 / 4.75 ms | 26% / 7% |
| RPUSH | 1,204,819 | 166,667 | 14% | 0.49 / 4.69 ms | 31% / 9% |
| LRANGE 0 99 | 167,645 | 31,721 | 19% | 3.22 / 23.79 ms | 17% / 2% |
| HSET | 823,045 | 126,502 | 15% | 0.83 / 5.78 ms | 24% / 14% |

**Append-only log, fsync every second, one command at a time per client (P=1)**

| Command | Real Redis | Mini Redis | Mini as % of real | p50 latency real / mini | Run-to-run spread real / mini |
|---|---:|---:|---:|---:|---:|
| PING | 93,809 | 35,817 | 38% | 0.27 / 1.44 ms | 9% / 24% |
| SET | 91,575 | 25,100 | 27% | 0.29 / 1.96 ms | 3% / 15% |
| GET | 93,809 | 31,172 | 33% | 0.26 / 1.58 ms | 7% / 11% |
| INCR | 104,384 | 25,867 | 25% | 0.24 / 1.90 ms | 22% / 7% |
| LPUSH | 106,838 | 25,800 | 24% | 0.25 / 1.96 ms | 4% / 16% |
| RPUSH | 102,669 | 23,563 | 23% | 0.25 / 1.98 ms | 13% / 6% |
| LRANGE 0 99 | 67,385 | 14,594 | 22% | 0.38 / 3.20 ms | 16% / 9% |
| HSET | 102,881 | 23,969 | 23% | 0.24 / 2.06 ms | 19% / 9% |

**Append-only log, fsync every second, 16 commands sent together (P=16)**

| Command | Real Redis | Mini Redis | Mini as % of real | p50 latency real / mini | Run-to-run spread real / mini |
|---|---:|---:|---:|---:|---:|
| PING | 1,724,138 | 244,798 | 14% | 0.29 / 3.06 ms | 3% / 4% |
| SET | 593,472 | 133,156 | 22% | 1.13 / 5.60 ms | 5% / 6% |
| GET | 947,867 | 163,532 | 17% | 0.71 / 4.55 ms | 11% / 3% |
| INCR | 611,621 | 127,877 | 21% | 1.11 / 5.76 ms | 14% / 5% |
| LPUSH | 800,000 | 136,705 | 17% | 0.81 / 5.63 ms | 18% / 12% |
| RPUSH | 819,672 | 134,862 | 16% | 0.77 / 5.46 ms | 24% / 15% |
| LRANGE 0 99 | 175,747 | 31,158 | 18% | 3.38 / 23.58 ms | 21% / 7% |
| HSET | 604,230 | 108,050 | 18% | 1.14 / 6.75 ms | 18% / 23% |

## Reading these numbers honestly

- **This machine is noisy.** The last column shows how far apart the three runs of
  the same test were: often 10-30%, up to 39%. Treat differences of 10-15
  percentage points between two rows as noise. The gap between Mini Redis and real
  Redis (several times) is far larger than that.
- **Real Redis may be faster than shown, especially at P=16.** Most commands reach
  0.6-1.7 million requests per second there, and `redis-benchmark` is a single-threaded
  program on one core, so it may be the limit. If so, real Redis's true number is
  higher and Mini Redis's percentage is lower than the chart says.
- **Only the ratio carries over to another machine, roughly.** Absolute numbers
  depend on the CPU.
- **Everything is on localhost.** There is no real network, which makes the per-request
  system-call cost look bigger than it would over a network.
- **The persistence setup looks almost free** for both servers because a write to
  the log only goes to the operating system's memory; the once-a-second `fsync` runs
  in a background thread in Mini Redis and a background thread in Redis.
  `appendfsync always` would be much slower for both, and was not measured.

## Where the time goes (profiling)

Before trusting any explanation, I ran Mini Redis under Python's `cProfile` while
`redis-benchmark` hammered it. (Profiling slows Python code down, so read the
shares as rough.)

**16 commands at a time, before the fix** (about 24,000 requests per second under
the profiler): the single biggest cost was `socket.send`, called **500,003 times for
500,000 commands**: one system call per reply, even though 16 commands had arrived
together.

| Where | Share of time |
|---|---:|
| Sending replies (`socket.send` system calls) | about 30% |
| Parsing requests (`resp.parse_command`, pure Python) | about 26% |
| Running the commands (the actual database work) | about 20% |
| Everything else (asyncio, waiting in `epoll`) | the rest |

So only about a fifth of the time was spent on the database itself.

**One command at a time:** one `send` and one `recv` per request are unavoidable
(real Redis does the same), but here each also passes through several layers of
Python: asyncio's stream classes, the event loop, the coroutine, the parser. The
profile is dominated by that per-request overhead, not by the database work.

### The fix: one write per batch, not one per command

If several commands arrive together, the server now runs all of them, writes their
changes to the log **once**, and sends all the replies in **one** `send`. Replies
still only go out after the log write, so durability is unchanged. A test checks that
replies to the good commands still arrive in order when bad data follows them.

**Mini Redis requests per second, 16 commands at a time, no persistence**

| Command | Before | After | Change | Run-to-run spread before / after |
|---|---:|---:|---:|---:|
| PING | 53,908 | 249,688 | +363% | 12% / 17% |
| SET | 41,832 | 151,976 | +263% | 11% / 6% |
| GET | 43,592 | 160,256 | +268% | 13% / 29% |
| INCR | 40,040 | 151,515 | +278% | 6% / 13% |
| LPUSH | 40,258 | 161,420 | +301% | 13% / 7% |
| RPUSH | 41,339 | 166,667 | +303% | 8% / 9% |
| LRANGE 0 99 | 16,059 | 31,721 | +98% | 19% / 2% |
| HSET | 35,537 | 126,502 | +256% | 11% / 14% |

**Mini Redis requests per second, 16 commands at a time, with the append-only log**

| Command | Before | After | Change | Run-to-run spread before / after |
|---|---:|---:|---:|---:|
| PING | 57,110 | 244,798 | +329% | 14% / 4% |
| SET | 33,744 | 133,156 | +295% | 7% / 6% |
| GET | 44,043 | 163,532 | +271% | 3% / 3% |
| INCR | 31,601 | 127,877 | +305% | 9% / 5% |
| LPUSH | 33,893 | 136,705 | +303% | 15% / 12% |
| RPUSH | 31,691 | 134,862 | +326% | 8% / 15% |
| LRANGE 0 99 | 17,596 | 31,158 | +77% | 8% / 7% |
| HSET | 30,317 | 108,050 | +256% | 21% / 23% |

**Mini Redis requests per second, one command at a time, no persistence**

| Command | Before | After | Change | Run-to-run spread before / after |
|---|---:|---:|---:|---:|
| PING | 34,722 | 48,356 | +39% | 16% / 31% |
| SET | 28,818 | 31,309 | +9% | 14% / 8% |
| GET | 30,193 | 32,425 | +7% | 2% / 7% |
| INCR | 29,499 | 29,869 | +1% | 11% / 8% |
| LPUSH | 31,153 | 33,003 | +6% | 13% / 14% |
| RPUSH | 29,394 | 28,986 | -1% | 4% / 10% |
| LRANGE 0 99 | 14,841 | 14,646 | -1% | 9% / 14% |
| HSET | 27,503 | 27,518 | +0% | 13% / 19% |

As predicted by the profile, the fix made pipelined workloads **about 3-4 times
faster** (LRANGE, which is dominated by building a large reply, about twice) and did
**nothing measurable** for one-command-at-a-time traffic: every change in that last
table is inside the run-to-run spread (PING's +39% had a 16-31% spread, so I don't
claim it). Batching only helps when commands arrive together.

(`benchmark_before_batching.json` holds the raw data from before the fix.)

## Why real Redis is still 5-7 times faster when pipelined, 2-5 times when not

- It is written in C. Parsing a request and running `SET` is a short stretch of
  machine code; in Python the same work is many function calls and object
  allocations. The pipelined profile counted 18.4 million Python function calls for
  500,000 commands: about 37 per command.
- Its event loop talks to the operating system directly. Mini Redis goes through
  asyncio's general-purpose stream layer.
- It parses requests in place and builds replies into one buffer, while Mini Redis
  creates many small `bytes` objects per command.

## What I would try next (not done)

Each of these is a measured next step, in the order the profile suggests:

1. **Make parsing cheaper.** Parsing is pure Python and about a quarter of the time
   (roughly 37 function calls per command overall). Fewer slices and copies per
   command, or one pre-compiled regular expression for the common case, would cut it.
2. **Skip asyncio's stream layer.** An `asyncio.Protocol` receives data and writes
   replies with fewer Python calls per request. This is the most likely help for
   the one-at-a-time case.
3. **Try `uvloop`,** a faster drop-in event loop. It is an extra dependency, so it is
   a choice, not an obvious win.
4. **Build replies faster,** especially for long lists (LRANGE).
