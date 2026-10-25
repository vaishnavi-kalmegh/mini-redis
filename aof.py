"""Append-only log (AOF): saves the data to disk so a restart doesn't wipe it.

How it works
------------
Every change to the data is appended to a file as a command, in RESP, the same
format clients use (and the same format real Redis uses for its own log).
At startup the file is read back and the commands are run again, which rebuilds
the data exactly as it was.

Three details make this correct:

1. Absolute times. "SET k v EX 10" is logged as "SET k v PXAT <exact time>".
   Replayed an hour later, "10 seconds from now" would be wrong.

2. Expired keys are logged as DEL. If a key expires and is then re-created
   (say with RPUSH), replay must not find the OLD key still sitting there.
   While replaying, nothing expires (store.loading), because the log describes
   the past and a later logged command may still touch a key that is "expired"
   by today's clock.

3. A crash can leave half a command at the very end. At startup that
   incomplete last command is ignored AND cut off the file, so the next append
   starts on a clean boundary instead of gluing onto the broken half.
   Damage anywhere else is different: that means data is genuinely lost or
   corrupt, so the server refuses to start instead of quietly losing it.
"""
import asyncio
import os

import commands
import resp


class AOFError(Exception):
    """The log is corrupt (not merely cut short at the end)."""


def encode_command(args):
    """One command as RESP, e.g. [b'SET', b'k', b'v'] -> *3 $3 SET $1 k $1 v"""
    out = [b"*%d\r\n" % len(args)]
    for arg in args:
        out.append(b"$%d\r\n%s\r\n" % (len(arg), arg))
    return b"".join(out)


def load(path, store, chunk_size=1 << 20):
    """Replay the log at `path` into `store`.

    Returns (commands_replayed, ignored_bytes). ignored_bytes is the size of an
    incomplete command found at the very end (usually 0); it is removed from the
    file. Raises AOFError if the file is damaged anywhere else.
    """
    if not os.path.exists(path):
        return 0, 0

    buf = bytearray()
    good = 0       # bytes of the file made of complete, replayed commands
    replayed = 0
    store.loading = True
    try:
        with open(path, "rb") as f:
            while True:
                chunk = f.read(chunk_size)
                if not chunk:
                    break
                buf.extend(chunk)
                while buf:
                    if buf[0:1] != b"*":
                        raise AOFError("bad format at byte %d: expected '*'" % good)
                    try:
                        parsed = resp.parse_command(buf)
                    except resp.ProtocolError as e:
                        raise AOFError("bad format at byte %d: %s" % (good, e))
                    if parsed is None:
                        break  # incomplete: more data may follow, or this is the torn tail
                    args, consumed = parsed
                    del buf[:consumed]
                    if args:
                        reply = commands.execute(store, args)
                        if reply.startswith(b"-"):
                            raise AOFError("command at byte %d failed when replayed (%s): %s" % (
                                good, args[0].decode(errors="replace"), reply[1:].strip().decode(errors="replace")))
                        replayed += 1
                    good += consumed
    finally:
        store.loading = False

    ignored = len(buf)
    if ignored:
        os.truncate(path, good)  # drop the broken tail so new appends start clean
    return replayed, ignored


class AppendOnlyLog:
    """Writes commands to the end of the log file.

    fsync_policy decides how long a write can be lost if the whole machine
    loses power (a mere crash of this program loses nothing either way,
    because the bytes are already handed to the operating system):
        "always"   fsync before every reply. Safest, slowest.
        "everysec" fsync about once a second. Lose at most ~1 second.
        "no"       never fsync; the operating system decides.
    """

    def __init__(self, path, fsync_policy="everysec"):
        if fsync_policy not in ("always", "everysec", "no"):
            raise ValueError("fsync policy must be always, everysec or no")
        self.path = path
        self.policy = fsync_policy
        self.unsynced = False
        # O_BINARY matters on Windows, where files are otherwise opened in text mode
        # and every \n would be rewritten as \r\n, corrupting the log.
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_BINARY", 0)
        self.fd = os.open(path, flags, 0o644)

    def append(self, command_list):
        """Write commands (a list of argument lists). Raises OSError if the
        write fails; the caller must not acknowledge the client in that case."""
        data = memoryview(b"".join(encode_command(c) for c in command_list))
        while data:
            written = os.write(self.fd, data)
            data = data[written:]
        if self.policy == "always":
            os.fsync(self.fd)
        else:
            self.unsynced = True

    async def sync_forever(self):
        """Background task for the "everysec" policy. fsync runs in a thread so
        a slow disk never freezes the event loop."""
        while True:
            await asyncio.sleep(1)
            if self.unsynced:
                self.unsynced = False
                await asyncio.to_thread(os.fsync, self.fd)

    def close(self):
        try:
            os.fsync(self.fd)
        finally:
            os.close(self.fd)
