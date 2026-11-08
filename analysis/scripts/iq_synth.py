#!/usr/bin/env python3
"""Synthesize HackRF-style cs8 test files for the ChiliPad IQ demodulator.

Generates 1 Mbit/s GFSK (Gaussian pre-modulation filter, BT parameter,
default 0.5) bursts with per-packet CFO, deviation, amplitude and start
time, plus AWGN, at a chosen offset from the capture centre frequency, and
writes interleaved signed int8 I/Q ("cs8", the format hackrf_transfer -r
produces), scaled so the file peak sits at ~86% of full scale (no clipping).

The demo scene (default) contains, with quiet gaps between them:

  * a 72 degF remote setpoint burst at t = 1 ms: >= 52 us of '1' carrier,
    24 preamble bits (0x55 x3), then decode.encode(72, lastbit) where
    lastbit is read from analysis/decode-output.txt the way gen_burst.py
    does it.  CFO +8 kHz, deviation 250 kHz, amplitude 1.0.
  * a family A pair at t = 6 ms: packet 1 (CFO +20 kHz, amp 0.7) and
    packet 2 (CFO -35 kHz, amp 0.9), 231 us sync-to-sync.  Each packet is
    28 alternating preamble bits + sync 632F4B + 56 header bits + "00".
    Header hex (after the sync): packet 1 BAC1BEC54D62B8, packet 2
    BEC1A7A94D6570 (tails as read by the dongle/ESP receivers).
  * a family B packet at t = 12 ms: 32 alternating preamble bits + the
    81-bit header (03486457DA26DDCA871C plus one unknown bit, synthesized
    as '1') + a random DC-heavy payload, 4000 bits (4 ms) in total.

SNR is per complex sample relative to a unit-amplitude packet:
noise_sigma = amp_ref/sqrt(2) * 10^(-snr_db/20).

usage: iq_synth.py <out.cs8> [--rate 20e6] [--snr 20] [--seed 1]
    [--offset 0] [--invert]

--offset shifts every packet's nominal channel away from DC (the file is
always written baseband at the capture centre frequency; demodulate with
iq_demod.py --freq <centre + offset>).  --invert flips every bit (unknown
HackRF polarity).

Importable helpers: scene(), synth(), gfsk_mod(), write_cs8(),
remote_air_bits(), fama_bits(), famb_bits(), lastbit_map().

Requires numpy only.  Python 3.12+.
"""
import argparse
import math
import re
import sys
from pathlib import Path

import numpy as np

from rfcommon import h2b
from decode import encode

BIT_RATE = 1e6
FAMA_SYNC = "632F4B"
FAMA_P1 = "BAC1BEC54D" + "62B8"     # T C1 W 4D + 16-bit tail (dongle reading)
FAMA_P2 = "BEC1A7A94D" + "6570"     # T C1 W 4D + 16-bit tail (ESP reading)
FAMB_HEADER = "03486457DA26DDCA871C"    # 80 bits; 81st bit unknown, '1' below
FAMA_SYNC_GAP_US = 231.0


def lastbit_map():
    """The true-last-bit dict, read from decode-output.txt like gen_burst.py."""
    line = [l for l in (Path(__file__).resolve().parent.parent
                        / "decode-output.txt").read_text().splitlines()
            if "true last bits" in l][0]
    return dict(re.findall(r"(\w+)=([01])", line.split(":", 1)[1]))


def remote_air_bits(setpoint=72, carrier_bits=52):
    """Full on-air bit stream of a setpoint press: carrier + preamble + burst.

    encode() starts at the last preamble bit, so the 24-bit 0x55 preamble
    contributes 23 bits before it (air = ...01010 | 1 | first codeword).
    """
    return "1" * carrier_bits + "01010101" * 3 + encode(setpoint, lastbit_map())[1:]


def fama_bits(header_hex, pre_bits=28):
    """One family A packet: preamble + 632F4B + 56 header bits + '00' end."""
    return "01" * (pre_bits // 2) + h2b(FAMA_SYNC) + h2b(header_hex) + "00"


def famb_bits(payload_bits, rng=None, pre_bits=32):
    """One family B packet: preamble + 81-bit header + DC-heavy payload."""
    if rng is None:
        rng = np.random.default_rng(0)
    return ("01" * (pre_bits // 2) + h2b(FAMB_HEADER) + "1"
            + dc_heavy_bits(payload_bits, rng))


def dc_heavy_bits(n, rng):
    """Random bits with long runs (mean run ~2.4, like the captured famB)."""
    out = []
    cur = int(rng.integers(2))
    while len(out) < n:
        run = int(rng.geometric(0.42))
        out.extend([cur] * run)
        cur ^= 1
    return "".join(map(str, out[:n]))


def gfsk_mod(bits, rate, dev, cfo=0.0, amp=1.0, bt=0.5):
    """GFSK-modulate a bit string at `rate` Hz; returns complex64 samples."""
    os = int(round(rate / BIT_RATE))
    assert abs(os * BIT_RATE - rate) < 1.0, f"rate {rate} not a bit-rate multiple"
    arr = np.frombuffer(bits.encode(), dtype=np.uint8).astype(np.float64) - 48.0
    nrz = np.repeat(arr * 2.0 - 1.0, os)
    sigma = os * math.sqrt(2 * math.log(2)) / (2 * math.pi * bt)
    half = int(math.ceil(3 * sigma))
    t = np.arange(-half, half + 1, dtype=np.float64)
    ker = np.exp(-0.5 * (t / sigma) ** 2)
    ker /= ker.sum()
    m = np.convolve(nrz, ker, mode="same")
    f_inst = cfo + dev * m
    ph = 2 * np.pi * np.cumsum(f_inst) / rate
    return (amp * np.exp(1j * ph)).astype(np.complex64)


def synth(rate=20e6, total_s=0.02, packets=(), snr_db=20, seed=1):
    """Sum packets (dicts with t/bits/cfo/dev/amp) into AWGN; complex64."""
    n = int(round(total_s * rate))
    iq = np.zeros(n, dtype=np.complex64)
    amp_ref = 1.0
    for p in packets:
        seg = gfsk_mod(p["bits"], rate, p["dev"], p["cfo"], p.get("amp", 1.0))
        s = int(round(p["t"] * rate))
        e = min(n, s + len(seg))
        if e > s:
            iq[s:e] += seg[:e - s]
        amp_ref = max(amp_ref, p.get("amp", 1.0))
    sigma = amp_ref / math.sqrt(2.0) * 10.0 ** (-snr_db / 20.0)
    rng = np.random.default_rng(seed)
    noise = (rng.standard_normal(n) + 1j * rng.standard_normal(n)) * sigma
    return (iq + noise.astype(np.complex64)).astype(np.complex64)


def write_cs8(path, iq, peak=110.0):
    """Write complex samples as interleaved signed int8, peak-scaled."""
    m = max(1e-12, float(np.max(np.abs(iq))))
    q = np.clip(np.rint(iq * (peak / m)), -127, 127)
    buf = np.empty(q.size * 2, dtype=np.int8)
    buf[0::2] = q.real.astype(np.int8)
    buf[1::2] = q.imag.astype(np.int8)
    with open(path, "wb") as f:
        buf.tofile(f)


def scene(rate=20e6, offset=0.0, snr_db=20, seed=1, invert=False, total_s=0.02):
    """The demo scene; returns (iq, truth) with per-packet parameters."""
    rng = np.random.default_rng(seed)
    t_p1 = 6e-3
    t_p2 = t_p1 + FAMA_SYNC_GAP_US * 1e-6           # sync-to-sync = 231 us
    packets = [
        dict(label="remote", t=1e-3, bits=remote_air_bits(72),
             cfo=8e3, dev=250e3, amp=1.0),
        dict(label="famA", t=t_p1, bits=fama_bits(FAMA_P1),
             cfo=20e3, dev=250e3, amp=0.7),
        dict(label="famA", t=t_p2, bits=fama_bits(FAMA_P2),
             cfo=-35e3, dev=250e3, amp=0.9),
        dict(label="famB", t=12e-3, bits=famb_bits(4000 - 32 - 81, rng),
             cfo=5e3, dev=250e3, amp=0.5),
    ]
    for p in packets:
        p["cfo"] += offset
        if invert:
            p["bits"] = p["bits"].translate(str.maketrans("01", "10"))
    iq = synth(rate=rate, total_s=total_s, packets=packets,
               snr_db=snr_db, seed=seed)
    return iq, dict(rate=rate, offset=offset, snr_db=snr_db, packets=packets)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0],
                                 epilog="See the module docstring for the scene.")
    ap.add_argument("out", help="output .cs8 file")
    ap.add_argument("--rate", type=float, default=20e6, help="sample rate Hz")
    ap.add_argument("--snr", type=float, default=20.0, help="per-sample SNR dB")
    ap.add_argument("--seed", type=int, default=1, help="PRNG seed")
    ap.add_argument("--offset", type=float, default=0.0,
                    help="packet channel offset from DC, Hz")
    ap.add_argument("--invert", action="store_true", help="flip all bits")
    a = ap.parse_args()
    iq, truth = scene(rate=a.rate, offset=a.offset, snr_db=a.snr,
                      seed=a.seed, invert=a.invert)
    write_cs8(a.out, iq)
    print(f"wrote {a.out}: {len(iq)} samples ({len(iq)/a.rate*1e3:.1f} ms), "
          f"rate {a.rate:.0f} S/s, offset {a.offset:.0f} Hz, "
          f"SNR {a.snr:.0f} dB, seed {a.seed}, invert={a.invert}")
    for p in truth["packets"]:
        print(f'  {p["label"]:>6}  t={p["t"]*1e6:.0f}us  bits={len(p["bits"])}  '
              f'cfo={p["cfo"]:+.0f}Hz  dev={p["dev"]:.0f}Hz  amp={p["amp"]}')


if __name__ == "__main__":
    main()
