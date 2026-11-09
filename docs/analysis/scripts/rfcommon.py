"""Shared helpers for ChiliPad burst analysis (Python 3, stdlib only)."""
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]   # repo root (docs/analysis/scripts/..)
CAP = REPO / "docs" / "captures"
SYNC4 = "9388B0CA"          # first 4 bytes of the 8-byte sync, consumed by the trigger
SYNC8 = "9388B0CAD019F2E6"

PKT = re.compile(r"^\+(?P<t>[\d.]+) P (?P<tus>\d+) dt=(?P<dt>\d+) ch=(?P<ch>\d+) "
                 r"rssi=(?P<rssi>-?\d+) RAW (?P<raw>[0-9A-Fa-f]+)")
ESP = re.compile(r"^\+(?P<t>[\d.]+) (?P<ms>\d+) RAW (?P<raw>[0-9A-Fa-f]+)")


def parse_nrf(path):
    out = []
    for line in Path(path).read_text().splitlines():
        m = PKT.match(line)
        if m:
            out.append(dict(t=float(m["t"]), t_us=int(m["tus"]), dt=int(m["dt"]),
                            rssi=int(m["rssi"]), raw=m["raw"].upper()))
    return out


def parse_esp(path):
    out = []
    for line in Path(path).read_text().splitlines():
        m = ESP.match(line)
        if m:
            out.append(dict(t=float(m["t"]), ms=int(m["ms"]), raw=m["raw"].upper()))
    return out


def h2b(h):
    return "".join(f"{int(c, 16):04b}" for c in h)


def b2h(b):
    """Bits -> hex; a trailing partial nibble is padded with '_' marker."""
    s = "".join(f"{int(b[i:i+4], 2):X}" for i in range(0, len(b) - len(b) % 4, 4))
    if len(b) % 4:
        s += "[" + b[len(b) - len(b) % 4:] + "]"
    return s


def ham(a, b):
    return sum(x != y for x, y in zip(a, b))


def onair(raw, trigger=SYNC4):
    """On-air bit stream: the trigger bytes followed by RAW."""
    return h2b(trigger + raw)


def bursts(nrf, gap_us=100000):
    """Group packets into bursts (dt > gap_us starts a new burst)."""
    groups = []
    for p in nrf:
        if not groups or p["dt"] > gap_us:
            groups.append([])
        groups[-1].append(p)
    return groups
