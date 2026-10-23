"""Tests that talk to the server over a raw socket, exactly like a real client would.

Run:  python -m unittest -v
"""
import socket
import subprocess
import sys
import time
import unittest

from testutil import cmd

PORT = 6391


class ServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.proc = subprocess.Popen(
            [sys.executable, "server.py", "--port", str(PORT)],
            stdout=subprocess.DEVNULL,
        )
        for _ in range(50):
            try:
                socket.create_connection(("127.0.0.1", PORT), timeout=0.2).close()
                return
            except OSError:
                time.sleep(0.1)
        raise RuntimeError("server did not start")

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        cls.proc.wait()

    def setUp(self):
        self.sock = socket.create_connection(("127.0.0.1", PORT), timeout=2)

    def tearDown(self):
        self.sock.close()

    def read(self, n):
        """Read exactly n bytes of reply."""
        data = b""
        while len(data) < n:
            chunk = self.sock.recv(n - len(data))
            if not chunk:
                break
            data += chunk
        return data

    def ask(self, *parts, expect_len):
        self.sock.sendall(cmd(*parts))
        return self.read(expect_len)

    # ---- basic commands ----

    def test_ping(self):
        self.assertEqual(self.ask("PING", expect_len=7), b"+PONG\r\n")

    def test_ping_with_message(self):
        self.assertEqual(self.ask("PING", "hi", expect_len=8), b"$2\r\nhi\r\n")

    def test_set_get(self):
        self.assertEqual(self.ask("SET", "name", "Rahul", expect_len=5), b"+OK\r\n")
        self.assertEqual(self.ask("GET", "name", expect_len=11), b"$5\r\nRahul\r\n")

    def test_get_missing_key(self):
        self.assertEqual(self.ask("GET", "nope", expect_len=5), b"$-1\r\n")

    def test_del_and_exists(self):
        self.ask("SET", "k1", "v", expect_len=5)
        self.ask("SET", "k2", "v", expect_len=5)
        self.assertEqual(self.ask("EXISTS", "k1", "k2", "k3", expect_len=4), b":2\r\n")
        self.assertEqual(self.ask("DEL", "k1", "k3", expect_len=4), b":1\r\n")
        self.assertEqual(self.ask("EXISTS", "k1", expect_len=4), b":0\r\n")

    def test_value_with_spaces_and_binary(self):
        value = b"hello world\r\n\x00end"
        self.ask("SET", "weird", value, expect_len=5)
        expected = b"$%d\r\n%s\r\n" % (len(value), value)
        self.assertEqual(self.ask("GET", "weird", expect_len=len(expected)), expected)

    def test_case_insensitive_command(self):
        self.assertEqual(self.ask("ping", expect_len=7), b"+PONG\r\n")

    # ---- expiry over the wire (real time, so keep the waits short) ----

    def test_set_px_expires_for_real(self):
        self.assertEqual(self.ask("SET", "short", "v", "PX", "100", expect_len=5), b"+OK\r\n")
        self.assertEqual(self.ask("GET", "short", expect_len=7), b"$1\r\nv\r\n")
        time.sleep(0.2)
        self.assertEqual(self.ask("GET", "short", expect_len=5), b"$-1\r\n")

    def test_ttl_and_expire_replies(self):
        self.ask("SET", "t", "v", expect_len=5)
        self.assertEqual(self.ask("TTL", "t", expect_len=5), b":-1\r\n")
        self.assertEqual(self.ask("EXPIRE", "t", "100", expect_len=4), b":1\r\n")
        self.assertEqual(self.ask("TTL", "t", expect_len=6), b":100\r\n")
        self.assertEqual(self.ask("TTL", "missing", expect_len=5), b":-2\r\n")

    # ---- counters, lists, hashes over the wire ----

    def test_incr_over_the_wire(self):
        self.assertEqual(self.ask("INCR", "wire_counter", expect_len=4), b":1\r\n")
        self.assertEqual(self.ask("DECR", "wire_counter", expect_len=4), b":0\r\n")

    def test_list_over_the_wire(self):
        self.assertEqual(self.ask("RPUSH", "wire_list", "a", "b", expect_len=4), b":2\r\n")
        expected = b"*2\r\n$1\r\na\r\n$1\r\nb\r\n"
        self.assertEqual(self.ask("LRANGE", "wire_list", "0", "-1", expect_len=len(expected)), expected)

    def test_hash_over_the_wire(self):
        self.assertEqual(self.ask("HSET", "wire_hash", "f", "v", expect_len=4), b":1\r\n")
        self.assertEqual(self.ask("HGET", "wire_hash", "f", expect_len=7), b"$1\r\nv\r\n")

    def test_wrongtype_over_the_wire(self):
        self.ask("RPUSH", "wire_list2", "a", expect_len=4)
        self.sock.sendall(cmd("GET", "wire_list2"))
        self.assertEqual(self.read(11), b"-WRONGTYPE ")

    # ---- errors ----

    def test_unknown_command(self):
        self.sock.sendall(cmd("FLY"))
        self.assertTrue(self.read(5).startswith(b"-ERR "))

    def test_wrong_arg_count(self):
        self.sock.sendall(cmd("GET"))
        self.assertTrue(self.read(5).startswith(b"-ERR "))

    # ---- the messy-network-data cases ----

    def test_command_split_across_packets(self):
        data = cmd("SET", "split", "value")
        for i in range(len(data)):  # send one byte at a time
            self.sock.sendall(data[i:i + 1])
            time.sleep(0.002)
        self.assertEqual(self.read(5), b"+OK\r\n")
        self.assertEqual(self.ask("GET", "split", expect_len=11), b"$5\r\nvalue\r\n")

    def test_commands_stuck_together(self):
        self.sock.sendall(cmd("SET", "a", "1") + cmd("GET", "a") + cmd("PING"))
        self.assertEqual(self.read(5 + 7 + 7), b"+OK\r\n$1\r\n1\r\n+PONG\r\n")

    def test_inline_command(self):
        self.sock.sendall(b"PING\r\n")
        self.assertEqual(self.read(7), b"+PONG\r\n")

    def test_garbage_closes_connection_with_error(self):
        self.sock.sendall(b"*1\r\nXXX\r\n")
        self.assertTrue(self.read(5).startswith(b"-ERR "))


if __name__ == "__main__":
    unittest.main()
