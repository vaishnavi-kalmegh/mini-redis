"""Tests for the append-only log (aof.py): saving data to disk and reading it back.

Run:  python -m unittest -v test_aof
"""
import asyncio
import copy
import os
import random
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest

import commands
import resp
from aof import AOFError, AppendOnlyLog, encode_command, load
from store import Store
from testutil import (FakeClock, call, close, connect, free_port, start_server,
                      stop_server)


def snapshot(store):
    """A frozen copy of everything the database holds."""
    return copy.deepcopy((store.data, store.expires))


def read_bytes(path):
    with open(path, "rb") as f:
        return f.read()


def read_log(path):
    """The log file as a list of commands, each a list of byte strings."""
    buf = bytearray(read_bytes(path))
    out = []
    while buf:
        args, consumed = resp.parse_command(buf)
        del buf[:consumed]
        out.append(args)
    return out


class LoggedSession:
    """Does what server.py does: run a command, then append whatever changes
    it made to the log. Making a new LoggedSession on the same file is a restart."""

    def __init__(self, path, clock, policy="no"):
        self.store = Store(clock=clock)
        load(path, self.store)
        self.store.record_writes = True
        self.log = AppendOnlyLog(path, policy)

    def _flush(self):
        pending = self.store.take_pending()
        if pending:
            self.log.append(pending)

    def run(self, *args):
        args = [a.encode() if isinstance(a, str) else a for a in args]
        reply = commands.execute(self.store, args)
        self._flush()
        return reply

    def sweep(self):
        removed = self.store.sweep()
        self._flush()
        return removed

    def close(self):
        self.log.close()


class AOFTestBase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.clock = FakeClock()

    def path(self, name="test.aof"):
        return os.path.join(self.dir, name)

    def session(self, name="test.aof"):
        return LoggedSession(self.path(name), self.clock)


class LogFormatTest(AOFTestBase):
    def test_encode_command(self):
        self.assertEqual(encode_command([b"SET", b"k", b"v"]), b"*3\r\n$3\r\nSET\r\n$1\r\nk\r\n$1\r\nv\r\n")
        self.assertEqual(encode_command([b"X", b"a\r\nb\x00"]), b"*2\r\n$1\r\nX\r\n$5\r\na\r\nb\x00\r\n")

    def test_missing_file_is_an_empty_database(self):
        store = Store()
        self.assertEqual(load(self.path("nope.aof"), store), (0, 0))
        self.assertEqual(store.data, {})
        self.assertFalse(os.path.exists(self.path("nope.aof")))  # loading never creates the file

    def test_empty_file(self):
        open(self.path(), "wb").close()
        self.assertEqual(load(self.path(), Store()), (0, 0))

    def test_reads_and_failed_writes_are_not_logged(self):
        s = self.session()
        s.run("SET", "str", "abc")
        s.run("RPUSH", "list", "a")
        size = os.path.getsize(self.path())
        for command in [("GET", "str"), ("GET", "nope"), ("TTL", "str"), ("EXISTS", "str"), ("LRANGE", "list", "0", "-1"),
                        ("HGET", "h", "f"), ("PING",), ("FLY",), ("COMMAND",),
                        ("INCR", "str"), ("LPUSH", "str", "x"), ("HSET", "list", "f", "v"),   # wrong type / not a number
                        ("EXPIRE", "nope", "10"), ("PEXPIREAT", "nope", "99999999999999"),    # no such key
                        ("DEL", "nope"), ("SET", "k", "v", "EX", "0"), ("SET", "k", "v", "FOO"),
                        ("EXPIRE", "str", "abc"), ("SET",), ("RPUSH", "list")]:
            s.run(*command)
        self.assertEqual(os.path.getsize(self.path()), size)
        s.close()

    def test_times_are_logged_as_absolute_times(self):
        s = self.session()
        now_ms = s.store.now_ms()
        s.run("SET", "a", "v", "EX", "10")
        s.run("SET", "b", "v", "PX", "2500")
        s.run("SET", "c", "v")
        s.run("EXPIRE", "c", "60")
        s.close()
        self.assertEqual(read_log(self.path()), [
            [b"SET", b"a", b"v", b"PXAT", str(now_ms + 10_000).encode()],
            [b"SET", b"b", b"v", b"PXAT", str(now_ms + 2_500).encode()],
            [b"SET", b"c", b"v"],
            [b"PEXPIREAT", b"c", str(now_ms + 60_000).encode()],
        ])

    def test_del_logs_only_keys_that_were_really_removed(self):
        s = self.session()
        s.run("SET", "a", "1")
        s.run("SET", "b", "1")
        s.run("DEL", "a", "nope", "b")
        s.close()
        self.assertEqual(read_log(self.path())[-1], [b"DEL", b"a", b"b"])

    def test_replaying_does_not_write_to_the_log_again(self):
        s = self.session()
        s.run("SET", "a", "1")
        s.run("RPUSH", "l", "x")
        s.close()
        store = Store(clock=self.clock)
        store.record_writes = True  # a log IS attached, but we are only replaying
        load(self.path(), store)
        self.assertEqual(store.pending, [])


class RestartTest(AOFTestBase):
    def test_every_type_survives_a_restart(self):
        s = self.session()
        s.run("SET", "name", "Vi")
        s.run("SET", "bin", b"a\r\nb\x00c")
        s.run("INCR", "visits"); s.run("INCR", "visits"); s.run("DECR", "down")
        s.run("RPUSH", "l", "b", "c"); s.run("LPUSH", "l", "a")
        s.run("HSET", "h", "f", "v", "g", "w")
        s.run("DEL", "gone"); s.run("SET", "gone", "x"); s.run("DEL", "gone")
        s.close()

        s = self.session()
        self.assertEqual(s.run("GET", "name"), b"$2\r\nVi\r\n")
        self.assertEqual(s.run("GET", "bin"), b"$6\r\na\r\nb\x00c\r\n")
        self.assertEqual(s.run("GET", "visits"), b"$1\r\n2\r\n")
        self.assertEqual(s.run("GET", "down"), b"$2\r\n-1\r\n")
        self.assertEqual(s.run("LRANGE", "l", "0", "-1"), b"*3\r\n$1\r\na\r\n$1\r\nb\r\n$1\r\nc\r\n")
        self.assertEqual(s.run("HGET", "h", "g"), b"$1\r\nw\r\n")
        self.assertEqual(s.run("EXISTS", "gone"), b":0\r\n")
        s.close()

    def test_new_writes_after_a_restart_are_appended_not_overwritten(self):
        s = self.session(); s.run("SET", "a", "1"); s.close()
        s = self.session(); s.run("INCR", "a"); s.close()
        s = self.session(); self.assertEqual(s.run("GET", "a"), b"$1\r\n2\r\n"); s.close()

    def test_expiry_survives_a_restart_with_the_right_time_left(self):
        s = self.session(); s.run("SET", "k", "v", "EX", "10"); s.close()
        self.clock.advance(4)
        s = self.session()
        self.assertEqual(s.run("TTL", "k"), b":6\r\n")  # 10 seconds minus the 4 that passed
        s.close()
        self.clock.advance(7)  # 11 seconds after the SET
        s = self.session()
        self.assertEqual(s.run("GET", "k"), b"$-1\r\n")
        s.close()
        s = self.session()  # the deletion was logged too
        self.assertEqual(s.run("EXISTS", "k"), b":0\r\n")
        s.close()

    def test_key_that_expired_while_the_server_was_off_is_gone(self):
        s = self.session(); s.run("SET", "k", "v", "EX", "5"); s.close()
        self.clock.advance(100)
        s = self.session()
        self.assertEqual(s.run("EXISTS", "k"), b":0\r\n")
        s.close()

    def test_replay_does_not_expire_keys_early(self):
        # The log says: SET with a 5s deadline, then EXPIRE to 1000s. If replay
        # applied the first deadline to the clock of a later restart, the key
        # would be dropped before the EXPIRE could rescue it.
        s = self.session()
        s.run("SET", "k", "v", "EX", "5")
        s.run("EXPIRE", "k", "1000")
        s.close()
        self.clock.advance(100)
        s = self.session()
        self.assertEqual(s.run("GET", "k"), b"$1\r\nv\r\n")
        self.assertEqual(s.run("TTL", "k"), b":900\r\n")
        s.close()

    def test_key_that_expired_and_was_recreated_replays_correctly(self):
        # "k" is a string that expires, then becomes a LIST. Replay must not find
        # the old string and answer WRONGTYPE: that is why expiry is logged as DEL.
        s = self.session()
        s.run("SET", "k", "old", "EX", "1")
        self.clock.advance(2)
        s.run("RPUSH", "k", "a")
        s.close()
        self.assertEqual(read_log(self.path())[1], [b"DEL", b"k"])
        s = self.session()
        self.assertEqual(s.run("LRANGE", "k", "0", "-1"), b"*1\r\n$1\r\na\r\n")
        s.close()

    def test_incr_on_an_expired_counter_starts_over_after_restart(self):
        s = self.session()
        s.run("SET", "n", "50", "EX", "1")
        self.clock.advance(2)
        self.assertEqual(s.run("INCR", "n"), b":1\r\n")
        s.close()
        s = self.session()
        self.assertEqual(s.run("GET", "n"), b"$1\r\n1\r\n")
        self.assertEqual(s.run("TTL", "n"), b":-1\r\n")
        s.close()

    def test_sweeper_deletions_are_logged(self):
        s = self.session()
        s.run("SET", "a", "v", "EX", "1")
        s.run("SET", "b", "v")
        self.clock.advance(2)
        self.assertEqual(s.sweep(), 1)
        self.assertEqual(read_log(self.path())[-1], [b"DEL", b"a"])
        s.close()
        s = self.session()
        self.assertEqual(s.run("EXISTS", "a", "b"), b":1\r\n")
        s.close()

    def test_set_clears_the_old_deadline_after_restart(self):
        s = self.session()
        s.run("SET", "k", "v", "EX", "10")
        s.run("SET", "k", "v2")
        s.close()
        self.clock.advance(100)
        s = self.session()
        self.assertEqual(s.run("GET", "k"), b"$2\r\nv2\r\n")
        self.assertEqual(s.run("TTL", "k"), b":-1\r\n")
        s.close()

    def test_incr_and_hset_keep_the_deadline_after_restart(self):
        s = self.session()
        s.run("SET", "n", "1", "EX", "100"); s.run("INCR", "n")
        s.run("HSET", "h", "a", "1"); s.run("EXPIRE", "h", "100"); s.run("HSET", "h", "b", "2")
        s.close()
        self.clock.advance(10)
        s = self.session()
        self.assertEqual(s.run("TTL", "n"), b":90\r\n")
        self.assertEqual(s.run("TTL", "h"), b":90\r\n")
        self.assertEqual(s.run("GET", "n"), b"$1\r\n2\r\n")
        s.close()


class DamagedLogTest(AOFTestBase):
    SCRIPT = [("SET", "a", "1"), ("SET", "bin", b"x\r\ny\x00"), ("RPUSH", "l", "one", "two", b"t\r\nhree"),
              ("HSET", "h", "f", "v", "g", "w"), ("INCR", "n"), ("SET", "t", "v", "EX", "100"),
              ("EXPIRE", "a", "500"), ("DEL", "bin"), ("LPUSH", "l", "zero")]

    def build_log(self):
        s = self.session()
        snapshots = [({}, {})]
        for command in self.SCRIPT:
            self.assertFalse(s.run(*command).startswith(b"-"), command)
            snapshots.append(snapshot(s.store))
        s.close()
        data = read_bytes(self.path())
        ends, pos, buf = [], 0, bytearray(data)
        while buf:
            _, consumed = resp.parse_command(buf)
            del buf[:consumed]
            pos += consumed
            ends.append(pos)
        self.assertEqual(len(ends), len(self.SCRIPT))
        return data, ends, snapshots

    def test_cutting_the_file_at_any_byte_loses_only_the_last_command(self):
        # The power cut can land anywhere in a write, including inside a value
        # that itself contains \r\n. Try every possible cut point.
        data, ends, snapshots = self.build_log()
        cut_path = self.path("cut.aof")
        for cut in range(len(data) + 1):
            with self.subTest(cut=cut):
                complete = sum(1 for e in ends if e <= cut)
                good_end = ends[complete - 1] if complete else 0
                with open(cut_path, "wb") as f:
                    f.write(data[:cut])
                store = Store(clock=self.clock)
                replayed, ignored = load(cut_path, store)
                self.assertEqual(replayed, complete)
                self.assertEqual(ignored, cut - good_end)
                self.assertEqual(os.path.getsize(cut_path), good_end)  # broken tail removed
                self.assertEqual(snapshot(store), snapshots[complete])

    def test_appending_after_a_torn_tail_does_not_glue_onto_the_broken_half(self):
        s = self.session(); s.run("SET", "a", "1"); s.close()
        with open(self.path(), "ab") as f:
            f.write(b"*3\r\n$3\r\nSET\r\n$1\r\nb\r\n$5\r\nhel")  # power cut mid-write
        s = self.session()  # loads, drops the tail
        s.run("SET", "c", "3")
        s.close()
        s = self.session()  # would fail or misread if the tail had been left in place
        self.assertEqual(s.run("GET", "a"), b"$1\r\n1\r\n")
        self.assertEqual(s.run("GET", "b"), b"$-1\r\n")
        self.assertEqual(s.run("GET", "c"), b"$1\r\n3\r\n")
        s.close()

    def assertRefusesAndLeavesFileAlone(self, content, expect_in_message=""):
        with open(self.path(), "wb") as f:
            f.write(content)
        with self.assertRaises(AOFError) as caught:
            load(self.path(), Store())
        self.assertIn(expect_in_message, str(caught.exception))
        self.assertEqual(read_bytes(self.path()), content)  # nothing was changed

    GOOD = encode_command([b"SET", b"a", b"1"])

    def test_garbage_in_the_middle_refuses_to_load(self):
        self.assertRefusesAndLeavesFileAlone(self.GOOD + b"garbage\r\n" + self.GOOD, "expected '*'")

    def test_bad_lengths_refuse_to_load(self):
        self.assertRefusesAndLeavesFileAlone(self.GOOD + b"*1\r\n$abc\r\n" + self.GOOD)
        self.assertRefusesAndLeavesFileAlone(self.GOOD + b"*1\r\n$3\r\nabcXY" + self.GOOD)  # bad terminator

    def test_zeroed_tail_refuses_to_load(self):
        # Some filesystems leave zeros after a power cut. That is not a clean
        # prefix of a command, so we refuse instead of guessing.
        self.assertRefusesAndLeavesFileAlone(self.GOOD + b"\x00" * 16)

    def test_a_command_that_fails_when_replayed_refuses_to_load(self):
        bad = encode_command([b"SET", b"k", b"abc"]) + encode_command([b"INCR", b"k"])
        self.assertRefusesAndLeavesFileAlone(bad, "INCR")
        self.assertRefusesAndLeavesFileAlone(encode_command([b"NOSUCHCOMMAND", b"x"]), "NOSUCHCOMMAND")


class RandomSequenceTest(AOFTestBase):
    """Run random commands, restarting now and then, and check that what comes
    back from the log is exactly what was in memory."""

    KEYS = [b"k%d" % i for i in range(6)]  # few keys, so types and expiry collide a lot
    VALUES = [b"1", b"42", b"-7", b"hello", b"a\r\nb", b""]

    def one_operation(self, rng, s):
        key = rng.choice(self.KEYS)
        value = rng.choice(self.VALUES)
        op = rng.choice(["SET", "SETEX", "SETPX", "GET", "DEL", "EXPIRE", "PEXPIREAT", "TTL", "INCR", "DECR",
                         "RPUSH", "LPUSH", "LRANGE", "HSET", "HGET", "EXISTS", "SWEEP", "TICK", "TICK"])
        if op == "SET": s.run("SET", key, value)
        elif op == "SETEX": s.run("SET", key, value, "EX", str(rng.randint(1, 4)))
        elif op == "SETPX": s.run("SET", key, value, "PX", str(rng.randint(100, 3000)))
        elif op == "GET": s.run("GET", key)
        elif op == "DEL": s.run("DEL", *rng.sample(self.KEYS, rng.randint(1, 3)))
        elif op == "EXPIRE": s.run("EXPIRE", key, str(rng.randint(-1, 4)))
        elif op == "PEXPIREAT": s.run("PEXPIREAT", key, str(s.store.now_ms() + rng.randint(-1000, 4000)))
        elif op == "TTL": s.run("TTL", key)
        elif op == "INCR": s.run("INCR", key)
        elif op == "DECR": s.run("DECR", key)
        elif op == "RPUSH": s.run("RPUSH", key, *[rng.choice(self.VALUES) for _ in range(rng.randint(1, 3))])
        elif op == "LPUSH": s.run("LPUSH", key, *[rng.choice(self.VALUES) for _ in range(rng.randint(1, 3))])
        elif op == "LRANGE": s.run("LRANGE", key, "0", "-1")
        elif op == "HSET": s.run("HSET", key, rng.choice([b"f", b"g"]), value)
        elif op == "HGET": s.run("HGET", key, b"f")
        elif op == "EXISTS": s.run("EXISTS", key)
        elif op == "SWEEP": s.sweep()
        elif op == "TICK": self.clock.advance(rng.random() * 2)

    def test_replay_rebuilds_exactly_what_was_in_memory(self):
        restarts = 0
        for seed in range(40):
            with self.subTest(seed=seed):
                rng = random.Random(seed)
                self.clock = FakeClock()
                name = "seed%d.aof" % seed
                s = self.session(name)
                for _ in range(200):
                    if rng.random() < 0.04:  # restart
                        before = snapshot(s.store)
                        s.close()
                        s = self.session(name)
                        self.assertEqual(snapshot(s.store), before)
                        restarts += 1
                    self.one_operation(rng, s)
                before = snapshot(s.store)
                s.close()
                s = self.session(name)
                self.assertEqual(snapshot(s.store), before)
                s.close()
        self.assertGreater(restarts, 100)  # the test really did exercise restarts


class EndToEndTest(unittest.IsolatedAsyncioTestCase):
    """The real server as a separate process, stopped the hard way."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)

    def path(self, name="e2e.aof"):
        return os.path.join(self.dir, name)

    async def start(self, path, *extra, **kw):
        proc, port = await asyncio.to_thread(start_server, "--aof-file", path, *extra, **kw)
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        return proc, port

    async def ask(self, port, *commands_):
        reader, writer = await connect(port)
        try:
            return [await call(reader, writer, *c) for c in commands_]
        finally:
            await close(writer)

    async def test_kill_9_loses_nothing(self):
        # A crash of the program (not the machine) can't lose acknowledged writes,
        # whatever the fsync policy, because the bytes are already with the OS.
        for policy in ("always", "everysec", "no"):
            with self.subTest(policy=policy):
                path = self.path(policy + ".aof")
                proc, port = await self.start(path, "--appendfsync", policy)
                await self.ask(port, ("SET", "a", "1"), ("INCR", "n"), ("INCR", "n"), ("RPUSH", "l", "x", "y"),
                               ("HSET", "h", "f", "v"), ("SET", "t", "v", "EX", "100"))
                proc.kill(); proc.wait()

                proc, port = await self.start(path, "--appendfsync", policy)
                replies = await self.ask(port, ("GET", "a"), ("GET", "n"), ("LRANGE", "l", "0", "-1"),
                                         ("HGET", "h", "f"), ("TTL", "t"))
                stop_server(proc)
                self.assertEqual(replies[:4], [b"1", b"2", [b"x", b"y"], b"v"])
                self.assertIn(replies[4], (99, 100))

    async def test_many_clients_then_kill_9(self):
        path = self.path()
        proc, port = await self.start(path)

        async def worker():
            reader, writer = await connect(port)
            for _ in range(50):
                await call(reader, writer, "INCR", "counter")
            await close(writer)

        await asyncio.gather(*[worker() for _ in range(20)])
        proc.kill(); proc.wait()
        proc, port = await self.start(path)
        self.assertEqual(await self.ask(port, ("GET", "counter")), [b"1000"])
        stop_server(proc)

    async def test_torn_tail_is_reported_cut_off_and_later_writes_stay_clean(self):
        path = self.path()
        proc, port = await self.start(path)
        await self.ask(port, ("SET", "a", "1"))
        proc.kill(); proc.wait()
        good_size = os.path.getsize(path)
        with open(path, "ab") as f:
            f.write(b"*3\r\n$3\r\nSET\r\n$1\r\nb\r\n$5\r\nhel")  # cut off mid-value

        proc, port = await self.start(path, stderr=subprocess.PIPE)
        self.assertEqual(os.path.getsize(path), good_size)
        self.assertEqual(await self.ask(port, ("GET", "a"), ("GET", "b"), ("SET", "c", "3")), [b"1", None, b"OK"])
        proc.kill(); proc.wait()
        warning = proc.stderr.read().decode()
        proc.stderr.close()
        self.assertIn("WARNING", warning)
        self.assertIn("incomplete", warning)

        proc, port = await self.start(path)
        self.assertEqual(await self.ask(port, ("GET", "a"), ("GET", "b"), ("GET", "c")), [b"1", None, b"3"])
        stop_server(proc)

    async def test_damaged_log_stops_the_server_and_leaves_the_file_alone(self):
        path = self.path()
        content = encode_command([b"SET", b"a", b"1"]) + b"garbage\r\n" + encode_command([b"SET", b"b", b"2"])
        with open(path, "wb") as f:
            f.write(content)
        result = subprocess.run([sys.executable, "server.py", "--port", str(free_port()), "--aof-file", path],
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 1)
        self.assertIn("damaged", result.stderr)
        self.assertNotIn("Traceback", result.stderr)  # a readable message, not a crash dump
        self.assertEqual(read_bytes(path), content)

    @unittest.skipIf(os.name == "nt", "SIGTERM is a Unix thing")
    async def test_sigterm_stops_cleanly_and_keeps_the_data(self):
        path = self.path()
        proc, port = await self.start(path)
        await self.ask(port, ("SET", "a", "1"))
        proc.send_signal(signal.SIGTERM)
        self.assertEqual(await asyncio.to_thread(proc.wait, 10), 0)
        proc, port = await self.start(path)
        self.assertEqual(await self.ask(port, ("GET", "a")), [b"1"])
        stop_server(proc)

    async def test_background_sweeper_logs_expired_keys(self):
        path = self.path()
        proc, port = await self.start(path)
        await self.ask(port, ("SET", "k", "v", "PX", "100"))
        deadline = time.time() + 5
        while [b"DEL", b"k"] not in read_log(path):
            self.assertLess(time.time(), deadline, "the sweeper never logged the expired key")
            await asyncio.sleep(0.2)
        proc.kill(); proc.wait()
        proc, port = await self.start(path)
        self.assertEqual(await self.ask(port, ("EXISTS", "k")), [0])
        stop_server(proc)

    @unittest.skipUnless(shutil.which("redis-check-aof"), "redis-check-aof is not installed")
    async def test_real_redis_accepts_our_log(self):
        path = self.path("check.aof")
        proc, port = await self.start(path)
        await self.ask(port, ("SET", "a", "1"), ("SET", "bin", b"a\r\nb\x00c"), ("SET", "t", "v", "EX", "100"),
                       ("INCR", "n"), ("RPUSH", "l", "x", "y"), ("LPUSH", "l", "w"), ("HSET", "h", "f", "v"),
                       ("EXPIRE", "a", "500"), ("DEL", "bin"))
        stop_server(proc)
        result = subprocess.run(["redis-check-aof", path], capture_output=True, text=True, timeout=30)
        self.assertIn("is valid", result.stdout + result.stderr)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
