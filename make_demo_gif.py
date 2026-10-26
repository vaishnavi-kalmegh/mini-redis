#!/usr/bin/env python3
"""Make docs/demo.gif: an animated terminal demo of Mini Redis.

    python make_demo_gif.py

Nothing is faked. The script starts the real server, runs the real `redis-cli`
commands (including a `kill -9` and a restart), captures what they actually print,
and draws that output as an animated terminal with a short explanation at each
step. Needs: redis-cli, Pillow, and matplotlib (only for its bundled monospace font).
"""
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import textwrap

import matplotlib
from PIL import Image, ImageDraw, ImageFont

REPO = os.path.dirname(os.path.abspath(__file__))
PORT = 6380
OUT = os.path.join(REPO, "docs", "demo.gif")

# ---- look ----
W, H = 900, 580
PAD_X, PAD_Y, BAR = 22, 14, 38
FONT_SIZE, LINE_H = 16, 23
BG, BAR_BG = "#161412", "#262220"
C = {"text": "#e6dfd3", "dim": "#8a8278", "prompt": "#9bc17b", "comment": "#6fb3b8",
     "error": "#e0715f", "number": "#e6b45a", "string": "#a9cf8a", "ok": "#9bc17b"}
FONT_DIR = os.path.join(matplotlib.get_data_path(), "fonts", "ttf")
FONT = ImageFont.truetype(os.path.join(FONT_DIR, "DejaVuSansMono.ttf"), FONT_SIZE)
BOLD = ImageFont.truetype(os.path.join(FONT_DIR, "DejaVuSansMono-Bold.ttf"), FONT_SIZE)
CHAR_W = FONT.getlength("M")
COLS = int((W - 2 * PAD_X) // CHAR_W)
ROWS = (H - BAR - 2 * PAD_Y) // LINE_H


# ---------------------------------------------------------------- capturing the real run

def run_cli(*args):
    r = subprocess.run(["redis-cli", "-p", str(PORT), "--no-raw", *args],
                       capture_output=True, text=True, timeout=15)
    return (r.stdout + r.stderr).rstrip("\n")


def start_server(workdir):
    proc = subprocess.Popen([sys.executable, os.path.join(REPO, "server.py"), "--port", str(PORT),
                             "--aof-file", "demo.aof"], cwd=workdir,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    return proc, "\n".join(proc.stdout.readline().rstrip() for _ in range(2))


def capture():
    """Run the demo for real. Returns a list of steps:
    ("comment", text) | ("cmd", shown_text, real_output) | ("blank",) | ("clear",)"""
    probe = socket.socket()
    probe.settimeout(0.3)
    if probe.connect_ex(("127.0.0.1", PORT)) == 0:
        sys.exit("port %d is already in use; stop whatever is using it first" % PORT)
    probe.close()

    workdir = tempfile.mkdtemp()
    steps, server = [], None
    try:
        def comment(text):
            steps.append(("comment", text))

        def cli(*args):
            steps.append(("cmd", "redis-cli " + " ".join(args), run_cli(*args)))

        def blank():
            steps.append(("blank",))

        comment("1. Start the server. --aof-file makes it save every change to a file")
        server, out = start_server(workdir)
        steps.append(("cmd", "python server.py --port 6380 --aof-file demo.aof &", out))
        blank()
        comment("2. Strings: SET stores a value, GET reads it back")
        cli("PING"); cli("SET", "name", "Vi"); cli("GET", "name")
        blank()
        comment("3. Keys can expire: EX 30 means delete after 30 seconds, TTL shows what is left")
        cli("SET", "session", "abc123", "EX", "30"); cli("TTL", "session")
        steps.append(("clear",))

        comment("4. Counters: INCR adds 1 (a missing key starts at 0)")
        cli("INCR", "visits"); cli("INCR", "visits")
        blank()
        comment("5. Lists: RPUSH adds to the end, LRANGE 0 -1 reads the whole list")
        cli("RPUSH", "queue", "first", "second", "third"); cli("LRANGE", "queue", "0", "-1")
        blank()
        comment("6. Hashes: a small dictionary stored under one key")
        cli("HSET", "user:1", "name", "Vi", "city", "Nagpur"); cli("HGET", "user:1", "city")
        steps.append(("clear",))

        comment("7. Wrong type: 'queue' is a list, so GET is refused (same error as real Redis)")
        cli("GET", "queue")
        blank()
        comment("8. Crash test: kill -9 gives the server no chance to save anything")
        server.kill(); server.wait()
        steps.append(("cmd", "kill -9 %1", ""))
        blank()
        comment("9. Restart: it replays the log, so everything is back")
        server, out = start_server(workdir)
        steps.append(("cmd", "python server.py --port 6380 --aof-file demo.aof &", out))
        cli("INCR", "visits"); cli("LRANGE", "queue", "0", "-1"); cli("TTL", "session")
        steps.append(("clear",))

        comment("10. The log is just the commands, in the format clients send (RESP).")
        comment("    This is the SET name Vi from step 2. *3 = three parts follow,")
        comment("    $3 = the next part is 3 bytes long.")
        shell = "head -n 7 demo.aof | tr -d '\\r'"
        r = subprocess.run(shell, shell=True, cwd=workdir, capture_output=True, text=True)
        steps.append(("cmd", shell, r.stdout.rstrip("\n")))
    finally:
        if server is not None:
            server.kill()
            server.wait()
        shutil.rmtree(workdir, ignore_errors=True)
    return steps


# ---------------------------------------------------------------- turning it into frames

def classify(line):
    """Colour an output line the way a syntax-highlighting terminal would."""
    if line.startswith("(error)"):
        return [(line, "error")]
    if line.startswith("(integer)"):
        return [(line, "number")]
    if line[:1].isdigit() and ") " in line[:5]:  # 1) "first"
        num, rest = line.split(") ", 1)
        return [(num + ") ", "number"), (rest, "string")]
    if line.startswith('"'):
        return [(line, "string")]
    if line in ("OK", "PONG"):
        return [(line, "ok")]
    if line.startswith(("Loaded", "Mini Redis")) or line.startswith(("*", "$")):
        return [(line, "dim")]
    return [(line, "text")]


def wrapped(text):
    return textwrap.wrap(text, COLS - 1, subsequent_indent="#   ") or [""]


def build_specs(steps):
    """The animation as a list of (kind, payload, duration_ms). Cheap to build; the
    pictures are drawn later, one at a time, so memory stays small."""
    specs, lines = [], []

    def snap():
        return tuple(tuple(l) for l in lines)

    def hold(ms, typing=None):
        specs.append(("term", (snap(), typing), ms))

    specs.append(("title", None, 3200))
    for step in steps:
        kind = step[0]
        if kind == "comment":
            for part in wrapped("# " + step[1]):
                lines.append([(part, "comment")])
            hold(900)
        elif kind == "blank":
            lines.append([("", "text")])
        elif kind == "clear":
            hold(1300)
            lines.clear()
            hold(250)
        elif kind == "cmd":
            text, output = step[1], step[2]
            for i in range(2, len(text) + 2, 2):  # typed two characters per frame
                hold(55, typing=text[:i])
            hold(350, typing=text + "​")  # whole command visible, cursor gone
            lines.append([("$ ", "prompt"), (text, "text")])
            if output:
                for out_line in output.split("\n"):
                    lines.append(classify(out_line))
            hold(min(900 + 140 * len(output.split("\n")) if output else 1000, 2600))
    hold(600)
    specs.append(("end", None, 4500))
    return specs


def new_canvas():
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, W, BAR], fill=BAR_BG)
    for i, col in enumerate(("#c0584c", "#d0a54b", "#7fa85a")):
        d.ellipse([16 + i * 22, 12, 28 + i * 22, 24], fill=col)
    title = "mini-redis  —  bash"
    d.text(((W - FONT.getlength(title)) / 2, 10), title, font=FONT, fill=C["dim"])
    return img, d


def draw_line(d, y, segments, typing_cursor=False):
    x = PAD_X
    for text, color in segments:
        d.text((x, y), text, font=FONT, fill=C[color])
        x += FONT.getlength(text)
    if typing_cursor:
        d.rectangle([x + 1, y + 3, x + CHAR_W - 1, y + LINE_H - 4], fill=C["text"])


def render(spec):
    kind, payload, _ = spec
    img, d = new_canvas()
    if kind in ("title", "end"):
        big = ImageFont.truetype(os.path.join(FONT_DIR, "DejaVuSansMono-Bold.ttf"), 44)
        if kind == "title":
            rows = [("Mini Redis", big, "text"),
                    ("a Redis-compatible server written in Python", FONT, "comment"),
                    ("", FONT, "text"),
                    ("The real redis-cli talks to it.", FONT, "dim"),
                    ("Everything on screen is real output.", FONT, "dim")]
        else:
            rows = [("Strings   Expiry   Counters   Lists   Hashes", FONT, "text"),
                    ("Saved to disk, and it survives kill -9", FONT, "comment"),
                    ("", FONT, "text"),
                    ("github.com/vaishnavi-kalmegh/mini-redis", BOLD, "string")]
        total = sum(60 if f is big else LINE_H + 8 for _, f, _ in rows)
        y = BAR + (H - BAR - total) / 2
        for text, f, color in rows:
            d.text(((W - f.getlength(text)) / 2, y), text, font=f, fill=C[color])
            y += 60 if f is big else LINE_H + 8
        return img

    lines, typing = payload
    shown = [list(l) for l in lines]
    if typing is not None:
        cursor = not typing.endswith("​")
        shown.append([("$ ", "prompt"), (typing.rstrip("​"), "text")])
    else:
        cursor = False
    shown = shown[-ROWS:]  # scroll when the screen is full
    y = BAR + PAD_Y
    for i, segments in enumerate(shown):
        draw_line(d, y, segments, typing_cursor=cursor and i == len(shown) - 1)
        y += LINE_H
    return img


def make_palette(specs):
    """One shared palette for the whole GIF: it keeps the colours steady from frame
    to frame and lets only the changed part of each frame be stored (small file)."""
    picks = [specs[i] for i in range(0, len(specs), max(1, len(specs) // 14))] + [specs[0], specs[-1]]
    montage = Image.new("RGB", (W, H * len(picks)))
    for i, spec in enumerate(picks):
        montage.paste(render(spec), (0, i * H))
    return montage.quantize(colors=48, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE)


def main():
    if not shutil.which("redis-cli"):
        sys.exit("redis-cli is not installed")
    print("Running the real demo...", flush=True)
    steps = capture()
    longest = max(len(l) for s in steps if s[0] == "cmd" for l in [s[1]] + s[2].split("\n"))
    assert longest <= COLS, "a line is %d characters but only %d fit" % (longest, COLS)

    specs = build_specs(steps)
    print("Drawing %d frames..." % len(specs), flush=True)
    palette = make_palette(specs)
    frames = (render(s).quantize(palette=palette, dither=Image.Dither.NONE) for s in specs)
    first = next(frames)
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    first.save(OUT, save_all=True, append_images=frames, duration=[s[2] for s in specs],
               loop=0, optimize=False, disposal=1)
    size = os.path.getsize(OUT)
    print("Wrote %s (%.0f KB, %d frames, %.0f seconds)" % (OUT, size / 1024, len(specs),
                                                          sum(s[2] for s in specs) / 1000))


if __name__ == "__main__":
    main()
