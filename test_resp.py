"""Unit tests for the RESP parser, with no network involved.

The most important one: feed a command one byte further each time and check the
parser says "not complete yet" at every split point, then succeeds at the end.
That is the "half a command arrived" bug in its purest form.
"""
import unittest

import resp
from resp import ProtocolError, parse_command


def encode(*parts):
    out = b"*%d\r\n" % len(parts)
    for p in parts:
        out += b"$%d\r\n%s\r\n" % (len(p), p)
    return out


class CompleteCommandTest(unittest.TestCase):
    def test_simple_command(self):
        data = encode(b"SET", b"a", b"1")
        self.assertEqual(parse_command(bytearray(data)), ([b"SET", b"a", b"1"], len(data)))

    def test_empty_argument(self):
        data = b"*2\r\n$3\r\nGET\r\n$0\r\n\r\n"
        self.assertEqual(parse_command(bytearray(data)), ([b"GET", b""], len(data)))

    def test_argument_containing_crlf_and_nul(self):
        value = b"a\r\nb\x00c\r\n"
        data = encode(b"SET", b"k", value)
        args, consumed = parse_command(bytearray(data))
        self.assertEqual(args[2], value)
        self.assertEqual(consumed, len(data))

    def test_only_the_first_of_two_commands_is_consumed(self):
        first, second = encode(b"PING"), encode(b"GET", b"x")
        buf = bytearray(first + second)
        args, consumed = parse_command(buf)
        self.assertEqual((args, consumed), ([b"PING"], len(first)))
        del buf[:consumed]
        self.assertEqual(parse_command(buf), ([b"GET", b"x"], len(second)))

    def test_inline_command(self):
        self.assertEqual(parse_command(bytearray(b"SET a  1\r\n")), ([b"SET", b"a", b"1"], 10))

    def test_blank_inline_line_is_an_empty_command(self):
        self.assertEqual(parse_command(bytearray(b"\r\nPING\r\n")), ([], 2))

    def test_zero_and_negative_counts_are_empty_commands(self):
        self.assertEqual(parse_command(bytearray(b"*0\r\n")), ([], 4))
        self.assertEqual(parse_command(bytearray(b"*-1\r\n")), ([], 5))

    def test_empty_buffer(self):
        self.assertIsNone(parse_command(bytearray()))


class SplitPointTest(unittest.TestCase):
    """Every strict prefix of a command must mean 'wait for more'."""

    def check_every_prefix(self, data, expected_args):
        for i in range(len(data)):
            self.assertIsNone(parse_command(bytearray(data[:i])), "prefix of length %d" % i)
        self.assertEqual(parse_command(bytearray(data)), (expected_args, len(data)))

    def test_array_command(self):
        self.check_every_prefix(encode(b"SET", b"key", b"value"), [b"SET", b"key", b"value"])

    def test_value_with_crlf_inside(self):
        self.check_every_prefix(encode(b"SET", b"k", b"x\r\ny"), [b"SET", b"k", b"x\r\ny"])

    def test_empty_argument(self):
        self.check_every_prefix(encode(b"GET", b""), [b"GET", b""])

    def test_inline_command(self):
        self.check_every_prefix(b"PING\r\n", [b"PING"])

    def test_long_value(self):
        value = b"z" * 10000
        self.check_every_prefix(encode(b"SET", b"k", value), [b"SET", b"k", value])


class MalformedInputTest(unittest.TestCase):
    def assertRejected(self, data):
        with self.assertRaises(ProtocolError):
            parse_command(bytearray(data))

    def test_non_numeric_count(self):
        self.assertRejected(b"*abc\r\n")

    def test_missing_dollar(self):
        self.assertRejected(b"*1\r\nXXX\r\n")

    def test_non_numeric_bulk_length(self):
        self.assertRejected(b"*1\r\n$abc\r\n")

    def test_negative_bulk_length(self):
        self.assertRejected(b"*1\r\n$-1\r\n")

    def test_bulk_not_terminated_by_crlf(self):
        self.assertRejected(b"*1\r\n$3\r\nabcXY")

    def test_bulk_length_over_limit(self):
        self.assertRejected(b"*1\r\n$%d\r\n" % (resp.MAX_BULK_LEN + 1))

    def test_argument_count_over_limit(self):
        self.assertRejected(b"*%d\r\n" % (resp.MAX_ARGS + 1))

    def test_absurdly_long_number_does_not_crash(self):
        self.assertRejected(b"*" + b"9" * 5000 + b"\r\n")

    def test_header_line_that_never_ends(self):
        self.assertRejected(b"*" + b"1" * (resp.MAX_LINE_LEN + 10))
        self.assertRejected(b"*1\r\n$" + b"1" * (resp.MAX_LINE_LEN + 10))

    def test_inline_line_that_never_ends(self):
        self.assertRejected(b"A" * (resp.MAX_LINE_LEN + 10))

    def test_limits_themselves_are_accepted(self):
        # The largest legal bulk length is not an error, it just isn't complete yet.
        self.assertIsNone(parse_command(bytearray(b"*1\r\n$%d\r\n" % resp.MAX_BULK_LEN)))


class EncodingTest(unittest.TestCase):
    def test_replies(self):
        self.assertEqual(resp.simple_string("OK"), b"+OK\r\n")
        self.assertEqual(resp.error("ERR x"), b"-ERR x\r\n")
        self.assertEqual(resp.integer(-5), b":-5\r\n")
        self.assertEqual(resp.bulk_string(b"hi"), b"$2\r\nhi\r\n")
        self.assertEqual(resp.bulk_string(b""), b"$0\r\n\r\n")
        self.assertEqual(resp.bulk_string(None), b"$-1\r\n")
        self.assertEqual(resp.array([]), b"*0\r\n")
        self.assertEqual(resp.array([resp.integer(1), resp.bulk_string(b"a")]), b"*2\r\n:1\r\n$1\r\na\r\n")


if __name__ == "__main__":
    unittest.main()
