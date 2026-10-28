#!/usr/bin/env python3
"""numba kernels for the FSUDAQ online pipeline.

`index_file` walks the board/couple aggregate headers of an .fsu file. It is a linked chain
of variable-length blocks, so the pure-Python version in fsu.py costs ~1 us per header.
Same output as fsu.index_file. `decode_range` decodes an indexed file.

    python3 fsu_fast.py RUN/*.fsu        # compare with fsu.py on real files
"""
from __future__ import annotations

import os

import numpy as np

try:
    from numba import njit
    HAVE_NUMBA = True
except ImportError:  # pragma: no cover
    HAVE_NUMBA = False

    def njit(*a, **k):
        def deco(f):
            return f
        return deco if not (a and callable(a[0])) else a[0]


# ------------------------------------------------------------------ index
@njit(cache=True, nogil=True)
def _index_kernel(w):
    n = w.size
    cap = n // 2 + 1
    ev_start = np.empty(cap, np.int64); nev = np.empty(cap, np.int64)
    couple = np.empty(cap, np.int64); agg = np.empty(cap, np.int64)
    wpe_a = np.empty(cap, np.int64); extra = np.empty(cap, np.int64)
    counters = np.empty(n // 4 + 1, np.int64)
    board_ids = np.zeros(32, np.int64)
    k = 0; na = 0; malformed = 0; pos = 0; bad_header = 0
    while pos < n:
        w0 = np.int64(w[pos])
        if (w0 >> 28) != 0xA:
            bad_header = 1
            break
        size = w0 & 0x0FFFFFFF
        if size < 4 or pos + size > n:
            break
        w1 = np.int64(w[pos + 1]); ctr = np.int64(w[pos + 2]) & 0x7FFFFF
        mask = w1 & 0xFF
        board_ids[(w1 >> 27) & 31] += 1
        counters[na] = ctr; na += 1
        p = pos + 4; end = pos + size
        for c in range(8):
            if not (mask >> c) & 1:
                continue
            if p + 2 > end:
                malformed += 1; break
            c0 = np.int64(w[p]); c1 = np.int64(w[p + 1])
            if not (c0 >> 31):
                malformed += 1; break
            csize = c0 & 0x3FFFFF
            has_wave = (c1 >> 27) & 1; has_extra = (c1 >> 28) & 1
            has_ts = (c1 >> 29) & 1; has_chg = (c1 >> 30) & 1
            nsw = ((c1 & 0xFFFF) * 8) // 2 if has_wave else 0
            wpe = has_ts + nsw + has_extra + has_chg
            body = csize - 2
            if wpe == 0 or body % wpe != 0 or p + csize > end:
                malformed += 1; p += csize; continue
            m = body // wpe
            if m > 0:
                ev_start[k] = p + 2; nev[k] = m; couple[k] = c
                agg[k] = ctr; wpe_a[k] = wpe; extra[k] = has_extra; k += 1
            p += csize
        pos += size
    return (ev_start[:k], nev[:k], couple[:k], agg[:k], wpe_a[:k], extra[:k],
            counters[:na], board_ids, na, malformed, n - pos, bad_header)


def index_file(words: np.ndarray):
    """Drop-in for fsu.index_file (same FileIndex), ~50x faster."""
    import fsu
    w = np.ascontiguousarray(words, dtype=np.uint32)
    (ev_start, nev, couple, agg, wpe, extra, counters, board_ids, n_agg,
     malformed, truncated, bad_header) = _index_kernel(w)
    if bad_header:
        pos = int(w.size - truncated)
        raise ValueError(f"expected board-aggregate header at word {pos}, got 0x{int(w[pos]):08X}")
    return fsu.FileIndex(ev_start=ev_start, nev=nev, couple=couple, agg_counter=agg, wpe=wpe,
                         has_extra=extra, counters=counters,
                         board_ids=set(int(i) for i in np.flatnonzero(board_ids)),
                         n_aggregates=int(n_agg), malformed=int(malformed), truncated_words=int(truncated))


# ------------------------------------------------------------------ decode
TICK_PS = 2000          # x730; a file's own tick comes from its name (fsu.tick_ps_of: 4 ns on x725)
FINE_DIV = 1024
from fsu import tick_ps_of   # noqa: E402


@njit(cache=True, nogil=True)
def _decode_kernel(w, ev_start, nev, couple, wpe, has_extra, t_lo, t_hi, tick, ts_out, ch_out, e_out, s_out):
    """Linear pass over couple aggregates: only what the pipeline needs (time, channel,
    long and short charge), keeping hits with t_lo <= ts < t_hi. Returns the number kept."""
    k = 0
    for a in range(ev_start.size):
        p = ev_start[a]; step = wpe[a]; c2 = couple[a] * 2; hx = has_extra[a]
        for j in range(nev[a]):
            ttt_w = np.int64(w[p])
            chg_w = np.int64(w[p + step - 1])
            if hx == 1:
                extra_w = np.int64(w[p + step - 2])
                ext = extra_w >> 16
                fine = extra_w & 0x3FF
            else:
                ext = 0; fine = 0
            ts = ((ext << 31) | (ttt_w & 0x7FFFFFFF)) * tick + (fine * tick) // FINE_DIV
            if ts >= t_lo and ts < t_hi:
                ts_out[k] = ts
                ch_out[k] = c2 + (ttt_w >> 31)
                e_out[k] = chg_w >> 16
                s_out[k] = chg_w & 0x7FFF
                k += 1
            p += step
    return k


def decode_range(words: np.ndarray, ix, t_lo=None, t_hi=None, short=False, tick_ps=TICK_PS):
    """Hits of an indexed file with t_lo <= ts < t_hi as (ts_ps int64, ch uint8, qlong uint16),
    and qshort uint16 (15 bits) as a fourth array with short=True; time tags of tick_ps."""
    w = np.ascontiguousarray(words, dtype=np.uint32)
    if t_lo is None:
        t_lo = np.int64(-1) << 62
    if t_hi is None:
        t_hi = np.int64(1) << 62
    tot = int(ix.nev.sum())
    ts = np.empty(tot, np.int64); ch = np.empty(tot, np.uint8); e = np.empty(tot, np.uint16)
    qs = np.empty(tot, np.uint16)
    k = _decode_kernel(w, ix.ev_start, ix.nev, ix.couple, ix.wpe, ix.has_extra,
                       np.int64(t_lo), np.int64(t_hi), np.int64(tick_ps), ts, ch, e, qs)
    return (ts[:k], ch[:k], e[:k], qs[:k]) if short else (ts[:k], ch[:k], e[:k])


# ------------------------------------------------------------------ incremental scan (the online dashboard)
# The format the online decoders read: time tag, extras option 2 (extended time stamp, flags, fine
# time) and charges, as odin's boards write. Any other couple header is counted and left out: with
# another extras option the extended time stamp and the fine time are not where the decoders look.
MAX_AGG_WORDS = 1 << 22        # 16 MB; the largest board aggregate in odin's data is 24572 words


@njit(cache=True, nogil=True)
def _event_ts(w, p, step, tick):
    extra_w = np.int64(w[p + step - 2])
    return (((extra_w >> 16) << 31) | (np.int64(w[p]) & 0x7FFFFFFF)) * tick + ((extra_w & 0x3FF) * tick) // FINE_DIV


@njit(cache=True, nogil=True)
def _agg_size(w, pos, n, bid, ctr):
    """Is w[pos] the start of a whole board aggregate of this file? A header with a plausible size,
    the file's board id (bid < 0: any), aggregate counter ctr (ctr < 0: any), and couple aggregates
    that fill it exactly. Returns its size in words, 0 if not, -1 if it may be but is not all written."""
    x = np.int64(w[pos]); size = x & 0x0FFFFFFF
    if (x >> 28) != 0xA or size < 4 or size > MAX_AGG_WORDS:
        return 0
    if pos + 3 >= n:
        return -1
    w1 = np.int64(w[pos + 1])
    if bid >= 0 and ((w1 >> 27) & 31) != bid:
        return 0
    if ctr >= 0 and (np.int64(w[pos + 2]) & 0x7FFFFF) != ctr:
        return 0
    if pos + size > n:
        return -1
    p = pos + 4; end = pos + size
    for c in range(8):
        if not (w1 >> c) & 1:
            continue
        if p + 2 > end:
            return 0
        c0 = np.int64(w[p]); c1 = np.int64(w[p + 1])
        if not (c0 >> 31):
            return 0
        csize = c0 & 0x3FFFFF
        has_wave = (c1 >> 27) & 1
        nsw = ((c1 & 0xFFFF) * 8) // 2 if has_wave else 0
        wpe = ((c1 >> 29) & 1) + nsw + ((c1 >> 28) & 1) + ((c1 >> 30) & 1)
        if csize < 2 or wpe == 0 or (csize - 2) % wpe != 0 or p + csize > end:
            return 0
        p += csize
    if p != end:
        return 0
    return size


CTR_MASK = 0x7FFFFF            # the board aggregate counter: 23 bits, wraps
FORWARD = 1 << 20              # a counter this far ahead of the last one read is a later aggregate (a gap:
                               # aggregates missing from the file); further, or behind, is not
NEAR = 64                      # after damage, an aggregate not all written yet is waited for if this near


@njit(cache=True, nogil=True)
def _fwd(a, b):
    """Counter b comes after counter a (1 .. FORWARD later, across the wrap)."""
    s = (b - a) & CTR_MASK
    return s >= 1 and s <= FORWARD


@njit(cache=True, nogil=True)
def _follows(w, pos, size, n, eof, last_ctr):
    """Is the whole aggregate at w[pos] (size words) one to read? Its counter is after the last one read
    (a copy of an older aggregate is not), and the next board aggregate starts right after it: a header
    of this board whose counter is after this one (or at the end of what is written). A read that ended
    inside an aggregate leaves a header promising more words than follow (seen 28 Sep 2026); the
    aggregate it claims then ends in the middle of the next one, where no such header is.
    Returns 1 yes, 0 no, -1 not known yet (the next header is not all in w and more data follow)."""
    ctr = np.int64(w[pos + 2]) & CTR_MASK
    if last_ctr >= 0 and not _fwd(last_ctr, ctr):
        return 0
    q = pos + size
    if q + 2 >= n:
        if eof:
            return 1                          # the end of what is written, or the next header not all written
        return -1
    x = np.int64(w[q]); s2 = x & 0x0FFFFFFF
    if (x >> 28) != 0xA or s2 < 4 or s2 > MAX_AGG_WORDS:
        return 0
    if ((np.int64(w[q + 1]) >> 27) & 31) != ((np.int64(w[pos + 1]) >> 27) & 31):
        return 0
    nc = np.int64(w[q + 2]) & CTR_MASK
    if _fwd(ctr, nc):
        return 1
    # the exact successor of the last one read, followed by a header out of order: read it, and the
    # next one is judged on its own
    return 1 if (last_ctr >= 0 and ctr == ((last_ctr + 1) & CTR_MASK)) else 0


@njit(cache=True, nogil=True)
def _scan_kernel(w, pos, n, stop, eof, bid, last_ctr, tick):
    """Index the complete board aggregates starting in w[pos:stop] (w holds data up to n; eof: n is the
    end of what is written). Per couple aggregate in the readable format: ev_start, nev, couple, wpe,
    t_first, t_last (the oldest and newest hit: the two channels of a pair are not in time order inside
    an aggregate). Also: the word after the last aggregate read, words skipped over damage, couple
    aggregates in another format, the file's board id, the last and the first aggregate counter read,
    and aggregates missing (counter gaps).
    After damage, reading goes on at the first whole aggregate of this board whose counter is after the
    last one read and which is itself followed by the next one (_follows); one word in 50 of real data
    looks like an aggregate header, so weaker tests skipped real data."""
    cap = (stop - pos) // 2 + 1
    ev_start = np.empty(cap, np.int64); nev = np.empty(cap, np.int64); couple = np.empty(cap, np.int64)
    wpe_a = np.empty(cap, np.int64); t_first = np.empty(cap, np.int64); t_last = np.empty(cap, np.int64)
    k = 0; skipped = 0; other = 0; gaps = 0; first_ctr = -1
    while pos < stop:
        size = _agg_size(w, pos, n, bid, -1)
        if size < 0:
            if eof:
                break                         # still being written
            size = 0                          # cannot be: an aggregate starting before stop is all in w
        good = 0
        if size > 0:
            good = _follows(w, pos, size, n, eof, last_ctr)
            if good < 0:
                break
        if good == 0:
            # damage: the first whole aggregate further on that is one to read
            q = pos + 1; found = -1; wait = False
            q_end = n if eof else stop
            while q < q_end:
                if (np.int64(w[q]) >> 28) == 0xA:
                    s2 = _agg_size(w, q, n, bid, -1)
                    if s2 < 0 and eof:
                        # not all written yet: wait for it if it may be the next one (a header of this board
                        # with a counter just after the last one read); a stray word that only looks like a
                        # header must not hold the reading up for ever
                        if q + 3 >= n or last_ctr < 0 or ((np.int64(w[q + 2]) - last_ctr) & CTR_MASK) - 1 < NEAR:
                            wait = True; break
                    if s2 > 0:
                        f = _follows(w, q, s2, n, eof, last_ctr)
                        if f < 0:
                            wait = True; break
                        if f == 1:
                            found = q; break
                q += 1
            if found < 0:
                if not eof and not wait:
                    skipped += stop - pos     # all of this piece is damage: go on from its end next time
                    pos = stop
                break                         # nothing readable after it yet: wait for more data
            skipped += found - pos
            pos = found
            continue
        # a whole aggregate: index its couple aggregates
        w1 = np.int64(w[pos + 1])
        bid = (w1 >> 27) & 31
        ctr = np.int64(w[pos + 2]) & CTR_MASK
        if last_ctr >= 0:
            gaps += ((ctr - last_ctr) & CTR_MASK) - 1
        else:
            first_ctr = ctr
        last_ctr = ctr
        p = pos + 4
        for c in range(8):
            if not (w1 >> c) & 1:
                continue
            c0 = np.int64(w[p]); c1 = np.int64(w[p + 1])
            csize = c0 & 0x3FFFFF
            has_wave = (c1 >> 27) & 1; has_extra = (c1 >> 28) & 1
            has_ts = (c1 >> 29) & 1; has_chg = (c1 >> 30) & 1; opt = (c1 >> 24) & 7
            nsw = ((c1 & 0xFFFF) * 8) // 2 if has_wave else 0
            wpe = has_ts + nsw + has_extra + has_chg
            m = (csize - 2) // wpe
            if m > 0:
                if has_ts == 1 and has_chg == 1 and has_extra == 1 and opt == 2:
                    lo = np.int64(1) << 62; hi = -(np.int64(1) << 62)
                    for j in range(m):
                        t = _event_ts(w, p + 2 + j * wpe, wpe, tick)
                        if t < lo:
                            lo = t
                        if t > hi:
                            hi = t
                    ev_start[k] = p + 2; nev[k] = m; couple[k] = c; wpe_a[k] = wpe
                    t_first[k] = lo; t_last[k] = hi
                    k += 1
                else:
                    other += 1
            p += csize
        pos += size
    return (ev_start[:k], nev[:k], couple[:k], wpe_a[:k], t_first[:k], t_last[:k], pos, skipped, other, bid, last_ctr,
            first_ctr, gaps)


class FileScan:
    """The readable couple aggregates of one growing .fsu file, indexed as far as it is written.
    update() reads only what was added since the last call (with pread: a file cut shorter while it
    is read gives a short read, not a SIGBUS as a memory map would); the index grows in place."""
    FIELDS = ("ev_start", "nev", "couple", "wpe", "t_first", "t_last")

    def __init__(self, path):
        self.path = path
        self.tick = tick_ps_of(path)          # ps per time-tag tick: 2 ns on x730, 4 ns on x725
        self.pos = 0                          # words indexed so far (up to a whole board aggregate)
        self.size = 0                         # bytes when last looked at
        self.ino = None
        self.bid = -1; self.last_ctr = -1     # the file's board id and last aggregate counter
        self.first_ctr = -1                   # its first aggregate counter
        self.a = {f: np.zeros(1024, np.int64) for f in self.FIELDS}
        self.n = 0                            # couple aggregates indexed
        self.hits = 0
        self.skipped = 0; self.other = 0
        self.gaps = 0                         # board aggregates missing (counter gaps)
        self.behind = False                   # the last update stopped at its limit: more is written than indexed
        self.stuck_since = None               # when the bytes after the index began to wait (None: none wait)

    def replaced(self):
        """True when the file at this path is no longer the one indexed (removed, or written anew)."""
        try:
            st = os.stat(self.path)
        except OSError:
            return True
        return (self.ino is not None and st.st_ino != self.ino) or st.st_size < self.size

    def tail_bytes(self):
        """Bytes after the last aggregate read: still being written, or (in a finished file) unreadable."""
        return self.size - 4 * self.pos

    def update(self, now=0.0, max_words=None):
        """Index what was added, at most max_words words of it. Returns the number of new couple aggregates."""
        try:
            st = os.stat(self.path)
        except OSError:
            return 0
        if self.ino is None:
            self.ino = st.st_ino
        self.size = st.st_size
        n_words = st.st_size // 4
        new = 0
        if n_words > self.pos:
            stop = n_words if max_words is None else min(n_words, self.pos + int(max_words))
            end = n_words if stop == n_words else min(n_words, stop + MAX_AGG_WORDS + 8)   # an aggregate starting before stop, and the header after it
            try:
                with open(self.path, "rb") as f:
                    w = np.fromfile(f, dtype="<u4", count=end - self.pos, offset=4 * self.pos)
            except (OSError, ValueError):
                w = np.zeros(0, np.uint32)
            eof = w.size < end - self.pos or end == n_words
            stop = min(stop, self.pos + w.size)
            if w.size:
                r = _scan_kernel(w, np.int64(0), np.int64(w.size), np.int64(stop - self.pos), eof,
                                 np.int64(self.bid), np.int64(self.last_ctr), np.int64(self.tick))
                new = int(r[0].size)
                if new:
                    need = self.n + new
                    if need > self.a["nev"].size:
                        cap = max(need, 2 * self.a["nev"].size)
                        for fld in self.FIELDS:
                            b = np.zeros(cap, np.int64); b[:self.n] = self.a[fld][:self.n]; self.a[fld] = b
                    for fld, x in zip(self.FIELDS, r[:6]):
                        self.a[fld][self.n:need] = x
                    self.a["ev_start"][self.n:need] += self.pos      # word offsets in the file
                    self.n = need
                    self.hits += int(r[1].sum())
                self.pos += int(r[6]); self.skipped += int(r[7]); self.other += int(r[8])
                self.bid = int(r[9]); self.last_ctr = int(r[10]); self.gaps += int(r[12])
                if self.first_ctr < 0 and int(r[11]) >= 0:
                    self.first_ctr = int(r[11])
            self.behind = stop < n_words and self.pos < n_words   # stopped at the limit, not at the end
        else:
            self.behind = False
        if self.tail_bytes() <= 0 or self.behind:
            self.stuck_since = None
        elif new or self.stuck_since is None:
            self.stuck_since = now
        return new

    def stuck_s(self, now):
        """How long bytes after the last aggregate read have waited without the index moving."""
        return 0.0 if self.stuck_since is None else now - self.stuck_since

    def arrays(self):
        """The index, one array per field (views, no copy)."""
        return {f: self.a[f][:self.n] for f in self.FIELDS}

    def release(self):
        """Drop the index (the file is read and passed)."""
        self.a = {f: np.zeros(0, np.int64) for f in self.FIELDS}
        self.n = 0


def decode_aggs(path, ev_start, nev, couple, wpe, t_lo=None, t_hi=None):
    """Hits of the given couple aggregates of a file (in the readable format) with t_lo <= ts < t_hi:
    (ts_ps int64, ch uint8, qlong uint16, qshort uint16), in file order."""
    if t_lo is None:
        t_lo = np.int64(-1) << 62
    if t_hi is None:
        t_hi = np.int64(1) << 62
    tot = int(np.sum(nev))
    ts = np.empty(tot, np.int64); ch = np.empty(tot, np.uint8); e = np.empty(tot, np.uint16); qs = np.empty(tot, np.uint16)
    if tot == 0:
        return ts, ch, e, qs
    last = int(np.max(ev_start + nev * wpe))
    w = np.memmap(path, dtype="<u4", mode="r", shape=(last,))
    k = _decode_kernel(w, np.ascontiguousarray(ev_start, np.int64), np.ascontiguousarray(nev, np.int64),
                       np.ascontiguousarray(couple, np.int64), np.ascontiguousarray(wpe, np.int64),
                       np.ones(len(ev_start), np.int64), np.int64(t_lo), np.int64(t_hi), np.int64(tick_ps_of(path)),
                       ts, ch, e, qs)
    del w
    return ts[:k], ch[:k], e[:k], qs[:k]


def _check_file(path):
    """Compare the numba decode of a real .fsu file with fsu._decode_chunk."""
    import time
    import fsu
    w = np.memmap(path, dtype="<u4", mode="r")
    t0 = time.perf_counter(); ix = fsu.index_file(w); t_ix = time.perf_counter() - t0
    tick = tick_ps_of(path)
    t0 = time.perf_counter(); d = fsu._decode_chunk(w, ix, 0, len(ix.nev), tick); t_np = time.perf_counter() - t0
    decode_range(w[:5000], index_file(w[:5000]))  # compile
    t0 = time.perf_counter(); ix2 = index_file(w); ts, ch, e = decode_range(w, ix2, tick_ps=tick); t_nb = time.perf_counter() - t0
    n = len(ts)
    assert n == len(d["ch"]), (n, len(d["ch"]))
    assert np.array_equal(ts, d["ts_ps"]) and np.array_equal(ch, d["ch"]) and np.array_equal(e, d["qlong"])
    assert np.array_equal(decode_range(w, ix2, short=True, tick_ps=tick)[3], d["qshort"])
    lo, hi = int(ts[n // 3]), int(ts[2 * n // 3])
    ts2, ch2, e2 = decode_range(w, ix2, lo, hi, tick_ps=tick)
    m = (d["ts_ps"] >= lo) & (d["ts_ps"] < hi)
    assert np.array_equal(ts2, d["ts_ps"][m]) and np.array_equal(e2, d["qlong"][m])
    print(f"{path.split('/')[-1]}: {n:,} hits identical; numpy index+decode {(t_ix + t_np) * 1e9 / n:.1f} ns/hit, "
          f"numba {t_nb * 1e9 / n:.1f} ns/hit ({(t_ix + t_np) / t_nb:.1f}x); time-window decode identical")
    # the incremental scan, fed the file in uneven pieces, finds the same hits
    fs = FileScan(path)
    words = w.size
    for cut in sorted(set(np.random.default_rng(1).integers(1, words, 20).tolist()) | {words}):
        r = _scan_kernel(w[:cut], np.int64(fs.pos), np.int64(cut), np.int64(cut), True, np.int64(fs.bid), np.int64(fs.last_ctr),
                         np.int64(fs.tick))   # written up to cut
        k = r[0].size
        for f, a in zip(FileScan.FIELDS, r[:6]):
            if fs.a[f].size < fs.n + k:
                b = np.zeros(2 * (fs.n + k), np.int64); b[:fs.n] = fs.a[f][:fs.n]; fs.a[f] = b
            fs.a[f][fs.n:fs.n + k] = a
        fs.n += k
        fs.pos = int(r[6]); fs.skipped += int(r[7]); fs.other += int(r[8]); fs.bid = int(r[9]); fs.last_ctr = int(r[10])
    A = fs.arrays()
    # the same through FileScan.update in 1 MB pieces (pread, a limit per update)
    fs2 = FileScan(path)
    while True:
        fs2.update(max_words=1 << 18)
        if not fs2.behind:
            break
    assert fs2.n == fs.n and fs2.pos == fs.pos and fs2.skipped == 0 and fs2.gaps == 0, (fs2.n, fs.n, fs2.pos, fs.pos, fs2.skipped, fs2.gaps)
    assert all(np.array_equal(fs2.arrays()[f], A[f]) for f in FileScan.FIELDS), "update in pieces differs"
    ts3, ch3, e3, qs3 = decode_aggs(path, A["ev_start"], A["nev"], A["couple"], A["wpe"])
    assert fs.pos == words - ix2.truncated_words and fs.skipped == 0, (fs.pos, fs.skipped)
    assert np.array_equal(ts3, ts) and np.array_equal(ch3, ch) and np.array_equal(e3, e), "scan in pieces differs"
    ends = np.cumsum(A["nev"]); starts = ends - A["nev"]
    assert np.array_equal(np.minimum.reduceat(ts, starts), A["t_first"]) and np.array_equal(np.maximum.reduceat(ts, starts), A["t_last"]), "first/last times"
    print(f"    incremental scan in 20 pieces: {A['nev'].sum():,} hits identical, {fs.other} couple aggregates in another format")


if __name__ == "__main__":
    import sys
    for arg in sys.argv[1:]:
        if arg.endswith(".fsu"):
            _check_file(arg)
