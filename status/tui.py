#!/usr/bin/env python3
"""The experiment status page (status_server.py) in a terminal, htop-style.

Reads GET /api/status every 2 s, as the page does, and draws it: a lamp per card, the cards with a dot
per item, the details of the selected item, and the past hour of every PLC reading. The status server
gives the latest value only, so the history comes from pion-ros's topic plots on romulus (the same
/plc_status/status fields, an hour of them; --plots), or failing that from this view's own polls since it
started. Read-only: it never saves limits or changes anything. Standard library only (curses).

    python3 status/tui.py                          # the status server on this machine, port 8060
    python3 status/tui.py --url http://odin.psi.ch:8060
    python3 status/tui.py --once                   # print one plain-text snapshot and exit
    python3 status/tui.py --plots ""               # no history from romulus

Keys: ↑↓ select · Enter details · Esc close · p pause · h help · q quit
"""
import argparse
import collections
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

_CTRL = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def clean(text):
    """Text safe to put on the terminal: no control bytes. An escape sequence in a server string
    (a log line with colours, an HTML error page) would otherwise show as ^[[..m junk or, printed raw,
    change the terminal's modes."""
    return _CTRL.sub("·", str(text))


def err_text(e, n=80):
    return clean(getattr(e, "reason", None) or e or type(e).__name__)[:n]


def read_json(r):
    """The body of an open urllib response as JSON; an HTML error page or a cut-off reply raises a short
    message about it, never the body itself."""
    body = r.read()
    try:
        return json.loads(body)
    except ValueError:
        raise ValueError(f"not JSON from {r.geturl().split('?')[0]} "
                         f"({r.headers.get('Content-Type') or 'no type'}, {len(body)} bytes)") from None

# the page's fixed order: Cherenkov, DAQ, Trackers, Online analysis, PLC, ROS
GROUPS = ("Cherenkov", "DAQ", "Trackers", "Online analysis", "PLC", "PLC inputs (not named)", "ROS")
LAMPS = ("Cherenkov", "DAQ", "Trackers", "Online analysis", "PLC", "ROS")
# the page's columns: Cherenkov | DAQ over Trackers | Online analysis over PLC over ROS
COLUMNS3 = (("Cherenkov",), ("DAQ", "Trackers"), ("Online analysis", "PLC", "PLC inputs (not named)", "ROS"))
COLUMNS2 = (("Cherenkov", "DAQ", "Trackers"), ("Online analysis", "PLC", "PLC inputs (not named)", "ROS"))
RANK = {"info": 0, "idle": 0, "ok": 1, "nodata": 2, "notice": 3, "problem": 4, "warn": 5, "bad": 6, "critical": 7}
WORDS = {"ok": "as it should be", "info": "a plain fact", "idle": "not running", "nodata": "no data",
         "notice": "to read", "problem": "data problem (not safety)", "warn": "SAFETY WARNING",
         "bad": "SAFETY ALARM", "critical": "SAFETY ALARM: ACT NOW"}
SPARK = "▁▂▃▄▅▆▇█"
HISTORY_S = 3600


# ---------------------------------------------------------------- helpers

def worst(levels):
    levels = [l for l in levels if l]
    return max(levels, key=lambda l: RANK.get(l, 0)) if levels else "nodata"


def spark(values, width):
    vals = [v for v in values if v is not None][-width:] if width > 0 else []
    if not vals:
        return ""
    lo, hi = min(vals), max(vals)
    lo = min(lo, hi - 0.02 * abs(hi) if hi else lo)      # a steady reading's last digit is not news
    if hi <= lo:
        return SPARK[3] * len(vals)
    return "".join(SPARK[min(7, int((v - lo) / (hi - lo) * 7 + 0.5))] for v in vals)


def spark_time(h, width, span_floor=0.0):
    """The history [(t, v)] as `width` time buckets of equal length (each its mean; a gap is a blank), scaled
    min→max with at least span_floor between them (a sensor's last digit flickering is not news)."""
    if width <= 0 or len(h) < 2:
        return ""
    t0, t1 = h[0][0], h[-1][0]
    if t1 <= t0:
        return ""
    sums, ns = [0.0] * width, [0] * width
    for t, v in h:
        i = min(width - 1, int((t - t0) / (t1 - t0) * width))
        sums[i] += v
        ns[i] += 1
    means = [s_ / n if n else None for s_, n in zip(sums, ns)]
    known = [m for m in means if m is not None]
    lo, hi = min(known), max(known)
    if hi - lo < span_floor:
        mid = (hi + lo) / 2
        lo, hi = mid - span_floor / 2, mid + span_floor / 2
    if hi <= lo:
        return "".join(SPARK[3] if m is not None else " " for m in means)
    return "".join(" " if m is None else SPARK[max(0, min(7, int((m - lo) / (hi - lo) * 7 + 0.5)))] for m in means)


def span_floor(item):
    """0.1% of the sensor's range (details.range), in its units: small enough for the pressure's 0.05 psig rule."""
    r = (item.get("details") or {}).get("range")
    try:
        return 0.001 * abs(float(r[1]) - float(r[0]))
    except (TypeError, ValueError, IndexError):
        return 0.0


def fmt(v):
    if isinstance(v, float):
        if math.isnan(v) or math.isinf(v):
            return str(v)
        return f"{v:.4g}"
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(fmt(x) for x in v) + "]"
    if isinstance(v, dict):
        return "{" + ", ".join(f"{k}: {fmt(x)}" for k, x in v.items()) + "}"
    return str(v)


def plc_source(item):
    """(field, scale, offset) of an item that is one PLC reading (details.source 'plc.ai0'), else None."""
    d = item.get("details") or {}
    src = d.get("source")
    if not isinstance(src, str) or not src.startswith("plc."):
        return None
    try:
        return src[4:], float(d.get("scale", 1) or 1), float(d.get("offset", 0) or 0)
    except (TypeError, ValueError):
        return None


def value_of(item):
    """A PLC reading's value, as the page shows it: raw × scale + offset (None if the item has none)."""
    d = item.get("details") or {}
    try:
        return float(d["raw"]) * float(d.get("scale", 1) or 1) + float(d.get("offset", 0) or 0)
    except (KeyError, TypeError, ValueError):
        return None


def wrap(text, width, indent=""):
    out, line = [], ""
    for word in str(text).split(" "):
        if len(line) + len(word) + 1 > width and line:
            out.append(line)
            line = indent
        line += ("" if not line or line == indent else " ") + word
    if line:
        out.append(line)
    return out or [""]


def row(*segs):
    return [s if isinstance(s, tuple) else (s, "") for s in segs]


# ---------------------------------------------------------------- state

class Ui:
    def __init__(self):
        self.sel = None          # (group, name) of the selected item
        self.details = False
        self.paused = False
        self.help = False
        self.order = []          # the items in display order, for the arrow keys
        self.scroll = 0


class Poller(threading.Thread):
    def __init__(self, url, ui, interval=2.0, plots=None):
        super().__init__(daemon=True)
        self.url, self.ui, self.interval = url.rstrip("/") + "/api/status", ui, interval
        self.plots = plots.rstrip("/") if plots else None
        self.plc = {}            # field: [(t, raw value)] from the topic plots, the past hour
        self.plc_last = 0.0
        self.plots_err = None
        self.state, self.err, self.at = None, None, None
        self.hist = collections.defaultdict(lambda: collections.deque(maxlen=int(HISTORY_S / interval) + 10))
        self.lock = threading.Lock()

    def fetch_once(self):
        try:
            with urllib.request.urlopen(self.url, timeout=5) as r:
                st = read_json(r)
            now = time.time()
            with self.lock:
                self.state, self.err, self.at = st, None, now
                self.interval = float(st.get("interval") or self.interval)
                for it in st.get("items") or []:
                    v = value_of(it)
                    if v is not None:
                        self.hist[(it.get("group"), it.get("name"))].append((now, v))
        except Exception as e:  # keep the last good state on screen, greyed: it is not current
            with self.lock:
                self.err = err_text(e, 80)
        self.fetch_plc()

    def fetch_plc(self):
        """The PLC fields the items read, from the topic plots: the past hour once, then what is new."""
        if not self.plots or not self.state:
            return
        fields = sorted({ps[0] for ps in (plc_source(i) for i in self.state.get("items") or []) if ps})
        if not fields:
            return
        since = self.plc_last or time.time() - HISTORY_S
        url = (f"{self.plots}/api/series?topic=/plc_status/status&fields={','.join(fields)}&since={since:.3f}")
        try:
            with urllib.request.urlopen(url, timeout=5) as r:
                d = read_json(r)
            t = d.get("t") or []
            cut = time.time() - HISTORY_S
            with self.lock:
                for f, vals in (d.get("fields") or {}).items():
                    h = self.plc.setdefault(f, [])
                    h += [(ti, v) for ti, v in zip(t, vals) if v is not None]
                    while h and h[0][0] < cut:
                        h.pop(0)
                if t:
                    self.plc_last = t[-1]
                self.plots_err = None
        except Exception as e:
            with self.lock:
                self.plots_err = err_text(e, 60)

    def run(self):
        while True:
            if not self.ui.paused:
                self.fetch_once()
            time.sleep(self.interval)

    def snapshot(self):
        """state, error, when, and per item its history [(t, value)]: from the topic plots when they have it."""
        with self.lock:
            hist = {k: list(v) for k, v in self.hist.items()}
            for it in (self.state or {}).get("items") or []:
                ps = plc_source(it)
                if ps and len(self.plc.get(ps[0]) or []) > 1:
                    f, sc, off = ps
                    hist[(it.get("group"), it.get("name"))] = [(t, v * sc + off) for t, v in self.plc[f]]
            return self.state, self.err, self.at, hist


# ---------------------------------------------------------------- rendering

def stale(at, interval):
    return at is None or time.time() - at > 3 * interval + 3


def lvl_style(level, current, blink_on):
    """A level's style; nothing coloured once the data is not current, as on the page."""
    if not current:
        return "dim"
    if level in ("bad", "critical"):
        return "bad_on" if blink_on else "bad"
    return {"ok": "ok", "notice": "notice", "problem": "problem", "warn": "warn"}.get(level, "dim")


def card(group, items, hist, ui, width, current, blink_on):
    lv = worst(i.get("level") for i in items)
    title = f"─ {group} "
    rows = [row((title, lvl_style(lv, current, blink_on) if RANK.get(lv, 0) > 1 else "bold"), ("─" * max(0, width - len(title)), "dim"))]
    for it in items:
        key = (it.get("group"), it.get("name"))
        sel = key == ui.sel
        st = lvl_style(it.get("level"), current, blink_on)
        name = it.get("name") or ""
        text = it.get("text") or ""
        h = hist.get(key) or []
        sw = 16 if (width >= 70 and h) else 0
        nw = min(max(len(i.get("name") or "") for i in items), max(14, width * 2 // 5))
        tw = max(0, width - 2 - nw - 1 - (sw + 1 if sw else 0))
        segs = [("● ", st), (f"{name[:nw]:<{nw}} ", "sel" if sel else "bold"), (f"{text[:tw]:<{tw}}", "sel" if sel else "")]
        if sw:
            segs.append((" " + spark_time(h, sw, span_floor(it)), "spark"))
        rows.append(segs)
    rows.append(row(""))
    return rows


def details(it, hist, width, height):
    if not it:
        return []
    key = (it.get("group"), it.get("name"))
    out = [row((f" {it.get('group')} · {it.get('name')} "[:width].ljust(width), "colhead"))]
    lv = it.get("level")
    out.append(row(("level ", "dim"), (f"{lv} ({WORDS.get(lv, '')})", "bold"), ("   ", ""),
                   (("age " + format(it.get("age_s"), ".1f") + " s") if it.get("age_s") is not None else "", "dim")))
    for line in wrap(it.get("text") or "", width):
        out.append(row((line, "")))
    h = hist.get(key) or []
    if h:
        vals = [v for _, v in h]
        span = h[-1][0] - h[0][0]
        lab = f"past {span / 60:.0f} min: {min(vals):.4g} – {max(vals):.4g}  "
        out.append(row(("history ", "dim"), (lab, "dim"), (spark_time(h, max(0, width - len(lab) - 9), span_floor(it)), "spark")))
    for k, v in (it.get("details") or {}).items():
        lines = wrap(f"{k}: {fmt(v)}", width - 2, "    ")
        for i, line in enumerate(lines):
            out.append(row(("  " + line, "dim" if i else "")))
    return out[:height]


def render(st, err, at, hist, ui, url, width, height, interval=2.0):
    blink_on = int(time.time() * 2) % 2 == 0
    if st is None:
        return [row(("Experiment status ", "title"), (url, "dim")), row(""), row((err or "connecting…", "bad" if err else "dim"))] \
            + [row("")] * max(0, height - 4) + [footer(ui, err, at, width)]
    current = not stale(at, interval)
    items = st.get("items") or []
    by = collections.OrderedDict((g, []) for g in GROUPS)
    for it in items:
        by.setdefault(it.get("group") or "?", []).append(it)
    by = collections.OrderedDict((g, v) for g, v in by.items() if v)
    if ui.help:
        rows = [row((l, "")) for l in HELP.splitlines()]
        return rows[:height - 1] + [row("")] * max(0, height - 1 - len(rows)) + [footer(ui, err, at, width)]

    top = []
    modes = "  ".join(f"{m.get('name', k)}: {m.get('value')}" for k, m in (st.get("modes") or {}).items())
    top.append(row(("Experiment status ", "title"), (url + "  ", "dim"), (f"modes  {modes}" if modes else "", "")))
    s = st.get("summary") or {}
    sl = s.get("level")
    badge = f" {WORDS.get(sl, sl or '?').upper()} "
    top.append(row((badge, "badge_" + lvl_style(sl, current, blink_on)), ("  " + (s.get("text") or ""), "bold")))
    lamps = []
    for g in LAMPS:
        lv = worst(i.get("level") for i in by.get(g, []))
        lamps += [("● ", lvl_style(lv, current, blink_on) if g in by else "dim"), (g + "    ", "bold" if RANK.get(lv, 0) > 3 else "")]
    top.append(lamps)
    if not current:
        top.append(row((f"NOT CURRENT: no answer from the status server ({err or 'stale'}): colours are off, as on the page", "bad")))
    top.append(row(""))

    # the cards in columns
    cols = COLUMNS3 if width >= 180 else COLUMNS2 if width >= 100 else (tuple(by),)
    known = {g for c in cols for g in c}
    extra = [g for g in by if g not in known]
    cols = [list(c) for c in cols]
    cols[-1] += extra
    gap = 2
    cw = (width - gap * (len(cols) - 1)) // len(cols)
    col_rows, order = [], []
    for c in cols:
        rs = []
        for g in c:
            if g in by:
                rs += card(g, by[g], hist, ui, cw, current, blink_on)
                order += [(i.get("group"), i.get("name")) for i in by[g]]
        col_rows.append(rs)
    ui.order = order
    if ui.sel not in order:
        ui.sel = order[0] if order else None
    body = []
    for i in range(max(len(r) for r in col_rows) if col_rows else 0):
        line = []
        for j, rs in enumerate(col_rows):
            segs = rs[i] if i < len(rs) else []
            used = sum(len(t) for t, _ in segs)
            line += segs + [(" " * (max(0, cw - used) + (gap if j < len(col_rows) - 1 else 0)), "")]
        body.append(line)

    sel_item = next((i for i in items if (i.get("group"), i.get("name")) == ui.sel), None)
    det = details(sel_item, hist, width, max(6, height // 3)) if ui.details else []
    room = max(1, height - len(top) - len(det) - 1)
    # keep the selected item on screen
    cur = next((i for i, r in enumerate(body) if any(st_ == "sel" for _, st_ in r)), None)
    if cur is not None:
        if cur < ui.scroll:
            ui.scroll = cur
        elif cur >= ui.scroll + room:
            ui.scroll = cur - room + 1
    ui.scroll = max(0, min(ui.scroll, max(0, len(body) - room)))
    shown = body[ui.scroll:ui.scroll + room]
    shown += [row("")] * (room - len(shown))
    return top + shown + det + [footer(ui, err, at, width)]


def footer(ui, err, at, width):
    keys = [("↑↓", "Select"), ("↵", "Details"), ("p", "Pause" if not ui.paused else "Resume"), ("h", "Help"), ("q", "Quit")]
    segs = []
    for k, label in keys:
        segs += [(k, "key"), (label + " ", "keylabel")]
    age = time.time() - at if at else None
    stat = ("PAUSED " if ui.paused else "") + (f"err: {err} " if err else "") + (f"updated {age:.0f}s ago" if age is not None else "")
    used = sum(len(t) for t, _ in segs)
    segs.append((stat.rjust(max(0, width - used))[:max(0, width - used)], "bad" if err else "keylabel"))
    return segs


HELP = """\
 Experiment status, terminal view (read-only)

   ↑ ↓ / j k      select an item
   Enter          its details (and the history this view has kept of a PLC reading)
   Esc            close the details
   PgUp PgDn      scroll
   p              pause updates
   h ?            this help
   q              quit

 Colours as on the page: green as it should be · grey a plain fact, not running
 or no data · blue to read · pink a data problem (not safety) · yellow safety
 warning · red, flashing: safety alarm. When the server stops answering every
 colour goes grey, so nothing is read as current. Limits are changed on the
 page (/limits, from odin or an ssh tunnel), never here.
"""


# ---------------------------------------------------------------- curses

def styles():
    curses.start_color()
    try:
        curses.use_default_colors()
        bg = -1
    except curses.error:
        bg = curses.COLOR_BLACK
    spec = {"title": (curses.COLOR_CYAN, bg, curses.A_BOLD), "dim": (curses.COLOR_WHITE, bg, curses.A_DIM),
            "bold": (-1 if bg == -1 else curses.COLOR_WHITE, bg, curses.A_BOLD),
            "ok": (curses.COLOR_GREEN, bg, curses.A_BOLD), "notice": (curses.COLOR_BLUE, bg, curses.A_BOLD),
            "problem": (curses.COLOR_MAGENTA, bg, curses.A_BOLD), "warn": (curses.COLOR_YELLOW, bg, curses.A_BOLD),
            "bad": (curses.COLOR_RED, bg, curses.A_BOLD), "bad_on": (curses.COLOR_WHITE, curses.COLOR_RED, curses.A_BOLD),
            "badge_ok": (curses.COLOR_BLACK, curses.COLOR_GREEN, curses.A_BOLD),
            "badge_notice": (curses.COLOR_WHITE, curses.COLOR_BLUE, curses.A_BOLD),
            "badge_problem": (curses.COLOR_BLACK, curses.COLOR_MAGENTA, curses.A_BOLD),
            "badge_warn": (curses.COLOR_BLACK, curses.COLOR_YELLOW, curses.A_BOLD),
            "badge_bad": (curses.COLOR_WHITE, curses.COLOR_RED, curses.A_BOLD),
            "badge_bad_on": (curses.COLOR_RED, curses.COLOR_WHITE, curses.A_BOLD),
            "badge_dim": (curses.COLOR_BLACK, curses.COLOR_WHITE, 0),
            "sel": (curses.COLOR_BLACK, curses.COLOR_CYAN, 0), "spark": (curses.COLOR_CYAN, bg, 0),
            "colhead": (curses.COLOR_BLACK, curses.COLOR_GREEN, 0),
            "key": (curses.COLOR_WHITE, bg, curses.A_BOLD), "keylabel": (curses.COLOR_BLACK, curses.COLOR_CYAN, 0)}
    pairs = {"": 0}
    for i, (name, (fg, b, attr)) in enumerate(spec.items(), start=1):
        try:
            curses.init_pair(i, fg, b)
            pairs[name] = curses.color_pair(i) | attr
        except curses.error:
            pairs[name] = attr
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
            except curses.error:
                pass
            x += len(text)
        if segs and segs[-1][1] in ("colhead", "keylabel") and x < width:
            try:
                scr.addstr(y, x, " " * (width - x), pairs.get(segs[-1][1], 0))
            except curses.error:
                pass
    scr.noutrefresh()
    curses.doupdate()


def handle_key(ch, ui):
    if ch in (ord("q"), ord("Q"), curses.KEY_F10):
        return False
    if ui.help:
        ui.help = False
        return True
    i = ui.order.index(ui.sel) if ui.sel in ui.order else 0
    if ch in (curses.KEY_DOWN, ord("j")) and ui.order:
        ui.sel = ui.order[min(len(ui.order) - 1, i + 1)]
    elif ch in (curses.KEY_UP, ord("k")) and ui.order:
        ui.sel = ui.order[max(0, i - 1)]
    elif ch in (10, 13, curses.KEY_ENTER):
        ui.details = not ui.details
    elif ch in (27, curses.KEY_BACKSPACE, 127):
        ui.details = False
    elif ch == curses.KEY_NPAGE:
        ui.scroll += 10
    elif ch == curses.KEY_PPAGE:
        ui.scroll = max(0, ui.scroll - 10)
    elif ch == ord("p"):
        ui.paused = not ui.paused
    elif ch in (ord("h"), ord("?"), curses.KEY_F1):
        ui.help = True
    return True


def main_curses(scr, url, ui, plots=None):
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    if hasattr(curses, "set_escdelay"):
        curses.set_escdelay(25)
    scr.timeout(250)
    pairs = styles()
    poller = Poller(url, ui, plots=plots)
    poller.start()
    while True:
        h, w = scr.getmaxyx()
        st, err, at, hist = poller.snapshot()
        try:
            rows = render(st, err, at, hist, ui, url, w, h, poller.interval)
        except Exception as e:  # odd data from the server: keep running, say why
            rows = [row((f"cannot draw this view: {type(e).__name__}: {err_text(e, max(20, w - 40))}", "bad")),
                    row(("still polling; h for help, q to quit", "dim"))]
        draw(scr, rows, pairs, w)
        ch = scr.getch()
        if ch in (-1, curses.KEY_RESIZE):
            continue
        if not handle_key(ch, ui):
            return


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--url", default="http://127.0.0.1:8060", help="the status server (default %(default)s)")
    ap.add_argument("--plots", default="http://romulus.psi.ch:8780",
                    help="pion-ros topic plots, for the PLC readings' past hour ('' for none; default %(default)s)")
    ap.add_argument("--once", action="store_true", help="print one plain-text snapshot and exit")
    ap.add_argument("--details", metavar="NAME", help="--once: also the details of the item with this name")
    ap.add_argument("--width", type=int, default=0)
    ap.add_argument("--height", type=int, default=0)
    a = ap.parse_args()
    locale.setlocale(locale.LC_ALL, "")
    ui = Ui()
    if a.once:
        import shutil
        p = Poller(a.url, ui, plots=a.plots)
        p.fetch_once()
        st, err, at, hist = p.snapshot()
        if a.details and st:
            it = next((i for i in st.get("items") or [] if i.get("name") == a.details), None)
            if it:
                ui.sel, ui.details = (it.get("group"), it.get("name")), True
        w = a.width or shutil.get_terminal_size((140, 50)).columns
        for segs in render(st, err, at, hist, ui, a.url, w, a.height or 50):
            print(clean("".join(t for t, _ in segs))[:w].rstrip())
        return 1 if st is None else 0
    hung = []

    def hangup(signum, frame):  # ssh dropped, window closed, kill: leave through curses.wrapper's cleanup
        hung.append(signum)
        raise SystemExit(128 + signum)
    for sig in (signal.SIGHUP, signal.SIGTERM, signal.SIGQUIT):
        signal.signal(sig, hangup)
    try:
        curses.wrapper(main_curses, a.url, ui, a.plots)
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
    short (hidden cursor, application keypad, mouse reporting, bracketed paste), so the shell does not
    echo escape codes. Harmless if all is already back."""
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
