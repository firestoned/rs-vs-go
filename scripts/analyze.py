#!/usr/bin/env python3
"""Summarize a cmbench run.

Usage: python3 scripts/analyze.py results/<run-id>   (or results/latest)

Standard library only for the numbers. If matplotlib is importable, plots are also written.

Every rate is a counter delta between consecutive samples, attributed to the phase label of
the later sample. Idle windows: idle_pre (before churn), post_near (0-10 min after churn),
post_mid (10-30 min after), post_late (30-60 min after). Invalid trials are reported and
excluded.

Outputs, all in the run directory:
  summary.md          the report
  metrics.csv         data dictionary: every metric key with label, unit, source and meaning
  summary_trials.csv  one row per impl, trial and metric (long format)
  summary_stats.csv   per impl and metric: n, mean, 95% t-interval, sd, min, max
  comparisons.csv     per metric: rust - go and rust / go with bootstrap 95% intervals
  timeseries.csv      per trial, BIN_S-second bins aligned on the start of churn
  threads.csv         per trial, window and OS thread: wakeups/s and CPU time
  rtm.csv             runtime and allocator internals per trial and window
  fairness.csv        per trial: START line, worker count, image, host CPU frequency policy
  plots/              PNGs, if matplotlib is available
"""
from __future__ import annotations

import bisect
import csv
import os
import random
import statistics as st
import sys
from collections import defaultdict
from pathlib import Path

PHASES = ["startup", "warmup", "idle_pre", "create", "delete", "post_near", "post_mid", "post_late"]
IDLE_WINDOWS = ["idle_pre", "post_near", "post_mid", "post_late"]
WINDOW_TITLES = {"idle_pre": "Idle BEFORE churn (baseline)",
                 "post_near": "Idle RIGHT AFTER churn (0-10 min)",
                 "post_mid": "Idle 10-30 MIN after churn",
                 "post_late": "Idle 30-60 MIN after churn"}
MIB = 1024 * 1024
BIN_S = int(os.environ.get("BIN_S", "60"))
COUNTERS = ["cpu_usage_usec", "cpu_user_usec", "cpu_system_usec", "throttled_usec", "vol_cs",
            "nonvol_cs", "pgfault", "pgmajfault", "sched_run_ns", "sched_wait_ns", "timeslices",
            "net_rx_bytes", "net_rx_packets", "net_tx_bytes", "net_tx_packets"]
T95 = {1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571, 6: 2.447, 7: 2.365, 8: 2.306,
       9: 2.262, 10: 2.228, 12: 2.179, 15: 2.131, 20: 2.086, 25: 2.060, 30: 2.042}

# Per-window metrics: key suffix, label, unit, source, meaning.
WINDOW_METRICS = [
    ("millicores", "CPU", "millicores", "cgroup cpu.stat usage_usec",
     "Mean CPU use over the window. 1 millicore = 1 ms of CPU time per second."),
    ("cpu_ms_per_min", "CPU time", "ms/min", "cgroup cpu.stat usage_usec", "Same as CPU, per minute."),
    ("user_pct", "User-mode share of CPU", "%", "cgroup cpu.stat user_usec / usage_usec",
     "Remainder is kernel time (syscalls, page faults, network stack)."),
    ("perf_cycles_per_s", "CPU cycles", "cycles/s", "perf stat cycles (hardware PMU)",
     "CPU work independent of clock frequency."),
    ("perf_instructions_per_s", "Instructions retired", "instructions/s", "perf stat instructions",
     "Instructions executed by the watcher's threads."),
    ("perf_ipc", "Instructions per cycle", "ratio", "perf stat instructions / cycles",
     "Low IPC in idle code usually means cold caches after each wakeup."),
    ("effective_ghz", "Effective clock while running", "GHz", "perf cycles / cgroup CPU time",
     "Clock rate the watcher's own work actually ran at. Shows the CPU frequency policy's effect."),
    ("wakeups_per_s", "Thread wakeups", "1/s", "sum of /proc/<pid>/task/*/schedstat timeslices",
     "How often any thread of the process was put on a CPU."),
    ("vol_cs_per_s", "Voluntary context switches (main thread only)", "1/s", "/proc/<pid>/status",
     "Main thread only, so not comparable across runtimes: Go's main thread parks while other "
     "threads do the work. Use Thread wakeups for the whole process."),
    ("nonvol_cs_per_s", "Involuntary context switches (main thread only)", "1/s", "/proc/<pid>/status",
     "Main thread only, as above. Preemptions, mostly noise from other work on the node."),
    ("sched_wait_ms_per_min", "Run-queue wait", "ms/min", "schedstat wait time",
     "Time runnable but waiting for a CPU. A noise indicator, not the watcher's cost."),
    ("active_pct", "Sample intervals with any CPU use", "%", "CPU delta per sampler tick",
     "Duty cycle at the sampler's resolution (default 100 ms)."),
    ("bursts_per_min", "Activity bursts", "1/min", "CPU delta per sampler tick",
     "Transitions from an idle tick to an active tick."),
    ("pgfault_per_s", "Page faults", "1/s", "cgroup memory.stat pgfault",
     "Memory being touched for the first time, or returned to the kernel and touched again."),
    ("anon_mean_mib", "Anonymous memory", "MiB", "cgroup memory.stat anon", "Heap and stacks."),
    ("anon_slope_mib_per_h", "Anonymous memory trend", "MiB/h", "least-squares slope of anon",
     "Drift within the window. Near 0 means the window is stationary."),
    ("rss_mean_mib", "Resident set size", "MiB", "/proc/<pid>/status VmRSS",
     "Includes the mapped binary and shared libraries."),
    ("mem_mean_mib", "memory.current", "MiB", "cgroup memory.current",
     "What the kubelet and the OOM killer account against the limit."),
    ("threads_max", "OS threads", "count", "/proc/<pid>/status Threads", "Kernel threads in the process."),
    ("threads_waking", "Threads that woke", "count", "threads.csv (1 s resolution)",
     "Distinct OS threads that were scheduled at least once in the window."),
    ("top_thread_share_pct", "Wakeups on the busiest thread", "%", "threads.csv",
     "Share of all wakeups taken by the single most active thread."),
    ("net_rx_pkts_per_min", "Network packets received", "1/min", "/proc/<pid>/net/dev (pod netns, no lo)",
     "API server traffic: watch events, bookmarks, TCP ACKs, keepalives."),
    ("net_tx_pkts_per_min", "Network packets sent", "1/min", "/proc/<pid>/net/dev (pod netns, no lo)", ""),
    ("net_rx_bytes_per_s", "Network bytes received", "B/s", "/proc/<pid>/net/dev (pod netns, no lo)", ""),
    ("net_tx_bytes_per_s", "Network bytes sent", "B/s", "/proc/<pid>/net/dev (pod netns, no lo)", ""),
    ("throttled_ms", "CFS throttled time", "ms", "cgroup cpu.stat throttled_usec", "Should be 0."),
    ("load1_max", "Node load1 max", "load", "/proc/loadavg on the node", "Noise check."),
]
DELTA_METRICS = ["millicores", "perf_cycles_per_s", "wakeups_per_s", "pgfault_per_s", "active_pct",
                 "anon_mean_mib", "rss_mean_mib", "mem_mean_mib", "net_rx_pkts_per_min"]
CHURN_METRICS = [
    ("startup.cpu_ms", "Startup CPU until first sync", "ms", "cgroup cpu.stat", "Process start to SYNCED."),
    ("sync_ms", "Time to first sync", "ms", "watcher SYNCED line", "Wall time from start to cache synced."),
    ("create.cpu_us_per_event", "Net CPU per ADD event", "us", "cgroup cpu.stat",
     "Create-phase CPU minus the trial's own idle baseline, divided by CM_COUNT."),
    ("delete.cpu_us_per_event", "Net CPU per DELETE event", "us", "cgroup cpu.stat", "As above, for deletes."),
    ("create.cycles_per_event", "Net cycles per ADD event", "cycles", "perf stat cycles",
     "Create-phase cycles minus the idle baseline rate, divided by CM_COUNT."),
    ("delete.cycles_per_event", "Net cycles per DELETE event", "cycles", "perf stat cycles", "As above, for deletes."),
    ("create.net_rx_bytes_per_event", "Bytes received per ADD event", "B", "/proc/<pid>/net/dev",
     "Wire size check: similar values mean the same encoding on both sides."),
    ("create.mem_max_mib", "Peak memory.current during create", "MiB", "cgroup memory.current", ""),
    ("churn.anon_growth_mib", "anon growth during churn", "MiB", "cgroup memory.stat anon",
     "Peak anon during create minus the idle baseline mean."),
    ("run.hwm_mib", "Process RSS high-water mark", "MiB", "/proc/<pid>/status VmHWM", ""),
    ("run.mem_peak_mib", "cgroup memory.peak", "MiB", "cgroup memory.peak", ""),
]
SCALE_WINDOWS = ["idle_pre", "post_late"]


# ---------------------------------------------------------------- loading
def num(v):
    if v in ("", "NA", None):
        return None
    try:
        return float(v)
    except ValueError:
        return v


def load_csv(path: Path, keep_str=("phase", "comm")) -> list[dict]:
    if not path.exists():
        return []
    lines = [ln for ln in path.read_text().splitlines() if ln and not ln.startswith("#")]
    if not lines:
        return []
    return [{k: (v if k in keep_str else num(v)) for k, v in r.items()} for r in csv.DictReader(lines)]


def load_kv(path: Path) -> dict:
    out = {}
    if not path.exists():
        return out
    for ln in path.read_text().splitlines():
        for tok in ([ln] if ln.startswith(("start_line=", "loadavg=", "vms_running=", "cur_mhz_hist=",
                                           "governors_all=")) else ln.split()):
            if "=" in tok:
                k, v = tok.split("=", 1)
                out[k] = v.strip()
    return out


def load_rtm(path: Path) -> list[dict]:
    out = []
    if not path.exists():
        return out
    for ln in path.read_text().splitlines():
        if not ln.startswith("RTM "):
            continue
        d = {}
        for tok in ln.split()[1:]:
            if "=" in tok:
                k, v = tok.split("=", 1)
                d[k] = num(v)
        if isinstance(d.get("ts"), float):
            out.append(d)
    return out


def load_perf(tdir: Path) -> list[tuple[float, str, float | None]]:
    start_f, data_f = tdir / "perf.start", tdir / "perf.csv"
    if not (start_f.exists() and data_f.exists()):
        return []
    start = float(start_f.read_text().strip())
    rows = []
    for ln in data_f.read_text().splitlines():
        if not ln.strip() or ln.startswith("#"):
            continue
        f = ln.split(",")
        if len(f) < 4:
            continue
        try:
            t = float(f[0])
        except ValueError:
            continue
        try:
            val = float(f[1])
        except ValueError:
            val = None  # <not supported> / <not counted>
        rows.append((start + t, f[3], val))
    return rows


# ---------------------------------------------------------------- stats helpers
def pct(values: list[float], q: float) -> float:
    s = sorted(values)
    if not s:
        return float("nan")
    idx = (len(s) - 1) * q
    lo, hi = int(idx), min(int(idx) + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (idx - lo)


def slope_per_hour(xs: list[float], ys: list[float]) -> float:
    if len(xs) < 3:
        return float("nan")
    mx, my = st.fmean(xs), st.fmean(ys)
    den = sum((x - mx) ** 2 for x in xs)
    return float("nan") if den == 0 else sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / den * 3600


def t95(df: int) -> float:
    keys = [k for k in T95 if k <= df]
    return T95[max(keys)] if keys else float("nan")


def ci95(vals: list[float]) -> tuple[float, float, float]:
    mean = st.fmean(vals)
    if len(vals) < 2:
        return mean, float("nan"), float("nan")
    half = t95(len(vals) - 1) * st.stdev(vals) / len(vals) ** 0.5
    return mean, mean - half, mean + half


def boot(a: list[float], b: list[float], fn, iters: int = 10000, seed: int = 42):
    if not a or not b:
        return float("nan"), float("nan"), float("nan")
    rng = random.Random(seed)
    out = []
    for _ in range(iters):
        v = fn(st.fmean(rng.choices(a, k=len(a))), st.fmean(rng.choices(b, k=len(b))))
        if v == v:
            out.append(v)
    point = fn(st.fmean(a), st.fmean(b))
    return point, pct(out, 0.025), pct(out, 0.975)


def diff(x, y):
    return x - y


def ratio(x, y):
    return x / y if y else float("nan")


def fmt(v) -> str:
    if v is None or v != v:
        return "n/a"
    a = abs(v)
    if a >= 1e6:
        return f"{v:.3g}"
    return f"{v:.4f}" if a < 1 else f"{v:.3f}" if a < 10 else f"{v:.1f}"


# ---------------------------------------------------------------- per-trial metrics
def phase_at(rows_ts: list[float], rows: list[dict], t: float) -> str:
    i = min(bisect.bisect_left(rows_ts, t), len(rows) - 1)
    return rows[i]["phase"]


def thread_metrics(threads: list[dict], rows: list[dict]) -> tuple[dict, list[dict]]:
    """Per window and OS thread: wakeups/s and CPU ms/min, from 1 s per-thread snapshots."""
    if not threads:
        return {}, []
    ts_list = [r["ts"] for r in rows]
    by_tid = defaultdict(list)
    for r in threads:
        if isinstance(r.get("ts"), float):
            by_tid[int(r["tid"])].append(r)
    acc = defaultdict(lambda: defaultdict(lambda: [0.0, 0.0, 0.0, "?"]))  # ph -> tid -> [slices, run_ns, dur, comm]
    for tid, rs in by_tid.items():
        rs.sort(key=lambda r: r["ts"])
        for prev, cur in zip(rs, rs[1:]):
            ph = phase_at(ts_list, rows, cur["ts"])
            a = acc[ph][tid]
            a[0] += max(0.0, cur["timeslices"] - prev["timeslices"])
            a[1] += max(0.0, cur["run_ns"] - prev["run_ns"])
            a[2] += cur["ts"] - prev["ts"]
            a[3] = cur.get("comm") or a[3]
    m, out = {}, []
    for ph, tids in acc.items():
        dur = max((v[2] for v in tids.values()), default=0)
        if dur <= 0:
            continue
        total = sum(v[0] for v in tids.values())
        m[f"{ph}.threads_waking"] = sum(1 for v in tids.values() if v[0] > 0)
        m[f"{ph}.top_thread_share_pct"] = 100 * max(v[0] for v in tids.values()) / total if total else 0.0
        for tid, (sl, run, d, comm) in sorted(tids.items()):
            out.append({"phase": ph, "tid": tid, "comm": comm, "wakeups_per_s": sl / dur,
                        "cpu_ms_per_min": run / 1e6 / (dur / 60), "observed_s": d})
    return m, out


def trial_metrics(rows, cm_count, rtm, snaps, perf, threads):
    acc = defaultdict(lambda: defaultdict(float))
    series = defaultdict(lambda: defaultdict(list))
    prev_active = False
    for prev, cur in zip(rows, rows[1:]):
        ph, a = cur["phase"], acc[cur["phase"]]
        a["duration_s"] += cur["ts"] - prev["ts"]
        for c in COUNTERS:
            a[c] += max(0.0, (cur[c] or 0) - (prev[c] or 0))
        active = (cur["cpu_usage_usec"] - prev["cpu_usage_usec"]) > 0
        a["intervals"] += 1
        a["active"] += active
        a["bursts"] += active and not prev_active
        prev_active = active
    for r in rows:
        s = series[r["phase"]]
        s["ts"].append(r["ts"])
        s["mem"].append(r["mem_current"])
        s["anon"].append(r["anon"])
        s["rss"].append(r["rss_kb"] * 1024)
        s["threads"].append(r["threads"])
        s["load1"].append(r["load1"])

    m: dict[str, float] = {}
    startup_rows = [r for r in rows if r["phase"] == "startup"]
    if startup_rows:
        m["startup.cpu_ms"] = startup_rows[-1]["cpu_usage_usec"] / 1000
    for ph in PHASES:
        a, s = acc.get(ph), series.get(ph)
        if not s:
            continue
        if a and a["duration_s"] > 0:
            d, mins = a["duration_s"], a["duration_s"] / 60
            m[f"{ph}.duration_s"] = d
            m[f"{ph}.cpu_ms"] = a["cpu_usage_usec"] / 1000
            m[f"{ph}.millicores"] = a["cpu_usage_usec"] / d / 1000
            m[f"{ph}.cpu_ms_per_min"] = a["cpu_usage_usec"] / 1000 / mins
            m[f"{ph}.user_pct"] = 100 * a["cpu_user_usec"] / a["cpu_usage_usec"] if a["cpu_usage_usec"] else float("nan")
            m[f"{ph}.throttled_ms"] = a["throttled_usec"] / 1000
            m[f"{ph}.wakeups_per_s"] = a["timeslices"] / d
            m[f"{ph}.vol_cs_per_s"] = a["vol_cs"] / d
            m[f"{ph}.nonvol_cs_per_s"] = a["nonvol_cs"] / d
            m[f"{ph}.sched_wait_ms_per_min"] = a["sched_wait_ns"] / 1e6 / mins
            m[f"{ph}.pgfault_per_s"] = a["pgfault"] / d
            m[f"{ph}.pgmajfault"] = a["pgmajfault"]
            m[f"{ph}.net_rx_pkts_per_min"] = a["net_rx_packets"] / mins
            m[f"{ph}.net_tx_pkts_per_min"] = a["net_tx_packets"] / mins
            m[f"{ph}.net_rx_bytes_per_s"] = a["net_rx_bytes"] / d
            m[f"{ph}.net_tx_bytes_per_s"] = a["net_tx_bytes"] / d
            m[f"{ph}.net_rx_bytes"] = a["net_rx_bytes"]
            m[f"{ph}.active_pct"] = 100 * a["active"] / a["intervals"]
            m[f"{ph}.bursts_per_min"] = a["bursts"] / mins
        m[f"{ph}.mem_mean_mib"] = st.fmean(s["mem"]) / MIB
        m[f"{ph}.mem_max_mib"] = max(s["mem"]) / MIB
        m[f"{ph}.anon_mean_mib"] = st.fmean(s["anon"]) / MIB
        m[f"{ph}.anon_max_mib"] = max(s["anon"]) / MIB
        m[f"{ph}.anon_slope_mib_per_h"] = slope_per_hour(s["ts"], [x / MIB for x in s["anon"]])
        m[f"{ph}.rss_mean_mib"] = st.fmean(s["rss"]) / MIB
        m[f"{ph}.threads_max"] = max(s["threads"])
        m[f"{ph}.load1_max"] = max(s["load1"])

    # Hardware counters, attributed to the phase of the sample closest in time.
    ts_list = [r["ts"] for r in rows]
    perf_bins = defaultdict(lambda: defaultdict(float))
    create_ts = next((r["ts"] for r in rows if r["phase"] == "create"), rows[0]["ts"])
    if perf:
        sums = defaultdict(lambda: defaultdict(float))
        for t, ev, val in perf:
            if val is None:
                continue
            sums[phase_at(ts_list, rows, t)][ev] += val
            perf_bins[int((t - create_ts) // BIN_S)][ev] += val
        for ph, evs in sums.items():
            d = m.get(f"{ph}.duration_s")
            if not d:
                continue
            for ev, total in evs.items():
                m[f"{ph}.perf_{ev.replace('-', '_')}_per_s"] = total / d
            m[f"{ph}.perf_cycles"] = evs.get("cycles", float("nan"))
            if evs.get("cycles"):
                m[f"{ph}.perf_ipc"] = evs.get("instructions", 0) / evs["cycles"]
                cpu_ms = m.get(f"{ph}.cpu_ms")
                if cpu_ms:
                    m[f"{ph}.effective_ghz"] = evs["cycles"] / (cpu_ms * 1e6)

    # Churn cost above this trial's own pre-churn baseline.
    base = m.get("idle_pre.millicores")
    base_cyc = m.get("idle_pre.perf_cycles_per_s")
    for ph in ("create", "delete"):
        d = m.get(f"{ph}.duration_s")
        if base is not None and d:
            m[f"{ph}.cpu_us_per_event"] = (m[f"{ph}.cpu_ms"] - base * d) * 1000 / cm_count
        if base_cyc is not None and d and m.get(f"{ph}.perf_cycles") == m.get(f"{ph}.perf_cycles"):
            m[f"{ph}.cycles_per_event"] = (m[f"{ph}.perf_cycles"] - base_cyc * d) / cm_count
        if f"{ph}.net_rx_bytes" in m:
            m[f"{ph}.net_rx_bytes_per_event"] = m[f"{ph}.net_rx_bytes"] / cm_count
    if "idle_pre.anon_mean_mib" in m and "create.anon_max_mib" in m:
        m["churn.anon_growth_mib"] = m["create.anon_max_mib"] - m["idle_pre.anon_mean_mib"]

    tm, thread_rows = thread_metrics(threads, rows)
    m.update(tm)

    # Paired, within-trial change of each idle window versus the pre-churn baseline.
    for w in IDLE_WINDOWS[1:]:
        for k in DELTA_METRICS:
            if f"{w}.{k}" in m and f"idle_pre.{k}" in m:
                m[f"delta_{w}.{k}"] = m[f"{w}.{k}"] - m[f"idle_pre.{k}"]
    m["run.hwm_mib"] = rows[-1]["hwm_kb"] * 1024 / MIB
    if rows[-1].get("mem_peak") is not None:
        m["run.mem_peak_mib"] = rows[-1]["mem_peak"] / MIB

    # Runtime / allocator internals: delta between the snapshot that opened a phase and the next.
    rtm_rows = []
    snap_ts = [s["ts"] for s in snaps]
    for a, b in zip(rtm, rtm[1:]):
        i = bisect.bisect_right(snap_ts, a["ts"] + 1.0) - 1
        if i < 0:
            continue
        ph, hours = snaps[i]["phase"], (b["ts"] - a["ts"]) / 3600
        for k, v in a.items():
            if k in ("ts", "impl") or not isinstance(v, float) or not isinstance(b.get(k), float):
                continue
            rtm_rows.append({"phase": ph, "key": k, "start": v, "end": b[k], "delta": b[k] - v,
                             "hours": hours, "per_hour": (b[k] - v) / hours if hours > 0 else float("nan")})

    # Time bins aligned on the start of churn, so bins line up across trials. Each bin's rate is
    # the counter delta from the previous bin's last sample to this bin's last sample.
    buckets = defaultdict(list)
    for r in rows:
        buckets[int((r["ts"] - create_ts) // BIN_S)].append(r)
    bins, prev_last = [], None
    for b in sorted(buckets):
        rs = buckets[b]
        first = prev_last or rs[0]
        last = rs[-1]
        prev_last = last
        d = last["ts"] - first["ts"]
        if d <= 0:
            continue
        rate = lambda c: (last[c] - first[c]) / d  # noqa: E731
        phases = [r["phase"] for r in rs]
        pb = perf_bins.get(b, {})
        bins.append({
            "bin": b, "min_since_churn": b * BIN_S / 60,
            "min_since_start": (last["ts"] - rows[0]["ts"]) / 60,
            "phase": max(set(phases), key=phases.count),
            "millicores": rate("cpu_usage_usec") / 1000,
            "user_millicores": rate("cpu_user_usec") / 1000,
            "system_millicores": rate("cpu_system_usec") / 1000,
            "cycles_per_s": (pb["cycles"] / BIN_S) if "cycles" in pb else "",
            "instructions_per_s": (pb["instructions"] / BIN_S) if "instructions" in pb else "",
            "wakeups_per_s": rate("timeslices"),
            "vol_cs_per_s": rate("vol_cs"),
            "nonvol_cs_per_s": rate("nonvol_cs"),
            "pgfault_per_s": rate("pgfault"),
            "net_rx_pkts_per_min": rate("net_rx_packets") * 60,
            "net_tx_pkts_per_min": rate("net_tx_packets") * 60,
            "anon_mib": st.fmean(r["anon"] for r in rs) / MIB,
            "rss_mib": st.fmean(r["rss_kb"] for r in rs) / 1024,
            "mem_mib": st.fmean(r["mem_current"] for r in rs) / MIB,
            "threads": max(r["threads"] for r in rs),
            "load1": max(r["load1"] for r in rs),
        })
    return m, bins, rtm_rows, thread_rows


# ---------------------------------------------------------------- report helpers
def values(per_impl, impl, key):
    return [m[key] for m in per_impl[impl] if key in m and m[key] == m[key]]


def stats_row(vals):
    mean, lo, hi = ci95(vals)
    return {"n": len(vals), "mean": mean, "ci_lo": lo, "ci_hi": hi,
            "sd": st.stdev(vals) if len(vals) > 1 else float("nan"), "min": min(vals), "max": max(vals)}


def table(md, per_impl, impls, specs, prefix="", show_ratio=True, stats_out=None, cmp_out=None, section=""):
    both = {"rs", "go"} <= set(impls)
    extra = (" | rs - go [95% CI]" + (" | rs / go [95% CI]" if show_ratio else "") + " |") if both else " |"
    md.append("| Metric | unit | " + " | ".join(impls) + extra)
    md.append("|---|---|" + "---|" * len(impls) + (("---|---|" if show_ratio else "---|") if both else ""))
    for key, label, unit, *_ in specs:
        full = prefix + key
        vals = {i: values(per_impl, i, full) for i in impls}
        if not any(vals.values()):
            continue
        cells = []
        for i in impls:
            if vals[i]:
                s = stats_row(vals[i])
                cells.append(f"{fmt(s['mean'])} [{fmt(s['ci_lo'])}, {fmt(s['ci_hi'])}]")
                if stats_out is not None:
                    stats_out.append({"section": section, "metric": full, "label": label, "unit": unit,
                                      "impl": i, **s})
            else:
                cells.append("n/a")
        row = f"| {label} | {unit} | " + " | ".join(cells) + " |"
        if both and vals["rs"] and vals["go"]:
            d, dlo, dhi = boot(vals["rs"], vals["go"], diff)
            r, rlo, rhi = boot(vals["rs"], vals["go"], ratio)
            row += f" {fmt(d)} [{fmt(dlo)}, {fmt(dhi)}] |"
            if show_ratio:
                row += f" {fmt(r)} [{fmt(rlo)}, {fmt(rhi)}] |"
            if cmp_out is not None:
                cmp_out.append({"section": section, "metric": full, "label": label, "unit": unit,
                                "rs_mean": st.fmean(vals["rs"]), "go_mean": st.fmean(vals["go"]),
                                "diff_rs_minus_go": d, "diff_ci_lo": dlo, "diff_ci_hi": dhi,
                                "ratio_rs_over_go": r, "ratio_ci_lo": rlo, "ratio_ci_hi": rhi,
                                "diff_excludes_zero": (dlo > 0 or dhi < 0) if dlo == dlo else ""})
        elif both:
            row += " n/a |" + (" n/a |" if show_ratio else "")
        md.append(row)
    md.append("")


def write_csv(path: Path, rows: list[dict], fields: list[str] | None = None):
    fields = fields or (list(rows[0].keys()) if rows else [])
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


# ---------------------------------------------------------------- plots
def maybe_plot(out: Path, ts_rows: list[dict], per_impl, impls, thread_rows, rtm_rows) -> None:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("matplotlib not available, skipping plots")
        return
    plots = out / "plots"
    plots.mkdir(exist_ok=True)
    colors = {"rs": "#d9730d", "go": "#1f6fb2"}
    names = {"rs": "Rust (kube-rs)", "go": "Go (client-go)"}

    # 1. Timelines aligned on churn: faint line per trial, bold mean per implementation.
    phase_marks = {}
    for r in ts_rows:
        phase_marks.setdefault(r["phase"], r["min_since_churn"])
    fields = [("millicores", "CPU (millicores)", True), ("cycles_per_s", "CPU cycles per second", True),
              ("wakeups_per_s", "Thread wakeups per second", True),
              ("pgfault_per_s", "Page faults per second", True),
              ("net_rx_pkts_per_min", "Packets received per minute", True),
              ("anon_mib", "Anonymous memory (MiB)", False), ("rss_mib", "RSS (MiB)", False),
              ("mem_mib", "memory.current (MiB)", False), ("threads", "OS threads", False)]
    for field, label, log in fields:
        if not any(r[field] != "" for r in ts_rows):
            continue
        fig, ax = plt.subplots(figsize=(13, 4.8))
        for impl in impls:
            by_trial = defaultdict(list)
            for r in ts_rows:
                if r["impl"] == impl and r[field] != "":
                    by_trial[r["trial"]].append((r["min_since_churn"], r[field]))
            for pts in by_trial.values():
                ax.plot(*zip(*pts), color=colors.get(impl), alpha=0.25, lw=0.7)
            agg = defaultdict(list)
            for pts in by_trial.values():
                for x, y in pts:
                    agg[x].append(y)
            xs = sorted(x for x in agg if len(agg[x]) == len(by_trial))
            if xs:
                ax.plot(xs, [st.fmean(agg[x]) for x in xs], color=colors.get(impl), lw=1.8,
                        label=f"{names.get(impl, impl)}, mean of {len(by_trial)}")
        for ph, x in phase_marks.items():
            ax.axvline(x, color="grey", lw=0.6, ls=":")
            ax.text(x, 1.01, ph, transform=ax.get_xaxis_transform(), fontsize=8, rotation=25)
        if log:
            ax.set_yscale("symlog", linthresh=0.01 if field != "cycles_per_s" else 1e4)
        ax.set_xlabel(f"minutes relative to start of churn ({BIN_S} s bins)")
        ax.set_ylabel(label)
        ax.set_title(label, pad=30)
        ax.legend(loc="upper left", fontsize=8)
        ax.grid(alpha=0.2)
        fig.tight_layout()
        fig.savefig(plots / f"timeline_{field}.png", dpi=130)
        plt.close(fig)

    # 2. Per-window means with 95% intervals, one chart per rubric.
    for key, label, unit, *_ in WINDOW_METRICS:
        data = {i: [values(per_impl, i, f"{w}.{key}") for w in IDLE_WINDOWS] for i in impls}
        if not any(v for i in impls for v in data[i]):
            continue
        fig, ax = plt.subplots(figsize=(8, 4.2))
        width = 0.8 / max(1, len(impls))
        for j, impl in enumerate(impls):
            means, errs, xs = [], [[], []], []
            for k, vals in enumerate(data[impl]):
                if not vals:
                    continue
                mean, lo, hi = ci95(vals)
                xs.append(k + (j - (len(impls) - 1) / 2) * width)
                means.append(mean)
                errs[0].append(mean - lo if lo == lo else 0)
                errs[1].append(hi - mean if hi == hi else 0)
                ax.scatter([xs[-1]] * len(vals), vals, color="black", s=8, zorder=3)
            ax.bar(xs, means, width, yerr=errs, capsize=3, color=colors.get(impl), alpha=0.85,
                   label=names.get(impl, impl))
        ax.set_xticks(range(len(IDLE_WINDOWS)), IDLE_WINDOWS)
        ax.set_ylabel(f"{label} ({unit})")
        ax.set_title(f"{label}: mean, 95% CI, dots = trials")
        ax.legend(fontsize=8)
        ax.grid(axis="y", alpha=0.2)
        fig.tight_layout()
        fig.savefig(plots / f"window_{key}.png", dpi=130)
        plt.close(fig)

    # 3. Wakeups per thread name, per window.
    if thread_rows:
        agg = defaultdict(lambda: defaultdict(list))
        for r in thread_rows:
            if r["phase"] in IDLE_WINDOWS:
                agg[(r["impl"], r["phase"])][r["comm"]].append(r["wakeups_per_s"])
        fig, ax = plt.subplots(figsize=(10, 4.5))
        labels_x, bottoms = [], {}
        comms = sorted({c for d in agg.values() for c in d})
        cmap = plt.get_cmap("tab10")
        x = 0
        for impl in impls:
            for w in IDLE_WINDOWS:
                labels_x.append(f"{impl}\n{w}")
                bottom = 0.0
                for ci, c in enumerate(comms):
                    vals = agg[(impl, w)].get(c, [])
                    trials = len({r["trial"] for r in thread_rows if r["impl"] == impl and r["phase"] == w}) or 1
                    v = sum(vals) / trials
                    if v:
                        ax.bar(x, v, bottom=bottom, color=cmap(ci % 10), label=c if c not in bottoms else None)
                        bottoms[c] = True
                        bottom += v
                x += 1
        ax.set_xticks(range(len(labels_x)), labels_x, fontsize=8)
        ax.set_ylabel("wakeups per second (mean per trial)")
        ax.set_title("Wakeups by OS thread name")
        ax.legend(fontsize=8)
        fig.tight_layout()
        fig.savefig(plots / "threads_wakeups.png", dpi=130)
        plt.close(fig)

    # 4. Go GC cycles per hour by window (Go only: the Rust side has no collector).
    gc = defaultdict(list)
    for r in rtm_rows:
        if r["impl"] == "go" and r["key"] == "gc_cycles_total_gc_cycles" and r["phase"] in IDLE_WINDOWS:
            gc[r["phase"]].append(r["per_hour"])
    if gc:
        fig, ax = plt.subplots(figsize=(7, 3.8))
        ws = [w for w in IDLE_WINDOWS if w in gc]
        ax.bar(ws, [st.fmean(gc[w]) for w in ws], color=colors["go"])
        for k, w in enumerate(ws):
            ax.scatter([k] * len(gc[w]), gc[w], color="black", s=8, zorder=3)
        ax.set_ylabel("GC cycles per hour")
        ax.set_title("Go garbage collections per hour, by idle window")
        fig.tight_layout()
        fig.savefig(plots / "go_gc_cycles.png", dpi=130)
        plt.close(fig)
    print(f"plots written to {plots}")


# ---------------------------------------------------------------- main
def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    run = Path(sys.argv[1]).resolve()
    per_impl: dict[str, list[dict]] = defaultdict(list)
    invalid, long_rows, ts_rows, thr_rows, rtm_rows, fair_rows = [], [], [], [], [], []
    for tdir in sorted(p for p in run.iterdir() if p.is_dir() and (p / "meta.env").exists()):
        meta = load_kv(tdir / "meta.env")
        host = load_kv(tdir / "host.txt")
        rows = load_csv(tdir / "samples.csv")
        impl, trial = meta.get("impl", "?"), meta.get("trial", "?")
        fair_rows.append({"impl": impl, "trial": trial, "dir": tdir.name, "valid": meta.get("valid"),
                          "namespace": meta.get("namespace", ""), "start_line": meta.get("start_line", ""),
                          "workers": meta.get("workers", ""), "watch_timeout_s": meta.get("watch_timeout_s", ""),
                          "image_id": meta.get("image_id", ""), "sync_ms": meta.get("sync_ms", ""),
                          "host_governor": host.get("governor", ""), "host_no_turbo": host.get("no_turbo", ""),
                          "host_loadavg": host.get("loadavg", ""),
                          "host_vms_running": len(host.get("vms_running", "").split())})
        if meta.get("valid") != "1" or len(rows) < 10:
            invalid.append((tdir.name, meta))
            continue
        m, bins, rtm, threads = trial_metrics(
            rows, int(meta["cm_count"]), load_rtm(tdir / "watcher.log"),
            load_csv(tdir / "snapshots.csv"), load_perf(tdir), load_csv(tdir / "threads.csv"))
        m["sync_ms"] = float(meta.get("sync_ms") or "nan")
        per_impl[impl].append(m)
        long_rows += [{"impl": impl, "trial": trial, "metric": k, "value": v} for k, v in m.items()]
        ts_rows += [{"impl": impl, "trial": trial, **b} for b in bins]
        thr_rows += [{"impl": impl, "trial": trial, **t} for t in threads]
        rtm_rows += [{"impl": impl, "trial": trial, **r} for r in rtm]

    impls = [i for i in ("rs", "go") if i in per_impl] + sorted(i for i in per_impl if i not in ("rs", "go"))
    stats_out, cmp_out = [], []
    md = [f"# cmbench summary: {run.name}", "",
          "Trials used: " + ", ".join(f"{i}={len(per_impl[i])}" for i in impls)]
    if invalid:
        md.append("Excluded trials: " + ", ".join(
            f"{n} (applies={m.get('applies')} deletes={m.get('deletes')} errors={m.get('watch_errors')})"
            for n, m in invalid))
    md += ["", "Cells: mean [95% t-interval] across trials. Difference and ratio columns: bootstrap 95% "
           "CI (10k resamples). A CI that excludes 0 (difference) or 1 (ratio) indicates a consistent "
           "difference; with few trials, intervals are wide by design.", ""]

    # Configuration parity, straight from what each binary reported.
    md += ["## Configuration parity (as reported by each trial)", "",
           "| trial | START line | host governor | no_turbo | host load at trial start |", "|---|---|---|---|---|"]
    for f in fair_rows:
        md.append(f"| {f['dir']} | `{f['start_line']}` | {f['host_governor'] or 'n/a'} | "
                  f"{f['host_no_turbo'] or 'n/a'} | {' '.join(f['host_loadavg'].split()[:3]) or 'n/a'} |")
    md.append("")

    for w in IDLE_WINDOWS:
        md += [f"## {WINDOW_TITLES[w]}", ""]
        table(md, per_impl, impls, WINDOW_METRICS, prefix=f"{w}.", stats_out=stats_out, cmp_out=cmp_out, section=w)

    delta_specs = [(k, lab, unit) for k, lab, unit, *_ in WINDOW_METRICS if k in DELTA_METRICS]
    for w in IDLE_WINDOWS[1:]:
        md += [f"## Change in {w} vs idle_pre (paired, same trial)", ""]
        # Ratios of near-zero deltas are meaningless, so only the difference is shown.
        table(md, per_impl, impls, delta_specs, prefix=f"delta_{w}.", show_ratio=False,
              stats_out=stats_out, cmp_out=cmp_out, section=f"delta_{w}")

    md += ["## Startup and churn", ""]
    table(md, per_impl, impls, CHURN_METRICS, stats_out=stats_out, cmp_out=cmp_out, section="churn")

    # Scale projection: what the idle cost means for a fleet.
    md += ["## Scale projection (per 1,000 idle watcher pods)", "",
           "CPU cores = millicores x 1000 / 1000. Core-hours per month = cores x 730. "
           "Memory = memory.current mean x 1000.", "",
           "| window | impl | CPU cores | core-hours / month | memory.current (GiB) | RSS (GiB) |",
           "|---|---|---|---|---|---|"]
    for w in SCALE_WINDOWS:
        for i in impls:
            mc, mem, rss = (values(per_impl, i, f"{w}.{k}") for k in ("millicores", "mem_mean_mib", "rss_mean_mib"))
            if mc and mem:
                cores = st.fmean(mc)
                md.append(f"| {w} | {i} | {fmt(cores)} | {fmt(cores * 730)} | "
                          f"{fmt(st.fmean(mem) * 1000 / 1024)} | {fmt(st.fmean(rss) * 1000 / 1024)} |")
    md.append("")

    md += ["## Wakeups by OS thread (mean wakeups/s per trial)", ""]
    if thr_rows:
        agg = defaultdict(float)
        ntr = defaultdict(set)
        for r in thr_rows:
            agg[(r["impl"], r["phase"], r["comm"])] += r["wakeups_per_s"]
            ntr[(r["impl"], r["phase"])].add(r["trial"])
        md += ["| impl | thread name | " + " | ".join(IDLE_WINDOWS) + " |", "|---|---|" + "---|" * len(IDLE_WINDOWS)]
        for i in impls:
            for c in sorted({k[2] for k in agg if k[0] == i}):
                cells = [fmt(agg.get((i, w, c), 0.0) / max(1, len(ntr[(i, w)]))) for w in IDLE_WINDOWS]
                md.append(f"| {i} | {c} | " + " | ".join(cells) + " |")
        md.append("")
    else:
        md += ["No per-thread data.", ""]

    md += ["## Runtime and allocator internals (self-reported at phase boundaries)", "",
           "Counters (cycles, cpu seconds, allocs) as mean increase per hour of the window; gauges "
           "(bytes, tasks, goroutines) as mean change across the window. Each implementation reports "
           "its own internals, so rows are not comparable across columns.", ""]
    for impl in impls:
        rr = [r for r in rtm_rows if r["impl"] == impl and r["phase"] in IDLE_WINDOWS]
        if not rr:
            continue
        md += [f"### {impl}", "", "| Key | " + " | ".join(IDLE_WINDOWS) + " |", "|---|" + "---|" * len(IDLE_WINDOWS)]
        for k in sorted({r["key"] for r in rr}):
            counter = any(s in k for s in ("cycles", "cpu_seconds", "allocs", "applies", "deletes"))
            cells = []
            for w in IDLE_WINDOWS:
                sel = [r for r in rr if r["key"] == k and r["phase"] == w]
                if not sel:
                    cells.append("n/a")
                elif counter:
                    cells.append(fmt(st.fmean(r["per_hour"] for r in sel if r["hours"] > 0)) + "/h")
                else:
                    dv = st.fmean(r["delta"] for r in sel)
                    cells.append(fmt(dv / MIB) + " MiB" if k.endswith("bytes") else fmt(dv))
            md.append(f"| {k} | " + " | ".join(cells) + " |")
        md.append("")

    md.append("Data files: metrics.csv (dictionary), summary_trials.csv, summary_stats.csv, comparisons.csv, "
              "timeseries.csv, threads.csv, rtm.csv, fairness.csv.")
    (run / "summary.md").write_text("\n".join(md) + "\n")

    # Data files for graphing.
    dictionary = []
    for w in IDLE_WINDOWS + ["create", "delete", "startup", "warmup"]:
        for k, label, unit, src, meaning in WINDOW_METRICS:
            dictionary.append({"metric": f"{w}.{k}", "window": w, "label": label, "unit": unit,
                               "source": src, "meaning": meaning})
    for w in IDLE_WINDOWS[1:]:
        for k, label, unit, src, meaning in WINDOW_METRICS:
            if k in DELTA_METRICS:
                dictionary.append({"metric": f"delta_{w}.{k}", "window": w, "label": f"Change in {label} vs idle_pre",
                                   "unit": unit, "source": src, "meaning": "Paired within-trial difference."})
    for k, label, unit, src, meaning in CHURN_METRICS:
        dictionary.append({"metric": k, "window": "churn", "label": label, "unit": unit, "source": src, "meaning": meaning})
    write_csv(run / "metrics.csv", dictionary)
    write_csv(run / "summary_trials.csv", long_rows, ["impl", "trial", "metric", "value"])
    write_csv(run / "summary_stats.csv", stats_out,
              ["section", "metric", "label", "unit", "impl", "n", "mean", "ci_lo", "ci_hi", "sd", "min", "max"])
    write_csv(run / "comparisons.csv", cmp_out)
    write_csv(run / "timeseries.csv", ts_rows)
    write_csv(run / "threads.csv", thr_rows, ["impl", "trial", "phase", "tid", "comm", "wakeups_per_s",
                                              "cpu_ms_per_min", "observed_s"])
    write_csv(run / "rtm.csv", rtm_rows, ["impl", "trial", "phase", "key", "start", "end", "delta", "hours", "per_hour"])
    write_csv(run / "fairness.csv", fair_rows)
    print("\n".join(md))
    maybe_plot(run, ts_rows, per_impl, impls, thr_rows, rtm_rows)
    return 0


if __name__ == "__main__":
    sys.exit(main())
