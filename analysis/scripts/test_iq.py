#!/usr/bin/env python3
"""Tests for iq_synth.py and iq_demod.py (stdlib unittest + numpy).

Run from analysis/scripts:

    python3 test_iq.py

Synthesizes the demo scene (and smaller purpose-built files) into a temp
directory, then checks that the demodulator finds every packet exactly once
with the right label, estimates CFO within 3 kHz, recovers the family A
headers and the family B duration, decodes the remote burst with <= 2 bit
errors at 20 dB SNR, works at a +6 MHz channel offset, with inverted bit
polarity, and across chunk boundaries.  Also cross-checks the polyphase
decimator against a brute-force reference.
"""
import math
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import iq_demod                                     # noqa: E402
import iq_synth                                     # noqa: E402
from decode import encode                           # noqa: E402

RATE = 20e6
CENTER = 2450e6
FAMA_SYNC_GAP_US = 231.0
FAMB_DUR_US = 4000.0                # 32 preamble + 81 header + 3887 payload bits
REMOTE_CFO = 8e3


def lastbit():
    return iq_synth.lastbit_map()


class TestPolyphase(unittest.TestCase):
    def test_polyphase_matches_direct(self):
        rng = np.random.default_rng(42)
        h = iq_demod.design_lpf(RATE, 1.2e6)
        D = 5
        x = (rng.standard_normal(5000) + 1j * rng.standard_normal(5000)).astype(np.complex64)
        y = iq_demod.polyphase_decim(x, h, D)
        K = len(h)
        xp = np.concatenate([np.zeros(K - 1, np.complex64), x])
        m_max = (len(xp) - K) // D + 1
        yd = np.array([sum(h[k] * xp[m * D + K - 1 - k] for k in range(K))
                       for m in range(m_max)])
        self.assertEqual(len(y), m_max)
        np.testing.assert_allclose(y, yd, rtol=1e-4, atol=1e-4)


class TestScene(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="iqtest-")
        cls.path = str(Path(cls.tmp) / "scene.cs8")
        cls.iq, cls.truth = iq_synth.scene(rate=RATE, snr_db=20, seed=7)
        iq_synth.write_cs8(cls.path, cls.iq)
        cls.bursts = iq_demod.demod_file(cls.path, freqs=[CENTER], rate=RATE,
                                         center=CENTER)

    def _by_label(self, label):
        return [b for b in self.bursts if b["label"] == label]

    def test_packets_found_once_with_labels(self):
        self.assertEqual([b["label"] for b in self.bursts],
                         ["remote", "famA", "famA", "famB"])

    def test_cfo_within_3khz(self):
        truth = sorted(self.truth["packets"], key=lambda p: p["t"])
        got = sorted(self.bursts, key=lambda b: b["t"])
        self.assertEqual([p["label"] for p in truth], [b["label"] for b in got])
        for p, b in zip(truth, got):
            self.assertLess(abs(b["cfo_hz"] - p["cfo"]), 3000,
                            f'{p["label"]}: got {b["cfo_hz"]}, want {p["cfo"]}')
            self.assertAlmostEqual(b["cfo_ppm"], b["cfo_hz"] / b["freq_hz"] * 1e6)

    def test_center_and_rate_from_meta(self):
        meta = Path(self.tmp) / "meta.txt"
        meta.write_text(f"freq_hz {CENTER:.0f}\nrate_sps {RATE:.0f}\n")
        try:
            got = iq_demod.read_meta(meta)
            self.assertEqual(float(got["freq_hz"]), CENTER)
            self.assertEqual(float(got["rate_sps"]), RATE)
        finally:
            meta.unlink()

    def test_fama_headers_and_gap(self):
        fa = self._by_label("famA")
        self.assertEqual(len(fa), 2)
        self.assertEqual(fa[0]["hdr"], iq_synth.FAMA_P1)
        self.assertEqual(fa[1]["hdr"], iq_synth.FAMA_P2)
        gap_us = (fa[1]["t"] - fa[0]["t"]) * 1e6
        self.assertAlmostEqual(gap_us, FAMA_SYNC_GAP_US, delta=5.0)

    def test_famb_duration(self):
        fb = self._by_label("famB")
        self.assertEqual(len(fb), 1)
        self.assertLess(abs(fb[0]["dur_us"] - FAMB_DUR_US), 20.0)

    def test_remote_bits(self):
        rem = self._by_label("remote")
        self.assertEqual(len(rem), 1)
        want = encode(72, lastbit())
        got = rem[0]["bits"][rem[0]["bit_off"]:rem[0]["bit_off"] + len(want)]
        errs = sum(a != b for a, b in zip(got, want))
        self.assertLessEqual(errs, 2, f"{errs} bit errors in remote burst")


class TestOffsetPlus6MHz(unittest.TestCase):
    def test_scene_at_plus_6mhz(self):
        tmp = tempfile.mkdtemp(prefix="iqtest-")
        path = str(Path(tmp) / "scene6.cs8")
        iq, _ = iq_synth.scene(rate=RATE, snr_db=20, offset=6e6, seed=3)
        iq_synth.write_cs8(path, iq)
        bursts = iq_demod.demod_file(path, freqs=[CENTER + 6e6], rate=RATE,
                                     center=CENTER)
        self.assertEqual([b["label"] for b in bursts],
                         ["remote", "famA", "famA", "famB"])
        rem = [b for b in bursts if b["label"] == "remote"][0]
        self.assertLess(abs(rem["cfo_hz"] - REMOTE_CFO), 3000)


class TestInvertedPolarity(unittest.TestCase):
    def test_inverted_scene(self):
        tmp = tempfile.mkdtemp(prefix="iqtest-")
        path = str(Path(tmp) / "sceneinv.cs8")
        iq, _ = iq_synth.scene(rate=RATE, snr_db=20, seed=11, invert=True)
        iq_synth.write_cs8(path, iq)
        bursts = iq_demod.demod_file(path, freqs=[CENTER], rate=RATE, center=CENTER)
        self.assertEqual([b["label"] for b in bursts],
                         ["remote", "famA", "famA", "famB"])
        rem = [b for b in bursts if b["label"] == "remote"][0]
        self.assertEqual(rem["pol"], -1)
        want = encode(72, lastbit())
        got = rem["bits"][rem["bit_off"]:rem["bit_off"] + len(want)]
        self.assertLessEqual(sum(a != b for a, b in zip(got, want)), 2)


class TestChunkBoundary(unittest.TestCase):
    def test_packet_straddling_boundary_found_once(self):
        tmp = tempfile.mkdtemp(prefix="iqtest-")
        path = str(Path(tmp) / "edge.cs8")
        bits = iq_synth.remote_air_bits(72)
        packets = [dict(label="remote", t=9e-3, bits=bits, cfo=REMOTE_CFO,
                        dev=250e3, amp=1.0)]
        iq = iq_synth.synth(rate=RATE, total_s=0.02, packets=packets,
                            snr_db=30, seed=5)
        iq_synth.write_cs8(path, iq)
        kw = dict(freqs=[CENTER], rate=RATE, center=CENTER)
        chunked = iq_demod.demod_file(path, chunk_s=0.01, overlap_s=0.005, **kw)
        whole = iq_demod.demod_file(path, **kw)
        self.assertEqual([b["label"] for b in chunked], ["remote"])
        self.assertEqual([b["label"] for b in whole], ["remote"])
        self.assertEqual(chunked[0]["bits"], whole[0]["bits"])
        self.assertLess(abs(chunked[0]["t"] - whole[0]["t"]), 1e-6)


if __name__ == "__main__":
    unittest.main(verbosity=2)
