"""Unit tests that call commands directly with a fake clock, so time-based
behaviour (expiry) is exact and the tests never sleep.

Run:  python -m unittest -v
"""
import unittest

import commands
from store import Store
from testutil import FakeClock


def parts(args):
    return [a.encode() if isinstance(a, str) else a for a in args]


class CommandTest(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.store = Store(clock=self.clock)

    def run_cmd(self, *args):
        return commands.execute(self.store, parts(args))


class ExpiryTest(CommandTest):
    def test_set_ex_key_disappears_exactly_at_deadline(self):
        self.run_cmd("SET", "k", "v", "EX", "10")
        self.clock.advance(9.999)
        self.assertEqual(self.run_cmd("GET", "k"), b"$1\r\nv\r\n")
        self.clock.advance(0.001)  # now == deadline -> expired
        self.assertEqual(self.run_cmd("GET", "k"), b"$-1\r\n")

    def test_set_px(self):
        self.run_cmd("SET", "k", "v", "PX", "250")
        self.clock.advance(0.249)
        self.assertEqual(self.run_cmd("EXISTS", "k"), b":1\r\n")
        self.clock.advance(0.001)
        self.assertEqual(self.run_cmd("EXISTS", "k"), b":0\r\n")

    def test_option_names_are_case_insensitive(self):
        self.assertEqual(self.run_cmd("set", "k", "v", "ex", "5"), b"+OK\r\n")
        self.assertEqual(self.run_cmd("TTL", "k"), b":5\r\n")

    def test_ttl_missing_and_no_expiry(self):
        self.assertEqual(self.run_cmd("TTL", "nope"), b":-2\r\n")
        self.run_cmd("SET", "k", "v")
        self.assertEqual(self.run_cmd("TTL", "k"), b":-1\r\n")

    def test_ttl_counts_down(self):
        self.run_cmd("SET", "k", "v", "EX", "10")
        self.assertEqual(self.run_cmd("TTL", "k"), b":10\r\n")
        self.clock.advance(3.2)  # 6.8s left -> rounds to 7
        self.assertEqual(self.run_cmd("TTL", "k"), b":7\r\n")

    def test_ttl_of_expired_key_is_minus_two(self):
        self.run_cmd("SET", "k", "v", "EX", "1")
        self.clock.advance(1)
        self.assertEqual(self.run_cmd("TTL", "k"), b":-2\r\n")

    def test_set_clears_old_ttl(self):
        self.run_cmd("SET", "k", "v", "EX", "10")
        self.run_cmd("SET", "k", "v2")
        self.assertEqual(self.run_cmd("TTL", "k"), b":-1\r\n")
        self.clock.advance(100)
        self.assertEqual(self.run_cmd("GET", "k"), b"$2\r\nv2\r\n")

    def test_set_at_the_exact_moment_of_expiry_makes_a_fresh_key(self):
        # The edge case from the plan: key expires at the same instant it is updated.
        self.run_cmd("SET", "k", "old", "EX", "5")
        self.clock.advance(5)
        self.run_cmd("SET", "k", "new")           # no TTL on the new value
        self.clock.advance(1000)
        self.assertEqual(self.run_cmd("GET", "k"), b"$3\r\nnew\r\n")

    def test_expire_sets_deadline(self):
        self.run_cmd("SET", "k", "v")
        self.assertEqual(self.run_cmd("EXPIRE", "k", "30"), b":1\r\n")
        self.assertEqual(self.run_cmd("TTL", "k"), b":30\r\n")
        self.clock.advance(30)
        self.assertEqual(self.run_cmd("EXISTS", "k"), b":0\r\n")

    def test_expire_on_missing_key(self):
        self.assertEqual(self.run_cmd("EXPIRE", "nope", "30"), b":0\r\n")

    def test_expire_zero_or_negative_deletes_key(self):
        self.run_cmd("SET", "a", "v")
        self.run_cmd("SET", "b", "v")
        self.assertEqual(self.run_cmd("EXPIRE", "a", "0"), b":1\r\n")
        self.assertEqual(self.run_cmd("EXPIRE", "b", "-5"), b":1\r\n")
        self.assertEqual(self.run_cmd("EXISTS", "a", "b"), b":0\r\n")

    def test_expire_rejects_non_integer(self):
        self.run_cmd("SET", "k", "v")
        self.assertTrue(self.run_cmd("EXPIRE", "k", "soon").startswith(b"-ERR value is not an integer"))
        self.assertTrue(self.run_cmd("EXPIRE", "k", " 5").startswith(b"-ERR value is not an integer"))

    def test_del_does_not_count_expired_key(self):
        self.run_cmd("SET", "k", "v", "EX", "1")
        self.clock.advance(1)
        self.assertEqual(self.run_cmd("DEL", "k"), b":0\r\n")

    def test_del_removes_expiry_too(self):
        self.run_cmd("SET", "k", "v", "EX", "10")
        self.run_cmd("DEL", "k")
        self.assertNotIn(b"k", self.store.expires)

    def test_pxat_sets_an_absolute_deadline(self):
        at = self.store.now_ms() + 5000
        self.assertEqual(self.run_cmd("SET", "k", "v", "PXAT", str(at)), b"+OK\r\n")
        self.assertEqual(self.run_cmd("TTL", "k"), b":5\r\n")
        self.clock.advance(5)
        self.assertEqual(self.run_cmd("EXISTS", "k"), b":0\r\n")

    def test_pexpireat(self):
        self.run_cmd("SET", "k", "v")
        self.assertEqual(self.run_cmd("PEXPIREAT", "k", str(self.store.now_ms() + 7000)), b":1\r\n")
        self.assertEqual(self.run_cmd("TTL", "k"), b":7\r\n")
        self.assertEqual(self.run_cmd("PEXPIREAT", "missing", "99999999999999"), b":0\r\n")
        self.assertEqual(self.run_cmd("PEXPIREAT", "k", "1000"), b":1\r\n")  # in the past: deleted
        self.assertEqual(self.run_cmd("EXISTS", "k"), b":0\r\n")
        self.assertTrue(self.run_cmd("PEXPIREAT", "k", "abc").startswith(b"-ERR value is not an integer"))
        self.assertTrue(self.run_cmd("PEXPIREAT", "k").startswith(b"-ERR wrong number of arguments"))

    def test_expiry_that_does_not_fit_in_64_bits_is_rejected(self):
        too_big = "invalid expire time in '%s' command"
        self.run_cmd("SET", "k", "v")
        for huge in ("9223372036854775807", "9223372036854776", "-9223372036854775808"):
            self.assertTrue(self.run_cmd("EXPIRE", "k", huge).startswith(b"-ERR " + (too_big % "expire").encode()), huge)
            self.assertTrue(self.run_cmd("EXPIRE", "missing", huge).startswith(b"-ERR " + (too_big % "expire").encode()), huge)
        for unit in ("EX", "PX"):
            self.assertTrue(self.run_cmd("SET", "k2", "v", unit, "9223372036854775807")
                            .startswith(b"-ERR " + (too_big % "set").encode()))
        self.assertEqual(self.run_cmd("EXISTS", "k", "k2"), b":1\r\n")  # k untouched, k2 never created
        self.assertEqual(self.run_cmd("TTL", "k"), b":-1\r\n")
        self.assertEqual(self.run_cmd("EXPIRE", "k", "4611686018427387"), b":1\r\n")  # big but legal

    def test_bad_set_options(self):
        self.assertTrue(self.run_cmd("SET", "k", "v", "EX", "0").startswith(b"-ERR invalid expire time"))
        self.assertTrue(self.run_cmd("SET", "k", "v", "EX", "-1").startswith(b"-ERR invalid expire time"))
        self.assertTrue(self.run_cmd("SET", "k", "v", "EX", "abc").startswith(b"-ERR value is not an integer"))
        self.assertTrue(self.run_cmd("SET", "k", "v", "EX").startswith(b"-ERR syntax error"))
        self.assertTrue(self.run_cmd("SET", "k", "v", "EX", "5", "PX", "5").startswith(b"-ERR syntax error"))
        self.assertTrue(self.run_cmd("SET", "k", "v", "FOO").startswith(b"-ERR syntax error"))
        self.assertEqual(self.run_cmd("EXISTS", "k"), b":0\r\n")  # failed SETs stored nothing


class CounterTest(CommandTest):
    def test_incr_creates_key_at_zero(self):
        self.assertEqual(self.run_cmd("INCR", "n"), b":1\r\n")
        self.assertEqual(self.run_cmd("INCR", "n"), b":2\r\n")
        self.assertEqual(self.run_cmd("GET", "n"), b"$1\r\n2\r\n")

    def test_decr_goes_negative(self):
        self.assertEqual(self.run_cmd("DECR", "n"), b":-1\r\n")
        self.assertEqual(self.run_cmd("DECR", "n"), b":-2\r\n")

    def test_incr_on_existing_number_string(self):
        self.run_cmd("SET", "n", "41")
        self.assertEqual(self.run_cmd("INCR", "n"), b":42\r\n")

    def test_incr_on_non_number_is_an_error_and_leaves_value_alone(self):
        self.run_cmd("SET", "n", "hello")
        self.assertTrue(self.run_cmd("INCR", "n").startswith(b"-ERR value is not an integer"))
        self.assertEqual(self.run_cmd("GET", "n"), b"$5\r\nhello\r\n")

    def test_incr_rejects_values_python_would_accept(self):
        for bad in (" 5", "5 ", "1_000", "+5", "007", "-0", "-007", "1.5", ""):
            self.run_cmd("SET", "n", bad)
            self.assertTrue(self.run_cmd("INCR", "n").startswith(b"-ERR value is not an integer"), bad)

    def test_incr_overflow(self):
        self.run_cmd("SET", "n", str(2 ** 63 - 1))
        self.assertTrue(self.run_cmd("INCR", "n").startswith(b"-ERR increment or decrement would overflow"))
        self.run_cmd("SET", "m", str(-(2 ** 63)))
        self.assertTrue(self.run_cmd("DECR", "m").startswith(b"-ERR increment or decrement would overflow"))

    def test_incr_keeps_ttl(self):
        self.run_cmd("SET", "n", "1", "EX", "10")
        self.run_cmd("INCR", "n")
        self.assertEqual(self.run_cmd("TTL", "n"), b":10\r\n")

    def test_incr_on_expired_key_starts_over(self):
        self.run_cmd("SET", "n", "50", "EX", "1")
        self.clock.advance(1)
        self.assertEqual(self.run_cmd("INCR", "n"), b":1\r\n")
        self.assertEqual(self.run_cmd("TTL", "n"), b":-1\r\n")

    def test_wrong_arg_count(self):
        self.assertTrue(self.run_cmd("INCR").startswith(b"-ERR wrong number of arguments"))
        self.assertTrue(self.run_cmd("DECR", "a", "b").startswith(b"-ERR wrong number of arguments"))


def arr(*items):
    """Expected RESP reply for a list of strings."""
    out = b"*%d\r\n" % len(items)
    for item in items:
        out += b"$%d\r\n%s\r\n" % (len(item), item.encode())
    return out


class ListTest(CommandTest):
    def test_rpush_keeps_order_and_returns_length(self):
        self.assertEqual(self.run_cmd("RPUSH", "l", "a", "b", "c"), b":3\r\n")
        self.assertEqual(self.run_cmd("LRANGE", "l", "0", "-1"), arr("a", "b", "c"))

    def test_lpush_reverses_arguments(self):
        self.assertEqual(self.run_cmd("LPUSH", "l", "a", "b", "c"), b":3\r\n")
        self.assertEqual(self.run_cmd("LRANGE", "l", "0", "-1"), arr("c", "b", "a"))

    def test_mixed_pushes(self):
        self.run_cmd("RPUSH", "l", "b")
        self.run_cmd("LPUSH", "l", "a")
        self.run_cmd("RPUSH", "l", "c")
        self.assertEqual(self.run_cmd("LRANGE", "l", "0", "-1"), arr("a", "b", "c"))

    def test_lrange_ranges(self):
        self.run_cmd("RPUSH", "l", "a", "b", "c", "d", "e")
        self.assertEqual(self.run_cmd("LRANGE", "l", "1", "2"), arr("b", "c"))
        self.assertEqual(self.run_cmd("LRANGE", "l", "-2", "-1"), arr("d", "e"))
        self.assertEqual(self.run_cmd("LRANGE", "l", "0", "100"), arr("a", "b", "c", "d", "e"))
        self.assertEqual(self.run_cmd("LRANGE", "l", "-100", "1"), arr("a", "b"))
        self.assertEqual(self.run_cmd("LRANGE", "l", "3", "1"), arr())
        self.assertEqual(self.run_cmd("LRANGE", "l", "10", "20"), arr())
        self.assertEqual(self.run_cmd("LRANGE", "l", "0", "-100"), arr())

    def test_lrange_missing_key_is_empty(self):
        self.assertEqual(self.run_cmd("LRANGE", "nope", "0", "-1"), arr())

    def test_lrange_non_integer_index(self):
        self.run_cmd("RPUSH", "l", "a")
        self.assertTrue(self.run_cmd("LRANGE", "l", "x", "1").startswith(b"-ERR value is not an integer"))

    def test_list_values_can_be_binary(self):
        self.run_cmd("RPUSH", "l", b"a\r\nb")
        self.assertEqual(self.run_cmd("LRANGE", "l", "0", "-1"), b"*1\r\n$4\r\na\r\nb\r\n")

    def test_list_can_expire_and_be_deleted(self):
        self.run_cmd("RPUSH", "l", "a")
        self.run_cmd("EXPIRE", "l", "5")
        self.clock.advance(5)
        self.assertEqual(self.run_cmd("EXISTS", "l"), b":0\r\n")
        self.run_cmd("RPUSH", "l2", "a")
        self.assertEqual(self.run_cmd("DEL", "l2"), b":1\r\n")

    def test_pushing_onto_an_expired_list_starts_fresh(self):
        self.run_cmd("RPUSH", "l", "old")
        self.run_cmd("EXPIRE", "l", "1")
        self.clock.advance(1)
        self.assertEqual(self.run_cmd("RPUSH", "l", "new"), b":1\r\n")
        self.assertEqual(self.run_cmd("TTL", "l"), b":-1\r\n")

    def test_wrong_arg_counts(self):
        self.assertTrue(self.run_cmd("LPUSH", "l").startswith(b"-ERR wrong number of arguments"))
        self.assertTrue(self.run_cmd("LRANGE", "l", "0").startswith(b"-ERR wrong number of arguments"))


class HashTest(CommandTest):
    def test_hset_hget(self):
        self.assertEqual(self.run_cmd("HSET", "h", "name", "Vi"), b":1\r\n")
        self.assertEqual(self.run_cmd("HGET", "h", "name"), b"$2\r\nVi\r\n")

    def test_hset_counts_only_new_fields(self):
        self.assertEqual(self.run_cmd("HSET", "h", "a", "1", "b", "2"), b":2\r\n")
        self.assertEqual(self.run_cmd("HSET", "h", "a", "9", "c", "3"), b":1\r\n")
        self.assertEqual(self.run_cmd("HGET", "h", "a"), b"$1\r\n9\r\n")
        self.assertEqual(self.run_cmd("HSET", "h", "a", "9"), b":0\r\n")

    def test_hget_missing_field_or_key(self):
        self.run_cmd("HSET", "h", "a", "1")
        self.assertEqual(self.run_cmd("HGET", "h", "zzz"), b"$-1\r\n")
        self.assertEqual(self.run_cmd("HGET", "nope", "a"), b"$-1\r\n")

    def test_hset_needs_complete_pairs(self):
        self.assertTrue(self.run_cmd("HSET", "h", "a").startswith(b"-ERR wrong number of arguments"))
        self.assertTrue(self.run_cmd("HSET", "h", "a", "1", "b").startswith(b"-ERR wrong number of arguments"))
        self.assertEqual(self.run_cmd("EXISTS", "h"), b":0\r\n")

    def test_hash_can_expire(self):
        self.run_cmd("HSET", "h", "a", "1")
        self.run_cmd("EXPIRE", "h", "2")
        self.clock.advance(2)
        self.assertEqual(self.run_cmd("HGET", "h", "a"), b"$-1\r\n")

    def test_hset_keeps_existing_ttl(self):
        self.run_cmd("HSET", "h", "a", "1")
        self.run_cmd("EXPIRE", "h", "10")
        self.run_cmd("HSET", "h", "b", "2")
        self.assertEqual(self.run_cmd("TTL", "h"), b":10\r\n")


class WrongTypeTest(CommandTest):
    def setUp(self):
        super().setUp()
        self.run_cmd("SET", "str", "v")
        self.run_cmd("RPUSH", "list", "a")
        self.run_cmd("HSET", "hash", "f", "v")

    def assertWrongType(self, *args):
        self.assertTrue(self.run_cmd(*args).startswith(b"-WRONGTYPE "), args)

    def test_string_commands_on_other_types(self):
        self.assertWrongType("GET", "list")
        self.assertWrongType("GET", "hash")
        self.assertWrongType("INCR", "list")
        self.assertWrongType("DECR", "hash")

    def test_list_commands_on_other_types(self):
        self.assertWrongType("LPUSH", "str", "x")
        self.assertWrongType("RPUSH", "hash", "x")
        self.assertWrongType("LRANGE", "str", "0", "-1")
        self.assertWrongType("LRANGE", "hash", "0", "-1")

    def test_hash_commands_on_other_types(self):
        self.assertWrongType("HSET", "str", "f", "v")
        self.assertWrongType("HSET", "list", "f", "v")
        self.assertWrongType("HGET", "str", "f")
        self.assertWrongType("HGET", "list", "f")

    def test_failed_command_leaves_data_alone(self):
        self.run_cmd("LPUSH", "str", "x")
        self.assertEqual(self.run_cmd("GET", "str"), b"$1\r\nv\r\n")

    def test_set_overwrites_any_type(self):
        self.run_cmd("SET", "list", "now a string")
        self.assertEqual(self.run_cmd("GET", "list"), b"$12\r\nnow a string\r\n")

    def test_del_and_exists_work_on_any_type(self):
        self.assertEqual(self.run_cmd("EXISTS", "str", "list", "hash"), b":3\r\n")
        self.assertEqual(self.run_cmd("DEL", "str", "list", "hash"), b":3\r\n")


class UnknownCommandTest(CommandTest):
    def test_message_matches_real_redis(self):
        self.assertEqual(self.run_cmd("FLY"), b"-ERR unknown command 'FLY', with args beginning with: \r\n")
        self.assertEqual(self.run_cmd("FLY", "a", "b"),
                         b"-ERR unknown command 'FLY', with args beginning with: 'a' 'b' \r\n")

    def test_line_breaks_in_the_name_cannot_break_the_reply(self):
        reply = self.run_cmd("A\r\nB", "x\ny")
        self.assertEqual(reply.count(b"\r\n"), 1)  # still exactly one line
        self.assertTrue(reply.endswith(b"\r\n"))

    def test_huge_arguments_are_cut_off(self):
        reply = self.run_cmd("FLY", "x" * 5000)
        self.assertLess(len(reply), 300)

    def test_raw_bytes_are_echoed_unchanged(self):
        self.assertIn(b"\xff\xfe", commands.execute(self.store, [b"FLY", b"\xff\xfe"]))


class SweepTest(CommandTest):
    def test_sweep_removes_expired_keys_only(self):
        self.run_cmd("SET", "short1", "v", "EX", "1")
        self.run_cmd("SET", "short2", "v", "EX", "1")
        self.run_cmd("SET", "long", "v", "EX", "100")
        self.run_cmd("SET", "forever", "v")
        self.clock.advance(2)
        removed = self.store.sweep()
        self.assertEqual(removed, 2)
        self.assertEqual(sorted(self.store.data), [b"forever", b"long"])
        self.assertEqual(list(self.store.expires), [b"long"])

    def test_sweep_keeps_going_when_most_keys_are_expired(self):
        for i in range(200):
            self.run_cmd("SET", "k%d" % i, "v", "EX", "1")
        self.clock.advance(2)
        self.store.sweep()
        self.assertEqual(self.store.data, {})

    def test_sweep_with_nothing_to_do(self):
        self.run_cmd("SET", "k", "v")
        self.assertEqual(self.store.sweep(), 0)


if __name__ == "__main__":
    unittest.main()
