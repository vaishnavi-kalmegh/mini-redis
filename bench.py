#!/usr/bin/env python3
"""Benchmark Mini Redis against real Redis using the real redis-benchmark tool.

    python bench.py                 # full run (several minutes), writes docs/benchmark.json + .png
    python bench.py --quick         # one short run, just to check the setup works

Fairness rules:
  * Both servers get the same commands from the same tool, on the same machine.
  * Neither saves to disk in the "no persistence" setup. In the "persistence" setup both
    write an append-only log and fsync once a second.
  * Each server is started fresh for every run, pinned to one CPU core. The benchmark
    tool is pinned to a different core (when the machine has at least two), so the
    two never compete for CPU.
  * Every setup is run several times, alternating which server goes first, and the
    MEDIAN is reported along with the spread.
"""
import argparse
import csv
import io
import json
import os
import platform
import shutil
import socket
import statistics
import subprocess
import sys
import tempfile
import time

from testutil import free_port

TESTS = ["ping_mbulk", "set", "get", "incr", "lpush", "rpush", "lrange_100", "hset"]
LABELS = {"ping_mbulk": "PING", "set": "SET", "get": "GET", "incr": "INCR", "lpush": "LPUSH",
          "rpush": "RPUSH", "lrange_100": "LRANGE 0 99", "hset": "HSET"}
SETUPS = {"none": "no persistence", "aof": "append-only log, fsync every second"}


def parse_csv(text):
    """Turn `redis-benchmark --csv` output into {test: {rps, p50, p99}}."""
    rows = {}
    for row in csv.DictReader(io.StringIO(text)):
        name = row["test"].split()[0].lower()  # "LRANGE_100 (first 100 elements)" -> "lrange_100"
        rows[name] = {"rps": float(row["rps"]),
                      "p50": float(row["p50_latency_ms"]),
                      "p99": float(row["p99_latency_ms"])}
    return rows


def pinned(cmd, core, cores):
    if cores >= 2 and shutil.which("taskset"):
        return ["taskset", "-c", str(core)] + cmd
    return cmd


def server_command(kind, port, setup, workdir):
    if kind == "real":
        cmd = ["redis-server", "--port", str(port), "--save", ""]
        if setup == "aof":
            cmd += ["--appendonly", "yes", "--appendfsync", "everysec", "--dir", workdir]
        else:
            cmd += ["--appendonly", "no"]
        return cmd
    cmd = [sys.executable, "server.py", "--port", str(port)]
    if setup == "aof":
        cmd += ["--aof-file", os.path.join(workdir, "mini.aof"), "--appendfsync", "everysec"]
    return cmd


def start(kind, setup, cores):
    workdir = tempfile.mkdtemp()
    port = free_port()
    proc = subprocess.Popen(pinned(server_command(kind, port, setup, workdir), 0, cores),
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(100):
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            return proc, port, workdir
        except OSError:
            time.sleep(0.1)
    proc.kill()
    raise RuntimeError("%s server did not start" % kind)


def run_benchmark(port, clients, requests, pipeline, cores, tests=TESTS):
    cmd = ["redis-benchmark", "-p", str(port), "-c", str(clients), "-n", str(requests),
           "-r", "100000", "-P", str(pipeline), "-t", ",".join(tests), "--csv"]
    out = subprocess.run(pinned(cmd, 1, cores), capture_output=True, text=True, timeout=1800)
    if out.returncode != 0:
        raise RuntimeError("redis-benchmark failed: " + out.stderr[-500:])
    return parse_csv(out.stdout)


def one_run(kind, setup, args, pipeline, requests, cores):
    proc, port, workdir = start(kind, setup, cores)
    try:
        run_benchmark(port, args.clients, 3000, pipeline, cores)  # warm-up, result thrown away
        return run_benchmark(port, args.clients, requests, pipeline, cores)
    finally:
        proc.terminate()
        proc.wait()
        shutil.rmtree(workdir, ignore_errors=True)


def collect(args):
    cores = os.cpu_count() or 1
    results = {}
    for setup in SETUPS:
        for pipeline in (1, 16):
            requests = args.requests if pipeline == 1 else args.requests_pipelined
            cell = {t: {"real": [], "mini": []} for t in TESTS}
            for run in range(args.runs):
                order = ["real", "mini"] if run % 2 == 0 else ["mini", "real"]  # alternate who goes first
                for kind in order:
                    print("  %-38s P=%-2d run %d/%d  %s" % (SETUPS[setup], pipeline, run + 1, args.runs, kind),
                          flush=True)
                    rows = one_run(kind, setup, args, pipeline, requests, cores)
                    for t in TESTS:
                        cell[t][kind].append(rows[t])
            results["%s/P%d" % (setup, pipeline)] = cell
    return results


def median(runs, field):
    return statistics.median(r[field] for r in runs)


def spread(runs):
    """How far the runs were from each other: (max - min) / median, as a percentage."""
    values = [r["rps"] for r in runs]
    return 100 * (max(values) - min(values)) / statistics.median(values)


def summary(results):
    """The numbers the README quotes: {cell: {test: {...}}}."""
    out = {}
    for cell, tests in results.items():
        out[cell] = {}
        for t, kinds in tests.items():
            real, mini = median(kinds["real"], "rps"), median(kinds["mini"], "rps")
            out[cell][t] = {"real_rps": real, "mini_rps": mini, "percent": 100 * mini / real,
                            "real_p50": median(kinds["real"], "p50"), "mini_p50": median(kinds["mini"], "p50"),
                            "real_p99": median(kinds["real"], "p99"), "mini_p99": median(kinds["mini"], "p99"),
                            "real_spread": spread(kinds["real"]), "mini_spread": spread(kinds["mini"])}
    return out


def markdown_tables(results):
    s = summary(results)
    lines = []
    for setup, setup_name in SETUPS.items():
        for pipeline, what in ((1, "one command at a time per client"), (16, "16 commands sent together")):
            lines.append("**%s, %s (P=%d)**\n" % (setup_name.capitalize(), what, pipeline))
            lines.append("| Command | Real Redis | Mini Redis | Mini as % of real | p50 latency real / mini | Run-to-run spread real / mini |")
            lines.append("|---|---:|---:|---:|---:|---:|")
            for t in TESTS:
                r = s["%s/P%d" % (setup, pipeline)][t]
                lines.append("| %s | %s | %s | %.0f%% | %.2f / %.2f ms | %.0f%% / %.0f%% |" % (
                    LABELS[t], format(round(r["real_rps"]), ","), format(round(r["mini_rps"]), ","),
                    r["percent"], r["real_p50"], r["mini_p50"], r["real_spread"], r["mini_spread"]))
            lines.append("")
    return "\n".join(lines)


def environment(args):
    def version(cmd):
        try:
            return subprocess.run(cmd, capture_output=True, text=True).stdout.strip().split("\n")[0]
        except OSError:
            return "unknown"
    cpu = "unknown"
    try:
        with open("/proc/cpuinfo") as f:
            cpu = next(line.split(":", 1)[1].strip() for line in f if line.startswith("model name"))
    except (OSError, StopIteration):
        pass
    return {"cpu": cpu, "cores": os.cpu_count(), "python": platform.python_version(),
            "redis_server": version(["redis-server", "--version"]).split(" sha=")[0],
            "redis_benchmark": version(["redis-benchmark", "--version"]),
            "kernel": platform.release(), "date": time.strftime("%Y-%m-%d"),
            "runs": args.runs, "clients": args.clients, "requests": args.requests,
            "requests_pipelined": args.requests_pipelined,
            "pinned": bool((os.cpu_count() or 1) >= 2 and shutil.which("taskset"))}


def make_chart(results, path, env):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    s = summary(results)
    colors = {"none": "#0072B2", "aof": "#E69F00"}  # colour-blind safe pair
    fig, axes = plt.subplots(1, 2, figsize=(11.5, 5.2), sharey=True)
    height = 0.38
    for ax, (pipeline, title) in zip(axes, ((1, "One command at a time (P=1)"),
                                            (16, "16 commands sent together (P=16)"))):
        for i, (setup, setup_name) in enumerate(SETUPS.items()):
            values = [s["%s/P%d" % (setup, pipeline)][t]["percent"] for t in TESTS]
            ys = [j + (i - 0.5) * height for j in range(len(TESTS))]
            bars = ax.barh(ys, values, height=height, color=colors[setup], label=setup_name)
            for bar, v in zip(bars, values):
                ax.text(bar.get_width() + 0.8, bar.get_y() + bar.get_height() / 2, "%.0f%%" % v,
                        va="center", fontsize=8.5)
        ax.set_title(title, fontsize=11)
        ax.set_yticks(range(len(TESTS)))
        ax.set_yticklabels([LABELS[t] for t in TESTS])
        ax.set_xlim(0, max(60, ax.get_xlim()[1] * 1.08))
        ax.set_xlabel("Mini Redis speed as % of real Redis (higher is better)")
        ax.grid(axis="x", alpha=0.3)
        ax.set_axisbelow(True)
        for side in ("top", "right"):
            ax.spines[side].set_visible(False)
    axes[0].invert_yaxis()  # the y axis is shared, so flip it once (PING at the top)
    handles, labels = axes[0].get_legend_handles_labels()
    fig.legend(handles, labels, loc="upper center", ncol=2, frameon=False, fontsize=9.5,
               bbox_to_anchor=(0.5, 0.94))
    fig.suptitle("Mini Redis vs real Redis (median of %d runs, %d clients)" % (env["runs"], env["clients"]),
                 fontsize=13, y=0.985)
    fig.text(0.5, 0.01, "%s, %d cores; %s; Python %s; server and benchmark each pinned to their own core"
             % (env["cpu"], env["cores"], env["redis_server"], env["python"]),
             ha="center", fontsize=8, color="#555")
    fig.tight_layout(rect=(0, 0.04, 1, 0.89))
    fig.savefig(path, dpi=140)
    plt.close(fig)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--runs", type=int, default=3, help="runs per setup (default 3)")
    p.add_argument("--clients", type=int, default=50)
    p.add_argument("--requests", type=int, default=50000, help="requests per test when not pipelining")
    p.add_argument("--requests-pipelined", type=int, default=200000, help="requests per test with -P 16")
    p.add_argument("--out", default="docs", help="folder for benchmark.json and benchmark.png")
    p.add_argument("--quick", action="store_true", help="one short run, to check the setup works")
    p.add_argument("--redraw", metavar="JSON",
                   help="don't benchmark: redraw the chart and tables from a saved benchmark.json")
    args = p.parse_args()
    if args.redraw:
        with open(args.redraw) as f:
            saved = json.load(f)
        os.makedirs(args.out, exist_ok=True)
        make_chart(saved["results"], os.path.join(args.out, "benchmark.png"), saved["environment"])
        print(markdown_tables(saved["results"]))
        return
    if args.quick:
        args.runs, args.requests, args.requests_pipelined = 1, 5000, 20000
    for tool in ("redis-server", "redis-benchmark"):
        if not shutil.which(tool):
            sys.exit("%s is not installed; it is needed for a comparison" % tool)

    print("Benchmarking (this takes a while)...", flush=True)
    results = collect(args)
    env = environment(args)
    os.makedirs(args.out, exist_ok=True)
    with open(os.path.join(args.out, "benchmark.json"), "w") as f:
        json.dump({"environment": env, "results": results}, f, indent=1)
    make_chart(results, os.path.join(args.out, "benchmark.png"), env)
    print()
    print(markdown_tables(results))
    print("Wrote %s/benchmark.json and %s/benchmark.png" % (args.out, args.out))


if __name__ == "__main__":
    main()
