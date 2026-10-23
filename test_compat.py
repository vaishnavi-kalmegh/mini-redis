"""Compare Mini Redis with REAL Redis, command by command.

Starts a real redis-server on a spare port, sends both servers the same ~270
commands (see compare_with_redis.py) and fails if any reply differs.
Skipped automatically when redis-server isn't installed.
"""
import asyncio
import shutil
import socket
import subprocess
import time
import unittest

import compare_with_redis as compare
from testutil import free_port, start_server, stop_server


def start_real_redis():
    port = free_port()
    proc = subprocess.Popen(
        ["redis-server", "--port", str(port), "--save", "", "--appendonly", "no"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    for _ in range(50):
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            return proc, port
        except OSError:
            time.sleep(0.1)
    proc.kill()
    raise RuntimeError("redis-server did not start")


@unittest.skipUnless(shutil.which("redis-server"), "redis-server is not installed")
class CompatibilityTest(unittest.IsolatedAsyncioTestCase):
    async def test_replies_match_real_redis(self):
        real, real_port = await asyncio.to_thread(start_real_redis)
        mini, mini_port = await asyncio.to_thread(start_server)
        try:
            diffs = await compare.run(compare.SCRIPT, mini_port, real_port)
        finally:
            stop_server(mini)
            stop_server(real)
        unexpected = [d for d in diffs if d[0][:1] not in compare.EXPECTED]
        report = "\n".join("%r\n  mini: %s\n  real: %s" % (c, compare.show(m), compare.show(r))
                           for c, m, r in unexpected)
        self.assertEqual(unexpected, [], "replies differ from real Redis:\n" + report)


if __name__ == "__main__":
    unittest.main()
