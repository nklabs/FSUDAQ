#!/usr/bin/env python3
"""htop-style terminal view of the online dashboard (online_dashboard.py).

Reads the running dashboard's /state once a second (and, on the channel view,
/compare for that one channel): the same requests the web page makes. Read-only:
it never calls /clear, /window, /set, /live or any other control route.
Standard library only (curses).

    python3 tui.py                         # the dashboard on this machine, port 8050
    python3 tui.py --url http://host:8050  # another one
    python3 tui.py --once [--view chan --slot 15]   # print one plain-text snapshot and exit

Keys: 1-4/Tab views · ↑↓ select · Enter channel detail · Esc back · s sort ·
      r reverse · z hide silent · b board filter · a sparkline scale · l log Q ·
      p pause · +/- refresh · h help · q quit
"""
import argparse
import curses
import json
import locale
import math
import os
import re
import signal
import sys
import threading
import time
import urllib.request

BLOCKS = " ▁▂▃▄▅▆▇█"
VIEWS = ("daq", "map", "rf", "boards", "tracker", "chan")
VIEW_NAMES = {"daq": "DAQ channels", "map": "Map", "rf": "RF timing", "boards": "Boards", "tracker": "Tracker", "chan": "Channel"}
SORTS = ("slot", "rate", "count", "role", "board")


# ---------------------------------------------------------------- formatting

_CTRL = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def clean(text):
    """Text safe to put on the terminal: no control bytes. An escape sequence in a server string
    (a log line with colours, an HTML error page) would otherwise show as ^[[..m junk or, printed raw,
    change the terminal's modes."""
    return _CTRL.sub("·", str(text))


def si(x, digits=3):
    """1234567 -> '1.23M'; None -> '–'."""
    if x is None:
        return "–"
    try:
        x = float(x)
    except (TypeError, ValueError):
        return "?"
    if math.isnan(x) or math.isinf(x):
        return "–"
    a = abs(x)
    for div, suf in ((1e12, "T"), (1e9, "G"), (1e6, "M"), (1e3, "k")):
        if a >= div:
            return f"{x / div:.{digits}g}{suf}"
    if a == 0 or a >= 1:
        return f"{x:.{digits}g}"
    return f"{x:.2g}"


def si_ns(ns):
    """A time in ns, in the unit that suits it."""
    a = abs(ns)
    for div, unit in ((1e9, "s"), (1e6, "ms"), (1e3, "µs")):
        if a >= div:
            return f"{ns / div:.3g}{unit}"
    return f"{ns:.3g}ns"


def dur(s):
    if s is None:
        return "–"
    s = float(s)
    if s < 60:
        return f"{s:.1f}s"
    if s < 3600:
        return f"{int(s // 60)}m{int(s % 60):02d}s"
    return f"{int(s // 3600)}h{int(s % 3600 // 60):02d}m"


def spark(values, width, zero=False):
    """One-line chart of the newest `width` values. None (not known yet) is a blank.
    zero=False scales min→max so changes on a steady rate show; zero=True scales 0→max."""
    vals = list(values)[-width:] if width > 0 else []
    known = [v for v in vals if v is not None]
    if not known:
        return " " * len(vals), None, None
    lo = 0 if zero else min(known)
    hi = max(known)
    lo_true = lo
    if not zero:
        lo = min(lo, hi - 0.02 * abs(hi))   # a span of at least 2%: a steady rate's ±1 is not news
    out = []
    for v in vals:
        if v is None:
            out.append(" ")
        elif hi <= lo:
            out.append(BLOCKS[4] if hi > 0 else BLOCKS[1])
        else:
            out.append(BLOCKS[1 + min(7, int((v - lo) / (hi - lo) * 7 + 0.5))])
    return "".join(out), lo_true, hi


def bar(frac, width):
    """htop meter: '|||||     ' filled to frac of width."""
    if width <= 0:
        return ""
    frac = 0.0 if frac is None or frac != frac else max(0.0, min(1.0, frac))
    n = int(round(frac * width))
    return "|" * n + " " * (width - n)


def rebin(counts, width):
    """Sum counts into `width` columns (or stretch, if there are fewer bins than columns)."""
    n = len(counts)
    if n == 0 or width <= 0:
        return []
    if n <= width:
        return [counts[j * n // width] for j in range(width)]
    return [sum(counts[j * n // width:(j + 1) * n // width]) for j in range(width)]


def vchart(vals, height, log=False):
    """A bar chart `height` rows tall, one column per value, eighth-cell resolution. Top row first."""
    if log:
        vals = [math.log10(1 + v) for v in vals]
    hi = max(vals) if vals else 0
    if hi <= 0:
        return [" " * len(vals)] * (height - 1) + ["▁" * len(vals)]
    lev = [v / hi * height * 8 for v in vals]
    return ["".join(BLOCKS[max(0, min(8, int(l - (height - 1 - r) * 8 + 0.5)))] if l > 0 else " " for l in lev)
            for r in range(height)]


def axis(lo, mid, hi, width, fmt):
    """'lo        mid        hi' under a chart."""
    if width < 8:
        return ""
    a, m, b = fmt(lo), fmt(mid), fmt(hi)
    left = width // 2 - len(m) // 2 - len(a)
    right = width - len(a) - max(1, left) - len(m) - len(b)
    return a + " " * max(1, left) + m + " " * max(1, right) + b


def chan_rate(c):
    """Live rate if the server has one, otherwise the run's mean."""
    r = c.get("rate")
    return r if r is not None else c.get("mean_rate")


# ---------------------------------------------------------------- state

class Ui:
    def __init__(self):
        self.view = "daq"
        self.back = "daq"        # Esc from the channel view
        self.sort = "slot"
        self.reverse = False
        self.hide_silent = False
        self.board = None        # serial to filter on, None = all
        self.sel = None          # the selected channel's slot
        self.scroll = 0
        self.paused = False
        self.help = False
        self.zero = False        # sparklines from 0 instead of min→max
        self.log = False         # the channel view's pulse height on a log scale
        self.interval = 1.0
        self.body_rows = 10      # set by render(), used for paging
        self.order = []          # the channels in display order, set by view_grid() / view_daq()
        self.grid_rows = []      # the grid's rows of slots, for the arrow keys
        self.table = False       # DAQ channels as the sortable table instead of the grid
        self.counts = False      # the grid and the map show hit counts instead of rates
        self.common_x = False    # the map's planes on one x axis
        self.top = True          # the map's top view
        self.two_periods = False # RF timing: two periods across
        self.centre = False      # RF timing: each plane's peak in the middle
        self.rf_plane = 1        # RF timing: the plane in the TOF × Qlong map (T1)


def row(*segs):
    return [s if isinstance(s, tuple) else (s, "") for s in segs]


def kv(*pairs):
    out = []
    for i, (k, v, st) in enumerate(pairs):
        if i:
            out.append(("  ", ""))
        out.append((k + " ", "dim"))
        out.append((str(v), st or "bold"))
    return out


def mode_style(mode):
    return {"live": "good", "running": "good", "replay": "info", "stopped": "warn"}.get(mode, "bad" if mode else "dim")


def total_series(daq):
    """Hits per second over all boards, RF left out (as the server's board rates do); None if a board is not known yet."""
    out = []
    for s in daq.get("seconds") or []:
        b = s.get("boards") or []
        out.append(None if not b or any(v is None for v in b) else sum(b))
    return out


def rf_info(st):
    """(RF slot, its channel entry, its board serial) or Nones."""
    daq = st.get("daq") or {}
    rf = daq.get("rf")
    c = next((c for c in daq.get("slots") or [] if c.get("slot") == rf), None) if rf is not None else None
    return rf, c, (c or {}).get("board")


# ---------------------------------------------------------------- columns
# (header, width, align, priority, fn) — on a narrow terminal the highest priority numbers go first.

def fit_columns(cols, width, keep):
    """The columns that fit, leaving `keep` cells for the bar or sparkline."""
    cols = list(cols)
    while cols and sum(c[1] + 1 for c in cols) + keep > width:
        worst = max(cols, key=lambda c: c[3])
        if worst[3] == 0:
            break
        cols.remove(worst)
    return cols


def fmt_cols(cols, item):
    return "".join((f"{c[4](item):>{c[1]}}" if c[2] == ">" else f"{c[4](item):<{c[1]}}")[:c[1]] + " " for c in cols)


def head_cols(cols):
    return "".join((f"{c[0]:>{c[1]}}" if c[2] == ">" else f"{c[0]:<{c[1]}}") + " " for c in cols)


# ---------------------------------------------------------------- views

def header(st, ui, url, width):
    rows = []
    mode = st.get("mode") or "?"
    run = st.get("run_file") or {}
    folder = st.get("folder") or "–"
    name = folder.rstrip("/").rsplit("/", 1)[-1]
    rows.append(row(("FSUDAQ online ", "title"), (f" {mode.upper()} ", "mode_" + mode_style(mode)),
                    "  ", (name, "bold"), ("  " + (st.get("status") or ""), "dim")))
    daq = st.get("daq") or {}
    lag = daq.get("lag_s")
    rows.append(kv(("data", dur(st.get("processed_s")), None),
                   ("hits", si(st.get("hits")), None),
                   ("bunches", si(st.get("bunches")), None),
                   ("lag", dur(lag), "warn" if lag and lag > 5 else None),
                   ("workers", f"{st.get('in_flight', 0)}/{st.get('workers', '?')}", None),
                   ("window", f"{st.get('window_ns', '?')} ns", None),
                   ("late", si(st.get("late")), "bad" if st.get("late") else None)))
    if run:
        comment = (run.get("start_comment") or "").replace("Start Comment:", "").strip()
        rows.append(kv(("run", run.get("run", "–"), None),
                       ("start", (run.get("start") or "–")[11:19], None),
                       ("stop", ((run.get("stop") or {}).get("time") or "running")[11:19], None))
                    + [("  " + comment, "dim")])
    # rate meters: the total (RF left out) and the RF reference, each with its per-second history
    tot = total_series(daq)
    now = daq.get("total_rate")
    if now is None:
        now = next((v for v in reversed(tot) if v is not None), None)
    rf, rfc, rfb = rf_info(st)
    rf_now = next((s.get("rf") for s in reversed(daq.get("seconds") or []) if s.get("rf") is not None), None)
    for label, val, series in (("rate", now, tot), ("RF  ", rf_now, [s.get("rf") for s in daq.get("seconds") or []])):
        if label.startswith("RF") and rf is None:
            continue
        pre = f"{label} {si(val):>6} Hz "
        sw = max(0, width - len(pre) - 18)
        line, lo, hi = spark(series, sw, ui.zero)
        rng = "" if lo is None else f" {si(lo)}–{si(hi)}"
        rows.append(row((label + " ", "dim"), (f"{si(val):>6} Hz ", "bold"), (line, "spark"), (rng, "dim")))
    for k, style in (("problems", "bad"), ("health", "bad"), ("warnings", "warn"), ("notes", "dim")):
        for msg in st.get(k) or []:
            rows.append(row((("!! " if style == "bad" else "!  " if style == "warn" else "·  ") + str(msg), style)))
    return rows


DAQ_COLS = [
    ("SLOT", 4, ">", 0, lambda c: str(c.get("slot", ""))),
    ("BOARD", 6, ">", 3, lambda c: str(c.get("board", ""))),
    ("CH", 2, ">", 2, lambda c: str(c.get("ch", ""))),
    ("ROLE", 10, "<", 0, lambda c: c.get("role") or "–"),
    ("COUNT", 7, ">", 1, lambda c: si(c.get("count") or 0)),
    ("RATE Hz", 7, ">", 0, lambda c: si(c.get("rate"))),
    ("MEAN Hz", 7, ">", 4, lambda c: si(c.get("mean_rate"))),
    ("LATE", 5, ">", 5, lambda c: si(c.get("late") or 0)),
    ("EARLY", 5, ">", 6, lambda c: si(c.get("early") or 0)),
]


def daq_order(st, ui):
    slots = list((st.get("daq") or {}).get("slots") or [])
    if ui.board is not None:
        slots = [c for c in slots if c.get("board") == ui.board]
    if ui.hide_silent:
        slots = [c for c in slots if c.get("count")]
    key = {"slot": lambda c: c.get("slot", 0),
           "rate": lambda c: chan_rate(c) or 0,
           "count": lambda c: c.get("count") or 0,
           "role": lambda c: (c.get("role") or "~", c.get("slot", 0)),
           "board": lambda c: (c.get("board") or 0, c.get("ch") or 0)}[ui.sort]
    slots.sort(key=key, reverse=ui.reverse)
    return slots


def view_daq(st, ui, width, room):
    slots = daq_order(st, ui)
    ui.order = [c.get("slot") for c in slots]
    if ui.sel not in ui.order:
        ui.sel = ui.order[0] if ui.order else None
    rf, _, _ = rf_info(st)
    hi = max([chan_rate(c) or 0 for c in slots if c.get("slot") != rf] + [1e-9])   # the RF's 10 kHz would set the scale
    live = any(c.get("rate") is not None for c in slots)
    cols = fit_columns(DAQ_COLS, width, 14)
    bw = max(0, width - len(head_cols(cols)) - 2)
    title = head_cols(cols) + f"{'live' if live else 'mean'} rate, 0–{si(hi)} Hz"
    head = [row((title[:width], "colhead"))]
    body = []
    for c in slots:
        r = chan_rate(c)
        style = "bad" if not c.get("count") else "dim" if c.get("slot") == rf else ""
        if c.get("slot") == ui.sel:
            style = "sel"
        fill = bar(min(1.0, (r or 0) / hi), bw)
        body.append(row((fmt_cols(cols, c), style), ("[", "dim"), (fill, "meter"), ("]", "dim")))
    filt = f"board {ui.board}" if ui.board is not None else "all boards"
    arrow = "▼" if ui.reverse else "▲"
    info = f"{len(body)} channels · {filt} · sort {ui.sort} {arrow}" + (" · silent hidden" if ui.hide_silent else "")
    return head, body, info, ui.order.index(ui.sel) if ui.sel in ui.order else None


GROUP_ORDER = ("T0", "T1", "T2")     # the tracker planes first, along the beam; the rest as they come


def plane_groups(slots):
    """[(name, [channel, ...])]: channels grouped by detector (the role before ':'), in position order
    (the number after ':'); roles without a number (cherenkov, RF) by slot."""
    groups = {}
    for c in slots:
        role = c.get("role") or "–"
        name, _, pos = role.partition(":")
        groups.setdefault(name, []).append(c)

    def pos(c):
        p = (c.get("role") or "").partition(":")[2]
        return (0, int(p), c.get("slot", 0)) if p.isdigit() else (1, 0, c.get("slot", 0))
    names = [n for n in GROUP_ORDER if n in groups] + [n for n in groups if n not in GROUP_ORDER]
    return [(n, sorted(groups[n], key=pos)) for n in names]


def cell_label(c):
    p = (c.get("role") or "").partition(":")[2]
    return p if p.isdigit() else str(c.get("slot", ""))


def mini_bar(frac, k):
    """k cells, eighth resolution."""
    lev = max(0.0, min(1.0, frac or 0.0)) * k * 8
    out = []
    for i in range(k):
        f = int(max(0, min(8, lev - i * 8)) + 0.5) if lev > i * 8 else 0
        out.append(BLOCKS[f] if f else "·" if i == 0 and lev == 0 else " ")
    return "".join(out)


def view_grid(st, ui, width, room):
    """Every channel on one page: one block per detector, channels in position order, a meter each."""
    slots = list((st.get("daq") or {}).get("slots") or [])
    groups = plane_groups(slots)
    metric = (lambda c: c.get("count") or 0) if ui.counts else chan_rate
    unit = "" if ui.counts else " Hz"
    lw = max([len(cell_label(c)) for c in slots] + [2])
    vw = 6
    # the widest meter that still puts every channel on the page
    for k in (8, 6, 4, 3, 2, 1):
        cw = lw + 1 + vw + 1 + k + 2
        per_row = max(1, (width + 2) // cw)
        need = sum(1 + -(-len(g) // per_row) for _, g in groups)
        if need <= room:
            break
    live = any(c.get("rate") is not None for c in slots)
    what = "hit count" if ui.counts else ("live rate" if live else "mean rate")
    head = [row((f"{what}{unit} per channel, in position order; each meter scaled to its detector's highest"
                 + (f" · board {ui.board} bright" if ui.board is not None else ""))[:width].ljust(width), "colhead")]
    body, grid_rows, order, cur = [], [], [], None
    for name, chans in groups:
        vals = [metric(c) for c in chans]
        hi = max([v or 0 for v in vals] + [1e-9])
        silent = sum(1 for c in chans if not c.get("count"))
        numbered = all((c.get("role") or "").partition(":")[2].isdigit() for c in chans)
        info = f"{len(chans)} ch · Σ {si(sum(v or 0 for v in vals))}{unit}" + (f" · {silent} silent" if silent else "") \
            + ("" if numbered else " · by slot") + "  "
        prof, _, _ = spark(vals, max(0, width - len(name) - len(info) - 2), zero=True)
        body.append(row((f"{name} ", "title"), (info, "bad" if silent else "dim"), (prof if numbered else "", "spark")))
        for i in range(0, len(chans), per_row):
            line, cells = [], []
            for c in chans[i:i + per_row]:
                v = metric(c)
                sel = c.get("slot") == ui.sel
                off = ui.board is not None and c.get("board") != ui.board
                lab_style = "sel" if sel else "bad" if not c.get("count") else "dim" if off else "bold"
                line += [(f"{cell_label(c):>{lw}}", lab_style),
                         (f" {si(v):>{vw}} ", "dim" if off else "bad" if not c.get("count") else ""),
                         (mini_bar((v or 0) / hi, k), "dim" if off else "meter"), ("  ", "")]
                cells.append(c.get("slot"))
                if sel:
                    cur = len(body)
            body.append(line)
            grid_rows.append(cells)
            order += cells
    ui.order, ui.grid_rows = order, grid_rows
    if ui.sel not in order:
        ui.sel = order[0] if order else None
        cur = 1 if order else None
    info = f"{len(slots)} channels · ←↑↓→ select · ↵ detail · t table · c {'rate' if ui.counts else 'count'}"
    return head, body, info, cur


def grid_move(ui, dr, dc):
    """Move the selection on the grid: dc along a row, dr between rows (same column, or the row's last)."""
    rows = getattr(ui, "grid_rows", None) or []
    where = next(((r, c) for r, cells in enumerate(rows) for c, s in enumerate(cells) if s == ui.sel), None)
    if where is None:
        return
    r, c = where
    if dc:
        flat = [s for cells in rows for s in cells]
        i = max(0, min(len(flat) - 1, flat.index(ui.sel) + dc))
        ui.sel = flat[i]
        return
    r = max(0, min(len(rows) - 1, r + dr))
    ui.sel = rows[r][min(c, len(rows[r]) - 1)]


def view_boards(st, ui, width, room):
    daq = st.get("daq") or {}
    secs = daq.get("seconds") or []
    rf, rfc, rfb = rf_info(st)
    ps = st.get("processed_s")

    def board_mean(b):           # as the per-second rates count: without the RF reference
        if not ps:
            return None
        n = (b.get("hits") or 0) - ((rfc or {}).get("count") or 0 if b.get("serial") == rfb else 0)
        return n / ps

    cols = fit_columns([
        ("BOARD", 6, ">", 0, lambda b: str(b.get("serial", "")) + ("*" if b.get("serial") == rfb else "")),
        ("ID", 2, ">", 6, lambda b: str(b.get("id", ""))),
        ("HITS", 7, ">", 1, lambda b: si(b.get("hits"))),
        ("Hz now", 7, ">", 0, lambda b: si(b.get("_now"))),
        ("Hz mean", 7, ">", 2, lambda b: si(board_mean(b))),
        ("FILES", 5, ">", 7, lambda b: str(b.get("files", 0))),
        ("MB", 7, ">", 5, lambda b: si((b.get("bytes") or 0) / 1e6)),
        ("GAPS", 4, ">", 3, lambda b: str(b.get("gaps", 0))),
        ("SKIP", 5, ">", 4, lambda b: si(b.get("skipped_words") or 0)),
        ("LATE", 5, ">", 6, lambda b: si(b.get("late") or 0)),
        ("QUIET", 6, ">", 3, lambda b: dur(b.get("quiet_s"))),
    ], width, 36)
    sw = max(0, width - len(head_cols(cols)) - 16)
    head = [row((head_cols(cols) + "hits/s of data time" + (" (0→max)" if ui.zero else " (min→max)"), "colhead"))]
    body = []
    for i, b in enumerate(st.get("boards") or []):
        series = [(s.get("boards") or [])[i] if i < len(s.get("boards") or []) else None for s in secs]
        b = dict(b, _now=next((v for v in reversed(series) if v is not None), None))
        alarm = b.get("quiet_alarm") or b.get("behind") or b.get("stuck") or b.get("gaps") or b.get("skipped_words")
        style = "bad" if alarm else ("dim" if not b.get("has_data") else "")
        line, lo, hi = spark(series, sw, ui.zero)
        body.append(row((fmt_cols(cols, b), style), (line, "spark"), ("" if lo is None else f" {si(lo)}–{si(hi)}", "dim")))
        flags = []
        if b.get("behind"):
            flags.append("behind")
        if b.get("stuck"):
            flags.append("stuck " + ",".join(map(str, b["stuck"])))
        if b.get("quiet_alarm"):
            flags.append("quiet alarm")
        if b.get("slow_couples"):
            flags.append("slow couples " + ",".join(map(str, b["slow_couples"])))
        if flags:
            body.append(row(("       !! " + "; ".join(flags), "bad")))
    if rfc:
        body.append(row(""))
        body.append(row((f"* ch {rfc.get('ch')} is the RF reference ({si(rfc.get('mean_rate'))} Hz): in HITS, "
                         "not in the rates (as on the web page); see RF in the header", "dim")))
    disk = daq.get("disk") or {}
    info = (f"{len(st.get('boards') or [])} boards · run {si(daq.get('run_bytes'))}B in {daq.get('files', '–')} files"
            + (f" · disk free {disk.get('free_gb', 0) / 1e3:.1f} of {disk.get('total_gb', 0) / 1e3:.1f} TB" if disk else ""))
    return head, body, info, None


def view_tracker(st, ui, width, room):
    counts = st.get("status_counts") or {}
    order = st.get("statuses") or list(counts)
    total = sum(counts.values()) or 1
    hist = [h for h in st.get("history") or [] if (h.get("t1") or 0) - (h.get("t0") or 0) > 0.9]   # not the partial second
    head = [row((f"{'FINDER RESULT':<12} {'BUNCHES':>9} {'SHARE':>6}  per second of data time"
                 + (" (0→max)" if ui.zero else " (min→max)"), "colhead"))]
    body = []
    sw = max(0, width - 33 - 16)

    def line(label, n, share, series, style=""):
        sp, lo, hi = spark(series, sw, ui.zero)
        return row((f"{label:<12} {si(n):>9} {share}  ", style), (sp, "spark"), ("" if lo is None else f" {si(lo)}–{si(hi)}", "dim"))
    for k in order:
        n = counts.get(k, 0)
        body.append(line(k, n, f"{100 * n / total:>5.1f}%", [(h.get("status") or {}).get(k, 0) for h in hist],
                         "good" if k in ("unique", "tracks") else ""))
    body.append(row(""))
    trk = [h.get("tracks", 0) for h in hist]
    bun = [h.get("bunches", 0) for h in hist]
    body.append(line("tracks/s", trk[-1] if trk else None, "      ", trk, "bold"))
    body.append(line("bunches/s", bun[-1] if bun else None, "      ", bun, "bold"))
    body.append(row(""))
    f = st.get("finder") or {}
    body.append(row(("Finder setup", "colhead")))
    for k in ("setting", "calibrated", "timing", "installed", "missing", "map_rows", "nsig", "threshold",
              "reference", "overflow", "uncalibrated"):
        if k in f:
            v = f[k]
            v = ", ".join(map(str, v)) if isinstance(v, list) else v
            body.append(row((f"  {k:<14}", "dim"), (str(v), "bold")))
    for k in ("map", "cabling"):
        if f.get(k):
            body.append(row((f"  {k:<14}", "dim"), (str(f[k]), "")))
    cnt = st.get("counters") or {}
    if cnt:
        body.append(row((f"  {'counters':<14}", "dim"), ("  ".join(f"{k} {si(v)}" for k, v in cnt.items()), "bold")))
    info = f"{si(sum(counts.values()))} bunches since Clear · unique {100 * counts.get('unique', 0) / total:.2f}%"
    return head, body, info, None


def crop(counts, lo_frac=0.001, hi_frac=0.995):
    """The bin range holding the middle of the distribution, a little padded."""
    tot = sum(counts)
    if tot <= 0:
        return 0, len(counts)
    acc, i0, i1 = 0, 0, len(counts)
    for i, n in enumerate(counts):
        if acc <= lo_frac * tot < acc + n:
            i0 = i
        if acc + n >= hi_frac * tot:
            i1 = i + 1
            break
        acc += n
    pad = max(1, (i1 - i0) // 20)
    return max(0, i0 - pad), min(len(counts), i1 + pad)


def chart_block(title, edges, counts, width, height, fmt, log=False, crop_it=True, note=""):
    """A titled bar chart with its x axis, as rows."""
    out = [row((title, "bold"), (note, "dim"))]
    if not counts or not sum(counts):
        out.append(row(("  no entries", "dim")))
        return out + [row("")] * height
    i0, i1 = crop(counts) if crop_it else (0, len(counts))
    vals = rebin(counts[i0:i1], width)
    for r in vchart(vals, height, log):
        out.append(row((r, "meter")))
    out.append(row((axis(edges[i0], edges[(i0 + i1) // 2], edges[i1], width, fmt), "dim")))
    return out


def clip(segs, w):
    """Segments cut to w cells."""
    out, n = [], 0
    for t, st in segs:
        if n >= w:
            break
        out.append((t[:w - n], st))
        n += len(t[:w - n])
    return out


def side_by_side(a, b, wa, gap=2):
    """Two lists of rows next to each other."""
    out = []
    for i in range(max(len(a), len(b))):
        ra = clip(a[i] if i < len(a) else [], wa)
        rb = b[i] if i < len(b) else []
        used = sum(len(t) for t, _ in ra)
        out.append(ra + [(" " * max(0, wa + gap - used), "")] + rb)
    return out


def view_chan(st, ui, width, room, ch):
    """One channel: pulse height, rate over time, time since the previous hit, time to the reference."""
    if ui.sel is None:
        return [row(("no channel selected: pick one on the DAQ channels view and press Enter", "dim"))], [], "", None
    c = next((c for c in (st.get("daq") or {}).get("slots") or [] if c.get("slot") == ui.sel), {})
    title = (f"slot {ui.sel} · board {c.get('board', '?')} ch {c.get('ch', '?')} · {c.get('role') or '–'} · "
             f"count {si(c.get('count'))} · rate {si(c.get('rate'))} Hz · mean {si(c.get('mean_rate'))} Hz"
             f" · late {si(c.get('late') or 0)} early {si(c.get('early') or 0)}")
    head = [row((title[:width].ljust(width), "colhead"))]
    if not ch or ch.get("slot") != ui.sel:
        return head, [row(("loading…", "dim"))], "↑↓ or [ ] other channels · Esc back", None
    body = []
    q = ch.get("q") or {}
    h_q = max(3, (room - 8) * 11 // 20)
    h_t = max(3, room - 8 - h_q)
    note = (f"  total {si(q.get('total'))} · Q=0 {si(q.get('zero'))} · saturated {si(q.get('sat'))}"
            + (" · log" if ui.log else ""))
    body += chart_block("Pulse height (Qlong)", q.get("edges") or [], q.get("counts") or [], width - 1, h_q,
                        lambda v: f"{v:.0f}", log=ui.log, note=note)
    rates = [r[1] for r in ch.get("rate") or []]
    sp, lo, hi = spark(rates, max(0, width - 30), ui.zero)
    body.append(row(("Rate ", "bold"), (f"{si(rates[-1] if rates else None):>6} Hz ", "bold"), (sp, "spark"),
                    ("" if lo is None else f" {si(lo)}–{si(hi)}", "dim")))
    body.append(row(""))
    wa = (width - 3) // 2
    iat = ch.get("iat") or {}
    left = chart_block("Time since the previous hit (log)", iat.get("edges") or [], iat.get("counts") or [], wa, h_t,
                       si_ns, crop_it=True)
    dt = ch.get("dt") or {}
    ref = ch.get("ref")
    if ref is None:
        right = [row(("Time to the reference", "bold")), row(("  no reference channel set (choose it on the web page)", "dim"))]
    else:
        right = chart_block(f"t − t_ref (slot {ref})", dt.get("edges") or [], dt.get("counts") or [], wa, h_t,
                            lambda v: f"{v:.0f}ns", crop_it=False)
    body += side_by_side(left, right, wa)
    return head, body, "↑↓ or [ ] other channels · l log · Esc back", None


# ---------------------------------------------------------------- shared drawing

HEAT = [(" ", ""), ("░", "heat1"), ("▒", "heat1"), ("▒", "heat2"), ("▓", "heat2"), ("▓", "heat3"), ("█", "heat3"), ("█", "heat4")]


def heat_style(frac):
    """A colour for 0..1 (the Map's planes and charts): cold to hot."""
    if frac is None or frac <= 0:
        return "dim"
    return ("heat1", "heat2", "heat3", "heat4")[min(3, int(frac * 4))]


def runs(chars, styles):
    """Characters with a style each -> segments, one per run of the same style."""
    out, cur, buf = [], None, []
    for ch, st in zip(chars, styles):
        if st != cur and buf:
            out.append(("".join(buf), cur))
            buf = []
        cur = st
        buf.append(ch)
    if buf:
        out.append(("".join(buf), cur))
    return out


def nice_ticks(lo, hi, width, min_gap=10):
    """Round tick values between lo and hi, at least min_gap columns apart."""
    span = hi - lo
    if span <= 0 or width <= 0:
        return []
    for step in (0.5, 1, 2, 5, 10, 20, 25, 50, 100, 200, 250, 500, 1000):
        if step / span * width >= min_gap:
            break
    first = math.ceil(lo / step) * step
    out, v = [], first
    while v <= hi + 1e-9:
        out.append(v)
        v += step
    return out


def tick_axis(lo, hi, width, unit=""):
    """An axis row: round values, each label centred under its position, none overlapping."""
    line = [" "] * width
    if width <= 0 or hi <= lo:
        return ""
    for v in nice_ticks(lo, hi, width, 10):
        x = int((v - lo) / (hi - lo) * (width - 1) + 0.5)
        lab = f"{v:g}{unit}"
        x0 = max(0, min(width - len(lab), x - len(lab) // 2))
        if all(c == " " for c in line[max(0, x0 - 1):min(width, x0 + len(lab) + 1)]):
            line[x0:x0 + len(lab)] = list(lab)
    return "".join(line)


def chart_rows(vals, height, styles=None, log=False):
    """vchart with a style per column: rows of segments."""
    rows = vchart(vals, height, log)
    if styles is None:
        return [row((r, "meter")) for r in rows]
    return [runs(r, styles) for r in rows]


class Canvas:
    """Braille dots: 2 × 4 per character. Each cell takes the style of its highest-priority dot."""
    BITS = ((0x01, 0x08), (0x02, 0x10), (0x04, 0x20), (0x40, 0x80))

    def __init__(self, w, h):
        self.w, self.h = w, h
        self.bits = [[0] * w for _ in range(h)]
        self.style = [[("", -1)] * w for _ in range(h)]
        self.text = {}

    def dot(self, px, py, style, prio=0):
        px, py = int(round(px)), int(round(py))
        if 0 <= px < self.w * 2 and 0 <= py < self.h * 4:
            cx, cy = px // 2, py // 4
            self.bits[cy][cx] |= self.BITS[py % 4][px % 2]
            if prio >= self.style[cy][cx][1]:
                self.style[cy][cx] = (style, prio)

    def line(self, a, b, style, prio=0):
        (x0, y0), (x1, y1) = a, b
        n = int(max(abs(x1 - x0), abs(y1 - y0))) + 1
        if n > 4000:
            return
        for i in range(n + 1):
            t = i / n
            self.dot(x0 + (x1 - x0) * t, y0 + (y1 - y0) * t, style, prio)

    def put(self, cx, cy, s, style):
        for i, ch in enumerate(s):
            if 0 <= cx + i < self.w and 0 <= cy < self.h:
                self.text[(cx + i, cy)] = (ch, style)

    def rows(self):
        out = []
        for y in range(self.h):
            chars, styles = [], []
            for x in range(self.w):
                if (x, y) in self.text:
                    ch, st = self.text[(x, y)]
                else:
                    b = self.bits[y][x]
                    ch, st = (chr(0x2800 + b), self.style[y][x][0]) if b else (" ", "")
                chars.append(ch)
                styles.append(st)
            out.append(runs(chars, styles))
        return out


# ---------------------------------------------------------------- map: where the hits are, to scale

_GEOM = {}


def geometry(setting):
    """{plane: {ch: (x_lo, x_hi, gang)}} from finder/channels.csv for one setting (the finder's own table)."""
    import csv
    import os
    if setting not in _GEOM:
        g = {}
        try:
            with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "finder", "channels.csv")) as fh:
                for r in csv.DictReader(fh):
                    if r["setting"] == setting:
                        g.setdefault(int(r["plane"]), {})[int(r["ch"])] = (float(r["a_lo_mm"]), float(r["a_hi_mm"]), int(r["gang_width"]))
        except (OSError, KeyError, ValueError):
            g = {}
        _GEOM[setting] = g
    return _GEOM[setting]


def owned(geo):
    """Each channel's own stretch of x: neighbouring strips overlap, so the boundary is halfway between centres.
    Returns (sorted channels, boundaries [n+1])."""
    chs = sorted(geo, key=lambda c: (geo[c][0] + geo[c][1]) / 2)
    cen = [(geo[c][0] + geo[c][1]) / 2 for c in chs]
    b = [geo[chs[0]][0]] + [(cen[i - 1] + cen[i]) / 2 for i in range(1, len(chs))] + [geo[chs[-1]][1]]
    return chs, b


def plane_metric(st, ui):
    """{plane: {ch: (value, channel entry)}} from the DAQ slots, by role 'Tn:ch'."""
    metric = (lambda c: c.get("count") or 0) if ui.counts else chan_rate
    out = {}
    for c in (st.get("daq") or {}).get("slots") or []:
        name, _, pos = (c.get("role") or "").partition(":")
        if name in ("T0", "T1", "T2") and pos.isdigit():
            out.setdefault(int(name[1]), {})[int(pos)] = (metric(c), c)
    return out


def density_columns(geo, vals, x0, x1, width):
    """Per column: (channel or None, value per mm or None)."""
    import bisect
    if not geo:
        return [(None, None)] * width
    chs, b = owned(geo)
    wid = {chs[i]: b[i + 1] - b[i] for i in range(len(chs))}
    out = []
    for j in range(width):
        x = x0 + (j + 0.5) * (x1 - x0) / width
        if x < b[0] or x > b[-1]:
            out.append((None, None))
            continue
        ch = chs[max(0, min(len(chs) - 1, bisect.bisect_right(b, x) - 1))]
        v = vals.get(ch, (None, None))[0]
        out.append((ch, None if v is None else v / max(wid[ch], 1e-6)))
    return out


def top_view(tv, geo_all, vals_all, width, height):
    """The floor tool's top view to scale in braille: z along the beam to the right, x (bend side) up."""
    v = tv.get("view") or {}
    z0, z1, x0, x1 = v.get("z_lo", -1000), v.get("z_hi", 3300), v.get("x_lo", -1000), v.get("x_hi", 1200)
    s = max((z1 - z0) / (width * 2), (x1 - x0) / (height * 4))          # mm per dot, the same both ways
    zc, xc = (z0 + z1) / 2, (x0 + x1) / 2
    cv = Canvas(width, height)

    def P(z, x):                                   # mm -> dot coordinates, the view centred
        return (z - zc) / s + width, (xc - x) / s + height * 2
    pole = tv.get("pole") or {}
    if pole:
        corners = [(pole["z0"], pole["x0"]), (pole["z1"], pole["x0"]), (pole["z1"], pole["x1"]), (pole["z0"], pole["x1"]), (pole["z0"], pole["x0"])]
        for a, b in zip(corners, corners[1:]):
            cv.line(P(*a), P(*b), "dim", 1)
    out = tv.get("magnet_outline") or []
    for a, b in zip(out, out[1:]):
        cv.line(P(*a), P(*b), "dim", 1)
    box = tv.get("cherenkov_box") or []
    for a, b in zip(box, box[1:]):
        cv.line(P(*a), P(*b), "info", 2)
    pe = tv.get("pipe_exit_z_mm")
    if pe is not None:
        cv.line(P(z0, 0), P(pe, 0), "dim", 1)
    zs = tv.get("z") or []
    for species, style in (("e-", "warn"), ("pi-", "spark")):
        for trk in (tv.get("paths") or {}).get(species) or []:
            pts = [P(z, x) for z, x in zip(zs, trk)]
            for a, b in zip(pts, pts[1:]):
                cv.line(a, b, style, 3)
    # the planes, each dot by the live hit density at its x
    for d in tv.get("detectors") or []:
        name = d.get("name", "")
        pl = int(name[1]) if len(name) == 2 and name[0] == "T" and name[1].isdigit() else None
        z, lo, hi = d.get("z", 0), d.get("x_lo", 0), d.get("x_hi", 0)
        (pz, py_hi), (_, py_lo) = P(z, hi), P(z, lo)
        geo = geo_all.get(pl) or {}
        n = max(2, int(abs(py_lo - py_hi)) + 1)
        cols = density_columns(geo, vals_all.get(pl) or {}, lo, hi, n) if geo else [(None, None)] * n
        peak = max([c[1] or 0 for c in cols] + [1e-12])
        for i in range(n):
            frac = (cols[i][1] or 0) / peak
            for dz in (-1, 0, 1):                  # three dots wide so it reads as a plane
                cv.dot(pz + dz, py_lo - i * (py_lo - py_hi) / (n - 1), heat_style(frac) if cols[i][1] else "bad", 9)
        cx, cy = int(pz // 2), int(py_hi // 4) - 1
        cv.put(cx - len(name) // 2, max(0, cy), name, "bold")
    if box:
        bz, bx = max(p[0] for p in box), max(p[1] for p in box)
        cx, cy = P(bz, bx)
        cv.put(int(cx // 2) + 1, max(0, int(cy // 4)), "Cherenkov", "info")
    cv.put(0, int(P(z0, 0)[1] // 4), "beam→", "dim")
    return cv.rows(), s


def strip(pl, geo, vals, x0, x1, width, height, ui, z=None):
    """One plane: a header, Hz/mm against x (to scale), the channel numbers, the mm axis."""
    name = f"T{pl}"
    chs = sorted(geo)
    cols = density_columns(geo, vals, x0, x1, width)
    dens = [c[1] or 0 for c in cols]
    peak = max(dens + [1e-12])
    unit = "hits/mm" if ui.counts else "Hz/mm"
    tot = sum((vals.get(c, (0,))[0] or 0) for c in chs)
    silent = [c for c in chs if c in vals and not (vals[c][1].get("count"))]
    uncabled = [c for c in chs if c not in vals]
    j = max(range(width), key=lambda i: dens[i]) if width else 0
    xpk = x0 + (j + 0.5) * (x1 - x0) / max(width, 1)
    lo, hi = min(g[0] for g in geo.values()), max(g[1] for g in geo.values())
    info = (f"  {'z ' + format(z, 'g') + ' mm · ' if z is not None else ''}x {lo:.0f}–{hi:.0f} mm · {len(chs)} ch · Σ {si(tot)}"
            f"{'' if ui.counts else ' Hz'} · peak {si(peak)} {unit} at x {xpk:.0f} mm (ch {cols[j][0]})")
    extra = (f" · silent {','.join(map(str, silent))}" if silent else "") + (f" · not cabled {','.join(map(str, uncabled))}" if uncabled else "")
    out = [row((name, "title"), (info, "dim"), (extra, "bad"))]
    styles = [heat_style(d / peak) if d else "" for d in dens]
    out += chart_rows(dens, height, styles, ui.log)
    # channel row: numbers at their centres where they fit; silent ×, not cabled ·
    line, sty = [" "] * width, [""] * width
    for i, (ch, d) in enumerate(cols):
        if ch is None:
            continue
        if ch in uncabled:
            line[i], sty[i] = "·", "dim"
        elif ch in silent:
            line[i], sty[i] = "×", "bad"
        else:
            line[i] = "─"
            sty[i] = "dim"
    for ch in chs:
        if not (ch == 1 or ch % 5 == 0 or ch == chs[-1]):
            continue
        c = (geo[ch][0] + geo[ch][1]) / 2
        x = int((c - x0) / (x1 - x0) * width)
        lab = str(ch)
        a = max(0, min(width - len(lab), x - len(lab) // 2))
        if 0 <= x < width and all(ch_ in "─·" for ch_ in line[max(0, a - 1):a + len(lab) + 1]) and \
                all(s_ != "bad" for s_ in sty[a:a + len(lab)]):
            for k, cc in enumerate(lab):
                line[a + k], sty[a + k] = cc, "bold"
    out.append(runs(line, sty))
    out.append(row((tick_axis(x0, x1, width, ""), "dim")))
    return out


def view_map(st, ui, width, room, tv):
    setting = (st.get("finder") or {}).get("setting") or "high"
    geo_all = geometry(setting)
    vals_all = plane_metric(st, ui)
    unit = "hits/mm" if ui.counts else "Hz/mm"
    live = any(c.get("rate") is not None for c in (st.get("daq") or {}).get("slots") or [])
    what = "hit count" if ui.counts else ("live rate" if live else "mean rate")
    head = [row((f"{what} per mm of x, to scale ({setting} setting, finder/channels.csv) · x: bend side · "
                 f"{'one x axis for all planes' if ui.common_x else 'each plane on its own x range'}"
                 + (" · log" if ui.log else ""))[:width].ljust(width), "colhead")]
    if not geo_all:
        return head, [row((f"no channel geometry for setting {setting!r} in finder/channels.csv", "bad"))], "", None
    zs = {d.get("name"): d.get("z") for d in (tv or {}).get("detectors") or []}
    planes = [p for p in (0, 1, 2) if p in geo_all]
    h = 4 if room >= 3 * 7 + 10 else 3
    need_strips = len(planes) * (h + 3)
    top_h = room - need_strips - 2
    body = []
    if ui.top and tv and top_h >= 6:
        rows, s = top_view(tv, geo_all, vals_all, width, top_h)
        body.append(row(("Top view, to scale", "bold"),
                         (f"  {s * 2:.0f} mm per column · the floor tool's outlines, ", "dim"), ("π⁻", "spark"),
                         (" and ", "dim"), ("e⁻", "warn"), (" tracks · planes coloured by ", "dim"),
                         ("low", "heat1"), (" → ", "dim"), ("high", "heat4"), (f" {unit}", "dim")))
        body += rows
    elif ui.top and not tv:
        body.append(row(("top view: none published by the floor tool (/expected_view)", "dim")))
    if ui.common_x:
        x0 = min(g[0] for p in planes for g in geo_all[p].values())
        x1 = max(g[1] for p in planes for g in geo_all[p].values())
        pad = 0.01 * (x1 - x0)
        x0, x1 = x0 - pad, x1 + pad
    for p in planes:
        geo = geo_all[p]
        if not ui.common_x:
            x0 = min(g[0] for g in geo.values())
            x1 = max(g[1] for g in geo.values())
        body += strip(p, geo, vals_all.get(p) or {}, x0, x1, width, h, ui, zs.get(f"T{p}"))
    others = [c for c in (st.get("daq") or {}).get("slots") or [] if not (c.get("role") or "").startswith("T")]
    if others:
        body.append(row(("other  ", "title"), ("   ".join(f"{c.get('role')} (slot {c.get('slot')}) {si(chan_rate(c))} Hz"
                                                         for c in others), "dim")))
    info = "x shared axis · v top view · c counts · l log"
    return head, body, info, None


# ---------------------------------------------------------------- RF timing: the time of flight against the accelerator's RF

def rotate(vals, shift):
    n = len(vals)
    return [vals[(i + shift) % n] for i in range(n)] if n else vals


def view_rf(st, ui, width, room, rf):
    tof, tag = (rf or {}).get("tof"), (rf or {}).get("tag")
    head = [row(("time of flight modulo the accelerator RF, channels aligned on this run's LED flashes "
                 "(as the web page's RF timing tab)")[:width].ljust(width), "colhead")]
    if not tof:
        return head, [row(((rf or {}).get("error") or "loading…", "dim"))], "", None
    P = tof.get("period_ns") or 19.75
    planes = tof.get("planes") or []
    nper = 2 if ui.two_periods else 1
    body = []
    lw = 17
    cw = width - lw

    # per plane: summed over charge, each to its own peak
    profiles = {p.get("name"): [sum(r) for r in p.get("counts") or []] for p in planes}
    shift = {}
    for name, prof in profiles.items():
        shift[name] = (max(range(len(prof)), key=lambda i: prof[i]) - len(prof) // 2) if ui.centre and prof and sum(prof) else 0
    a0 = -P / 2 if ui.centre else 0.0             # centred: ns from each plane's own peak
    hpl = 3 if room >= 40 else 2
    body.append(row(("TOF modulo the RF, all charges", "bold"),
                    (f"  period {P:.3f} ns · {si(tof.get('rf_hits'))} RF triggers · each plane to its own peak"
                     + (" · centred on each peak" if ui.centre else " · 0 = the RF reference's phase") + (" · log" if ui.log else ""), "dim")))
    for p in planes:
        name = p.get("name")
        prof = rotate(profiles[name], shift[name]) * nper
        cols = rebin(prof, cw)
        for i, r in enumerate(chart_rows(cols, hpl, log=ui.log)):
            lab = f"{name:<10}{si(p.get('n')):>6}" if i == hpl - 1 else ""
            body.append([(f"{lab:<{lw}}", "bold")] + r)
    body.append(row((" " * lw + tick_axis(a0, a0 + nper * P, cw, ""), "dim")))
    body.append(row((" " * lw + ("ns from each plane's peak" if ui.centre else "ns after the RF reference's phase").center(cw), "dim")))

    # the chosen plane: TOF modulo the RF against Qlong
    if planes:
        p = planes[ui.rf_plane % len(planes)]
        name = p.get("name")
        counts = p.get("counts") or []
        nq = len(counts[0]) if counts else 0
        qmax = p.get("qmax") or tof.get("qmax") or 8192
        qprof = [sum(r[q] for r in counts) for q in range(nq)]
        q0, q1 = crop(qprof, 0.0, 0.995)
        hm = max(4, min(14, room - 30))
        body.append(row(("TOF modulo the RF against Qlong · ", "bold"), (name, "title"),
                        (f"  n {si(p.get('n'))} · shade: log of the count · n: next plane", "dim")))
        grid = [rotate([counts[t][q] for t in range(len(counts))], shift.get(name, 0)) * nper for q in range(q0, q1)]
        # rows: Q from high (top) to low; columns: phase
        cells = []
        for r in range(hm):
            qa = q0 + (q1 - q0) * (hm - 1 - r) // hm
            qb = q0 + (q1 - q0) * (hm - r) // hm
            band = [sum(col) for col in zip(*grid[qa - q0:max(qb, qa + 1) - q0])] if grid else []
            cells.append(rebin(band, cw))
        top = max([math.log10(1 + v) for line in cells for v in line] + [1e-9])
        for r, line in enumerate(cells):
            qa = q0 + (q1 - q0) * (hm - 1 - r) // hm
            lab = f"{qa * qmax / max(nq, 1):>7.0f} " if r % 3 == 0 or r == hm - 1 else " " * 8
            chars, sty = [], []
            for v in line:
                lev = 0 if v <= 0 else max(1, min(7, int(math.log10(1 + v) / top * 7 + 0.5)))
                chars.append(HEAT[lev][0])
                sty.append(HEAT[lev][1])
            body.append([(f"{'Qlong' if r == 0 else '':<6}"[:lw - 8].ljust(lw - 8), "dim"), (lab, "dim")] + runs(chars, sty))
        body.append(row((" " * lw + tick_axis(a0, a0 + nper * P, cw, ""), "dim")))

    # the gas Cherenkov's tag fraction against T1's phase
    if tag and tag.get("den"):
        den, num, rnd = tag.get("den") or [], tag.get("num") or [], tag.get("rnd") or []
        frac = [(n / d if d else 0) for n, d in zip(num, den)]
        rfrac = [(r / d if d else 0) for r, d in zip(rnd, den)]
        sh = shift.get("T1", 0)
        frac, rfrac = rotate(frac, sh) * nper, rotate(rfrac, sh) * nper
        hi = max(frac + [1e-9])
        body.append(row(("Cherenkov tag fraction of T1 hits against T1's RF phase", "bold"),
                        (f"  random level ~{100 * sum(rfrac) / max(1, len(rfrac)):.2f}% · LED flashes left out", "dim")))
        cols = rebin(frac, cw)
        # rebin sums when it groups bins: the fraction wants the mean
        k = max(1, len(frac) // max(1, cw)) if len(frac) > cw else 1
        cols = [v / k for v in cols]
        for i, r in enumerate(chart_rows(cols, 3)):
            lab = f"{100 * hi:>5.1f}%" if i == 0 else ("   0%" if i == 2 else "")
            body.append([(f"{lab:>{lw - 1}} ", "dim")] + r)
        body.append(row((" " * lw + tick_axis(a0, a0 + nper * P, cw, ""), "dim")))

    # each PMT's timing: t(T1) − t(Cherenkov), the coincidence window marked
    pmts = (tag or {}).get("pmts") or []
    if pmts:
        body.append(row(("Cherenkov timing: t(T1 hit) − t(Cherenkov hit), after the LED timing", "bold"),
                        ("  ━ the coincidence window", "dim")))
        wa = (width - 3) // max(1, len(pmts)) - (2 if len(pmts) > 1 else 0)
        blocks = []
        dt0, step = tag.get("dt0_ns", -60), tag.get("dt_bin_ns", 0.5)
        for pm in pmts:
            dt = pm.get("dt") or []
            hi_ns = dt0 + step * len(dt)
            inw = 100 * (pm.get("in_window") or 0) / max(1, pm.get("pairs") or 0)
            b = [row((f"PMT {pm.get('board')}/{pm.get('ch')}", "title"),
                     (f" delay {pm.get('delay_ns')} peak {pm.get('peak_ns')} ns · {inw:.0f}% of {si(pm.get('pairs'))} in window", "dim"))]
            b += chart_rows(rebin(dt, wa), 4)
            w0, w1 = (pm.get("window_ns") or [0, 0])[:2]
            win = "".join("━" if w0 <= dt0 + (j + 0.5) * (hi_ns - dt0) / wa <= w1 else " " for j in range(wa))
            b.append(row((win, "warn")))
            b.append(row((tick_axis(dt0, hi_ns, wa, ""), "dim")))
            blocks.append(b)
        merged = blocks[0]
        for b in blocks[1:]:
            merged = side_by_side(merged, b, wa)
        body += merged

    # the LED alignment: each board's shift
    al = tof.get("align") or {}
    if al:
        by = {}
        for c in al.get("channels") or []:
            by.setdefault((c.get("board")), set()).add((c.get("shift_ns"), c.get("how")))
        parts = []
        for bd, sh in by.items():
            vals_ = sorted(s for s, _ in sh if s is not None)
            hows = {h for _, h in sh}
            if vals_:
                parts.append(f"{bd} {vals_[0]:+.2f}" + (f"…{vals_[-1]:+.2f}" if vals_[-1] != vals_[0] else "")
                             + ("" if hows <= {"board"} else f" ({'/'.join(sorted(map(str, hows)))})"))
        body.append(row(("LED alignment", "bold"), (f"  {al.get('method')} · {si(al.get('flashes'))} flashes · "
                                                     "each board's shift, ns (how, when not the whole board alike):", "dim")))
        line = "  "
        for part in parts:                         # wrapped to the width
            if len(line) + len(part) + 3 > width and line.strip():
                body.append(row((line, "")))
                line = "  "
            line += part + "   "
        if line.strip():
            body.append(row((line, "")))
    info = "n plane · w 1/2 periods · e centre on peak · l log"
    return head, body, info, None


# ---------------------------------------------------------------- screen

HELP = """\
 FSUDAQ online, terminal view (read-only)

   1-6 / Tab      DAQ channels / Map / RF timing / Boards / Tracker / Channel
   ← ↑ ↓ →        select a channel on the grid (↑ ↓ in the table), or scroll
   t              DAQ channels as the grid (all on one page) or the sortable table
   c              the grid shows hit counts instead of rates
   Enter          the selected channel in detail
   [ ]  ↑ ↓       previous / next channel in the detail view
   Esc ⌫          back from the detail view
   s              sort the table: slot, rate, count, role, board
   r              reverse the sort
   z              hide channels with no hits
   b              pick out one board (cycles; all at the end)
   a              sparklines: min→max (default) or 0→max
   x v            Map: one x axis for all planes · the top view on/off
   n w e          RF timing: next plane in the Qlong map · two periods · centre on the peak
   l              log scale (channel pulse height, map, RF timing)
   PgUp PgDn Home End   scroll
   p              pause updates
   + -            refresh interval
   h ?            this help
   q              quit

 The grid: one block per detector (T0, T1, T2, then the rest), channels
 in position order, each meter scaled to its block's highest channel;
 the sparkline after a block's name is its profile across the positions.
 The map: each plane's rate per mm of x (strips overlap and widen away
 from the focus, so per mm, not per channel), to scale from the finder's
 channels.csv for the run's setting; × silent channel, · not cabled.
 The top view is the floor tool's published picture (/expected_view).
 Red channels have no hits; the RF reference is dim and is not
 in the rate scale or the board rates (as on the web page). Rates are
 live while a run is written, otherwise the run's mean. Nothing here
 changes the dashboard: Clear, window, reference and run selection
 stay on the web page.
"""


def render(st, ui, url, width, height, fetch, ch=None, extra=None):
    """The whole screen: header, column heads, scrolling body, footer."""
    if st is None:
        msg = fetch.get("error") or "connecting…"
        rows = [row(("FSUDAQ online ", "title"), (url, "dim")), row(""), row((msg, "bad" if fetch.get("error") else "dim"))]
        return rows + [row("")] * max(0, height - len(rows) - 1) + [footer(ui, fetch, width)]
    if ui.help:
        rows = [row((l, "")) for l in HELP.splitlines()]
        return rows[:height - 1] + [row("")] * max(0, height - 1 - len(rows)) + [footer(ui, fetch, width)]
    top = header(st, ui, url, width)
    room = max(3, height - len(top) - 3)          # tabs, column heads, footer
    extra = extra or {}
    if ui.view == "chan":
        head, body, info, cur = view_chan(st, ui, width, room, ch)
    elif ui.view == "map":
        head, body, info, cur = view_map(st, ui, width, room, extra.get("tv"))
    elif ui.view == "rf":
        head, body, info, cur = view_rf(st, ui, width, room, extra.get("rf"))
    else:
        daq = view_daq if ui.table else view_grid
        head, body, info, cur = {"daq": daq, "boards": view_boards, "tracker": view_tracker}[ui.view](st, ui, width, room)
    tabs = []
    for i, v in enumerate(VIEWS):
        name = VIEW_NAMES[v] + (f" {ui.sel}" if v == "chan" and ui.sel is not None else "")
        tabs.append((f" {i + 1} {name} ", "tab_on" if v == ui.view else "tab"))
        tabs.append((" ", ""))
    tabs.append(("  " + info, "dim"))
    top = top + [row(*tabs)] + head
    room = max(1, height - len(top) - 1)
    ui.body_rows = room
    if cur is not None:                           # keep the selection on screen
        if cur < ui.scroll:
            ui.scroll = cur
        elif cur >= ui.scroll + room:
            ui.scroll = cur - room + 1
    ui.scroll = max(0, min(ui.scroll, len(body) - room))
    shown = body[ui.scroll:ui.scroll + room]
    shown += [row("")] * (room - len(shown))
    return top + shown + [footer(ui, fetch, width)]


def footer(ui, fetch, width):
    keys = [("1-6", "View"), ("↵", "Detail"), ("t", "Table" if not ui.table else "Grid"), ("s", "Sort"), ("r", "Rev"), ("z", "Silent"), ("b", "Board"),
            ("a", "Scale"), ("p", "Pause" if not ui.paused else "Resume"), ("h", "Help"), ("q", "Quit")]
    if width < 100:
        keys = [k for k in keys if k[0] in ("1-6", "↵", "t", "h", "q")]
    segs = []
    for k, label in keys:
        segs += [(k, "key"), (label + " ", "keylabel")]
    age = time.time() - fetch["at"] if fetch.get("at") else None
    stat = ("PAUSED " if ui.paused else "") + (f"err: {fetch['error']} " if fetch.get("error") else "")
    stat += f"{age:.0f}s ago · {ui.interval:g}s" if age is not None else ""
    used = sum(len(t) for t, _ in segs)
    segs.append((stat.rjust(max(0, width - used))[:max(0, width - used)], "bad" if fetch.get("error") else "keylabel"))
    return segs


# ---------------------------------------------------------------- data

def get_json(url):
    with urllib.request.urlopen(url, timeout=5) as r:
        body = r.read()
        kind = r.headers.get("Content-Type", "")
    try:
        return json.loads(body)
    except ValueError:  # an HTML error page or a cut-off reply: say so, never show the body
        raise ValueError(f"not JSON from {url.split('?')[0].rsplit('/', 1)[-1] or '/'} "
                         f"({kind or 'no type'}, {len(body)} bytes)") from None


def err_text(e, n=80):
    return clean(getattr(e, "reason", None) or e or type(e).__name__)[:n]


class Poller(threading.Thread):
    """Fetches in the background so the keys never wait on the network."""

    def __init__(self, url, ui):
        super().__init__(daemon=True)
        self.base, self.ui = url.rstrip("/"), ui
        self.state, self.info, self.chan = None, {}, None
        self.extra, self.extra_at = {}, {}
        self.lock = threading.Lock()
        self.wake = threading.Event()

    def fetch_once(self):
        t = time.time()
        try:
            st = get_json(self.base + "/state")
            with self.lock:
                self.state, self.info = st, {"at": time.time(), "ms": 1e3 * (time.time() - t)}
        except Exception as e:  # keep showing the last good state, say why it is stale
            with self.lock:
                self.info = dict(self.info, error=err_text(e))
        self.fetch_chan()
        self.fetch_extra()

    def fetch_extra(self, force=False):
        """The Map's top view (it changes only with a floor-tool publish) and the RF timing histograms
        (every 3 s, as the web page does), each only while its view is open."""
        now = time.time()
        want = {"map": ("tv", 60.0), "rf": ("rf", 3.0)}.get(self.ui.view)
        if not want:
            return
        key, every = want
        if not force and now - self.extra_at.get(key, 0) < every:
            return
        self.extra_at[key] = now
        try:
            if key == "tv":
                val = get_json(self.base + "/expected_view").get("top_view")
            else:
                val = dict(tof=get_json(self.base + "/rftof?align=1"), tag=get_json(self.base + "/rftag?align=1"))
        except Exception as e:
            val = None if key == "tv" else dict((self.extra.get("rf") or {}), error=f"RF timing: {err_text(e)}")
        with self.lock:
            self.extra[key] = val

    def fetch_chan(self):
        """The selected channel's histograms, only while its view is open. /compare is /channel without
        the PSD map and rate spectrum: less for the server to send."""
        sl = self.ui.sel
        if self.ui.view != "chan" or sl is None:
            return
        try:
            d = get_json(f"{self.base}/compare?slots={int(sl)}")
            chans = d.get("channels") or []
            if chans:
                with self.lock:
                    self.chan = dict(chans[0], ref=d.get("ref"))
        except Exception:
            pass

    def run(self):
        while True:
            if not self.ui.paused:
                self.fetch_once()
            self.wake.wait(self.ui.interval)
            self.wake.clear()

    def snapshot(self):
        with self.lock:
            return self.state, dict(self.info), self.chan, dict(self.extra)


# ---------------------------------------------------------------- curses

def styles():
    curses.start_color()
    try:
        curses.use_default_colors()
        bg = -1
    except curses.error:
        bg = curses.COLOR_BLACK
    pairs = {}
    spec = {"title": (curses.COLOR_CYAN, bg, curses.A_BOLD), "dim": (curses.COLOR_WHITE, bg, curses.A_DIM),
            "bold": (-1 if bg == -1 else curses.COLOR_WHITE, bg, curses.A_BOLD),
            "good": (curses.COLOR_GREEN, bg, 0), "warn": (curses.COLOR_YELLOW, bg, 0), "bad": (curses.COLOR_RED, bg, curses.A_BOLD),
            "info": (curses.COLOR_CYAN, bg, 0),
            "mode_good": (curses.COLOR_BLACK, curses.COLOR_GREEN, curses.A_BOLD),
            "mode_warn": (curses.COLOR_BLACK, curses.COLOR_YELLOW, curses.A_BOLD),
            "mode_info": (curses.COLOR_BLACK, curses.COLOR_CYAN, curses.A_BOLD),
            "mode_bad": (curses.COLOR_WHITE, curses.COLOR_RED, curses.A_BOLD),
            "mode_dim": (curses.COLOR_BLACK, curses.COLOR_WHITE, 0),
            "colhead": (curses.COLOR_BLACK, curses.COLOR_GREEN, 0),
            "sel": (curses.COLOR_BLACK, curses.COLOR_CYAN, 0),
            "meter": (curses.COLOR_GREEN, bg, curses.A_BOLD), "spark": (curses.COLOR_CYAN, bg, 0),
            "tab": (curses.COLOR_WHITE, bg, curses.A_DIM), "tab_on": (curses.COLOR_BLACK, curses.COLOR_CYAN, curses.A_BOLD),
            "heat1": (curses.COLOR_BLUE, bg, curses.A_BOLD), "heat2": (curses.COLOR_CYAN, bg, 0),
            "heat3": (curses.COLOR_YELLOW, bg, curses.A_BOLD), "heat4": (curses.COLOR_RED, bg, curses.A_BOLD),
            "key": (curses.COLOR_WHITE, bg, curses.A_BOLD), "keylabel": (curses.COLOR_BLACK, curses.COLOR_CYAN, 0)}
    for i, (name, (fg, b, attr)) in enumerate(spec.items(), start=1):
        try:
            curses.init_pair(i, fg, b)
            pairs[name] = curses.color_pair(i) | attr
        except curses.error:
            pairs[name] = attr
    pairs[""] = 0
    return pairs


def draw(scr, rows, pairs, width):
    scr.erase()
    h = scr.getmaxyx()[0]
    for y, segs in enumerate(rows[:h]):
        x = 0
        for text, style in segs:
            if x >= width:
                break
            text = clean(text)[:width - x]
            try:
                scr.addstr(y, x, text, pairs.get(style, 0))
            except curses.error:   # the bottom-right cell
                pass
            x += len(text)
        # column heads, the selected row and the footer run the full width
        fill = next((s for _, s in segs if s == "sel"), None) or (segs[-1][1] if segs else None)
        if fill in ("colhead", "keylabel", "sel") and x < width:
            try:
                scr.addstr(y, x, " " * (width - x), pairs.get(fill, 0))
            except curses.error:
                pass
    scr.noutrefresh()
    curses.doupdate()


def board_serials(state):
    return [b.get("serial") for b in (state or {}).get("boards") or []]


def move_sel(ui, step):
    if not ui.order:
        return
    i = ui.order.index(ui.sel) if ui.sel in ui.order else 0
    ui.sel = ui.order[max(0, min(len(ui.order) - 1, i + step))]


def handle_key(ch, ui, poller):
    """Returns False to quit."""
    state, _, _, _ = poller.snapshot()
    page = max(1, ui.body_rows - 1)
    chan_before = (ui.view, ui.sel)
    if ch in (ord("q"), ord("Q"), curses.KEY_F10):
        return False
    if ui.help and ch in (27, ord("h"), ord("?"), curses.KEY_F1):
        ui.help = False
    elif ord("1") <= ch < ord("1") + len(VIEWS):
        ui.view, ui.scroll, ui.help = VIEWS[ch - ord("1")], 0, False
    elif ui.view == "daq" and not ui.table and ch in (curses.KEY_LEFT, curses.KEY_RIGHT, curses.KEY_UP, curses.KEY_DOWN,
                                                      ord("j"), ord("k")):
        grid_move(ui, {curses.KEY_UP: -1, curses.KEY_DOWN: 1, ord("k"): -1, ord("j"): 1}.get(ch, 0),
                  {curses.KEY_LEFT: -1, curses.KEY_RIGHT: 1}.get(ch, 0))
    elif ch in (9, curses.KEY_RIGHT):
        ui.view, ui.scroll = VIEWS[(VIEWS.index(ui.view) + 1) % len(VIEWS)], 0
    elif ch in (curses.KEY_BTAB, curses.KEY_LEFT):
        ui.view, ui.scroll = VIEWS[(VIEWS.index(ui.view) - 1) % len(VIEWS)], 0
    elif ch in (10, 13, curses.KEY_ENTER) and ui.view == "daq" and ui.sel is not None:
        ui.view, ui.scroll = "chan", 0
    elif ch in (27, curses.KEY_BACKSPACE, 127, 8) and ui.view == "chan":
        ui.view, ui.scroll = "daq", 0
    elif ch in (ord("s"), curses.KEY_F6):
        ui.sort = SORTS[(SORTS.index(ui.sort) + 1) % len(SORTS)]
        ui.reverse = ui.sort in ("rate", "count")   # biggest first, like htop's CPU%
    elif ch == ord("r"):
        ui.reverse = not ui.reverse
    elif ch == ord("z"):
        ui.hide_silent = not ui.hide_silent
    elif ch == ord("b"):
        opts = board_serials(state) + [None]
        ui.board = opts[(opts.index(ui.board) + 1) % len(opts)] if ui.board in opts else opts[0]
        ui.scroll = 0
    elif ch == ord("x") and ui.view == "map":
        ui.common_x = not ui.common_x
    elif ch == ord("v") and ui.view == "map":
        ui.top = not ui.top
    elif ch == ord("w") and ui.view == "rf":
        ui.two_periods = not ui.two_periods
    elif ch == ord("e") and ui.view == "rf":
        ui.centre = not ui.centre
    elif ch == ord("n") and ui.view == "rf":
        ui.rf_plane += 1
    elif ch == ord("t"):
        ui.table, ui.scroll = not ui.table, 0
    elif ch == ord("c"):
        ui.counts = not ui.counts
    elif ch == ord("a"):
        ui.zero = not ui.zero
    elif ch == ord("l"):
        ui.log = not ui.log
    elif ch in (ord("h"), ord("?"), curses.KEY_F1):
        ui.help = True
    elif ch == ord("p"):
        ui.paused = not ui.paused
    elif ch in (ord("+"), ord("=")):
        ui.interval = min(30.0, ui.interval * 2)
    elif ch == ord("-"):
        ui.interval = max(0.5, ui.interval / 2)
    elif ui.view in ("daq", "chan") and ch in (curses.KEY_DOWN, ord("j"), ord("]")):
        move_sel(ui, 1)
    elif ui.view in ("daq", "chan") and ch in (curses.KEY_UP, ord("k"), ord("[")):
        move_sel(ui, -1)
    elif ui.view == "daq" and ch in (curses.KEY_NPAGE, ord(" ")):
        move_sel(ui, page)
    elif ui.view == "daq" and ch == curses.KEY_PPAGE:
        move_sel(ui, -page)
    elif ui.view == "daq" and ch == curses.KEY_HOME:
        move_sel(ui, -10 ** 6)
    elif ui.view == "daq" and ch == curses.KEY_END:
        move_sel(ui, 10 ** 6)
    elif ch in (curses.KEY_DOWN, ord("j")):
        ui.scroll += 1
    elif ch in (curses.KEY_UP, ord("k")):
        ui.scroll = max(0, ui.scroll - 1)
    elif ch in (curses.KEY_NPAGE, ord(" ")):
        ui.scroll += page
    elif ch == curses.KEY_PPAGE:
        ui.scroll = max(0, ui.scroll - page)
    elif ch == curses.KEY_HOME:
        ui.scroll = 0
    elif ch == curses.KEY_END:
        ui.scroll = 10 ** 6       # clamped by render()
    if (ui.view, ui.sel) != chan_before and ui.view == "chan":
        threading.Thread(target=poller.fetch_chan, daemon=True).start()   # the new channel now, not at the next tick
    if ui.view != chan_before[0] and ui.view in ("map", "rf"):
        threading.Thread(target=poller.fetch_extra, daemon=True).start()
    if ch in (ord("+"), ord("="), ord("-"), ord("p")):
        poller.wake.set()
    return True


def main_curses(scr, url, ui):
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    curses.set_escdelay(25) if hasattr(curses, "set_escdelay") else None
    scr.timeout(200)
    pairs = styles()
    poller = Poller(url, ui)
    poller.start()
    while True:
        h, w = scr.getmaxyx()
        state, info, ch, extra = poller.snapshot()
        try:
            rows = render(state, ui, url, w, h, info, ch, extra)
        except Exception as e:  # odd data from the server (a run starting, a restart): keep running, say why
            rows = [row(("FSUDAQ online ", "title"), (url, "dim")), row(""),
                    row((f"cannot draw this view: {type(e).__name__}: {err_text(e, max(20, w - 40))}", "bad")),
                    row(("still polling; another view (1-6) or q to quit", "dim"))]
            rows += [row("")] * max(0, h - len(rows) - 1) + [footer(ui, info, w)]
        draw(scr, rows, pairs, w)
        key = scr.getch()
        if key in (-1, curses.KEY_RESIZE):
            continue
        if not handle_key(key, ui, poller):
            return


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--url", default="http://127.0.0.1:8050", help="the dashboard (default %(default)s)")
    ap.add_argument("--interval", type=float, default=1.0, help="seconds between updates (default %(default)s)")
    ap.add_argument("--view", choices=VIEWS, default="daq")
    ap.add_argument("--table", action="store_true", help="DAQ channels as the sortable table")
    ap.add_argument("--slot", type=int, default=None, help="the selected channel (for --view chan)")
    ap.add_argument("--once", action="store_true", help="print one plain-text snapshot and exit")
    ap.add_argument("--width", type=int, default=0, help="--once: width (default: the terminal's)")
    ap.add_argument("--height", type=int, default=0, help="--once: height (default: 50)")
    a = ap.parse_args()
    locale.setlocale(locale.LC_ALL, "")
    ui = Ui()
    ui.view, ui.interval, ui.sel, ui.table = a.view, max(0.5, a.interval), a.slot, a.table
    if a.once:
        import shutil
        p = Poller(a.url, ui)
        p.fetch_once()
        p.fetch_extra(force=True)
        st, info, ch, extra = p.snapshot()
        w = a.width or shutil.get_terminal_size((140, 50)).columns
        for segs in render(st, ui, a.url, w, a.height or 50, info, ch, extra):
            print(clean("".join(t for t, _ in segs))[:w].rstrip())
        return 1 if st is None else 0

    hung = []

    def hangup(signum, frame):  # ssh dropped, window closed, kill: leave through curses.wrapper's cleanup
        hung.append(signum)
        raise SystemExit(128 + signum)
    for sig in (signal.SIGHUP, signal.SIGTERM, signal.SIGQUIT):
        signal.signal(sig, hangup)
    try:
        curses.wrapper(main_curses, a.url, ui)
    except KeyboardInterrupt:
        pass
    except BaseException:
        if not hung:
            restore_terminal()
            raise                                # a real bug: show the traceback on the restored terminal
    restore_terminal()
    if hung:                                     # the tty may be gone: no traceback, no flush, just go
        os._exit(128 + hung[0])
    return 0


def restore_terminal():
    """After curses' own endwin: put back what a full-screen program can leave behind if its exit is cut
    short (hidden cursor, application keypad, mouse reporting, bracketed paste), so the
    shell does not echo escape codes. Harmless if all is already back."""
    try:
        fd = os.open("/dev/tty", os.O_WRONLY | os.O_NOCTTY)
    except OSError:
        return                                   # no terminal any more (ssh gone): nothing to restore
    try:
        os.write(fd, b"\x1b[?1000l\x1b[?1002l\x1b[?1003l\x1b[?1006l\x1b[?2004l"
                     b"\x1b[?1l\x1b>\x1b[0m\x1b[?25h")
    except OSError:
        pass
    finally:
        os.close(fd)


if __name__ == "__main__":
    sys.exit(main())
