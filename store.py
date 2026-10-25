"""The database: keys, values, and expiry times.

Expiry works the same way real Redis does it, in two parts:

1. Lazy: every time a key is touched we first check its deadline. If the
   deadline has passed, we delete the key and behave as if it never existed.
2. Active: a background task calls sweep() about once a second to clean out
   expired keys nobody is asking for, so they don't fill memory forever.

Deadlines are stored as unix time in milliseconds (not a monotonic clock) so
that Stage 4 (saving to disk) can write them to a file and still mean the same
thing after a restart.

Rule for the edge case "what if a key expires at the exact moment someone
touches it?": a key is expired when now >= deadline. The check always runs
before the command acts, so the command sees the key as already gone.
"""
import random
import time


class Store:
    def __init__(self, clock=time.time):
        self.data = {}      # key -> value
        self.expires = {}   # key -> deadline in unix milliseconds
        self.clock = clock  # injectable so tests can control time

        # Support for the append-only log (aof.py):
        self.record_writes = False  # True when a log is attached
        self.loading = False        # True while the log is being replayed at startup
        self.pending = []           # changes made but not yet appended to the log

    def now_ms(self):
        return int(self.clock() * 1000)

    # ---- the change log ----

    def emit(self, *args):
        """Record a command that reproduces a change we just made. Commands call
        this with an exact, replayable form (absolute deadlines, never "10 seconds
        from now"). Does nothing unless a log is attached, or while replaying it."""
        if self.record_writes and not self.loading:
            self.pending.append(args)

    def take_pending(self):
        pending, self.pending = self.pending, []
        return pending

    # ---- expiry ----

    def _expire_if_needed(self, key):
        if self.loading:
            # While replaying the log nothing expires: the log describes the past,
            # and an expired key might still be changed by a later logged command.
            return False
        deadline = self.expires.get(key)
        if deadline is not None and self.now_ms() >= deadline:
            self.delete(key)
            # Log the deletion. Otherwise replay would bring the key back, and a
            # later command on a re-created key would act on the wrong old value.
            self.emit(b"DEL", key)
            return True
        return False

    def set_expiry(self, key, deadline_ms):
        """Give an existing key a deadline. Returns False if the key doesn't exist."""
        if self.get(key) is None:
            return False
        self.expires[key] = deadline_ms
        return True

    def ttl_ms(self, key):
        """Milliseconds left. -2 if the key doesn't exist, -1 if it has no expiry."""
        if self.get(key) is None:
            return -2
        deadline = self.expires.get(key)
        if deadline is None:
            return -1
        return max(deadline - self.now_ms(), 0)

    def sweep(self, sample=20, max_rounds=16):
        """Delete some expired keys. Returns how many were removed.

        Looks at a random sample of keys that have deadlines. If more than a
        quarter of the sample was expired there are probably plenty more, so
        go again (up to max_rounds so one sweep can't hog the server).
        """
        removed_total = 0
        for _ in range(max_rounds):
            keys = list(self.expires)
            if not keys:
                break
            picked = random.sample(keys, min(sample, len(keys)))
            removed = sum(1 for k in picked if self._expire_if_needed(k))
            removed_total += removed
            if removed * 4 <= len(picked):
                break
        return removed_total

    # ---- reading and writing ----

    def get(self, key):
        """Return the value, or None if missing or expired."""
        self._expire_if_needed(key)
        return self.data.get(key)

    def exists(self, key):
        return self.get(key) is not None

    def put(self, key, value):
        """Store a value but leave any existing expiry alone (used by INCR)."""
        self._expire_if_needed(key)
        self.data[key] = value

    def set(self, key, value, deadline_ms=None):
        """Store a value and replace the expiry (SET wipes the old TTL)."""
        self.data[key] = value
        if deadline_ms is None:
            self.expires.pop(key, None)
        else:
            self.expires[key] = deadline_ms

    def delete(self, key):
        """Remove a key and its expiry. Returns True if it was there."""
        self.expires.pop(key, None)
        return self.data.pop(key, None) is not None
