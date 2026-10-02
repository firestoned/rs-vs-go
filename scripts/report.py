#!/usr/bin/env python3
"""Charts and an Excel workbook for a cmbench run.

Usage: python3 scripts/report.py results/<run-id> [--baseline results/<earlier-run-id>]

Reads the CSVs written by analyze.py, so run that first (on both runs when using --baseline).
Writes into <run>/report/:
  light/*.png, *.svg   transparent background, dark text: for white slides and documents
  dark/*.png,  *.svg   transparent background, light text: for dark slides
  cmbench-<run-id>.xlsx  every table, plus native Excel charts you can restyle
Needs matplotlib and openpyxl.

Colors: Rust = orange, Go = blue (validated categorical pair, colorblind-safe in both modes).
Bars are means across trials, whiskers are 95% t-intervals, dots are individual trials.
"""
from __future__ import annotations

import csv
import statistics as st
import sys
from collections import defaultdict
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.ticker import FuncFormatter  # noqa: E402

IMPLS = ["rs", "go"]
NAMES = {"rs": "Rust (kube-rs)", "go": "Go (client-go)"}
WINDOWS = ["idle_pre", "post_near", "post_mid", "post_late"]
WLABEL = {"idle_pre": "Before churn\n(baseline)", "post_near": "0-10 min\nafter churn",
          "post_mid": "10-30 min\nafter churn", "post_late": "30-60 min\nafter churn"}
THEMES = {
    "light": {"ink": "#0b0b0b", "ink2": "#52514e", "grid": "#d6d5d0", "band": "#8a8984",
              "rs": "#eb6834", "go": "#2a78d6"},
    "dark": {"ink": "#ffffff", "ink2": "#c3c2b7", "grid": "#4a4944", "band": "#8a8984",
             "rs": "#d95926", "go": "#3987e5"},
}
T95 = {1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571, 6: 2.447, 7: 2.365, 8: 2.306, 9: 2.262}
TIMELINE_FIELDS = [
    ("millicores", "CPU", "millicores", True),
    ("user_millicores", "User-mode CPU", "millicores", True),
    ("system_millicores", "Kernel-mode CPU", "millicores", True),
    ("wakeups_per_s", "Thread wakeups", "per second", True),
    ("pgfault_per_s", "Page faults", "per second", True),
    ("net_rx_pkts_per_min", "Network packets received", "per minute", True),
    ("net_tx_pkts_per_min", "Network packets sent", "per minute", True),
    ("anon_mib", "Anonymous memory", "MiB", False),
    ("rss_mib", "Resident set size", "MiB", False),
    ("mem_mib", "memory.current", "MiB", False),
    ("threads", "OS threads", "count", False),
]
FOREST = ["millicores", "wakeups_per_s", "active_pct", "bursts_per_min", "pgfault_per_s",
          "anon_mean_mib", "rss_mean_mib", "mem_mean_mib", "threads_max", "net_rx_pkts_per_min"]
COMPARE = ["idle_pre.millicores", "post_late.millicores", "idle_pre.wakeups_per_s",
           "post_late.wakeups_per_s", "idle_pre.net_rx_pkts_per_min", "idle_pre.mem_mean_mib",
           "create.cpu_us_per_event", "create.net_rx_bytes_per_event"]


# ---------------------------------------------------------------- data
def fnum(v):
    try:
        x = float(v)
        return x if x == x else None
    except (TypeError, ValueError):
        return None


def read_csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


class Run:
    def __init__(self, d: Path):
        self.dir = d
        self.name = d.name
        if not (d / "summary_trials.csv").exists():
            sys.exit(f"{d}: run scripts/analyze.py first")
        self.trials = defaultdict(lambda: defaultdict(dict))  # impl -> trial -> metric -> value
        for r in read_csv(d / "summary_trials.csv"):
            v = fnum(r["value"])
            if v is not None:
                self.trials[r["impl"]][r["trial"]][r["metric"]] = v
        self.meta = {r["metric"]: r for r in read_csv(d / "metrics.csv")}
        self.stats = read_csv(d / "summary_stats.csv")
        self.cmp = read_csv(d / "comparisons.csv")
        self.ts = read_csv(d / "timeseries.csv")
        self.threads = read_csv(d / "threads.csv")
        self.rtm = read_csv(d / "rtm.csv")
        self.fair = read_csv(d / "fairness.csv")

    def vals(self, impl, metric):
        return [t[metric] for t in self.trials.get(impl, {}).values() if metric in t]

    def label(self, metric):
        m = self.meta.get(metric)
        return (m["label"], m["unit"], m["meaning"]) if m else (metric, "", "")

    def window_keys(self):
        return [k.split(".", 1)[1] for k, m in self.meta.items() if m["window"] == "idle_pre"
                and not k.startswith("delta_")]


def ci(vals):
    if not vals:
        return None, None, None
    mean = st.fmean(vals)
    if len(vals) < 2:
        return mean, mean, mean
    half = T95.get(len(vals) - 1, 2.0) * st.stdev(vals) / len(vals) ** 0.5
    return mean, mean - half, mean + half


# ---------------------------------------------------------------- matplotlib styling
TH = THEMES["light"]


def use_theme(name):
    global TH
    TH = THEMES[name]
    plt.rcParams.update({
        "figure.facecolor": "none", "axes.facecolor": "none", "savefig.transparent": True,
        "text.color": TH["ink"], "axes.labelcolor": TH["ink2"], "axes.edgecolor": TH["grid"],
        "xtick.color": TH["ink2"], "ytick.color": TH["ink2"], "axes.titlecolor": TH["ink"],
        "font.family": ["Helvetica Neue", "Helvetica", "Arial", "DejaVu Sans"], "font.size": 10,
        "axes.titlesize": 13, "axes.titleweight": "bold", "axes.titlelocation": "left",
        "axes.spines.top": False, "axes.spines.right": False, "axes.grid": True,
        "axes.grid.axis": "y", "grid.color": TH["grid"], "grid.linewidth": 0.6,
        "axes.axisbelow": True, "legend.frameon": False, "legend.fontsize": 9,
        "svg.fonttype": "none",
    })


def short(v):
    if v is None:
        return ""
    a = abs(v)
    if a == 0:
        return "0"
    if a >= 1e6:
        return f"{v / 1e6:.2f}M"
    if a >= 1e4:
        return f"{v / 1e3:.1f}k"
    if a >= 100:
        return f"{v:.0f}"
    if a >= 10:
        return f"{v:.1f}"
    if a >= 1:
        return f"{v:.2f}"
    return f"{v:.2g}" if a >= 0.001 else f"{v:.1e}"


def finish(fig, ax, title, subtitle, out: Path, name: str):
    ax.set_title(title, pad=24 if subtitle else 10)
    if subtitle:
        ax.text(0, 1.02, subtitle, transform=ax.transAxes, fontsize=8.5, color=TH["ink2"], va="bottom")
    fig.tight_layout()
    for ext in ("png", "svg"):
        fig.savefig(out / f"{name}.{ext}", dpi=200 if ext == "png" else None)
    plt.close(fig)


def grouped_bars(ax, groups, data, labels=True, dots=True):
    """data[impl] = list of value-lists, one per group."""
    width, gap = 0.36, 0.03
    for j, impl in enumerate(IMPLS):
        off = (j - 0.5) * (width + gap)
        for k, vals in enumerate(data[impl]):
            mean, lo, hi = ci(vals)
            if mean is None:
                continue
            x = k + off
            ax.bar(x, mean, width, color=TH[impl], label=NAMES[impl] if k == 0 else None, zorder=2)
            if len(vals) > 1:
                ax.errorbar(x, mean, yerr=[[mean - lo], [hi - mean]], color=TH["ink"], lw=1,
                            capsize=3, zorder=3)
            if dots:
                ax.scatter([x] * len(vals), vals, s=10, color=TH["ink"], alpha=0.55, zorder=4,
                           edgecolors="none")
            if labels:
                top = max([hi or mean, mean] + vals) if mean >= 0 else min([lo or mean, mean] + vals)
                ax.annotate(short(mean), (x, top), xytext=(0, 3 if mean >= 0 else -10),
                            textcoords="offset points", ha="center", fontsize=7.5, color=TH["ink2"])
    ax.set_xticks(range(len(groups)), groups)
    ax.axhline(0, color=TH["ink2"], lw=0.8, zorder=1)
    ax.legend(loc="upper left", bbox_to_anchor=(0, 1.0), ncols=2)
    ax.margins(y=0.18)


# ---------------------------------------------------------------- charts
def chart_windows(run: Run, out: Path):
    n = 0
    for key in run.window_keys():
        data = {i: [run.vals(i, f"{w}.{key}") for w in WINDOWS] for i in IMPLS}
        if not any(v for i in IMPLS for v in data[i]):
            continue
        label, unit, meaning = run.label(f"idle_pre.{key}")
        fig, ax = plt.subplots(figsize=(9, 4.6))
        grouped_bars(ax, [WLABEL[w] for w in WINDOWS], data)
        ax.set_ylabel(unit)
        finish(fig, ax, label, meaning, out, f"window_{key}")
        n += 1
    return n


def chart_deltas(run: Run, out: Path):
    n = 0
    keys = sorted({k.split(".", 1)[1] for k in run.meta if k.startswith("delta_")},
                  key=lambda k: FOREST.index(k) if k in FOREST else 99)
    for key in keys:
        ws = WINDOWS[1:]
        data = {i: [run.vals(i, f"delta_{w}.{key}") for w in ws] for i in IMPLS}
        if not any(v for i in IMPLS for v in data[i]):
            continue
        label, unit, _ = run.label(f"idle_pre.{key}")
        fig, ax = plt.subplots(figsize=(8, 4.4))
        grouped_bars(ax, [WLABEL[w] for w in ws], data)
        ax.set_ylabel(f"change in {unit}")
        finish(fig, ax, f"Change in {label} after churn",
               "Same-trial difference from that trial's own pre-churn baseline. 0 = back to baseline.",
               out, f"delta_{key}")
        n += 1
    return n


def chart_churn(run: Run, out: Path):
    n = 0
    for key, m in run.meta.items():
        if m["window"] != "churn":
            continue
        data = {i: [run.vals(i, key)] for i in IMPLS}
        if not all(data[i][0] for i in IMPLS):
            continue
        fig, ax = plt.subplots(figsize=(5.2, 4.2))
        width = 0.55
        for k, impl in enumerate(IMPLS):
            vals = data[impl][0]
            mean, lo, hi = ci(vals)
            ax.bar(k, mean, width, color=TH[impl], zorder=2)
            ax.errorbar(k, mean, yerr=[[mean - lo], [hi - mean]], color=TH["ink"], lw=1, capsize=3, zorder=3)
            ax.scatter([k] * len(vals), vals, s=10, color=TH["ink"], alpha=0.55, zorder=4, edgecolors="none")
            ax.annotate(short(mean), (k, max([hi, mean] + vals)), xytext=(0, 3), textcoords="offset points",
                        ha="center", fontsize=8, color=TH["ink2"])
        ax.set_xticks(range(len(IMPLS)), [NAMES[i] for i in IMPLS])
        ax.axhline(0, color=TH["ink2"], lw=0.8)
        ax.set_ylabel(m["unit"])
        ax.margins(y=0.15)
        finish(fig, ax, m["label"], m["meaning"][:95], out, f"churn_{key.replace('.', '_')}")
        n += 1
    return n


def phase_spans(rows):
    spans = defaultdict(lambda: [1e9, -1e9])
    for r in rows:
        x = float(r["min_since_churn"])
        s = spans[r["phase"]]
        s[0], s[1] = min(s[0], x), max(s[1], x + 1)
    return spans


def timeline_series(run: Run, field, smooth=1):
    """Per impl: list of per-trial (xs, ys) and the mean across trials at bins all trials share."""
    out = {}
    phase_of = {}
    for r in run.ts:
        phase_of.setdefault(float(r["min_since_churn"]), r["phase"])
    for impl in IMPLS:
        by_trial = defaultdict(dict)
        for r in run.ts:
            if r["impl"] == impl and r["phase"] not in ("startup",):
                y = fnum(r.get(field))
                if y is not None:
                    by_trial[r["trial"]][float(r["min_since_churn"])] = y
        if not by_trial:
            continue
        common = sorted(set.intersection(*(set(d) for d in by_trial.values())))
        mean = [st.fmean(d[x] for d in by_trial.values()) for x in common]
        if smooth > 1:
            # Trailing window that restarts at each phase boundary, so the churn spike never
            # bleeds into the idle windows that follow it.
            ph = [phase_of.get(x, "") for x in common]
            sm = []
            for i in range(len(mean)):
                j = i
                while j > 0 and i - j + 1 < smooth and ph[j - 1] == ph[i]:
                    j -= 1
                sm.append(st.fmean(mean[j: i + 1]))
            mean = sm
        out[impl] = ([sorted(d.items()) for d in by_trial.values()], common, mean)
    return out


def chart_timelines(run: Run, out: Path):
    spans = phase_spans(run.ts)
    phase_of = {}
    for r in run.ts:
        phase_of.setdefault(float(r["min_since_churn"]), r["phase"])
    n = 0
    variants = [(f, lab, unit, log, 1) for f, lab, unit, log in TIMELINE_FIELDS]
    variants += [("millicores", "CPU, 10-minute rolling mean", "millicores", False, 10),
                 ("wakeups_per_s", "Thread wakeups, 10-minute rolling mean", "per second", False, 10)]
    for field, label, unit, log, smooth in variants:
        series = timeline_series(run, field, smooth)
        if not series:
            continue
        fig, ax = plt.subplots(figsize=(12, 4.6))
        bands = [(ph, *spans[ph]) for ph in ("warmup", "idle_pre", "post_near", "post_mid", "post_late") if ph in spans]
        if "create" in spans and "delete" in spans:
            bands.insert(2, ("churn", spans["create"][0], spans["delete"][1]))
        for k, (ph, a, b) in enumerate(bands):
            ax.axvspan(a, b, color=TH["rs"] if ph == "churn" else TH["band"],
                       alpha=0.10 if ph == "churn" else (0.08 if k % 2 else 0), lw=0, zorder=0)
            ax.text((a + b) / 2, 1.0, ph, transform=ax.get_xaxis_transform(), ha="center", va="bottom",
                    fontsize=7.5, color=TH["ink2"])
        for impl, (trials, xs, mean) in series.items():
            if smooth == 1:
                for pts in trials:
                    ax.plot([p[0] for p in pts], [p[1] for p in pts], color=TH[impl], alpha=0.18, lw=0.7)
            if smooth > 1:  # leave a gap at churn so the axis fits the idle windows
                mean = [float("nan") if phase_of.get(x) in ("create", "delete") else y for x, y in zip(xs, mean)]
            ax.plot(xs, mean, color=TH[impl], lw=2, label=f"{NAMES[impl]}, mean of {len(trials)} trials")
            if mean:
                ax.annotate(NAMES[impl].split()[0], (xs[-1], mean[-1]), xytext=(4, 0), textcoords="offset points",
                            va="center", fontsize=8.5, color=TH["ink"])
        if log:
            ax.set_yscale("symlog", linthresh={"threads": 1}.get(field, 0.01))
            ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: short(v)))
        ax.set_xlabel("minutes from the start of churn (1-minute bins)")
        ax.set_ylabel(unit)
        ax.legend(loc="upper left", bbox_to_anchor=(0, 0.98), ncols=2)
        ax.grid(axis="x", visible=False)
        ax.margins(x=0.01)
        sub = "Bold line = mean across trials, faint lines = individual trials." if smooth == 1 else \
            "Mean across trials, trailing 10-minute window restarting at each phase. Churn itself omitted."
        finish(fig, ax, label, sub, out, f"timeline_{field}" + (f"_rolling{smooth}" if smooth > 1 else ""))
        n += 1
    return n


def chart_forest(run: Run, out: Path):
    rows = []
    for key in FOREST:
        for w in ("idle_pre", "post_late"):
            c = next((c for c in run.cmp if c["metric"] == f"{w}.{key}"), None)
            if c and fnum(c["ratio_rs_over_go"]):
                rows.append((run.label(f"idle_pre.{key}")[0], w, fnum(c["ratio_rs_over_go"]),
                             fnum(c["ratio_ci_lo"]), fnum(c["ratio_ci_hi"])))
    if not rows:
        return 0
    labels = list(dict.fromkeys(r[0] for r in rows))
    fig, ax = plt.subplots(figsize=(9, 0.45 * len(labels) + 1.6))
    marks = {"idle_pre": ("o", TH["ink"], "Before churn"), "post_late": ("s", TH["ink2"], "30-60 min after churn")}
    for lab, w, r, lo, hi in rows:
        y = labels.index(lab) + (-0.15 if w == "idle_pre" else 0.15)
        mk, col, _ = marks[w]
        if r > 0:
            ax.errorbar(r, y, xerr=[[r - (lo or r)], [(hi or r) - r]], fmt=mk, color=col, ms=6, lw=1, capsize=2)
            ax.annotate(f"{r:.2f}x", (hi or r, y), xytext=(4, 0), textcoords="offset points", va="center",
                        fontsize=7.5, color=TH["ink2"])
    for w, (mk, col, lab) in marks.items():
        ax.plot([], [], mk, color=col, label=lab)
    ax.axvline(1, color=TH["ink2"], lw=1)
    ax.set_xscale("log")
    ax.set_xticks([0.02, 0.05, 0.1, 0.2, 0.5, 1, 2])
    ax.xaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:g}x"))
    ax.xaxis.set_minor_formatter(FuncFormatter(lambda v, _: ""))
    ax.set_yticks(range(len(labels)), labels)
    ax.invert_yaxis()
    ax.grid(axis="y", visible=False)
    ax.grid(axis="x", visible=True)
    ax.set_xlabel("Rust / Go  (left of 1x: Rust uses less;  right: Go uses less)")
    ax.legend(loc="lower left")
    finish(fig, ax, "Rust relative to Go, idle windows",
           "Ratio of means with bootstrap 95% interval. Log scale.", out, "ratio_overview")
    return 1


def chart_runtime(run: Run, out: Path):
    n = 0
    for key, label, unit in [("gc_cycles_total_gc_cycles", "Go garbage collections per hour", "GC cycles / hour"),
                             ("cpu_classes_gc_total_cpu_seconds", "Go GC CPU time per hour", "CPU seconds / hour")]:
        per_w = {w: [fnum(r["per_hour"]) for r in run.rtm if r["impl"] == "go" and r["key"] == key and r["phase"] == w
                     and fnum(r["per_hour"]) is not None] for w in WINDOWS}
        if not any(per_w.values()):
            continue
        fig, ax = plt.subplots(figsize=(8, 4.2))
        for k, w in enumerate(WINDOWS):
            vals = per_w[w]
            if not vals:
                continue
            mean, lo, hi = ci(vals)
            ax.bar(k, mean, 0.55, color=TH["go"], zorder=2, label=NAMES["go"] if k == 0 else None)
            ax.scatter([k] * len(vals), vals, s=10, color=TH["ink"], alpha=0.55, zorder=4, edgecolors="none")
            ax.annotate(short(mean), (k, max([hi, mean] + vals)), xytext=(0, 3), textcoords="offset points",
                        ha="center", fontsize=8, color=TH["ink2"])
        ax.set_xticks(range(len(WINDOWS)), [WLABEL[w] for w in WINDOWS])
        ax.set_ylabel(unit)
        ax.margins(y=0.15)
        finish(fig, ax, label, "Self-reported by the Go runtime (runtime/metrics). Rust has no collector.",
               out, f"go_{key}")
        n += 1
    return n


def chart_scale(run: Run, out: Path):
    n = 0
    for key, title, unit, fn in [
        ("millicores", "CPU per 1,000 idle watcher pods", "core-hours per month", lambda v: v * 730),
        ("mem_mean_mib", "Memory per 1,000 idle watcher pods (memory.current)", "GiB", lambda v: v * 1000 / 1024),
        ("rss_mean_mib", "RSS per 1,000 idle watcher pods", "GiB", lambda v: v * 1000 / 1024),
    ]:
        ws = ["idle_pre", "post_late"]
        data = {i: [[fn(v) for v in run.vals(i, f"{w}.{key}")] for w in ws] for i in IMPLS}
        fig, ax = plt.subplots(figsize=(7, 4.2))
        grouped_bars(ax, [WLABEL[w] for w in ws], data, dots=False)
        ax.set_ylabel(unit)
        finish(fig, ax, title, "Projection: per-pod mean x 1,000. Core-hours = cores x 730 h.", out, f"scale_{key}")
        n += 1
    return n


def chart_threads(run: Run, out: Path):
    if not run.threads:
        return 0
    n = 0
    for w in ("idle_pre", "post_late"):
        agg = defaultdict(list)
        trials = defaultdict(set)
        for r in run.threads:
            if r["phase"] == w:
                agg[(r["impl"], r["comm"])].append(fnum(r["wakeups_per_s"]) or 0)
                trials[r["impl"]].add(r["trial"])
        keys = sorted(agg, key=lambda k: (IMPLS.index(k[0]) if k[0] in IMPLS else 9, -sum(agg[k])))
        if not keys:
            continue
        fig, ax = plt.subplots(figsize=(8, 0.5 * len(keys) + 1.6))
        for y, (impl, comm) in enumerate(keys):
            v = sum(agg[(impl, comm)]) / max(1, len(trials[impl]))
            ax.barh(y, v, 0.6, color=TH[impl], zorder=2)
            ax.annotate(short(v), (v, y), xytext=(4, 0), textcoords="offset points", va="center",
                        fontsize=8, color=TH["ink2"])
        ax.set_yticks(range(len(keys)), [f"{NAMES[i].split()[0]}: {c}" for i, c in keys])
        ax.invert_yaxis()
        ax.grid(axis="y", visible=False)
        ax.grid(axis="x", visible=True)
        ax.set_xlabel("wakeups per second (mean per trial, summed over threads with that name)")
        ax.margins(x=0.15)
        finish(fig, ax, f"Wakeups by OS thread name, {WLABEL[w].replace(chr(10), ' ').lower()}",
               "Go names every thread after the binary, so its threads cannot be told apart by name.",
               out, f"threads_{w}")
        n += 1
    return n


def chart_baseline(run: Run, base: Run, out: Path):
    n = 0
    for metric in COMPARE:
        data = {i: [base.vals(i, metric), run.vals(i, metric)] for i in IMPLS}
        if not all(any(v) for v in data.values()):
            continue
        label, unit, _ = run.label(metric)
        w = metric.split(".", 1)[0]
        fig, ax = plt.subplots(figsize=(7, 4.4))
        grouped_bars(ax, ["Library defaults\n(" + base.name + ")", "Parity settings\n(" + run.name + ")"], data)
        ax.set_ylabel(unit)
        finish(fig, ax, f"{label}: defaults vs parity",
               f"Window: {w}. Defaults: Go 2 workers, protobuf, 30 s TCP keepalive, 300-600 s watch timeout.",
               out, f"compare_{metric.replace('.', '_')}")
        n += 1
    return n


# ---------------------------------------------------------------- excel
def build_excel(run: Run, base: Run | None, path: Path):
    from openpyxl import Workbook
    from openpyxl.chart import BarChart, LineChart, Reference
    from openpyxl.chart.data_source import NumDataSource, NumRef
    from openpyxl.chart.error_bar import ErrorBars
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    bold = Font(bold=True)
    head_fill = PatternFill("solid", fgColor="EDECE8")
    colors = {"rs": "EB6834", "go": "2A78D6"}

    def sheet(title, header, rows, widths=None):
        ws = wb.create_sheet(title)
        ws.append(header)
        for c in ws[1]:
            c.font, c.fill = bold, head_fill
        for r in rows:
            ws.append([fnum(v) if isinstance(v, str) and fnum(v) is not None else v for v in r])
        ws.freeze_panes = "A2"
        for i, h in enumerate(header, 1):
            ws.column_dimensions[get_column_letter(i)].width = (widths or {}).get(h, max(10, min(40, len(str(h)) + 2)))
        if rows:
            ws.auto_filter.ref = ws.dimensions
        return ws

    # Read me
    ws = wb.active
    ws.title = "Read me"
    first = run.fair[0] if run.fair else {}
    notes = [
        ("cmbench results", ""), ("Run", run.name), ("Baseline run (library defaults)", base.name if base else "none"),
        ("Trials", ", ".join(f"{i}={len(run.trials.get(i, {}))}" for i in IMPLS)),
        ("Configuration (both binaries)", first.get("start_line", "")),
        ("Host CPU policy", f"governor={first.get('host_governor', '')} no_turbo={first.get('host_no_turbo', '')}"),
        ("", ""),
        ("How to read", "Means across trials with 95% t-intervals. Diff and ratio columns use a bootstrap 95% "
                        "interval. Diff = Rust minus Go; ratio = Rust / Go (below 1 means Rust uses less)."),
        ("Windows", "idle_pre = 60 min before any churn; post_near = 0-10 min, post_mid = 10-30 min, "
                    "post_late = 30-60 min after creating and deleting 100 ConfigMaps."),
        ("Caveat", "Context-switch counters cover the main thread only; use Thread wakeups for the whole process."),
        ("", ""),
        ("Sheets", "Idle windows, Changes vs baseline, Startup and churn: tidy summaries feeding the Charts sheet. "
                   "Timeline means feeds Timeline charts. Per trial, Stats, Comparisons, Timeseries, Threads, "
                   "Runtime internals, Fairness, Dictionary: full data."),
    ]
    for k, v in notes:
        ws.append([k, v])
    ws["A1"].font = Font(bold=True, size=14)
    for row in ws.iter_rows(min_row=2):
        row[0].font = bold
        row[1].alignment = Alignment(wrap_text=True, vertical="top")
    ws.column_dimensions["A"].width = 34
    ws.column_dimensions["B"].width = 120

    # Tidy window table: one block of 4 rows per metric, used by the charts.
    stats = {(s["metric"], s["impl"]): s for s in run.stats}
    cmps = {c["metric"]: c for c in run.cmp}

    def block_sheet(title, specs):
        """specs: list of (metric_suffix_key, label, unit, [(group label, full metric)])."""
        ws = wb.create_sheet(title)
        hdr = ["metric", "unit", "window", "Rust mean", "Rust 95% lo", "Rust 95% hi", "Go mean", "Go 95% lo",
               "Go 95% hi", "Rust - Go", "diff lo", "diff hi", "Rust / Go", "ratio lo", "ratio hi",
               "Rust err+", "Rust err-", "Go err+", "Go err-"]
        ws.append(hdr)
        for c in ws[1]:
            c.font, c.fill = bold, head_fill
        blocks = []
        for key, label, unit, groups in specs:
            start = ws.max_row + 1
            for glabel, full in groups:
                rs, go, c = stats.get((full, "rs"), {}), stats.get((full, "go"), {}), cmps.get(full, {})
                rm, rl, rh = fnum(rs.get("mean")), fnum(rs.get("ci_lo")), fnum(rs.get("ci_hi"))
                gm, gl, gh = fnum(go.get("mean")), fnum(go.get("ci_lo")), fnum(go.get("ci_hi"))
                err = lambda m, x: abs(x - m) if m is not None and x is not None else 0  # noqa: E731
                ws.append([label, unit, glabel, rm, rl, rh, gm, gl, gh, fnum(c.get("diff_rs_minus_go")),
                           fnum(c.get("diff_ci_lo")), fnum(c.get("diff_ci_hi")), fnum(c.get("ratio_rs_over_go")),
                           fnum(c.get("ratio_ci_lo")), fnum(c.get("ratio_ci_hi")),
                           err(rm, rh), err(rm, rl), err(gm, gh), err(gm, gl)])
            if any(ws.cell(r, 4).value is not None or ws.cell(r, 7).value is not None
                   for r in range(start, ws.max_row + 1)):
                blocks.append((label, unit, start, ws.max_row))
            ws.append([])
        ws.freeze_panes = "D2"
        ws.column_dimensions["A"].width = 44
        ws.column_dimensions["C"].width = 26
        for col in range(4, 20):
            ws.column_dimensions[get_column_letter(col)].width = 12
            for r in range(2, ws.max_row + 1):
                ws.cell(r, col).number_format = "0.0000"
        return ws, blocks

    window_specs = []
    for key in run.window_keys():
        label, unit, _ = run.label(f"idle_pre.{key}")
        window_specs.append((key, label, unit, [(WLABEL[w].replace("\n", " "), f"{w}.{key}") for w in WINDOWS]))
    ws_win, win_blocks = block_sheet("Idle windows", window_specs)

    delta_keys = list(dict.fromkeys(k.split(".", 1)[1] for k in run.meta if k.startswith("delta_")))
    delta_specs = [(k, "Change in " + run.label(f"idle_pre.{k}")[0], run.label(f"idle_pre.{k}")[1],
                    [(WLABEL[w].replace("\n", " "), f"delta_{w}.{k}") for w in WINDOWS[1:]]) for k in delta_keys]
    ws_del, del_blocks = block_sheet("Changes vs baseline", delta_specs)

    churn_specs = [(k, m["label"], m["unit"], [("all trials", k)]) for k, m in run.meta.items() if m["window"] == "churn"]
    ws_ch, ch_blocks = block_sheet("Startup and churn", churn_specs)

    # Native charts, one per rubric, with the 95% interval as custom error bars.
    wc = wb.create_sheet("Charts", 1)
    wc["A1"] = "One chart per rubric. Bars = mean across trials, whiskers = 95% interval. Data: the sheet named in each chart title."
    wc["A1"].font = bold
    pos = 0

    def add_bar(src_ws, label, unit, r0, r1, sheetname):
        nonlocal pos
        ch = BarChart()
        ch.type = "col"
        ch.title = f"{label} ({unit})" if unit else label
        ch.y_axis.title = unit
        ch.height, ch.width = 7.5, 15
        ch.gapWidth = 60
        ch.overlap = -5
        cats = Reference(src_ws, min_col=3, min_row=r0, max_row=r1)
        for impl, col, ep, em in (("rs", 4, 16, 17), ("go", 7, 18, 19)):
            ref = Reference(src_ws, min_col=col, min_row=r0, max_row=r1)
            ch.add_data(ref, titles_from_data=False)
            s = ch.series[-1]
            from openpyxl.chart.series import SeriesLabel
            s.tx = SeriesLabel(v=NAMES[impl])
            s.graphicalProperties.solidFill = colors[impl]
            s.graphicalProperties.line.noFill = True
            q = f"'{sheetname}'!${get_column_letter(ep)}${r0}:${get_column_letter(ep)}${r1}"
            qm = f"'{sheetname}'!${get_column_letter(em)}${r0}:${get_column_letter(em)}${r1}"
            s.errBars = ErrorBars(errDir="y", errBarType="both", errValType="cust", noEndCap=False,
                                  plus=NumDataSource(numRef=NumRef(f=q)), minus=NumDataSource(numRef=NumRef(f=qm)))
        ch.set_categories(cats)
        ch.legend.position = "b"
        ch.y_axis.majorGridlines = ch.y_axis.majorGridlines
        ch.x_axis.delete = False
        ch.y_axis.delete = False
        col = "A" if pos % 2 == 0 else "J"
        row = 3 + (pos // 2) * 16
        wc.add_chart(ch, f"{col}{row}")
        pos += 1

    for label, unit, r0, r1 in win_blocks:
        add_bar(ws_win, label, unit, r0, r1, "Idle windows")
    for label, unit, r0, r1 in del_blocks:
        add_bar(ws_del, label, unit, r0, r1, "Changes vs baseline")
    for label, unit, r0, r1 in ch_blocks:
        add_bar(ws_ch, label, unit, r0, r1, "Startup and churn")

    # Timeline means and line charts.
    wt = wb.create_sheet("Timeline means")
    fields = [f for f, *_ in TIMELINE_FIELDS]
    series = {f: timeline_series(run, f) for f in fields}
    xs = sorted({x for f in fields for impl in series[f] for x in series[f][impl][1]})
    phase_of = {}
    for r in run.ts:
        phase_of.setdefault(float(r["min_since_churn"]), r["phase"])
    hdr = ["minutes from churn", "phase"] + [f"{f} {NAMES[i]}" for f in fields for i in IMPLS]
    wt.append(hdr)
    for c in wt[1]:
        c.font, c.fill = bold, head_fill
    lookup = {(f, i): dict(zip(series[f][i][1], series[f][i][2])) for f in fields for i in series[f]}
    for x in xs:
        wt.append([x, phase_of.get(x, "")] + [lookup.get((f, i), {}).get(x) for f in fields for i in IMPLS])
    wt.freeze_panes = "C2"
    for i in range(1, len(hdr) + 1):
        wt.column_dimensions[get_column_letter(i)].width = 16
    wl = wb.create_sheet("Timeline charts", 2)
    wl["A1"] = "Mean across trials per 1-minute bin, x = minutes from the start of churn. Data: Timeline means."
    wl["A1"].font = bold
    for k, (f, lab, unit, _) in enumerate(TIMELINE_FIELDS):
        lc = LineChart()
        lc.title = f"{lab} ({unit})"
        lc.y_axis.title = unit
        lc.x_axis.title = "minutes from churn"
        lc.height, lc.width = 7.5, 24
        for j, impl in enumerate(IMPLS):
            col = 3 + fields.index(f) * 2 + j
            lc.add_data(Reference(wt, min_col=col, min_row=1, max_row=len(xs) + 1), titles_from_data=True)
            s = lc.series[-1]
            s.graphicalProperties.line.solidFill = colors[impl]
            s.graphicalProperties.line.width = 19050
            s.smooth = False
        lc.set_categories(Reference(wt, min_col=1, min_row=2, max_row=len(xs) + 1))
        lc.x_axis.tickLblSkip = 15
        lc.x_axis.delete = False
        lc.y_axis.delete = False
        lc.legend.position = "b"
        wl.add_chart(lc, f"A{3 + k * 16}")

    # Full data sheets.
    trial_cols = [(i, t) for i in IMPLS for t in sorted(run.trials.get(i, {}), key=int)]
    metrics = sorted({m for i in run.trials for t in run.trials[i].values() for m in t})
    sheet("Per trial", ["metric", "label", "unit"] + [f"{NAMES[i].split()[0]} #{t}" for i, t in trial_cols],
          [[m, run.label(m)[0], run.label(m)[1]] + [run.trials[i][t].get(m) for i, t in trial_cols] for m in metrics],
          {"metric": 36, "label": 40})
    for title, rows in [("Stats", run.stats), ("Comparisons", run.cmp), ("Threads", run.threads),
                        ("Runtime internals", run.rtm), ("Fairness", run.fair), ("Timeseries", run.ts)]:
        if rows:
            sheet(title, list(rows[0].keys()), [list(r.values()) for r in rows])
    scale = []
    for w in ("idle_pre", "post_late"):
        for i in IMPLS:
            mc, mem, rss = (run.vals(i, f"{w}.{k}") for k in ("millicores", "mem_mean_mib", "rss_mean_mib"))
            if mc:
                scale.append([w, NAMES[i], st.fmean(mc), st.fmean(mc) * 730, st.fmean(mem) * 1000 / 1024,
                              st.fmean(rss) * 1000 / 1024])
    sheet("Scale per 1000 pods", ["window", "impl", "CPU cores", "core-hours / month", "memory.current GiB", "RSS GiB"], scale)
    if base:
        rows = []
        for m in COMPARE:
            for i in IMPLS:
                b, r = base.vals(i, m), run.vals(i, m)
                if b and r:
                    rows.append([m, run.label(m)[0], run.label(m)[1], NAMES[i], st.fmean(b), st.fmean(r),
                                 st.fmean(r) / st.fmean(b) if st.fmean(b) else None])
        sheet("Defaults vs parity", ["metric", "label", "unit", "impl", f"defaults ({base.name})",
                                     f"parity ({run.name})", "parity / defaults"], rows, {"label": 40})
    sheet("Dictionary", ["metric", "window", "label", "unit", "source", "meaning"],
          [[m["metric"], m["window"], m["label"], m["unit"], m["source"], m["meaning"]] for m in run.meta.values()],
          {"metric": 36, "label": 40, "source": 40, "meaning": 90})
    wb.save(path)
    return pos


def main() -> int:
    args = sys.argv[1:]
    if not args or args[0].startswith("-"):
        print(__doc__)
        return 2
    run = Run(Path(args[0]).resolve())
    base = Run(Path(args[args.index("--baseline") + 1]).resolve()) if "--baseline" in args else None
    root = run.dir / "report"
    counts = {}
    for theme in THEMES:
        out = root / theme
        out.mkdir(parents=True, exist_ok=True)
        use_theme(theme)
        counts = {
            "window": chart_windows(run, out), "delta": chart_deltas(run, out), "churn": chart_churn(run, out),
            "timeline": chart_timelines(run, out), "ratio": chart_forest(run, out),
            "go_runtime": chart_runtime(run, out), "scale": chart_scale(run, out),
            "threads": chart_threads(run, out), "compare": chart_baseline(run, base, out) if base else 0,
        }
    xlsx = root / f"cmbench-{run.name}.xlsx"
    n_xl = build_excel(run, base, xlsx)
    print(f"charts per theme: {counts} (total {sum(counts.values())}), as PNG and SVG in {root}/light and {root}/dark")
    print(f"excel: {xlsx} ({n_xl} native bar charts + {len(TIMELINE_FIELDS)} timeline charts)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
