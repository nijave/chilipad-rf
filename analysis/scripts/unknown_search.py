"""Search every nRF and ESP capture line for the signatures of the unknown frames.

Run from analysis/scripts:  python3 unknown_search.py
Each signature is searched at every bit offset, plain and inverted, in the on-air
bit stream (trigger + RAW).  A hit is <= 12% bit errors over the signature length.
"""
import re
from pathlib import Path
from rfcommon import CAP, h2b, b2h, ham

SIG = {
    # family A, packet 1: the 82 bits after the AAAAAA trigger shared by both A frames
    "A.p1": "632F4BBAC1BEC54D62B8",
    # family A, packet 2 (A1 bit 255..), 64 bits shared by both A frames
    "A.p2": None,
    # family B: the 80 bits after the trigger shared by both B frames
    "B": "03486457DA26DDCA871C",
    "C13": "3EA3A30D200622C02078",
    "C57": "E4A2A60AF60D6DD98A56",
    # 202434 t=21.05: a codeword burst with an unknown alphabet (header-like X Y Z X X Y Z)
    "X": "BCCF8ED681DFB775",
    "Y": "AB5F181C3CF736DD",
    "Z": "679BD7B836E669B1",
    # 202434 weak recurring family D (-70..-73 dBm), 72 bits after the 555555 trigger
    "D": "AF5567D7",
    # 202434 t=1.75 sparse frame
    "SPARSE": "30020808011A0C420034001030C80810",
}

TRIG = re.compile(r"trigger ([0-9A-F]{6,10})")
PKT = re.compile(r"^\+(?P<t>[\d.]+) P \d+ dt=\d+ ch=(?P<ch>\d+) rssi=(?P<rssi>-?\d+) RAW (?P<raw>[0-9A-F]+)")
ESP = re.compile(r"^\+(?P<t>[\d.]+) \d+ RAW (?P<raw>[0-9A-F]+)")
ESPTRIG = re.compile(r"RX_ADDR_P0=([0-9A-F]+) \(on chip\)")


def a_p2():
    from unknown_frames import frame, CAP4
    b = frame(CAP4, 16.457)["bits"]
    return b2h(b[255:255 + 64])


def lines():
    for d in sorted(CAP.iterdir()):
        f = d / "nrf.log"
        if f.exists():
            trig = ""
            for ln in f.read_text().splitlines():
                m = TRIG.search(ln)
                if m and "[addr]" in ln:
                    trig = m[1]
                m = PKT.match(ln)
                if m:
                    yield d.name, "nrf", float(m["t"]), int(m["rssi"]), trig, h2b(trig + m["raw"])
        f = d / "esp.log"
        if f.exists():
            trig = ""
            for ln in f.read_text().splitlines():
                m = ESPTRIG.search(ln)
                if m:
                    trig = m[1]
                m = ESP.match(ln)
                if m:
                    yield d.name, "esp", float(m["t"]), None, trig, h2b(m["raw"])


def search(bits, sig):
    n = len(sig)
    inv = sig.translate(str.maketrans("01", "10"))
    best = (n, 0, "")
    for p in range(0, len(bits) - n + 1):
        w = bits[p:p + n]
        for tag, s in (("", sig), ("inv", inv)):
            e = ham(w, s)
            if e < best[0]:
                best = (e, p, tag)
    return best


def main():
    SIG["A.p2"] = a_p2()
    sigs = {k: h2b(v) for k, v in SIG.items()}
    hits = {k: [] for k in sigs}
    for cap, src, t, rssi, trig, bits in lines():
        for k, s in sigs.items():
            e, p, tag = search(bits, s)
            if e <= 0.12 * len(s):
                hits[k].append((cap, src, t, rssi, trig, p, e, tag))
    for k, hs in hits.items():
        print(f"== {k} ({SIG[k]}, {len(sigs[k])} bits): {len(hs)} hits")
        for h in hs:
            cap, src, t, rssi, trig, p, e, tag = h
            print(f"   {cap} {src} t={t:9.3f} rssi={rssi} trig={trig} bit {p} err {e} {tag}")


if __name__ == "__main__":
    main()
