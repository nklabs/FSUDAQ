"""cabling.csv from the patch panel: BNC n in sequence over 15880, 15879, 58918, 62839, 65316 (ch0-15) and then
1399, 30505, 30508, 1443 (ch0-7): BNC000-007 T0 1-8, BNC008-063 T1 1-56, BNC064-109 T2 1-46, BNC110-111 the
Cherenkov; then the 3 Nov 2026 swaps (15880 ch8-15 with the last channel of each other board) and the RF on
62839 ch15 (in place of T1-56). Rows stay in the file's existing order (FSUDAQ's link order).

    python3 write_cabling.py CABLING_CSV
"""
import csv
import sys

PATH = sys.argv[1]
order = [(15880, 16), (15879, 16), (58918, 16), (62839, 16), (65316, 16), (1399, 8), (30505, 8), (30508, 8), (1443, 8)]
det, n = {}, 0
for s, k in order:
    for c in range(k):
        b = n
        n += 1
        det[(s, c)] = (("trk", 0, b + 1) if b < 8 else ("trk", 1, b - 7) if b < 64 else ("trk", 2, b - 63) if b < 110
                       else ("cherenkov", "", ""))
for a, b in [((15880, 8), (15879, 15)), ((15880, 9), (58918, 15)), ((15880, 10), (62839, 14)), ((15880, 11), (65316, 15)),
             ((15880, 12), (1399, 7)), ((15880, 13), (30508, 7)), ((15880, 14), (30505, 7)), ((15880, 15), (1443, 7))]:
    det[a], det[b] = det[b], det[a]
assert det[(62839, 15)] == ("trk", 1, 56)
det[(62839, 15)] = ("rf", "", "")

old = list(csv.DictReader(open(PATH)))
new = [[r["serial"], r["channel"], *det[(int(r["serial"]), int(r["channel"]))]] for r in old]
assert len(new) == len(det) == len(old)
changed = []
for r, m in zip(old, new):
    if (r["kind"], r["plane"], r["ch"]) != tuple(str(x) for x in m[2:]):
        changed.append((r["serial"], r["channel"], f"{r['kind']} {r['plane']} {r['ch']}", f"{m[2]} {m[3]} {m[4]}"))
bad = [c for c in changed if not (c[2].startswith("trk 2") and c[3].startswith("trk 2"))]
assert not bad, f"rows other than T2 would change: {bad}"
with open(PATH, "w", newline="") as f:
    w = csv.writer(f, lineterminator="\n")
    w.writerow(["serial", "channel", "kind", "plane", "ch"])
    w.writerows(new)
print(len(changed), "rows changed, all T2:")
for c in changed:
    print("   ", c)
