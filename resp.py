"""RESP (REdis Serialization Protocol): parsing requests and encoding replies."""

# Limits, so one broken or hostile client can't make the server eat all memory.
MAX_BULK_LEN = 512 * 1024 * 1024   # one argument, same limit as real Redis
MAX_ARGS = 1024 * 1024             # arguments in one command
MAX_LINE_LEN = 64 * 1024           # a header line ("*3", "$5", or an inline command)


class ProtocolError(Exception):
    """Raised when the client sends something that is not valid RESP."""


def parse_command(buf: bytearray):
    """Try to parse ONE command from the front of buf.

    Returns:
        (args, consumed)  -- args is a list of bytes, consumed is how many
                             bytes of buf this command used.
        None              -- the command is not complete yet; wait for more data.

    Raises:
        ProtocolError     -- the data is malformed.

    This is what handles the "messy network data" problem: a command may arrive
    in pieces (we return None and the caller waits), or several commands may
    arrive glued together (the caller calls this in a loop).
    """
    if not buf:
        return None

    # Inline commands, e.g. typing "PING\r\n" in telnet.
    if buf[0:1] != b"*":
        end = buf.find(b"\r\n")
        if end == -1:
            if len(buf) > MAX_LINE_LEN:
                raise ProtocolError("too big inline request")
            return None
        return bytes(buf[:end]).split(), end + 2

    # Array of bulk strings: *<count>\r\n  then  $<len>\r\n<data>\r\n  per item.
    pos = buf.find(b"\r\n")
    if pos == -1:
        if len(buf) > MAX_LINE_LEN:
            raise ProtocolError("too big mbulk count string")
        return None
    try:
        count = int(buf[1:pos])
    except ValueError:
        raise ProtocolError("invalid multibulk length")
    if count > MAX_ARGS:
        raise ProtocolError("invalid multibulk length")
    pos += 2

    args = []
    for _ in range(max(count, 0)):
        if pos >= len(buf):
            return None
        if buf[pos:pos + 1] != b"$":
            raise ProtocolError("expected '$', got %r" % bytes(buf[pos:pos + 1]))
        end = buf.find(b"\r\n", pos)
        if end == -1:
            if len(buf) - pos > MAX_LINE_LEN:
                raise ProtocolError("too big bulk count string")
            return None
        try:
            length = int(buf[pos + 1:end])
        except ValueError:
            raise ProtocolError("invalid bulk length")
        if length < 0 or length > MAX_BULK_LEN:
            raise ProtocolError("invalid bulk length")
        start = end + 2
        if len(buf) < start + length + 2:
            return None  # the data itself hasn't fully arrived yet
        if buf[start + length:start + length + 2] != b"\r\n":
            raise ProtocolError("bulk string not terminated by CRLF")
        args.append(bytes(buf[start:start + length]))
        pos = start + length + 2

    return args, pos


# ---- Encoding replies ----

def simple_string(s: str) -> bytes:
    return b"+" + s.encode() + b"\r\n"


def error(msg) -> bytes:
    """msg is str or bytes. Line breaks are replaced with spaces: an error is a
    single line, and a CRLF inside it (say, from a client-chosen command name)
    would end the reply early and corrupt everything after it."""
    if isinstance(msg, str):
        msg = msg.encode()
    return b"-" + msg.replace(b"\r", b" ").replace(b"\n", b" ") + b"\r\n"


def integer(n: int) -> bytes:
    return b":" + str(n).encode() + b"\r\n"


def bulk_string(value) -> bytes:
    """value is bytes, or None for 'key does not exist' ($-1)."""
    if value is None:
        return b"$-1\r\n"
    return b"$" + str(len(value)).encode() + b"\r\n" + value + b"\r\n"


def array(items) -> bytes:
    """items is a list of already-encoded replies."""
    return b"*" + str(len(items)).encode() + b"\r\n" + b"".join(items)
