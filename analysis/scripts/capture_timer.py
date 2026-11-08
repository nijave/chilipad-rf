#!/usr/bin/env python3
"""Full-screen step timer for a scripted capture (stdlib tkinter only).

Shows the seconds elapsed since the capture started, the current step and
the next one, and flashes the screen when a step begins. With --meta it
waits for hackrf_capture.sh's meta.txt and counts from its start_epoch_ns,
so the timer and the IQ file share one clock.

usage:
  capture_timer.py --duration 90 --step 10:Power --step 20:UP \
      [--meta ../../captures/<stamp>-hackrf/meta.txt | --meta-dir ../../captures]

--meta-dir waits for a new *-hackrf/meta.txt to appear under the directory
(the capture started after the timer). Escape or q closes the window.
Over SSH: DISPLAY=:0 XAUTHORITY=<the X server's -auth file>.
"""
import argparse
import time
import tkinter as tk
from pathlib import Path


def parse_step(s):
    t, _, text = s.partition(":")
    return float(t), text


def start_from_meta(path):
    for line in path.read_text().splitlines():
        if line.startswith("start_epoch_ns "):
            return int(line.split()[1]) / 1e9
    return None


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--duration", type=float, required=True, help="seconds")
    ap.add_argument("--step", action="append", type=parse_step, default=[],
                    metavar="SEC:TEXT", help="step at SEC seconds (repeatable)")
    ap.add_argument("--meta", type=Path, help="meta.txt of a running capture")
    ap.add_argument("--meta-dir", type=Path,
                    help="wait for a new */meta.txt under this directory")
    a = ap.parse_args()
    steps = sorted([(0.0, "Wait")] + a.step)

    seen = set(a.meta_dir.glob("*/meta.txt")) if a.meta_dir else set()
    state = {"t0": None if (a.meta or a.meta_dir) else time.time(), "idx": -1}

    root = tk.Tk()
    root.title("capture timer")
    root.attributes("-fullscreen", True)
    root.attributes("-topmost", True)
    bg = "black"
    root.configure(bg=bg)
    big = tk.Label(root, font=("DejaVu Sans Mono", 220, "bold"), fg="white", bg=bg)
    now = tk.Label(root, font=("DejaVu Sans", 72, "bold"), fg="#ffd34d", bg=bg)
    nxt = tk.Label(root, font=("DejaVu Sans", 40), fg="#9aa0a6", bg=bg)
    for w in (big, now, nxt):
        w.pack(expand=True)
    root.bind("<Escape>", lambda e: root.destroy())
    root.bind("q", lambda e: root.destroy())

    def flash(n=6):
        if n <= 0:
            for w in (root, big, now, nxt):
                w.configure(bg=bg)
            return
        c = "#c62828" if n % 2 else bg
        for w in (root, big, now, nxt):
            w.configure(bg=c)
        root.after(150, flash, n - 1)

    def tick():
        if state["t0"] is None:
            meta = a.meta
            if meta is None:
                new = set(a.meta_dir.glob("*/meta.txt")) - seen
                meta = max(new, key=lambda p: p.stat().st_mtime) if new else None
            if meta and meta.exists():
                state["t0"] = start_from_meta(meta)
            if state["t0"] is None:
                big.configure(text="--")
                now.configure(text="Waiting for the capture to start")
                nxt.configure(text="")
                root.after(100, tick)
                return
        el = time.time() - state["t0"]
        if el >= a.duration:
            big.configure(text=f"{a.duration:.0f}")
            now.configure(text="DONE")
            nxt.configure(text="Escape to close")
            if state["idx"] != len(steps):
                state["idx"] = len(steps)
                flash()
            return
        i = max(k for k, (t, _) in enumerate(steps) if t <= el)
        if i != state["idx"]:
            state["idx"] = i
            if i > 0:
                flash()
        big.configure(text=f"{el:5.1f}")
        now.configure(text=steps[i][1])
        if i + 1 < len(steps):
            t, text = steps[i + 1]
            nxt.configure(text=f"next at {t:.0f} s ({t - el:.0f} s): {text}")
        else:
            nxt.configure(text=f"end at {a.duration:.0f} s ({a.duration - el:.0f} s)")
        root.after(100, tick)

    tick()
    root.mainloop()


if __name__ == "__main__":
    main()
