"""Tests with many clients connected at the same time.

Run:  python -m unittest -v test_concurrency
"""
import asyncio
import os
import random
import resource
import unittest

from testutil import (RespError, call, close, cmd, connect, read_reply,
                      start_server, stop_server)


class ConcurrencyTest(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.proc, cls.port = start_server()

    @classmethod
    def tearDownClass(cls):
        stop_server(cls.proc)

    async def test_many_clients_incrementing_one_counter(self):
        # All commands run one at a time on one thread, so no increment can be
        # lost or duplicated: the replies must be exactly 1..N with no repeats.
        clients, per_client = 50, 100

        async def worker():
            reader, writer = await connect(self.port)
            seen = [await call(reader, writer, "INCR", "counter") for _ in range(per_client)]
            await close(writer)
            return seen

        results = await asyncio.gather(*[worker() for _ in range(clients)])
        everything = sorted(v for seen in results for v in seen)
        self.assertEqual(everything, list(range(1, clients * per_client + 1)))
        for seen in results:  # each client sees its own replies in increasing order
            self.assertEqual(seen, sorted(seen))

    async def test_replies_never_get_mixed_up_between_clients(self):
        async def worker(i):
            reader, writer = await connect(self.port)
            key = "own-%d" % i
            self.assertEqual(await call(reader, writer, "SET", key, str(i)), b"OK")
            await asyncio.sleep(random.random() * 0.02)  # let other clients run in between
            self.assertEqual(await call(reader, writer, "GET", key), str(i).encode())
            await close(writer)

        await asyncio.gather(*[worker(i) for i in range(100)])

    async def test_partial_commands_from_two_clients_interleave_safely(self):
        (ra, wa), (rb, wb) = await connect(self.port), await connect(self.port)
        a, b = cmd("SET", "ia", "from-a"), cmd("SET", "ib", "from-b")
        half_a, half_b = len(a) // 2, len(b) // 2

        wa.write(a[:half_a]); await wa.drain()
        wb.write(b[:half_b]); await wb.drain()
        await asyncio.sleep(0.05)  # both commands are now half-sent
        wb.write(b[half_b:]); await wb.drain()
        wa.write(a[half_a:]); await wa.drain()

        self.assertEqual(await asyncio.wait_for(read_reply(ra), 5), b"OK")
        self.assertEqual(await asyncio.wait_for(read_reply(rb), 5), b"OK")
        self.assertEqual(await call(ra, wa, "GET", "ib"), b"from-b")
        self.assertEqual(await call(rb, wb, "GET", "ia"), b"from-a")
        await close(wa); await close(wb)

    async def test_clients_that_disconnect_mid_command(self):
        for _ in range(20):
            reader, writer = await connect(self.port)
            writer.write(b"*3\r\n$3\r\nSET\r\n$7\r\ncut-off\r\n$5\r\nab")  # value never finishes
            await writer.drain()
            await close(writer)
        reader, writer = await connect(self.port)
        self.assertEqual(await call(reader, writer, "PING"), b"PONG")
        self.assertIsNone(await call(reader, writer, "GET", "cut-off"))  # half a command stored nothing
        self.assertIsNone(self.proc.poll(), "server crashed")
        await close(writer)

    async def test_many_idle_connections(self):
        wanted = 150
        if resource.getrlimit(resource.RLIMIT_NOFILE)[0] < 512:
            self.skipTest("file descriptor limit too low")
        conns = await asyncio.gather(*[connect(self.port) for _ in range(wanted)])
        await asyncio.sleep(0.2)  # all idle at once
        replies = await asyncio.gather(*[call(r, w, "PING") for r, w in conns])
        self.assertEqual(replies, [b"PONG"] * wanted)
        for _, w in conns:
            await close(w)

    async def test_large_value(self):
        value = os.urandom(5_000_000)  # random bytes, includes \r\n and \x00 by chance
        reader, writer = await connect(self.port)
        self.assertEqual(await call(reader, writer, "SET", "big", value), b"OK")
        got = await call(reader, writer, "GET", "big")
        self.assertTrue(got == value, "5 MB value came back different")
        await close(writer)

    async def test_pipelining_thousands_of_commands(self):
        n = 5000
        reader, writer = await connect(self.port)
        batch = b"".join(cmd("SET", "pipe-%d" % i, str(i)) for i in range(n))
        batch += b"".join(cmd("GET", "pipe-%d" % i) for i in range(n))

        async def send():
            writer.write(batch)  # one big write: thousands of commands arrive stuck together
            await writer.drain()

        async def receive():
            return [await read_reply(reader) for _ in range(2 * n)]

        _, replies = await asyncio.wait_for(asyncio.gather(send(), receive()), 30)
        self.assertEqual(replies[:n], [b"OK"] * n)
        self.assertEqual(replies[n:], [str(i).encode() for i in range(n)])
        await close(writer)

    async def test_a_client_that_never_reads_does_not_block_others(self):
        reader_a, writer_a = await connect(self.port)
        reader_b, writer_b = await connect(self.port)
        await call(reader_a, writer_a, "SET", "fat", b"x" * 200_000)

        # Client A asks for 20 MB of replies and never reads any of it.
        writer_a.write(b"".join(cmd("GET", "fat") for _ in range(100)))
        try:
            await asyncio.wait_for(writer_a.drain(), 1)
        except asyncio.TimeoutError:
            pass
        await asyncio.sleep(0.2)

        # Client B must still be served promptly.
        for _ in range(20):
            self.assertEqual(await call(reader_b, writer_b, "PING", timeout=2), b"PONG")
        self.assertIsNone(self.proc.poll(), "server crashed")
        writer_a.transport.abort()
        await close(writer_b)

    async def test_errors_on_one_connection_do_not_affect_another(self):
        (ra, wa), (rb, wb) = await connect(self.port), await connect(self.port)
        wa.write(b"*1\r\nGARBAGE\r\n")  # protocol error: server drops this client
        await wa.drain()
        reply = await asyncio.wait_for(read_reply(ra), 5)
        self.assertIsInstance(reply, RespError)
        self.assertEqual(await asyncio.wait_for(ra.read(), 5), b"")  # then closes the connection
        self.assertEqual(await call(rb, wb, "PING"), b"PONG")
        await close(wa); await close(wb)


class QueryBufferLimitTest(unittest.IsolatedAsyncioTestCase):
    async def test_client_sending_endless_unfinished_command_is_dropped(self):
        proc, port = await asyncio.to_thread(start_server, "--max-query-buffer", "10000")
        try:
            reader, writer = await connect(port)
            # Promises a 1 MB value, then sends 20 KB of it and stops.
            writer.write(b"*3\r\n$3\r\nSET\r\n$1\r\nk\r\n$1000000\r\n" + b"x" * 20000)
            await writer.drain()
            reply = await asyncio.wait_for(read_reply(reader), 5)
            self.assertIsInstance(reply, RespError)
            self.assertIn("buffer limit", str(reply))
            self.assertEqual(await asyncio.wait_for(reader.read(), 5), b"")  # connection closed
            await close(writer)

            reader, writer = await connect(port)  # the server itself is fine
            self.assertEqual(await call(reader, writer, "PING"), b"PONG")
            await close(writer)
        finally:
            stop_server(proc)


if __name__ == "__main__":
    unittest.main()
