#!/usr/bin/env python3
"""Per-burst RF detail for ChiliPad setpoint bursts in a HackRF cs8 capture.

usage: burst_detail.py <cap_dir> <scripts_dir> <sends_file|-> [--sp 68] [--win 1.5]

sends_file: lines "<epoch_s> <device> <n_bursts>"; each send gets a demod
window [send-0.3 s, send+win].  '-' = demod the whole file.
Writes <cap_dir>/detail.jsonl and prints one line per 'remote'-labelled burst.
"""
import argparse, json, math, re, sys
from pathlib import Path
import numpy as np

ap = argparse.ArgumentParser()
ap.add_argument("cap"); ap.add_argument("scripts"); ap.add_argument("sends")
ap.add_argument("--sp", type=int, default=68)
ap.add_argument("--win", type=float, default=1.5)
ap.add_argument("--min-pwr", type=float, default=-200)
a = ap.parse_args()
sys.path.insert(0, a.scripts)
import iq_demod as D
from decode import encode, FILE_BITS
from rfcommon import h2b

cap = Path(a.cap)
meta = D.read_meta(cap / "meta.txt")
RATE = float(meta["rate_sps"]); CEN = float(meta["freq_hz"]); T0 = int(meta["start_epoch_ns"]) / 1e9
CH = 2450e6
mm = np.memmap(cap / "iq.cs8", dtype=D.CS8, mode="r")
N = len(mm)

line = [l for l in (Path(a.scripts) / ".." / "decode-output.txt").read_text().splitlines()
        if "true last bits" in l][0]
lastbit = dict(re.findall(r"(\w+)=([01])", line.split(":", 1)[1]))
SYNC = h2b("9388B0CA")


def stream(sp):
    return encode(sp, lastbit)[:FILE_BITS]


def dongle_air(sp):
    s = stream(sp)
    SPLIT, LEAD = 1053, 211
    lead = "".join("0" if (LEAD - i) % 2 == 1 else "1" for i in range(LEAD))
    full1 = lead + s[:SPLIT]
    addr1 = full1[:32]
    pre = "01010101" if addr1[0] == "0" else "10101010"
    return pre + full1 + s[SPLIT:]          # bits continue straight (split is in-stream)


S_EXP = stream(a.sp)
J_S = S_EXP.find(SYNC)
AIR = dongle_air(a.sp)
J_AIR = AIR.find(SYNC)
JUNCTION_AIR = 8 + 32 + 154 * 8                # first bit of the 2nd nRF preamble
LPF = D.design_lpf(RATE, 1.2e6)


def load(s0, s1):
    s0 = max(0, s0); s1 = min(N, s1)
    raw = mm[s0:s1].astype(np.float32) / 128.0
    z = (raw[:, 0] + 1j * raw[:, 1]).astype(np.complex64)
    n = np.arange(s0, s1, dtype=np.float64)
    lo = np.exp(-2j * np.pi * (CH - CEN) * (n % (1 << 30)) / RATE).astype(np.complex64)
    return z * lo, raw


def gauss_m(bits_sym, t_first, n, bt=0.5):
    """frequency shape (+-1) at sample times n (in samples) for symbols bits_sym
    whose first symbol starts at sample t_first; 1 us symbols."""
    sps = RATE / 1e6
    k = np.floor((n - t_first) / sps).astype(int)
    nrz = np.zeros(len(n))
    ok = (k >= 0) & (k < len(bits_sym))
    nrz[ok] = bits_sym[k[ok]]
    sigma = sps * math.sqrt(2 * math.log(2)) / (2 * math.pi * bt)
    half = int(3 * sigma) + 1
    ker = np.exp(-0.5 * (np.arange(-half, half + 1) / sigma) ** 2); ker /= ker.sum()
    return np.convolve(nrz, ker, mode="same"), ok


def analyse(rec, idx):
    sps = int(RATE / 1e6)
    t = rec["t"]; dur = rec["dur_us"] * 1e-6
    s0 = int((t - 300e-6) * RATE); s1 = int((t + dur + 300e-6) * RATE)
    zb, raw = load(s0, s1)
    clip = float((np.abs(raw) >= 126 / 128).mean())
    y = np.convolve(zb, LPF, mode="same")
    p = (y.real ** 2 + y.imag ** 2).astype(np.float64)
    ker = np.ones(2 * sps) / (2 * sps)
    ps = np.convolve(p, ker, mode="same")
    i_s = int(300e-6 * RATE); i_e = i_s + int(dur * RATE)
    q = int(20e-6 * RATE)
    quiet = [np.median(ps[k:k + q]) for k in list(range(0, i_s - int(30e-6 * RATE) - q, q)) +
             list(range(i_e + int(60e-6 * RATE), len(ps) - q, q))]
    floor_p = float(np.min(quiet))
    q_start = [k for k in list(range(0, i_s - int(30e-6 * RATE) - q, q)) +
               list(range(i_e + int(60e-6 * RATE), len(ps) - q, q))][int(np.argmin(quiet))]
    Pb = np.median(ps[i_s + int(200e-6 * RATE): i_e - int(200e-6 * RATE)]) - floor_p
    amps = np.sqrt(np.maximum(ps - floor_p, 0) / Pb)      # 1.0 = body amplitude
    A = 1.0
    floor_amp = math.sqrt(floor_p / Pb)
    mid = (i_s + i_e) // 2
    sa = max(0, i_s - int(30e-6 * RATE)); se = min(len(amps), i_e + int(30e-6 * RATE))
    def up(lvl):      # first upward crossing from just before the detected start
        above = np.flatnonzero(amps[sa:mid] > lvl)
        return sa + int(above[0]) if len(above) else sa
    def down(lvl):    # last downward crossing up to just after the detected end
        above = np.flatnonzero(amps[mid:se] > lvl)
        return mid + int(above[-1]) + 1 if len(above) else se
    r10, r50, r90 = (up(x) for x in (0.1, 0.5, 0.9))
    f90, f50, f10 = (down(x) for x in (0.9, 0.5, 0.1))
    rise_us = (r90 - r10) / RATE * 1e6
    fall_us = (f10 - f90) / RATE * 1e6
    on_us = (f50 - r50) / RATE * 1e6
    # instantaneous frequency, power-weighted
    f = np.angle(y[1:] * np.conj(y[:-1])) * RATE / (2 * np.pi)
    f = np.concatenate([[0.0], f])
    w = np.concatenate([[0.0], p[1:] * p[:-1]])
    # symbols in raw-frequency sense: +1 = higher frequency
    bits = np.frombuffer(rec["bits"].encode(), np.uint8) - 48
    sym = (bits.astype(float) * 2 - 1) * rec["pol"]
    off = rec["bit_off"]
    # coarse bit-0 position: record t is the burst start (i0) in file time
    # bits[0] belongs to y-index i0 + best_phi (<= 3 decimated samples = <1 us)
    n = np.arange(len(y), dtype=float)
    lo_k = max(0, off - 20); hi_k = min(len(sym), off + (len(S_EXP) - J_S) - 20)
    fit_lo = i_s + lo_k * sps; fit_hi = i_s + hi_k * sps
    best = None
    for dt in np.arange(-1.5 * sps, 1.5 * sps, 0.25):
        m, ok = gauss_m(sym, i_s + dt, n)
        sl = slice(fit_lo, fit_hi)
        X = np.column_stack([np.ones(fit_hi - fit_lo), m[sl]])
        ww = w[sl]
        coef, *_ = np.linalg.lstsq(X * np.sqrt(ww)[:, None], f[sl] * np.sqrt(ww), rcond=None)
        res = f[sl] - X @ coef
        sse = float(np.sum(ww * res ** 2) / np.sum(ww))
        if best is None or sse < best[0]:
            best = (sse, dt, coef)
    sse, dt, (cfo, dev) = best
    m, _ = gauss_m(sym, i_s + dt, n)
    resid = f - cfo - dev * m
    rms_res = math.sqrt(sse)
    t_bit0 = i_s + dt                       # sample index of bits[0]
    # windowed CFO / deviation (100 us windows) across the data
    win = 100 * sps
    cw, dw, tw = [], [], []
    for s in range(fit_lo, fit_hi - win + 1, win):
        sl = slice(s, s + win)
        X = np.column_stack([np.ones(win), m[sl]])
        ww = np.sqrt(w[sl])
        c, *_ = np.linalg.lstsq(X * ww[:, None], f[sl] * ww, rcond=None)
        cw.append(c[0]); dw.append(c[1]); tw.append((s - t_bit0) / RATE * 1e6)
    cw = np.array(cw); dw = np.array(dw); tw = np.array(tw)
    slope = float(np.polyfit(tw, cw, 1)[0] * 1000) if len(tw) > 2 else float("nan")  # Hz per ms
    # phase residual for junction / continuity
    ph = np.unwrap(np.angle(y))
    mph = np.cumsum((cfo + dev * m) * 2 * np.pi / RATE)
    phr = ph - mph
    # junction (dongle): air bit JUNCTION_AIR; sync at bits index off <-> air J_AIR
    jk = off + (JUNCTION_AIR - J_AIR)       # bit index of 2nd preamble start
    jn = int(t_bit0 + jk * sps)
    junc = None
    if 50 * sps < jn < len(y) - 50 * sps:
        pre = slice(jn - 30 * sps, jn - 2 * sps); post = slice(jn + 2 * sps, jn + 30 * sps)
        def lin(sl):
            xx = np.arange(sl.start, sl.stop)
            return np.polyfit(xx, phr[sl], 1)
        a1 = lin(pre); a2 = lin(post)
        jump = (np.polyval(a2, jn) - np.polyval(a1, jn))
        jump = (jump + np.pi) % (2 * np.pi) - np.pi
        loc = slice(jn - 5 * sps, jn + 5 * sps)
        ref = slice(jn - 200 * sps, jn - 20 * sps)
        junc = dict(phase_jump_deg=float(np.degrees(jump)),
                    max_res_khz=float(np.max(np.abs(resid[loc])) / 1e3),
                    rms_res_khz=float(np.sqrt(np.mean(resid[loc] ** 2)) / 1e3),
                    ref_rms_res_khz=float(np.sqrt(np.mean(resid[ref] ** 2)) / 1e3),
                    min_amp_rel=float(np.min(amps[loc]) / A),
                    ref_min_amp_rel=float(np.min(amps[ref]) / A))
    # lead-in: from the 50 % rise to the first bit of the alternating
    # preamble run that leads into the sync (demodulated bits)
    k = off - 1
    while k > 0 and bits[k - 1] != bits[k]:
        k -= 1
    alt_n = int(t_bit0 + k * sps)
    k_mod = alt_n
    lead_us = (alt_n - r50) / RATE * 1e6
    lead_f = float(np.sum((f * w)[r90:alt_n - sps]) / np.sum(w[r90:alt_n - sps]) - cfo) if alt_n - sps - r90 > 2 * sps else float("nan")
    alt_bits = off - k
    # spectrum of the body, raw 20 MS/s (no LPF)
    bs = slice(r90 + 20 * sps, f90 - 20 * sps)
    seg = zb[bs]
    nfft = 1024
    nseg = len(seg) // nfft
    win_h = np.hanning(nfft)
    P = np.zeros(nfft)
    for k in range(nseg):
        P += np.abs(np.fft.fftshift(np.fft.fft(seg[k * nfft:(k + 1) * nfft] * win_h))) ** 2
    P /= max(nseg, 1)
    qseg = zb[q_start - 2 * nfft: q_start + 2 * nfft] if q_start >= 2 * nfft else zb[q_start:q_start + 3 * nfft]
    Q = np.zeros(nfft)
    for k in range(len(qseg) // nfft):
        Q += np.abs(np.fft.fftshift(np.fft.fft(qseg[k * nfft:(k + 1) * nfft] * win_h))) ** 2
    Q /= max(len(qseg) // nfft, 1)
    fr = np.fft.fftshift(np.fft.fftfreq(nfft, 1 / RATE))
    def sm(v):
        return np.convolve(v, np.ones(5) / 5, mode="same")
    Ps, Qs = sm(P), sm(Q)
    Pn = np.maximum(Ps - Qs, 1e-30)
    inb = np.abs(fr) < 3e6
    c = np.cumsum(Pn[inb]); c /= c[-1]
    fi = fr[inb]
    obw99 = float(fi[np.searchsorted(c, 0.995)] - fi[np.searchsorted(c, 0.005)])
    pk = Ps.max(); jp = int(np.argmax(Ps))
    lo_i = jp
    while lo_i > 0 and Ps[lo_i] > pk / 100: lo_i -= 1
    hi_i = jp
    while hi_i < nfft - 1 and Ps[hi_i] > pk / 100: hi_i += 1
    bw20 = float(fr[hi_i] - fr[lo_i])
    dcmask = np.abs(fr - (CEN - CH)) < 300e3
    out = (np.abs(fr) > 1.5e6) & ~dcmask
    exc = Pn[out]
    spur_dbc = float(10 * np.log10(exc.max() / pk))
    spur_f = float(fr[out][np.argmax(exc)])
    floor_dbc = float(10 * np.log10(np.median(Qs[out]) / pk))
    psd_pts = {}
    for off_hz in (-3e6, -2e6, -1.5e6, -1e6, -0.5e6, 0, 0.5e6, 1e6, 1.5e6, 2e6, 3e6):
        j = int(np.argmin(np.abs(fr - off_hz)))
        psd_pts[f"{off_hz/1e6:+.1f}"] = float(10 * np.log10(Ps[j] / pk))
    # bit errors vs expected stream (from the sync onward)
    got = rec["bits"][off:off + len(S_EXP) - J_S]
    exp = S_EXP[J_S:J_S + len(got)]
    errpos = [i for i, (g, e) in enumerate(zip(got, exp)) if g != e]
    # best setpoint (for the remote reference where the setpoint is unknown)
    best_sp = None
    if errpos:
        cand = []
        for sp in range(55, 94):
            se = stream(sp); js = se.find(SYNC)
            ee = se[js:js + len(got)]
            cand.append((sum(1 for g, e in zip(got, ee) if g != e), sp))
        cand.sort(); best_sp = cand[0][1]
        if best_sp != a.sp:
            se = stream(best_sp); js = se.find(SYNC); ee = se[js:js + len(got)]
            errpos = [i for i, (g, e) in enumerate(zip(got, ee)) if g != e]
    # lead bits before the sync vs dongle air
    lead_err = None
    if off >= J_AIR - 8:
        gl = rec["bits"][off - (J_AIR - 40):off]
        el = AIR[40:J_AIR]
        lead_err = sum(1 for g, e in zip(gl, el) if g != e)
    out = dict(idx=idx, t=t, epoch=T0 + t, dur_us=rec["dur_us"], on_us=on_us,
               pwr_db=rec["pwr_db"], clip=clip, snr_db=float(10 * np.log10(Pb / floor_p)),
               cfo_demod=rec["cfo_hz"], dev_demod=rec["dev_hz"],
               cfo_hz=float(cfo), cfo_ppm=float(cfo / CH * 1e6), dev_hz=float(dev),
               rms_res_khz=rms_res / 1e3, timing_dt=float(dt),
               cfo_win_span_hz=float(cw.max() - cw.min()) if len(cw) else None,
               cfo_win_std_hz=float(cw.std()) if len(cw) else None,
               cfo_slope_hz_per_ms=slope, cfo_first_minus_mean_hz=float(cw[0] - cw.mean()) if len(cw) else None,
               dev_win_span_hz=float(dw.max() - dw.min()) if len(dw) else None,
               cfo_win=[round(float(x)) for x in cw], dev_win=[round(float(x)) for x in dw],
               rise_us=rise_us, r50_us=(r50 - i_s) / RATE * 1e6, alt_us=(alt_n - i_s) / RATE * 1e6, bit0_us=(t_bit0 - i_s) / RATE * 1e6, fall_us=fall_us, lead_us=lead_us, lead_f_hz=lead_f,
               junction=junc, obw99_hz=obw99, bw20_hz=bw20, spur_dbc=spur_dbc,
               spur_f_hz=spur_f, floor_dbc=floor_dbc, alt_bits=alt_bits, psd=psd_pts, psd_raw=[float(x) for x in 10*np.log10(Ps/pk)], n_bits_cmp=len(got), errs=len(errpos),
               errpos=errpos[:40], best_sp=best_sp or a.sp, lead_err=lead_err,
               bit_off=off, label=rec["label"], err_sync=rec["err"])
    return out


recs = []
if a.sends == "-":
    windows = [(0.0, None, "?", 0)]
else:
    windows = []
    for ln in Path(a.sends).read_text().split("\n"):
        if not ln.strip() or ln.startswith("#"):
            continue
        ep, dev_name, nb = ln.split()[:3]
        st = float(ep) - T0 - 0.3
        windows.append((max(0.0, st), a.win, dev_name, int(nb)))
for st, du, name, nb in windows:
    rr = D.demod_file(str(cap / "iq.cs8"), freqs=[CH], rate=RATE, center=CEN, start=st, duration=du)
    for r in rr:
        if r["label"] != "remote" or r["pwr_db"] < a.min_pwr:
            continue
        r["device"] = name
        recs.append(r)
outs = []
for i, r in enumerate(recs):
    o = analyse(r, i)
    o["device"] = r["device"]
    outs.append(o)
    j = o["junction"] or {}
    print(f'{o["device"]:8s} t={o["t"]:.6f} dur={o["dur_us"]:.0f} on={o["on_us"]:.1f} pwr={o["pwr_db"]:.1f} '
          f'snr={o["snr_db"]:.1f} cfo={o["cfo_hz"]:+.0f}({o["cfo_ppm"]:+.2f}ppm) dev={o["dev_hz"]:.0f} '
          f'res={o["rms_res_khz"]:.1f}k slope={o["cfo_slope_hz_per_ms"]:+.0f}Hz/ms span={o["cfo_win_span_hz"]:.0f} '
          f'rise={o["rise_us"]:.1f} fall={o["fall_us"]:.1f} lead={o["lead_us"]:.1f}us@{o["lead_f_hz"]:+.0f} alt={o["alt_bits"]} '
          f'obw={o["obw99_hz"]/1e3:.0f}k bw20={o["bw20_hz"]/1e3:.0f}k spur={o["spur_dbc"]:.1f}@{o["spur_f_hz"]/1e6:+.2f}(floor{o["floor_dbc"]:.0f}) '
          f'err={o["errs"]}/{o["n_bits_cmp"]} sp={o["best_sp"]} lead_err={o["lead_err"]} '
          f'jph={j.get("phase_jump_deg", float("nan")):+.0f} jres={j.get("max_res_khz", float("nan")):.0f}k '
          f'cw={o["cfo_win"]} jamp={j.get("min_amp_rel", float("nan")):.2f} clip={o["clip"]:.4f}')
with open(cap / "detail.jsonl", "w") as fh:
    for o in outs:
        fh.write(json.dumps(o) + "\n")
