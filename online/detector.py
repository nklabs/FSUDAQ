#!/usr/bin/env python3
"""Channel map for the pion spectrometer: strips -> amplifiers -> (Board, Channel).

Three tracker planes read out by winner-takes-all amplifiers, plus auxiliary
counters on the same digitizers.  Geometry and ganging come from
`momentum_map_meta.json` (`n_amplifiers`, `t*_pattern`, `geometry_cm`), which is
what the momentum map was built against -- the classifier's triplet
`(ch0, ch1, ch2)` is amplifier numbers in this map, 1-based.

    plane   pattern [1x,2x,4x,8x]   amplifiers   strips
    T0      [ 8,  0,  0, 0]              8          8
    T1      [16,  8,  4, 7]             35        104
    T2      [24, 12, 10, 5]             51        128
                                    -------    -------
                                        94        240

An amplifier ganging 8 strips sees ~8x the traffic of a single-strip one, which
is what sets the per-channel rates in the generator.

Plane z positions are cumulative from `geometry_cm`; z(T2) - z(T0) = 96.5888 cm
reproduces `TOF_BASELINE_CM = 96.589` in online_classify.py, so the T0->T2
timestamp difference is the TOF the classifier expects.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

GANGS = (1, 2, 4, 8)          # strips per amplifier group

# geometry_cm, cumulative from T0
_T0_TO_MAG = 24.0825
_MAG_LENGTH = 50.0
_MAG_TO_T1 = 7.5003
_T1_TO_T2 = 15.006
PLANE_Z_CM = (
    0.0,
    _T0_TO_MAG + _MAG_LENGTH + _MAG_TO_T1,          # 81.5828
    _T0_TO_MAG + _MAG_LENGTH + _MAG_TO_T1 + _T1_TO_T2,   # 96.5888
)
TOF_BASELINE_CM = PLANE_Z_CM[2] - PLANE_Z_CM[0]

#: auxiliary counters, in the order classify() takes them.  A counter absent
#: from a ChannelMap is NOT READ OUT: the pipeline reports None for it, never
#: False.  That is what lets the chain run while counters are commissioned.
AUX_COUNTERS = ("coll_veto", "cg_electron", "lucite", "edge_high_p", "edge_low_p",
                "bunch_multi")


@dataclass(frozen=True)
class Plane:
    """One tracker plane: how its strips are ganged into amplifiers.

    `pattern` counts the groups of each size in GANGS order (n_1x, n_2x, n_4x, n_8x).
    """

    name: str
    pattern: tuple[int, int, int, int]

    @property
    def n_amps(self) -> int:
        return int(sum(self.pattern))

    @property
    def n_strips(self) -> int:
        return int(sum(n * g for n, g in zip(self.pattern, GANGS)))

    @property
    def strips_per_amp(self) -> np.ndarray:
        return np.repeat(GANGS, self.pattern).astype(np.int16)


#: the deployed spectrometer (momentum_map_meta.json)
DEPLOYED: list[Plane] = [
    Plane("T0", (8, 0, 0, 0)),
    Plane("T1", (16, 8, 4, 7)),
    Plane("T2", (24, 12, 10, 5)),
]

#: the example decomposition from the original spec -- kept so the I/O numbers
#: taken against it stay reproducible.  NOT the deployed geometry.
EXAMPLE: list[Plane] = [
    Plane("T0", (8, 0, 0, 0)),
    Plane("T1", (24, 24, 8, 0)),
    Plane("T2", (24, 8, 8, 6)),
]

PRESETS = {"deployed": DEPLOYED, "example": EXAMPLE}


def parse_planes(spec: str) -> list[Plane]:
    """A preset name, or 'T0:8,0,0,0/T1:16,8,4,7/T2:24,12,10,5'."""
    if spec.lower() in PRESETS:
        return PRESETS[spec.lower()]
    out = []
    for part in spec.split("/"):
        name, _, counts = part.partition(":")
        nums = tuple(int(x) for x in counts.split(","))
        if len(nums) != 4:
            raise ValueError(f"{part!r}: need four counts (1x, 2x, 4x, 8x)")
        out.append(Plane(name, nums))
    return out


class ChannelMap:
    """Global channel index <-> (Board, Channel), and what each channel is.

    Tracker amplifiers come first, in plane order, then whichever auxiliary
    counters are installed.  Arrays are indexed by global channel:

        plane    0/1/2 for a tracker amplifier, -1 for an aux counter
        amp      1-based amplifier number within its plane (0 for aux)
        gang     strips ganged into this amplifier (0 for aux)
        board    CoMPASS Board id
        channel  CoMPASS Channel id within the board
    """

    def __init__(
        self,
        planes: list[Plane] = DEPLOYED,
        board_channels: int = 16,
        aux: tuple[str, ...] = AUX_COUNTERS,
    ):
        self.planes = planes
        self.board_channels = int(board_channels)
        self.aux = tuple(aux)

        n_trk = sum(p.n_amps for p in planes)
        n_tot = n_trk + len(self.aux)
        self.n_tracker = n_trk

        self.plane = np.full(n_tot, -1, np.int16)
        self.amp = np.zeros(n_tot, np.int16)
        self.gang = np.zeros(n_tot, np.int16)
        at = 0
        for i, p in enumerate(planes):
            sl = slice(at, at + p.n_amps)
            self.plane[sl] = i
            self.amp[sl] = np.arange(1, p.n_amps + 1)
            self.gang[sl] = p.strips_per_amp
            at += p.n_amps

        #: name -> global channel index, for the counters that are installed
        self.aux_index = {name: n_trk + k for k, name in enumerate(self.aux)}

        idx = np.arange(n_tot, dtype=np.int32)
        self.board = (idx // self.board_channels).astype(np.int16)
        self.channel = (idx % self.board_channels).astype(np.int16)

    @property
    def n_channels(self) -> int:
        return int(self.plane.size)

    @property
    def n_strips(self) -> int:
        return int(self.gang.sum())

    @property
    def n_boards(self) -> int:
        return int(self.board.max()) + 1

    def plane_slice(self, i: int) -> slice:
        start = sum(p.n_amps for p in self.planes[:i])
        return slice(start, start + self.planes[i].n_amps)

    def lookup_table(self) -> np.ndarray:
        """lut[Board, Channel] -> global channel index; -1 where nothing sits."""
        lut = np.full((self.n_boards, self.board_channels), -1, np.int32)
        lut[self.board, self.channel] = np.arange(self.n_channels, dtype=np.int32)
        return lut

    def summary(self) -> str:
        lines = [
            f"{self.n_channels} channels "
            f"({self.n_tracker} tracker + {len(self.aux)} aux) / "
            f"{self.n_strips} strips / {self.n_boards} boards x {self.board_channels} ch",
            f"{'plane':<8} {'amps':>5} {'strips':>7}  {'pattern':<18} {'z [cm]':>8}",
        ]
        for i, p in enumerate(self.planes):
            z = PLANE_Z_CM[i] if i < len(PLANE_Z_CM) else float("nan")
            lines.append(
                f"{p.name:<8} {p.n_amps:>5} {p.n_strips:>7}  {str(list(p.pattern)):<18} {z:>8.4f}"
            )
        if self.aux:
            lines.append("aux: " + ", ".join(
                f"{n}=(B{self.board[i]},C{self.channel[i]})"
                for n, i in self.aux_index.items()
            ))
        missing = [c for c in AUX_COUNTERS if c not in self.aux_index]
        if missing:
            lines.append("not read out (reported as None): " + ", ".join(missing))
        return "\n".join(lines)


if __name__ == "__main__":
    import sys

    spec = sys.argv[1] if len(sys.argv) > 1 else "deployed"
    bc = int(sys.argv[2]) if len(sys.argv) > 2 else 16
    print(ChannelMap(parse_planes(spec), bc).summary())
    print(f"\nTOF baseline z(T2)-z(T0) = {TOF_BASELINE_CM:.4f} cm "
          f"(online_classify.TOF_BASELINE_CM = 96.589)")
