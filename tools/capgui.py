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
        default=str(Path(__file__).resolve().parents[1] / "captures"),
        help="capture output directory (default: the repo's captures/)",
    )
    p.add_argument("--duration", default="30", help="capture length in seconds")
    p.add_argument("--note", default="", help="note saved in meta.txt")
    for key in ("esp", "nrf"):
        p.add_argument(
            f"--{key}-start",
            default="p",
            help="start commands; 'cmd@sec' sends cmd sec seconds after zero",
        )
        p.add_argument(f"--{key}-stop", default="p", help="stop commands")
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
            # ESP8266: DTR/RTS low so opening the port does not reset it.
            # nRF52 CDC ACM: the Zephyr USB stack ignores the port until the
            # host asserts DTR, so raise it there.
            ser.dtr = dev.key != "esp"
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
        self.session = 0  # bumped on every schedule/cancel; stale callbacks drop
        self._pending: list[str] = []  # after() ids of this session's start cmds

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
                c, 1, i * 2, f"{key.upper()} start cmds", "p", 24
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
        self._cancel_pending()
        for dev in self.devices:
            self._schedule(dev, self.start_e[dev.key].get())

    def _cancel_pending(self) -> None:
        """Drop every queued start command and invalidate any already in flight."""
        for aid in self._pending:
            # Tk ignores ids that already fired, so no need to track completion.
            self.root.after_cancel(aid)
        self._pending.clear()
        self.session += 1

    def _schedule(self, dev: Device, cmds: str) -> None:
        """Send space-separated commands 100 ms apart; 'cmd@sec' runs at sec."""
        s = self.session
        for i, tok in enumerate(cmds.split()):
            cmd, _, at = tok.partition("@")
            try:
                ms = int(float(at) * 1000) if at else i * 100
            except ValueError:
                ms = i * 100
            self._pending.append(
                self.root.after(
                    ms, lambda d=dev, c=cmd, m=ms, s=s: self._send(d, c, m, s)
                )
            )

    def _send(self, dev: Device, cmd: str, ms: int, session: int) -> None:
        if session != self.session or self.state not in ("recording", "stopping"):
            return
        dev.send(cmd)
        self._event(f"{time.monotonic() - self.t0:+.6f} {dev.key} cmd {cmd}")

    def stop_capture(self, reason: str) -> None:
        if self.state in ("preroll", "recording"):
            self._cancel_pending()
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


def headless_check() -> None:
    """--selftest only: start/stop scheduling with a fake root, no display needed.

    Starts a session with plain and 'cmd@sec' start commands, stops it early,
    starts a second one, and checks no first-session command leaks into it.
    """

    class FakeRoot:
        def __init__(self) -> None:
            self.now = 0
            self.jobs: dict[str, tuple[int, object]] = {}
            self.n = 0

        def after(self, ms, fn):
            self.n += 1
            aid = f"after#{self.n}"
            self.jobs[aid] = (self.now + ms, fn)
            return aid

        def after_cancel(self, aid) -> None:
            self.jobs.pop(aid, None)

        def run_until(self, t: int) -> None:
            while True:
                due = [(at, aid) for aid, (at, _) in self.jobs.items() if at <= t]
                if not due:
                    break
                at, aid = min(due, key=lambda x: (x[0], int(x[1].split("#")[1])))
                self.now = at
                self.jobs.pop(aid)[1]()
            self.now = t

    class Stub:
        def __init__(self, text: str = "") -> None:
            self.text = text

        def get(self) -> str:
            return self.text

        def config(self, **kw) -> None:
            pass

    class Log:
        def __init__(self) -> None:
            self.lines: list[str] = []

        def write(self, s: str) -> None:
            self.lines.append(s.strip())

        def flush(self) -> None:
            pass

        def close(self) -> None:
            pass

    root = FakeRoot()
    sent: list[tuple[int, str, str]] = []
    devices = []
    for key in ("esp", "nrf"):
        d = Device(key, "none")
        d.send = lambda c, k=key: sent.append((root.now, k, c))  # type: ignore[method-assign]
        devices.append(d)
    app = App.__new__(App)
    app.root, app.devices, app.selftest = root, devices, False
    app.by_key = {d.key: d for d in devices}
    app.state, app.session, app._pending = "idle", 0, []
    app.allf = app.evtf = None
    app._bg_widgets = []
    app.rem_lbl = app.btn = app.status_lbl = Stub()
    app.start_e = {"esp": Stub("p"), "nrf": Stub("a b x20@5")}
    app.stop_e = {"esp": Stub("p"), "nrf": Stub("p q")}
    app.session_dir = Path("/nonexistent")

    def begin() -> Log:
        log = Log()
        app.evtf = log
        app.state, app.t0 = "recording", time.monotonic()
        app._cancel_pending()
        for dev in devices:
            app._schedule(dev, app.start_e[dev.key].get())
        return log

    # Session 1: let plain cmds go out, stop at 1 s before x20@5 fires.
    log1 = begin()
    root.run_until(1000)
    assert [c for _, _, c in sent] == ["p", "a", "b"], sent
    assert sum(" cmd " in s for s in log1.lines) == 3, log1.lines
    app.stop_capture("selftest")
    assert app._pending == [], app._pending
    root.run_until(2000)  # stop cmds + _finish_stop
    stop_sent = [(k, c) for t, k, c in sent if t >= 1000]
    assert stop_sent == [("esp", "p"), ("nrf", "p"), ("nrf", "q")], stop_sent
    assert app.state == "idle", app.state

    # Session 2 starts at 3 s; session 1's x20 (due at 5 s) must not fire.
    root.run_until(3000)
    del sent[:]
    log2 = begin()
    root.run_until(7000)
    want = [(3000, "esp", "p"), (3000, "nrf", "a"), (3100, "nrf", "b")]
    assert sent == want, sent
    root.run_until(8000)  # session 2's own x20@5
    assert sent == want + [(8000, "nrf", "x20")], sent
    assert sum(" cmd " in s for s in log2.lines) == 4, log2.lines

    # A stale callback that slips past cancel is still dropped by session tag.
    stale = app.session - 1
    n = len(sent)
    app._send(devices[1], "x20", 0, stale)
    assert len(sent) == n, sent
    print("headless check ok: stop cancels pending start commands")


def main() -> None:
    args = parse_args()
    if args.selftest:
        headless_check()
    try:
        root = tk.Tk()
    except tk.TclError as e:
        if not args.selftest:
            raise
        print(f"GUI selftest skipped: {e}")
        sys.exit(0)
    devices = [Device("esp", args.esp), Device("nrf", args.nrf)]
    app = App(root, devices, Path(args.out).expanduser(), args.selftest)
    fields = [(app.dur_e, args.duration), (app.note_e, args.note)]
    for key in ("esp", "nrf"):
        fields.append((app.start_e[key], getattr(args, f"{key}_start")))
        fields.append((app.stop_e[key], getattr(args, f"{key}_stop")))
    for entry, value in fields:
        entry.delete(0, "end")
        entry.insert(0, value)
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
