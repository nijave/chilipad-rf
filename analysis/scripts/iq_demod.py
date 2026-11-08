#!/usr/bin/env python3
"""Offline 2-FSK demodulator for HackRF cs8 captures of ChiliPad traffic.

Reads interleaved signed int8 I/Q ("cs8", what hackrf_transfer -r writes),
memory-mapped so multi-GB files work, and scans one or more channel
frequencies for 1 Mbit/s 2-FSK/GFSK bursts.  Per burst it reports start
time, duration, power, CFO/deviation estimates and a classification against
the known on-air patterns:

  remote  32-bit trigger 9388B0CA (SYNC4 of the setpoint-burst sync)
  famA    24-bit sync 632F4B of the poll/reply pair (header hex reported)
  famB    40-bit header prefix 03486457DA of the family B frames (another
          2.4 GHz device, not the Cube)

Bit polarity as seen by the HackRF is unknown, so every burst is classified
in both polarities and the matching one is reported (pol = +1 means the
higher frequency is '1'; -1 the inverted reading).  CFO is estimated as the
midpoint of the mean frequency of the '1' and '0' symbol decisions, so it
does not depend on DC balance.

Processing is chunked with overlap, so packets straddling chunk boundaries
are detected exactly once.

usage:
  python3 iq_demod.py <file.cs8> [--rate 20e6] [--center 2.45e9]
      [--freq HZ ...]  (default channel 2450 MHz; --rate and --center
      default to the meta.txt that hackrf_capture.sh writes beside the file) [--start S] [--duration S] [--sps 4] [--thresh-db 10]
      [--jsonl PATH] [--bits-dir DIR] [--chunk-s 0.5] [--overlap-s 0.05]

Output: one human-readable line per burst on stdout.  --jsonl writes one
JSON object per burst (all fields; "bits" is the 0/1 string in the matched
polarity).  --bits-dir DIR writes one small text file per burst, a single
line in the ESP log format that rfcommon.parse_esp reads:

  +<t_seconds> <t_us> RAW <HEX> label=<label> pol=<+1|-1> err=<n>

<t_seconds> is the burst start measured from the beginning of the file,
<t_us> the same in integer microseconds.  RAW holds the demodulated burst
bits in the matched polarity, truncated to whole nibbles.  For labelled
bursts it starts at the matched pattern (so RAW begins with the sync word,
like the dongle/ESP logs); for unknown bursts it starts at the burst start.

Requires numpy only.  Python 3.12+.
"""
import argparse
import json
import math
import sys
from pathlib import Path

import numpy as np

from rfcommon import h2b, b2h

BIT_RATE = 1e6
FS_REF = 2 * 127.0 ** 2        # full-scale int8 complex magnitude, squared
CS8 = np.dtype((np.int8, (2,)))

# (label, bit pattern, max bit errors)
PATTERNS = [
    ("remote", h2b("9388B0CA"), 2),
    ("famA", h2b("632F4B"), 1),
    ("famB", h2b("03486457DA"), 4),
]


def design_lpf(rate, cutoff, taps=0):
    """Windowed-sinc low-pass FIR (Kaiser), real float32 taps."""
    if taps <= 0:
        taps = int(rate / 125e3) | 1          # ~161 taps at 20 MS/s
    n = np.arange(taps, dtype=np.float64) - (taps - 1) / 2
    h = np.sinc(2 * cutoff * n / rate) * np.kaiser(taps, 8.0)
    return (h / h.sum()).astype(np.float32)


def polyphase_decim(x, h, d):
    """Decimate complex x by integer d with FIR h: y[m] = sum_k h[k] x[m*d-k].

    y[m] corresponds to input sample m*d (front-padded), i.e. the first
    output sees the full filter once d >= 1 samples plus the filter half
    have passed; equivalent to np.convolve(x, h, 'full')[(len(h)-1)::d].
    """
    K = len(h)
    xp = np.concatenate([np.zeros(K - 1, dtype=np.result_type(x)), x])
    m_max = (len(xp) - K) // d + 1
    y = np.zeros(m_max, dtype=np.result_type(x, h))
    for p in range(d):
        hp = h[p::d]
        up = xp[K - 1 - p::d]
        c = np.convolve(up, hp)[:m_max]
        y[:len(c)] += c
    return y


def _runs(mask):
    """Contiguous True runs of a boolean array as (start, end) index pairs."""
    idx = np.flatnonzero(mask)
    if len(idx) == 0:
        return []
    breaks = np.flatnonzero(np.diff(idx) > 1)
    starts = np.concatenate([[0], breaks + 1])
    ends = np.concatenate([breaks, [len(idx) - 1]])
    return [(int(idx[s]), int(idx[e]) + 1) for s, e in zip(starts, ends)]


def _find_match(bits, pat, tol):
    """Earliest offset with fewest errors <= tol; (offset, n_errors) or None."""
    if len(bits) < len(pat):
        return None
    win = np.lib.stride_tricks.sliding_window_view(bits, len(pat))
    errs = (win != pat).sum(axis=1)
    ok = np.flatnonzero(errs <= tol)
    if len(ok) == 0:
        return None
    j = ok[np.argmin(errs[ok])]
    return int(j), int(errs[j])


def _invert(u):
    return (1 - u).astype(np.uint8)


def classify(bits_arr):
    """Match a bit array against the known patterns, both polarities.

    Returns (label, offset, pol, errors).  Best (lowest error, then
    earliest) match wins; pol=+1 for the raw reading, -1 for inverted.
    """
    best = None
    for pol, u in ((1, bits_arr), (-1, _invert(bits_arr))):
        for name, pat_hex, tol in PATTERNS:
            pat = np.frombuffer(pat_hex.encode(), dtype=np.uint8) - 48
            m = _find_match(u, pat, tol)
            if m is None:
                continue
            off, err = m
            cand = (err, off, pol, name)
            if best is None or cand[:2] < best[:2]:
                best = cand
    if best is None:
        return "unknown", 0, 1, None
    return best[3], best[1], best[2], best[0]


def _gauss_kernel(sps, bt):
    """Gaussian GFSK pre-modulation kernel in decimated samples."""
    sigma = sps * math.sqrt(2 * math.log(2)) / (2 * math.pi * bt)
    half = max(1, int(math.ceil(3 * sigma)))
    t = np.arange(-half, half + 1, dtype=np.float64)
    ker = np.exp(-0.5 * (t / sigma) ** 2)
    return ker / ker.sum()


def analyze_burst(y, rate_out, sps, i0, i1, bt=0.5):
    """Demodulate one burst (decimated indices i0:i1) into a record dict."""
    margin = 4 * sps
    a = max(0, i0 - margin)
    b = min(len(y), i1 + margin)
    sl = y[a:b].astype(np.complex64)
    zp = sl[1:] * np.conj(sl[:-1])
    f = np.angle(zp) * (rate_out / (2 * math.pi))     # inst. freq, Hz
    p = (sl.real ** 2 + sl.imag ** 2).astype(np.float64)
    # Receiver filtering turns the FM signal's spectrum lines into deep
    # envelope nulls (worst on alternating preambles); the phase there is
    # garbage, so every frequency sample is weighted by the power of both
    # samples that formed it (limiter behaviour).
    w = p[:-1] * p[1:]
    w = np.concatenate([[0.0], w])                    # w[n] for f[n]
    base = i0 - a
    span = i1 - i0
    c1, c2 = (sps - 1) // 2, sps // 2                 # centre samples of a symbol

    def positions(phi, ks):
        pos1 = base + ks * sps + phi + c1
        pos2 = base + ks * sps + phi + c2
        ok = (pos1 >= 1) & (pos2 < len(f))
        return pos1[ok], pos2[ok]

    ks_full = np.arange(max(span - 2 * sps, 1))
    best_phi, best_metric = 0, -1.0
    for phi in range(sps):
        pos1, pos2 = positions(phi, ks_full)
        if len(pos1) == 0:
            continue
        met = (np.sum(np.abs(f[pos1]) * w[pos1]) / np.sum(w[pos1])
               + np.sum(np.abs(f[pos2]) * w[pos2]) / np.sum(w[pos2]))
        if met > best_metric:
            best_phi, best_metric = phi, met

    ks = np.arange(max(span - 2 * sps, 1))
    pos1, pos2 = positions(best_phi, ks)
    val = (f[pos1] + f[pos2]) / 2.0
    bits_u8 = (val > 0).astype(np.uint8)

    # CFO/deviation by least squares against a re-modulated bit model
    # f[n] ~ cfo + dev*m[n]; this removes the ISI/DC-balance bias of plain
    # symbol means while keeping the two-mean midpoint as the fallback.
    hi_m = bits_u8 == 1
    m1 = float(np.mean(val[hi_m])) if hi_m.any() else 0.0
    m0 = float(np.mean(val[~hi_m])) if (~hi_m).any() else 0.0
    ker = _gauss_kernel(sps, bt)
    n0 = base + best_phi
    nrz = np.repeat(bits_u8.astype(np.float64) * 2.0 - 1.0, sps)
    mseg = np.convolve(nrz, ker, mode="same")
    mfull = np.zeros(len(f), dtype=np.float64)
    e1 = min(len(mfull), n0 + len(mseg))
    mfull[n0:e1] = mseg[:e1 - n0]
    lo_fit = max(n0 + len(ker) // 2, 1)
    hi_fit = min(n0 + span * sps - len(ker) // 2, len(f) - 1)
    cfo = (m1 + m0) / 2.0
    dev = (m1 - m0) / 2.0
    if hi_fit - lo_fit > 4 * sps:
        A = np.column_stack([np.ones(hi_fit - lo_fit), mfull[lo_fit:hi_fit]])
        fw = w[lo_fit:hi_fit]
        aw = A * fw[:, None]
        (cfo, dev) = np.linalg.solve(A.T @ aw, aw.T @ f[lo_fit:hi_fit])
        cfo, dev = float(cfo), float(dev)
    pw = float(np.mean(p[margin:margin + span])) if span else 0.0
    bitstr = "".join(map(chr, bits_u8 + 48))
    label, off, pol, err = classify(bits_u8)
    if pol == -1:                      # report bits in the matched polarity
        bitstr = bitstr.translate(str.maketrans("01", "10"))
    return dict(t=0.0, dur_us=span / rate_out * 1e6,
                pwr_db=10 * math.log10(max(pw, 1e-30) / FS_REF),
                cfo_hz=cfo, cfo_ppm=None, dev_hz=dev,
                label=label, bit_off=off, pol=pol, err=err,
                hdr=None, bits=bitstr, nsamp=span)


def demod_channel(mm, n_total, freq, *, center, rate, start, duration, sps,
                  lp_cut, thresh_db, merge_us, min_us, chunk_s, overlap_s,
                  lpf_taps, bt):
    """Scan one channel frequency; returns burst records sorted by time."""
    f_off = freq - center
    if abs(f_off) > rate / 2 - lp_cut:
        raise SystemExit(f"freq {freq:.0f} too far from centre {center:.0f} "
                         f"for rate {rate:.0f}")
    if abs(f_off) < lp_cut / 2:
        print(f"# warning: {freq:.0f} sits within {abs(f_off):.0f} Hz of the "
              "tuned centre; the HackRF DC spike overlaps the channel. Tune "
              "the capture a few MHz away (hackrf_capture.sh does by default).",
              file=sys.stderr)
    d = rate / (sps * BIT_RATE)
    if abs(d - round(d)) > 1e-9:
        raise SystemExit(f"rate {rate:.0f} / (sps {sps} * 1 Mbit/s) is not "
                         "an integer; pick another --sps")
    d = int(round(d))
    rate_out = rate / d

    h = design_lpf(rate, lp_cut, lpf_taps)
    K = len(h)
    chunk = int(chunk_s * rate)
    ov = int(overlap_s * rate)
    s0 = int(start * rate)
    s_stop = n_total if duration is None else min(n_total, int((start + duration) * rate))
    recs = []
    while s0 < s_stop:
        last = s0 + chunk >= s_stop
        a = max(0, s0 - K)
        b = min(n_total, s0 + chunk + ov, s_stop + ov)
        raw = mm[a:b].astype(np.float32) / 128.0
        z = (raw[:, 0] + 1j * raw[:, 1]).astype(np.complex64)
        n = np.arange(len(z), dtype=np.float64)
        ph0 = -2 * math.pi * f_off * (a % (1 << 40)) / rate
        lo = np.exp(1j * (ph0 - 2 * math.pi * f_off * n / rate)).astype(np.complex64)
        y = polyphase_decim(z * lo, h, d)
        # polyphase_decim yields full_conv[m*d], centred on input sample
        # m*d - (K-1)/2, so y[0] corresponds to raw time a - (K-1)/2
        t0 = (a - (K - 1) / 2) / rate            # absolute time of y[0]

        p = (y.real ** 2 + y.imag ** 2).astype(np.float64)
        floor = float(np.median(p[::16])) if len(p) else 0.0
        thresh = floor * 10 ** (thresh_db / 10)
        groups = _runs(p > thresh)
        merged = []
        for g in groups:
            if merged and (g[0] - merged[-1][1]) / rate_out * 1e6 < merge_us:
                merged[-1] = (merged[-1][0], g[1])
            else:
                merged.append(list(g))
        for i0, i1 in merged:
            dur_us = (i1 - i0) / rate_out * 1e6
            if dur_us < min_us:
                continue
            r = analyze_burst(y, rate_out, sps, i0, i1, bt=bt)
            r["t"] = t0 + i0 / rate_out
            r["freq_hz"] = freq
            r["cfo_ppm"] = r["cfo_hz"] / freq * 1e6
            if r["t"] < s0 / rate:               # started in the previous chunk
                continue
            if not last and r["t"] >= (s0 + chunk) / rate:
                continue                         # will be seen by the next chunk
            if r["t"] >= s_stop / rate:
                continue                         # beyond the requested slice
            recs.append(r)
        s0 += chunk

    recs.sort(key=lambda r: r["t"])
    prev = None
    for r in recs:
        fama_hex = None
        if r["label"] == "famA":
            s = r["bits"][r["bit_off"] + 24:r["bit_off"] + 80]
            fama_hex = b2h(s) if len(s) == 56 else None
        r["hdr"] = fama_hex
        r["gap_us"] = None if prev is None else (r["t"] - prev) * 1e6
        prev = r["t"]
    return recs


def demod_file(path, freqs=(2450e6,), rate=20e6, center=2450e6, start=0.0,
               duration=None, sps=4, lp_cut=1.2e6, thresh_db=10.0,
               merge_us=5.0, min_us=20.0, chunk_s=0.5, overlap_s=0.05,
               lpf_taps=0, bt=0.5):
    """Demodulate a cs8 file on one or more channels; list of burst records."""
    mm = np.memmap(path, dtype=CS8, mode="r")
    n_total = len(mm)
    recs = []
    for freq in freqs:
        recs += demod_channel(mm, n_total, freq, center=center, rate=rate,
                              start=start, duration=duration, sps=sps,
                              lp_cut=lp_cut, thresh_db=thresh_db,
                              merge_us=merge_us, min_us=min_us,
                              chunk_s=chunk_s, overlap_s=overlap_s,
                              lpf_taps=lpf_taps, bt=bt)
    recs.sort(key=lambda r: (r["t"], r["freq_hz"]))
    return recs


def _fmt(rec):
    cfo = f'{rec["cfo_hz"]:+.0f}Hz'
    ppm = f'{rec["cfo_ppm"]:+.2f}ppm'
    err = "-" if rec["err"] is None else str(rec["err"])
    hdr = rec["hdr"] or "-"
    gap = "-" if rec["gap_us"] is None else f'{rec["gap_us"]:.0f}'
    return (f'{rec["freq_hz"]/1e6:.3f}MHz t={rec["t"]:.6f}s '
            f'dur={rec["dur_us"]:.0f}us pwr={rec["pwr_db"]:.1f}dBFS '
            f'cfo={cfo}({ppm}) dev={rec["dev_hz"]:.0f}Hz '
            f'label={rec["label"]} off={rec["bit_off"]} '
            f'pol={rec["pol"]:+d} err={err} hdr={hdr} gap={gap}')


def _json(rec):
    out = {k: v for k, v in rec.items() if k != "bits"}
    out["bits"] = rec["bits"]
    return json.dumps(out)


DEFAULT_CHANNEL = 2450e6


def read_meta(path):
    """key -> value from a hackrf_capture.sh meta.txt ({} when absent)."""
    out = {}
    if path.is_file():
        for line in path.read_text().splitlines():
            k, _, v = line.partition(" ")
            out[k] = v
    return out


def _bits_line(rec):
    raw = rec["bits"]
    if rec["label"] != "unknown":
        raw = raw[rec["bit_off"]:]
    raw = raw[:len(raw) - len(raw) % 4]              # whole nibbles only
    err = "na" if rec["err"] is None else str(rec["err"])
    return (f'+{rec["t"]:.6f} {int(round(rec["t"] * 1e6))} RAW {b2h(raw)} '
            f'label={rec["label"]} pol={rec["pol"]:+d} err={err}')


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0],
                                 epilog="See the module docstring for details.")
    ap.add_argument("file", help="input .cs8 file (interleaved int8 I/Q)")
    ap.add_argument("--rate", type=float, default=None,
                    help="sample rate Hz (default: meta.txt next to the file, "
                    "else 20e6)")
    ap.add_argument("--center", type=float, default=None,
                    help="capture centre frequency Hz (default: meta.txt next "
                    "to the file, else 2.45e9)")
    ap.add_argument("--freq", type=float, action="append", default=None,
                    metavar="HZ", help="channel to demodulate (repeatable; "
                    "default: the centre frequency)")
    ap.add_argument("--start", type=float, default=0.0,
                    help="analyse from this file offset, seconds")
    ap.add_argument("--duration", type=float, default=None,
                    help="length of the slice to analyse, seconds")
    ap.add_argument("--sps", type=int, default=4,
                    help="decimated samples per bit (default 4)")
    ap.add_argument("--lp-cut", type=float, default=1.2e6,
                    help="low-pass cutoff Hz (default 1.2e6)")
    ap.add_argument("--thresh-db", type=float, default=10.0,
                    help="burst threshold above noise floor, dB")
    ap.add_argument("--bt", type=float, default=0.5,
                    help="GFSK BT of the re-modulation CFO model (0=off)")
    ap.add_argument("--merge-us", type=float, default=5.0,
                    help="merge bursts separated by less than this, us")
    ap.add_argument("--min-us", type=float, default=20.0,
                    help="ignore bursts shorter than this, us")
    ap.add_argument("--chunk-s", type=float, default=0.5,
                    help="processing chunk length, seconds")
    ap.add_argument("--overlap-s", type=float, default=0.05,
                    help="chunk overlap, seconds (> longest packet)")
    ap.add_argument("--jsonl", metavar="PATH", help="write JSON lines here")
    ap.add_argument("--bits-dir", metavar="DIR",
                    help="write one parse_esp-style file per burst here")
    a = ap.parse_args(argv)
    meta = read_meta(Path(a.file).with_name("meta.txt"))
    if a.rate is None:
        a.rate = float(meta.get("rate_sps", 20e6))
    if a.center is None:
        a.center = float(meta.get("freq_hz", 2450e6))
    freqs = a.freq or [DEFAULT_CHANNEL]
    recs = demod_file(a.file, freqs=freqs, rate=a.rate, center=a.center,
                      start=a.start, duration=a.duration, sps=a.sps,
                      lp_cut=a.lp_cut, thresh_db=a.thresh_db,
                      merge_us=a.merge_us, min_us=a.min_us,
                      chunk_s=a.chunk_s, overlap_s=a.overlap_s, bt=a.bt)
    print(f'# iq_demod: file={a.file} rate={a.rate:.0f} center={a.center:.0f} '
          f'freqs={",".join(f"{f:.0f}" for f in freqs)} sps={a.sps} '
          f'burst_thresh={a.thresh_db}dB')
    print("# t_s dur_us pwr_dBFS cfo_Hz(ppm) dev_Hz label off pol err hdr gap_us")
    for r in recs:
        print(_fmt(r))
    if a.jsonl:
        with open(a.jsonl, "w") as f:
            for r in recs:
                f.write(_json(r) + "\n")
    if a.bits_dir:
        d = Path(a.bits_dir)
        d.mkdir(parents=True, exist_ok=True)
        for i, r in enumerate(recs, 1):
            name = f'{i:04d}-{r["label"]}-{int(round(r["t"] * 1e6))}.log'
            (d / name).write_text(_bits_line(r) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
