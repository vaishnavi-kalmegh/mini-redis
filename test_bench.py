"""Small tests for the benchmark script's own arithmetic, so a wrong number
can't sneak into the README. (The benchmark itself is run by hand: python bench.py)"""
import unittest

import bench

SAMPLE = '''"test","rps","avg_latency_ms","min_latency_ms","p50_latency_ms","p95_latency_ms","p99_latency_ms","max_latency_ms"
"PING_MBULK","100000.00","0.250","0.100","0.250","0.400","0.500","1.000"
"LRANGE_100 (first 100 elements)","50000.00","0.500","0.200","0.480","0.900","1.200","2.000"
'''


def runs(*values):
    return [{"rps": v, "p50": 1.0, "p99": 2.0} for v in values]


class BenchTest(unittest.TestCase):
    def test_parse_csv(self):
        rows = bench.parse_csv(SAMPLE)
        self.assertEqual(sorted(rows), ["lrange_100", "ping_mbulk"])
        self.assertEqual(rows["ping_mbulk"], {"rps": 100000.0, "p50": 0.25, "p99": 0.5})
        self.assertEqual(rows["lrange_100"]["rps"], 50000.0)

    def test_median_ignores_one_wild_run(self):
        self.assertEqual(bench.median(runs(100, 110, 5000), "rps"), 110)

    def test_spread_is_a_percentage_of_the_median(self):
        self.assertAlmostEqual(bench.spread(runs(90, 100, 110)), 20.0)
        self.assertEqual(bench.spread(runs(100)), 0.0)

    def test_summary_percent(self):
        results = {"none/P1": {"set": {"real": runs(100000, 100000, 100000), "mini": runs(20000, 25000, 30000)}}}
        row = bench.summary(results)["none/P1"]["set"]
        self.assertEqual(row["real_rps"], 100000)
        self.assertEqual(row["mini_rps"], 25000)
        self.assertEqual(row["percent"], 25.0)

    def test_server_commands_match_the_setup(self):
        real = bench.server_command("real", 1234, "aof", "/tmp/x")
        self.assertIn("--appendonly", real)
        self.assertEqual(real[real.index("--appendonly") + 1], "yes")
        mini = bench.server_command("mini", 1234, "aof", "/tmp/x")
        self.assertIn("--aof-file", mini)
        self.assertNotIn("--aof-file", bench.server_command("mini", 1234, "none", "/tmp/x"))
        self.assertEqual(bench.server_command("real", 1234, "none", "/tmp/x")[-2:], ["--appendonly", "no"])


if __name__ == "__main__":
    unittest.main()
