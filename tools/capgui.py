# /// script
# requires-python = ">=3.11"
# dependencies = ["pyserial"]
# ///
"""Capture GUI: huge clock to align phone video with RF packet logs.

Run:  uv run tools/capgui.py [--esp PORT] [--nrf PORT] [--out DIR]
Space = start/stop, M = mark, Escape = stop.
"""

from __future__ import annotations

import argparse
import contextlib
import math
import queue
import sys
import threading
import time
import tkinter as tk
from collections import deque
from datetime import datetime
from pathlib import Path

import serial

BAUD = 115200
IDLE_BG = "#9e9e9e"
PREROLL_BG = "#e0a010"
REC_BG = "#2bb32b"
STOP_BG = "#e02020"
FLASH_BG = "#00ffff"
SQ_BG = "#1a1a1a"
SQ_FG = "#b0b0b0"
PANEL_BG = "#404040"
CLOCK_FONT = ("DejaVu Sans Mono", 150, "bold")
BIG_FONT = ("DejaVu Sans", 36, "bold")
SQ_FONT = ("DejaVu Sans", 40, "bold")
MED_FONT = ("DejaVu Sans", 18)
SMALL_FONT = ("DejaVu Sans", 14)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="chilipad-rf capture GUI")
    p.add_argument(
        "--esp", default="/dev/ttyUSB0", help="ESP sniffer port, 'none' to disable"
    )
    p.add_argument(
        "--nrf", default="/dev/ttyACM1", help="nRF sniffer port, 'none' to disable"
    )
    p.add_argument(
        "--out",
        default=str(Path.home() / "chilipad-rf" / "captures"),
        help="capture output directory",
    )
    p.add_argument("--selftest", action="store_true", help=argparse.SUPPRESS)
    return p.parse_args()


class Device:
    """One sniffer; state shared between the GUI thread and its reader thread."""

    def __init__(self, key: str, port: str):
        self.key = key
        self.port = port
        self.enabled = port.lower() != "none"
        self.connected = False
        self.ser: serial.Serial | None = None
        self.lock = threading.Lock()
        self.packets = 0
        self.flash_until = 0.0
        self.recent: deque[str] = deque(maxlen=200)  # last lines while idle
        self.logf = None  # <key>.log file, open only during a session
        self.sq_lbl: tk.Label | None = None
        self.cnt_lbl: tk.Label | None = None
        self.stt_lbl: tk.Label | None = None

    def send(self, cmd: str) -> None:
        with self.lock:
            ser = self.ser
            if ser is None or not self.connected:
                return
            # Port may have vanished between the check and the write; that is
            # fine — the reader thread will flag the device as missing.
            with contextlib.suppress(OSError, serial.SerialException):
                ser.write((cmd + "\n").encode("utf-8"))
                ser.flush()


def reader_loop(dev: Device, q: queue.Queue, stop: threading.Event) -> None:
    """Open (and reopen every 2 s on failure) the port, pushing lines to the queue."""
    if not dev.enabled:
        return
    while not stop.is_set():
        ser: serial.Serial | None = None
        try:
            ser = serial.Serial()
            ser.port, ser.baudrate = dev.port, BAUD
            ser.dtr = False  # do not reset the ESP8266 on open
            ser.rts = False
            ser.timeout = 0.2
            ser.open()
            with dev.lock:
                dev.ser = ser
            dev.connected = True
            while not stop.is_set():
                raw = ser.readline()
                if not raw:
                    continue
                line = raw.decode("utf-8", "replace").strip()
                if line:
                    q.put((time.monotonic(), dev.key, line))
        except (OSError, serial.SerialException):
            pass
        finally:
            dev.connected = False
            with dev.lock:
                dev.ser = None
                if ser is not None:
                    with contextlib.suppress(Exception):
                        ser.close()
            time.sleep(2.0)


def fake_reader_loop(dev: Device, q: queue.Queue, stop: threading.Event) -> None:
    """--selftest only: emits a ' RAW ' line every 200 ms without a serial port."""
    if not dev.enabled:
        return
    n = 0
    dev.connected = True
    while not stop.is_set():
        n += 1
        q.put((time.monotonic(), dev.key, f"t={dev.key} n={n} RAW 5aa5"))
        stop.wait(0.2)


class App:
    def __init__(
        self, root: tk.Tk, devices: list[Device], out_dir: Path, selftest: bool
    ):
        self.root = root
        self.devices = devices
        self.by_key = {d.key: d for d in devices}
        self.out_dir = out_dir
        self.selftest = selftest
        self.q: queue.Queue = queue.Queue()
        self.stop_evt = threading.Event()
        self.state = "idle"  # idle | preroll | recording | stopping
        self.t0 = 0.0
        self.pr_end = 0.0
        self.duration = 30.0
        self.mark_n = 0
        self.clock_txt = "+0.0"
        self.session_dir: Path | None = None
        self.allf = None
        self.evtf = None
        self._bg_widgets: list[tk.Widget] = []

        root.title("chilipad-rf capture")
        root.geometry("1200x700")
        root.rowconfigure(0, weight=1)
        root.columnconfigure(0, weight=1)

        self.clock_lbl = tk.Label(root, text="+0.0", font=CLOCK_FONT, fg="#101010")
        self.clock_lbl.grid(row=0, column=0, sticky="nsew")
        self.rem_lbl = tk.Label(root, text="", font=BIG_FONT, fg="#101010")
        self.rem_lbl.grid(row=1, column=0, sticky="ew")
        self.dev_frame = tk.Frame(root)
        self.dev_frame.grid(row=2, column=0, sticky="ew")
        for dev in devices:
            self._make_device_panel(dev)
        self.ctl = tk.Frame(root)
        self.ctl.grid(row=3, column=0, sticky="ew", padx=10, pady=6)
        self._make_controls()
        self.status_lbl = tk.Label(
            root,
            text="idle — Space to start",
            font=SMALL_FONT,
            fg="#101010",
            anchor="w",
        )
        self.status_lbl.grid(row=4, column=0, sticky="ew", padx=10, pady=(0, 6))

        self._bg_widgets += [
            root,
            self.clock_lbl,
            self.rem_lbl,
            self.dev_frame,
            self.ctl,
            self.status_lbl,
        ]
        self._set_bg(IDLE_BG)

        for dev in devices:
            if dev.enabled:
                target = fake_reader_loop if selftest else reader_loop
                threading.Thread(
                    target=target, args=(dev, self.q, self.stop_evt), daemon=True
                ).start()

        root.bind_all("<space>", lambda e: self._hotkey("space"))
        root.bind_all("<Key-m>", lambda e: self._hotkey("mark"))
        root.bind_all("<Key-M>", lambda e: self._hotkey("mark"))
        root.bind_all("<Escape>", lambda e: self._hotkey("esc"))
        root.protocol("WM_DELETE_WINDOW", root.destroy)
        self.tick()

    def _make_device_panel(self, dev: Device) -> None:
        f = tk.Frame(self.dev_frame, bg=PANEL_BG, padx=8, pady=8)
        f.pack(side="left", padx=14, pady=8)
        dev.sq_lbl = tk.Label(
            f, text=dev.key.upper(), font=SQ_FONT, width=7, height=2, bg=SQ_BG, fg=SQ_FG
        )
        dev.sq_lbl.pack()
        dev.cnt_lbl = tk.Label(
            f, text="0 packets", font=MED_FONT, bg=PANEL_BG, fg="#ffffff"
        )
        dev.cnt_lbl.pack()
        dev.stt_lbl = tk.Label(
            f, text="missing", font=MED_FONT, bg=PANEL_BG, fg="#ffffff"
        )
        dev.stt_lbl.pack()

    def _field(
        self, parent: tk.Frame, row: int, col: int, label: str, default: str, width: int
    ) -> tk.Entry:
        lbl = tk.Label(parent, text=label, font=SMALL_FONT, fg="#101010")
        lbl.grid(row=row, column=col, sticky="e", padx=(10, 2))
        self._bg_widgets.append(lbl)
        e = tk.Entry(parent, width=width, font=SMALL_FONT)
        e.insert(0, default)
        e.grid(row=row, column=col + 1, sticky="w")
        return e

    def _make_controls(self) -> None:
        c = self.ctl
        self.dur_e = self._field(c, 0, 0, "Duration (s)", "30", 6)
        self.pre_e = self._field(c, 0, 2, "Pre-roll (s)", "3", 6)
        self.note_e = self._field(c, 0, 4, "Note", "", 32)
        self.btn = tk.Button(
            c, text="Start", font=MED_FONT, width=10, command=self.toggle
        )
        self.btn.grid(row=0, column=6, padx=12)
        self.start_e: dict[str, tk.Entry] = {}
        self.stop_e: dict[str, tk.Entry] = {}
        for i, key in enumerate(("esp", "nrf")):
            self.start_e[key] = self._field(
                c, 1, i * 2, f"{key.upper()} start cmds", "p", 10
            )
            self.stop_e[key] = self._field(
                c, 1, i * 2 + 2, f"{key.upper()} stop cmds", "p", 10
            )

    def _set_bg(self, color: str) -> None:
        for w in self._bg_widgets:
            w.configure(bg=color)

    def _status(self, text: str) -> None:
        self.status_lbl.config(text=text)

    def _event(self, line: str) -> None:
        if self.evtf is not None:
            self.evtf.write(line + "\n")
            self.evtf.flush()

    def toggle(self) -> None:
        if self.state == "idle":
            self.start_capture()
        else:
            self.stop_capture("button")

    def start_capture(self) -> None:
        if self.state != "idle":
            return
        try:
            dur = float(self.dur_e.get())
            pre = float(self.pre_e.get())
        except ValueError:
            self._status("bad duration / pre-roll value")
            return
        if dur <= 0 or pre < 0:
            self._status("duration must be > 0, pre-roll >= 0")
            return
        self.duration = dur
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        sdir = self.out_dir / stamp
        n = 2
        while sdir.exists():
            sdir = self.out_dir / f"{stamp}-{n}"
            n += 1
        sdir.mkdir(parents=True)
        self.session_dir = sdir
        # Long-lived handles for the whole session, closed in _close_files.
        for dev in self.devices:
            dev.logf = (
                open(sdir / f"{dev.key}.log", "a", encoding="utf-8")  # noqa: SIM115
                if dev.enabled
                else None
            )
        self.allf = open(sdir / "all.log", "a", encoding="utf-8")  # noqa: SIM115
        self.evtf = open(sdir / "events.log", "a", encoding="utf-8")  # noqa: SIM115
        meta = [
            f"start: {datetime.now().isoformat(timespec='milliseconds')}",
            f"note: {self.note_e.get()}",
            f"duration: {dur}",
            f"pre_roll: {pre}",
        ]
        for dev in self.devices:
            meta.append(f"{dev.key}_port: {dev.port if dev.enabled else '(disabled)'}")
            meta.append(f"{dev.key}_start: {self.start_e[dev.key].get()}")
            meta.append(f"{dev.key}_stop: {self.stop_e[dev.key].get()}")
        (sdir / "meta.txt").write_text("\n".join(meta) + "\n", encoding="utf-8")
        self.mark_n = 0
        self.state = "preroll"
        self.pr_end = time.monotonic() + pre
        self._set_bg(PREROLL_BG)
        self.btn.config(text="Stop")
        self.rem_lbl.config(text="starting…")
        self._status(str(sdir))

    def _begin_recording(self, now: float) -> None:
        self.t0 = now
        with open(self.session_dir / "meta.txt", "a", encoding="utf-8") as f:
            f.write(f"t0: {datetime.now().isoformat(timespec='milliseconds')}\n")
        self._event("+0.000000 start")
        self.state = "recording"
        self._set_bg(REC_BG)
        self.clock_txt = "+0.0"
        for dev in self.devices:
            for i, cmd in enumerate(self.start_e[dev.key].get().split()):
                self.root.after(i * 100, lambda d=dev, c=cmd: d.send(c))

    def stop_capture(self, reason: str) -> None:
        if self.state == "preroll":
            self.state = "idle"
            self._close_files()
            self._set_bg(IDLE_BG)
            self.rem_lbl.config(text="")
            self.btn.config(text="Start")
            self._status(f"cancelled before start ({reason}) — {self.session_dir}")
            return
        if self.state != "recording":
            return
        t = time.monotonic() - self.t0
        self._event(f"{t:+.6f} stop")
        delay = 0
        for dev in self.devices:
            for i, cmd in enumerate(self.stop_e[dev.key].get().split()):
                self.root.after(i * 100, lambda d=dev, c=cmd: d.send(c))
                delay = max(delay, i * 100)
        self.root.after(delay + 300, self._finish_stop)
        self.state = "stopping"  # keep logging while stop commands settle

    def _finish_stop(self) -> None:
        self.state = "idle"
        self._close_files()
        self._set_bg(STOP_BG)
        self.rem_lbl.config(text="")
        self.btn.config(text="Start")
        self._status(f"saved {self.session_dir}")
        self.root.after(
            500, lambda: self._set_bg(IDLE_BG) if self.state == "idle" else None
        )
        if self.selftest:
            self.root.after(700, self.root.quit)

    def _close_files(self) -> None:
        for f in [self.allf, self.evtf] + [d.logf for d in self.devices]:
            if f is not None:
                with contextlib.suppress(Exception):
                    f.close()
        self.allf = self.evtf = None
        for d in self.devices:
            d.logf = None

    def add_mark(self) -> None:
        if self.state != "recording":
            self._status("mark ignored (not recording)")
            return
        self.mark_n += 1
        rel = time.monotonic() - self.t0
        self._event(f"{rel:+.6f} mark {self.mark_n}")
        self._status(f"mark {self.mark_n} @ +{rel:.1f}")

    def _hotkey(self, what: str) -> None:
        w = self.root.focus_get()
        if what != "esc" and isinstance(w, (tk.Entry, tk.Button)):
            return  # let typing and button-space behave normally
        if what == "space":
            self.toggle()
        elif what == "mark":
            self.add_mark()
        elif what == "esc":
            self.stop_capture("escape")

    def _drain(self) -> None:
        dirty = False
        while True:
            try:
                t, key, line = self.q.get_nowait()
            except queue.Empty:
                break
            dev = self.by_key[key]
            if " RAW " in line:
                dev.packets += 1
                dev.flash_until = time.monotonic() + 0.15
            if self.state in ("recording", "stopping"):
                rel = f"{t - self.t0:+.6f}"
                dev.logf.write(f"{rel} {line}\n")
                self.allf.write(f"{rel} {key} {line}\n")
                dirty = True
            else:
                dev.recent.append(line)  # in-memory only while idle
        if dirty:
            self.allf.flush()
            for dev in self.devices:
                if dev.logf is not None:
                    dev.logf.flush()

    def _update_dev(self, dev: Device, now: float) -> None:
        hot = now < dev.flash_until
        dev.sq_lbl.config(bg=FLASH_BG if hot else SQ_BG, fg="#000000" if hot else SQ_FG)
        dev.cnt_lbl.config(text=f"{dev.packets} packets")
        st = (
            "disabled"
            if not dev.enabled
            else ("connected" if dev.connected else "missing")
        )
        dev.stt_lbl.config(text=st)

    def tick(self) -> None:
        now = time.monotonic()
        self._drain()
        if self.state == "preroll":
            if now >= self.pr_end:
                self._begin_recording(now)
            else:
                self.clock_lbl.config(text=f"-{math.ceil(self.pr_end - now)}")
        elif self.state == "recording":
            t = now - self.t0
            self.clock_txt = f"+{t:.1f}"
            self.clock_lbl.config(text=self.clock_txt)
            self.rem_lbl.config(text=f"{max(self.duration - t, 0.0):.1f} s left")
            if t >= self.duration:
                self.stop_capture("timer")
        for dev in self.devices:
            self._update_dev(dev, now)
        self.root.after(50, self.tick)


def main() -> None:
    args = parse_args()
    root = tk.Tk()
    devices = [Device("esp", args.esp), Device("nrf", args.nrf)]
    app = App(root, devices, Path(args.out).expanduser(), args.selftest)
    if args.selftest:
        app.dur_e.delete(0, "end")
        app.dur_e.insert(0, "2")
        app.pre_e.delete(0, "end")
        app.pre_e.insert(0, "1")
        root.after(300, app.start_capture)
    root.mainloop()
    if args.selftest:
        app.stop_evt.set()
        print(app.session_dir)
        sys.exit(0)


if __name__ == "__main__":
    main()
