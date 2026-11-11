#!/usr/bin/env python3
"""Home Assistant MQTT bridge for the ChiliPad Cube, through the chilipad-sniffer dongle.

The dongle (nRF52840, USB CDC ACM, product "chilipad-sniffer") sends a setpoint
burst on the command `S<degF>` and, while capturing with the trigger
`9388B0CA`, prints one `P ... RAW <hex>` line per received window. This
script:

* exposes the Cube as an MQTT `climate` entity (Home Assistant discovery);
* sends `S<n>` when Home Assistant sets a temperature;
* decodes presses of the real remote from the capture and publishes them, so
  Home Assistant follows manual changes ("sniff" mode, on by default).

The Cube sends no feedback. The state is the last value sent or sniffed.

Modes:
    chilipad_bridge.py --mqtt-host broker            # normal service
    chilipad_bridge.py --once 72                     # send one setpoint, exit
    chilipad_bridge.py --selftest                    # decoder test, no hardware
"""

from __future__ import annotations

import argparse
import collections
import json
import logging
import os
import queue
import re
import signal
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("chilipad")

# ---------------------------------------------------------------------------
# Radio packet model (from docs/analysis/burst-structure.md and docs/analysis/scripts/)
# ---------------------------------------------------------------------------

TRIGGER = "9388B0CA"  # dongle trigger address; RAW starts right after it
HW_MIN, HW_MAX = 55, 93  # range the firmware `S` command accepts

# 64-bit blocks in the 9388B0CA-aligned (RAW) view, first bit shown as 1.
# The true codeword starts 1 bit later; LAST_ZERO lists those whose last bit is 0.
BLOCKS = {
    "S": "9388B0CAD019F2E6",
    "C4": "C4F30304A5ECD55C",
    "A2": "A2E1F402A42EF39A",
    "A4": "A4717A73025C4516",
    "A7": "A7E03171410A772C",
    "8A": "8A27C039D2C6C844",
    "81": "81C0514D66CA15F8",
    "CC": "CC18FE2A95645252",
    "B5": "B52BE101838EE2C9",
    "A0": "A00C1ED3888E902E",
    "F0": "F020764CA1565672",
    "C5": "C53AD4112D89E197",
    "E3": "E33124028D105F97",
    "93": "93035E1B4395C402",
    "8B": "8B18AA986C3302DE",
    "8C": "8CB0A48A7E244638",
}
# Nibble value of each block, in order 0..F.
BY_NIBBLE = [
    "S",
    "C4",
    "A2",
    "A4",
    "A7",
    "8A",
    "81",
    "CC",
    "B5",
    "A0",
    "F0",
    "C5",
    "E3",
    "93",
    "8B",
    "8C",
]
LAST_ZERO = {"A4", "CC", "C5", "A2"}  # all other codewords end in 1
HEADER = [0x80, 0x03, 0x80, 0x03, 0x5A, 0x5A, 0x80, 0x03]
TRAILER = "1010"
END_TAIL = "0010011" + "0000000"
DATA_BITS = 1 + 16 * 64 + 4 + 20 * 64  # 2309
FROM32_CUT = 256  # bits; window that triggered on the internal 9388B0CA at byte 32
BURST_GAP_US = 100_000  # dt above this starts a new burst
BURST_IDLE_S = 0.3  # finish a burst after this much silence


def h2b(h: str) -> str:
    return "".join(f"{int(c, 16):04b}" for c in h)


def crc16_kermit(data: bytes) -> int:
    crc = 0
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0x8408 if crc & 1 else crc >> 1
    return crc


CMD_SET = 0x44  # setpoint press (also powers the Cube on)
CMD_OFF = 0x50  # power-off press; still carries the remote's setpoint
CMDS = {CMD_SET: "auto", CMD_OFF: "off"}
# Payload bytes 4 and 5. Probably a per-remote device ID (a hypothesis from one
# remote, see docs/analysis/burst-structure.md); --device-id replaces it.
DEVICE_ID = bytes([0x40, 0x40])


def packet(sp: int, cmd: int = CMD_SET) -> bytes:
    """Logical packet: length, 7-byte payload, CRC-16/KERMIT (low byte first).

    Payload `A5 <cmd> SP ID0 ID1 00 X` with X = (cmd + SP + ID0 + ID1) & 0xFF.
    With the ID 40 40, X = SP-60 for a setpoint press and SP-48 for an off
    press."""
    id0, id1 = DEVICE_ID
    body = bytes(
        [0x07, 0xA5, cmd, sp & 0xFF, id0, id1, 0x00, (cmd + sp + id0 + id1) & 0xFF]
    )
    c = crc16_kermit(body)
    return body + bytes([c & 0xFF, c >> 8])


def encode(sp: int, cmd: int = CMD_SET) -> str:
    """On-air bits of a press at `sp` degF, from the first bit of 9388B0CA."""

    def cw(n: int) -> str:
        name = BY_NIBBLE[n]
        return h2b(BLOCKS[name])[1:] + ("0" if name in LAST_ZERO else "1")

    def nibbles(bs):
        for b in bs:
            yield b & 0xF
            yield b >> 4

    bits = "1" + "".join(cw(n) for n in nibbles(HEADER)) + TRAILER
    bits += "".join(cw(n) for n in nibbles(packet(sp, cmd)))
    assert len(bits) == DATA_BITS
    return bits + END_TAIL


@dataclass
class Window:
    dt: int  # us since the previous received window
    raw: str  # RAW hex (bytes after the trigger)


@dataclass
class Decode:
    setpoint: int | None
    cmd: int
    errors: int
    nbits: int
    margin: int
    form: str
    reason: str = ""


class PressDecoder:
    """Match a burst (one or more windows) against every candidate setpoint.

    Same method as docs/analysis/scripts/identify_presses.py: place each window at
    the sum of the dt values since the first window (rounded to 4 bits), then
    count bit errors against the encoding of each candidate. Candidates are
    the setpoint (0x44) and off (0x50) presses for 40..100 degF, so that codes
    outside the usable range (for example the 46 "low" code) are recognised
    and rejected instead of misread.
    """

    def __init__(
        self,
        lo: int = 40,
        hi: int = 100,
        max_error_rate: float = 0.03,
        min_margin: int = 8,
        min_bits: int = 256,
    ):
        self.enc: dict[tuple[int, int, str], str] = {}
        for cmd in CMDS:
            for sp in range(lo, hi + 1):
                e = encode(sp, cmd)
                self.enc[(sp, cmd, "full")] = e
                self.enc[(sp, cmd, "from32")] = e[FROM32_CUT:]
        self.max_error_rate = max_error_rate
        self.min_margin = min_margin
        self.min_bits = min_bits

    @staticmethod
    def _score(enc: str, wins: list[Window]) -> tuple[int, int]:
        off = bad = n = 0
        for i, w in enumerate(wins):
            if i:
                off += w.dt
            o = round(off / 4) * 4
            b = h2b(TRIGGER + w.raw)
            lim = min(len(b), len(enc) - o)
            if lim <= 0:
                continue
            bad += (int(b[:lim], 2) ^ int(enc[o : o + lim], 2)).bit_count()
            n += lim
        return bad, n

    def decode(self, wins: list[Window]) -> Decode:
        if not wins:
            return Decode(None, 0, 0, 0, 0, "", "empty")
        best: dict[tuple[int, int], tuple[int, int, str]] = {}
        for (sp, cmd, form), e in self.enc.items():
            bad, n = self._score(e, wins)
            cur = best.get((sp, cmd))
            if cur is None or bad < cur[0]:
                best[(sp, cmd)] = (bad, n, form)
        ranked = sorted((v[0], key) for key, v in best.items())
        (bad, (sp, cmd)), (bad2, _) = ranked[0], ranked[1]
        _, n, form = best[(sp, cmd)]
        margin = bad2 - bad
        d = Decode(sp, cmd, bad, n, margin, form)
        if n < self.min_bits:
            d.setpoint, d.reason = None, f"too few bits ({n})"
        elif bad > self.max_error_rate * n:
            d.setpoint, d.reason = None, f"too many errors ({bad}/{n})"
        elif margin < self.min_margin:
            d.setpoint, d.reason = None, f"ambiguous (margin {margin})"
        return d


class BurstAssembler:
    """Group `P` windows into bursts by their dt and hand each burst to a callback."""

    def __init__(
        self, on_burst, gap_us: int = BURST_GAP_US, idle_s: float = BURST_IDLE_S
    ):
        self.on_burst = on_burst
        self.gap_us = gap_us
        self.idle_s = idle_s
        self.wins: list[Window] = []
        self.last_rx = 0.0
        self.lock = threading.Lock()

    def feed(self, w: Window, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        done = None
        with self.lock:
            if self.wins and w.dt > self.gap_us:
                done, self.wins = self.wins, []
            self.wins.append(w)
            self.last_rx = now
        if done:
            self.on_burst(done)

    def flush(self, now: float | None = None, force: bool = False) -> None:
        now = time.monotonic() if now is None else now
        with self.lock:
            if not self.wins or (not force and now - self.last_rx < self.idle_s):
                return
            done, self.wins = self.wins, []
        self.on_burst(done)

    def reset(self) -> None:
        with self.lock:
            self.wins = []


P_LINE = re.compile(
    r"(?:^|\s)P (?P<tus>\d+) dt=(?P<dt>\d+) ch=(?P<ch>\d+) "
    r"rssi=(?P<rssi>-?\d+) RAW (?P<raw>[0-9A-Fa-f]+)"
)
TX_LINE = re.compile(r"^\[tx\] sent=(\d+) segs=(\d+) timeouts=(\d+)")
CAP_LINE = re.compile(r"^\[cap\] capture (ON|OFF)")
HELP_CAP_LINE = re.compile(r"^capture\s*:\s*(on|off)")
READY_LINE = re.compile(r"^\[ok\] chilipad-sniffer ready")
UNKNOWN_LINE = re.compile(r"^(\[\?\]|\[[SO]\] (usage|encoder failed))")


def parse_p_line(line: str) -> Window | None:
    m = P_LINE.search(line)
    if not m:
        return None
    return Window(dt=int(m["dt"]), raw=m["raw"].upper())


def parse_tx_line(line: str) -> tuple[int, int, int] | None:
    m = TX_LINE.match(line)
    return (int(m[1]), int(m[2]), int(m[3])) if m else None


def tx_ok(res: tuple[int, int, int] | None) -> bool:
    return res is not None and res[0] >= 1 and res[2] == 0


def parse_setpoint(payload: str, lo: int, hi: int) -> int | None:
    """Home Assistant sends e.g. '72' or '72.0'. Returns a clamped int or None."""
    try:
        v = float(payload.strip())
    except ValueError:
        return None
    if v != v or v in (float("inf"), float("-inf")):
        return None
    n = int(round(v))
    if n < lo or n > hi:
        log.warning("setpoint %s outside %d..%d, clamped", payload.strip(), lo, hi)
    return max(lo, min(hi, n))


# ---------------------------------------------------------------------------
# Dongle serial link
# ---------------------------------------------------------------------------

VID = 0x2FE3
PRODUCT = "chilipad-sniffer"
BAUD = 115200


def is_esp_port(dev: str) -> bool:
    return "ttyUSB" in dev


def find_dongle_port() -> str | None:
    from serial.tools import list_ports

    hits = []
    for p in list_ports.comports():
        if is_esp_port(p.device):
            continue
        prod = (p.product or "") + " " + (p.description or "")
        score = (p.vid == VID) + (PRODUCT in prod)
        if score:
            hits.append((score, p.device))
    hits.sort(reverse=True)
    return hits[0][1] if hits else None


class Dongle:
    """Owns the serial port: reconnects, reads lines in a thread, lets callers
    write commands and wait for reply lines."""

    def __init__(self, port: str | None, on_line, on_connect=None, on_disconnect=None):
        self.port_arg = port
        self.on_line = on_line
        self.on_connect = on_connect
        self.on_disconnect = on_disconnect
        self.ser = None
        self.connected = threading.Event()
        self.capturing: bool | None = None
        self.stop_evt = threading.Event()
        self.wlock = threading.Lock()
        self.cond = threading.Condition()
        self.seq = 0
        self.recent: collections.deque[tuple[int, str]] = collections.deque(maxlen=400)
        self.thread = threading.Thread(target=self._run, name="serial", daemon=True)

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.stop_evt.set()
        self.thread.join(timeout=3)

    def _resolve(self) -> str | None:
        if self.port_arg:
            return self.port_arg
        return find_dongle_port()

    def _run(self) -> None:
        import serial

        missing_logged = False
        while not self.stop_evt.is_set():
            port = self._resolve()
            if not port:
                if not missing_logged:
                    log.warning(
                        "dongle not found (VID 0x%04x / %r); retrying", VID, PRODUCT
                    )
                    missing_logged = True
                self.stop_evt.wait(2.0)
                continue
            ser = None
            try:
                ser = serial.Serial()
                ser.port, ser.baudrate, ser.timeout = port, BAUD, 0.2
                ser.dtr = True  # Zephyr CDC ACM ignores the host until DTR is set
                ser.rts = False
                ser.exclusive = True
                ser.open()
                missing_logged = False
                self.ser = ser
                self.capturing = None
                self.connected.set()
                log.info("dongle connected on %s", port)
                if self.on_connect:
                    self.on_connect()
                buf = b""
                while not self.stop_evt.is_set():
                    chunk = ser.read(ser.in_waiting or 1)
                    if not chunk:
                        continue
                    buf += chunk
                    while b"\n" in buf:
                        raw, buf = buf.split(b"\n", 1)
                        line = raw.decode("utf-8", "replace").strip()
                        if line:
                            self._dispatch(line)
            except (OSError, serial.SerialException) as e:
                log.warning("serial error on %s: %s", port, e)
            finally:
                was = self.connected.is_set()
                self.connected.clear()
                self.capturing = None
                with self.wlock:
                    self.ser = None
                if ser is not None:
                    try:
                        ser.close()
                    except Exception:  # noqa: BLE001
                        pass
                if was and not self.stop_evt.is_set():
                    log.warning("dongle disconnected")
                    if self.on_disconnect:
                        self.on_disconnect()
            self.stop_evt.wait(2.0)

    def _dispatch(self, line: str) -> None:
        if m := CAP_LINE.match(line):
            self.capturing = m[1] == "ON"
        elif m := HELP_CAP_LINE.match(line):
            self.capturing = m[1] == "on"
        elif READY_LINE.match(line):
            self.capturing = False
        if not line.startswith("P "):
            log.debug("dongle: %s", line)
            with self.cond:
                self.seq += 1
                self.recent.append((self.seq, line))
                self.cond.notify_all()
        try:
            self.on_line(line)
        except Exception:  # noqa: BLE001
            log.exception("line handler failed")

    def mark(self) -> int:
        with self.cond:
            return self.seq

    def write(self, cmd: str) -> None:
        with self.wlock:
            if self.ser is None:
                raise OSError("dongle not connected")
            log.debug("-> %s", cmd)
            self.ser.write((cmd + "\n").encode("ascii"))
            self.ser.flush()

    def wait_for(
        self, pattern: re.Pattern, after: int, timeout: float
    ) -> re.Match | None:
        deadline = time.monotonic() + timeout
        with self.cond:
            while True:
                for s, line in self.recent:
                    if s > after and (m := pattern.match(line)):
                        return m
                left = deadline - time.monotonic()
                if left <= 0 or not self.connected.is_set():
                    return None
                self.cond.wait(left)

    def command(self, cmd: str, pattern: re.Pattern, timeout: float) -> re.Match | None:
        mark = self.mark()
        self.write(cmd)
        return self.wait_for(pattern, mark, timeout)

    # -- capture control ----------------------------------------------------

    def query_capture(self) -> bool | None:
        m = self.command("h", HELP_CAP_LINE, 2.0)
        return None if m is None else m[1] == "on"

    def set_capture(self, on: bool) -> bool:
        if self.capturing is None:
            self.query_capture()
        for _ in range(2):
            if self.capturing is on:
                return True
            m = self.command("p", CAP_LINE, 1.5)
            if m is None:
                self.query_capture()
        return self.capturing is on


ANY_REPLY = re.compile(r"^\[(cfg|addr|\?)\]")
TX_OR_ERR = re.compile(
    r"^(\[tx\] sent=\d+ segs=\d+ timeouts=\d+|\[\?\].*|\[[SO]\] (usage|encoder failed).*)"
)
RX_CONFIG = ["c50", "mb1", "l64", f"w{TRIGGER}"]


def send_press(
    dongle: Dongle, letter: str, n: int, attempts: int = 2, timeout: float = 3.0
) -> bool:
    """Send `S<n>` (setpoint / power on) or `O<n>` (power off) and wait for a
    clean `[tx]` line; retry once. The caller handles capture."""
    cmd = f"{letter}{n}"
    for attempt in range(1, attempts + 1):
        try:
            m = dongle.command(cmd, TX_OR_ERR, timeout)
        except OSError as e:
            log.error("send %s failed: %s", cmd, e)
            return False
        if m is None:
            log.warning(
                "%s: no [tx] reply within %.0f s (attempt %d)", cmd, timeout, attempt
            )
            continue
        if UNKNOWN_LINE.match(m[0]):
            log.error("%s: dongle refused the command: %s", cmd, m[0])
            return False
        res = parse_tx_line(m[0])
        if tx_ok(res):
            log.info("%s sent: %s", cmd, m[0])
            return True
        log.warning("%s: transmit incomplete: %s (attempt %d)", cmd, m[0], attempt)
    return False


# ---------------------------------------------------------------------------
# Bridge (MQTT + worker)
# ---------------------------------------------------------------------------


@dataclass
class Topics:
    base: str
    discovery: str
    command: str = field(init=False)
    state: str = field(init=False)
    mode_command: str = field(init=False)
    mode_state: str = field(init=False)
    availability: str = field(init=False)

    def __post_init__(self):
        self.command = f"{self.base}/temperature/set"
        self.state = f"{self.base}/temperature/state"
        self.mode_command = f"{self.base}/mode/set"
        self.mode_state = f"{self.base}/mode/state"
        self.availability = f"{self.base}/availability"


class Bridge:
    def __init__(self, args):
        self.args = args
        self.topics = Topics(
            base=f"chilipad/{args.unique_id}",
            discovery=f"homeassistant/climate/{args.unique_id}/config",
        )
        self.decoder = PressDecoder()
        self.assembler = BurstAssembler(self._on_burst)
        self.jobs: queue.Queue = queue.Queue()
        self.state: int | None = None
        self.mode: str | None = None  # "auto" or "off"
        self.state_lock = threading.Lock()
        self.stop_evt = threading.Event()
        self.mqtt = None
        self.mqtt_up = threading.Event()
        self.dongle = Dongle(
            args.port,
            self._on_line,
            on_connect=lambda: self.jobs.put(("setup",)),
            on_disconnect=self._publish_availability,
        )

    # -- dongle side ---------------------------------------------------------

    def _on_line(self, line: str) -> None:
        if READY_LINE.match(line):
            log.info("dongle rebooted; reconfiguring")
            self.jobs.put(("setup",))
            return
        if not self.args.sniff:
            return
        w = parse_p_line(line)
        if w:
            self.assembler.feed(w)

    def _on_burst(self, wins: list[Window]) -> None:
        d = self.decoder.decode(wins)
        if d.setpoint is None:
            log.debug("burst of %d windows not decoded: %s", len(wins), d.reason)
            return
        if not HW_MIN <= d.setpoint <= HW_MAX:
            log.info(
                "remote sent code %d degF (outside %d..%d); ignored",
                d.setpoint,
                HW_MIN,
                HW_MAX,
            )
            return
        mode = CMDS[d.cmd]
        log.info(
            "remote press: %s %d degF (%d/%d bit errors, margin %d, %s)",
            "OFF" if mode == "off" else "set",
            d.setpoint,
            d.errors,
            d.nbits,
            d.margin,
            d.form,
        )
        self._set_state(d.setpoint, mode)

    def _setup_dongle(self) -> None:
        if not self.dongle.connected.wait(5):
            return
        time.sleep(0.3)
        try:
            if self.args.sniff:
                self.dongle.set_capture(False)
                for cmd in RX_CONFIG:
                    self.dongle.command(cmd, ANY_REPLY, 1.0)
                if not self.dongle.set_capture(True):
                    log.error("could not start capture")
                else:
                    log.info("capture on (%s)", " ".join(RX_CONFIG))
            else:
                self.dongle.query_capture()
        except OSError as e:
            log.warning("dongle setup failed: %s", e)
        self.assembler.reset()
        self._publish_availability()

    def _transmit(self, letter: str, n: int) -> None:
        """Send S<n> (mode auto, setpoint n) or O<n> (mode off)."""
        mode = "off" if letter == "O" else "auto"
        if not self.dongle.connected.is_set():
            log.error("cannot send %s%d: dongle not connected", letter, n)
            self._publish_state()
            return
        ok = False
        try:
            if self.args.sniff and not self.dongle.set_capture(False):
                log.warning("could not stop capture before transmit")
            self.assembler.flush(force=True)
            ok = send_press(self.dongle, letter, n)
        except OSError as e:
            log.error("transmit failed: %s", e)
        finally:
            if self.args.sniff and self.dongle.connected.is_set():
                self.assembler.reset()
                try:
                    if not self.dongle.set_capture(True):
                        log.error("could not restart capture after transmit")
                except OSError as e:
                    log.warning("capture restart failed: %s", e)
        if ok:
            self._set_state(n, mode)
        else:
            log.error("%s%d not sent after retry", letter, n)
            self._publish_state()  # let Home Assistant fall back to the last value

    def _handle(self, job: tuple) -> None:
        with self.state_lock:
            sp = self.state
        if job[0] == "set":
            # A temperature command always sends S, so it also powers the Cube on.
            self._transmit("S", job[1])
        elif job[0] == "mode":
            if sp is None:
                # Nothing sent, sniffed or restored yet. Home Assistant hides
                # the temperature control while the target is unknown, so a
                # mode change must work without one.
                sp = self.args.default_setpoint
                log.info("mode %s: no setpoint known yet; using %d degF", job[1], sp)
            self._transmit("O" if job[1] == "off" else "S", sp)
        elif job[0] == "seed":
            # Runs a few seconds after the MQTT connect, once the retained
            # state (if any) has arrived. Without a known setpoint the Home
            # Assistant dial shows "Unknown" and offers no way to set one.
            with self.state_lock:
                if self.state is not None:
                    return
                self.state = self.args.default_setpoint
                self.mode = self.mode or "off"
                log.info(
                    "no state on the broker; showing %s %d degF until a press is "
                    "sent or sniffed",
                    self.mode,
                    self.state,
                )
            self._publish_state()

    def _worker(self) -> None:
        while not self.stop_evt.is_set():
            try:
                job = self.jobs.get(timeout=0.1)
            except queue.Empty:
                self.assembler.flush()
                continue
            if job[0] in ("set", "mode"):
                # Coalesce a run of slider moves into the latest value; keep order
                # relative to mode changes.
                batch = [job]
                while True:
                    try:
                        nxt = self.jobs.get_nowait()
                    except queue.Empty:
                        break
                    if nxt[0] not in ("set", "mode"):
                        self.jobs.put(nxt)
                        break
                    if nxt[0] == "set" and batch[-1][0] == "set":
                        batch[-1] = nxt
                    else:
                        batch.append(nxt)
                for j in batch:
                    self._handle(j)
            elif job[0] == "setup":
                self._setup_dongle()
            elif job[0] == "seed":
                self._handle(job)
            elif job[0] == "stop":
                return

    # -- MQTT side -----------------------------------------------------------

    def _set_state(self, n: int, mode: str) -> None:
        with self.state_lock:
            self.state = n
            self.mode = mode
        self._publish_state()

    def _publish_state(self) -> None:
        with self.state_lock:
            n, mode = self.state, self.mode
        if self.mqtt is None:
            return
        if n is not None:
            self.mqtt.publish(self.topics.state, str(n), qos=1, retain=True)
        if mode is not None:
            self.mqtt.publish(self.topics.mode_state, mode, qos=1, retain=True)

    def _publish_availability(self) -> None:
        if self.mqtt is None or not self.mqtt_up.is_set():
            return
        online = self.dongle.connected.is_set()
        self.mqtt.publish(
            self.topics.availability,
            "online" if online else "offline",
            qos=1,
            retain=True,
        )

    def _discovery_payload(self) -> dict:
        a, t = self.args, self.topics
        return {
            "name": None,  # entity takes the device name
            "unique_id": a.unique_id,
            "object_id": a.unique_id,
            "device": {
                "identifiers": [a.unique_id],
                "name": a.name,
                "model": "ChiliPad Cube via chilipad-sniffer RF bridge",
                "manufacturer": "chilipad-rf (unofficial)",
            },
            "availability_topic": t.availability,
            "payload_available": "online",
            "payload_not_available": "offline",
            "modes": ["off", "auto"],
            "mode_command_topic": t.mode_command,
            "mode_state_topic": t.mode_state,
            "temperature_command_topic": t.command,
            "temperature_state_topic": t.state,
            "min_temp": a.min,
            "max_temp": a.max,
            "temp_step": 1,
            "precision": 1.0,
            "temperature_unit": a.units,
            "optimistic": False,
            "qos": 1,
        }

    def _publish_discovery(self) -> None:
        self.mqtt.publish(
            self.topics.discovery,
            json.dumps(self._discovery_payload()),
            qos=1,
            retain=True,
        )

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        if reason_code.is_failure:
            log.error("MQTT connect failed: %s", reason_code)
            return
        log.info("MQTT connected to %s:%d", self.args.mqtt_host, self.args.mqtt_port)
        self.mqtt_up.set()
        client.subscribe(
            [
                (self.topics.command, 1),
                (self.topics.mode_command, 1),
                (self.topics.state, 1),
                (self.topics.mode_state, 1),
                ("homeassistant/status", 1),
            ]
        )
        self._publish_discovery()
        self._publish_availability()
        self._publish_state()
        threading.Timer(3.0, self.jobs.put, args=[("seed",)]).start()

    def _on_disconnect(self, client, userdata, flags, reason_code, properties=None):
        self.mqtt_up.clear()
        log.warning("MQTT disconnected: %s", reason_code)

    def _on_message(self, client, userdata, msg):
        payload = msg.payload.decode("utf-8", "replace")
        if msg.topic == self.topics.command:
            n = parse_setpoint(payload, self.args.min, self.args.max)
            if n is None:
                log.warning("ignored invalid setpoint %r", payload)
                return
            log.info("Home Assistant set %d degF", n)
            self.jobs.put(("set", n))
        elif msg.topic == self.topics.mode_command:
            mode = payload.strip().lower()
            if mode not in ("off", "auto"):
                log.warning("ignored unsupported mode %r", payload)
                return
            log.info("Home Assistant set mode %s", mode)
            self.jobs.put(("mode", mode))
        elif msg.topic == self.topics.state:
            # Our own retained state: restores the last value after a restart.
            with self.state_lock:
                if self.state is None:
                    try:
                        self.state = int(float(payload))
                        log.info("restored last state %d degF from broker", self.state)
                    except ValueError:
                        pass
        elif msg.topic == self.topics.mode_state:
            with self.state_lock:
                if self.mode is None and payload in ("off", "auto"):
                    self.mode = payload
                    log.info("restored last mode %s from broker", payload)
        elif msg.topic == "homeassistant/status" and payload == "online":
            log.info("Home Assistant restarted; resending discovery")
            self._publish_discovery()
            self._publish_availability()
            self._publish_state()

    def run(self) -> int:
        import paho.mqtt.client as mqtt

        a = self.args
        client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2, client_id=f"chilipad-bridge-{a.unique_id}"
        )
        if a.mqtt_user:
            client.username_pw_set(a.mqtt_user, a.mqtt_password or None)
        client.will_set(self.topics.availability, "offline", qos=1, retain=True)
        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.on_message = self._on_message
        client.reconnect_delay_set(min_delay=1, max_delay=60)
        self.mqtt = client

        worker = threading.Thread(target=self._worker, name="worker", daemon=True)
        worker.start()
        self.dongle.start()

        def _stop(*_):
            self.stop_evt.set()

        signal.signal(signal.SIGTERM, _stop)
        signal.signal(signal.SIGINT, _stop)

        client.connect_async(a.mqtt_host, a.mqtt_port, keepalive=60)
        client.loop_start()
        try:
            while not self.stop_evt.wait(1.0):
                pass
        finally:
            log.info("stopping")
            if self.mqtt_up.is_set():
                client.publish(
                    self.topics.availability, "offline", qos=1, retain=True
                ).wait_for_publish(2)
            client.disconnect()
            client.loop_stop()
            self.jobs.put(("stop",))
            worker.join(timeout=5)
            self.dongle.stop()
        return 0


# ---------------------------------------------------------------------------
# --once and --selftest
# ---------------------------------------------------------------------------


def run_once(args, n: int) -> int:
    letter = "O" if args.off else "S"
    if not HW_MIN <= n <= HW_MAX:
        log.error("--once %d outside %d..%d", n, HW_MIN, HW_MAX)
        return 2
    dongle = Dongle(args.port, on_line=lambda line: None)
    dongle.start()
    try:
        if not dongle.connected.wait(10):
            log.error("dongle not found or not connected")
            return 1
        time.sleep(0.3)
        was = dongle.query_capture()
        if was:
            dongle.set_capture(False)
        ok = send_press(dongle, letter, n)
        if was:
            dongle.set_capture(True)
        print(f"{letter}{n}: {'ok' if ok else 'FAILED'}")
        return 0 if ok else 1
    except OSError as e:
        log.error("%s", e)
        return 1
    finally:
        dongle.stop()


def selftest(fixture_root: Path) -> int:
    fails = 0

    def check(cond: bool, what: str) -> None:
        nonlocal fails
        print(f"  [{'ok' if cond else 'FAIL'}] {what}")
        fails += not cond

    print("encoder")
    check(
        packet(75).hex().upper() == "07A5444B4040000FB6F5",
        "75 F packet + CRC-16/KERMIT",
    )
    check(packet(57)[-2:] == bytes([0x50, 0x22]), "57 F CRC 50 22")
    check(
        all(
            packet(sp, cmd)[7] == (cmd + sp - 0x80) & 0xFF
            for cmd in CMDS
            for sp in range(40, 101)
        ),
        "checksum byte = captured (cmd + SP - 0x80) for ID 40 40",
    )
    burst75 = fixture_root / "docs/analysis/bursts/75F.hex"
    if burst75.exists():
        hx = burst75.read_text().strip()
        bits = h2b(hx)
        check(encode(75)[:2320] == bits[:2320], f"encode(75) == {burst75.name}")
    else:
        print(f"  [skip] {burst75} not found")

    print("line parsing")
    w = parse_p_line("P 3991348315 dt=2959261 ch=50 rssi=-44 RAW D019F2E6 | REV 0B98")
    check(w is not None and w.dt == 2959261 and w.raw == "D019F2E6", "P line")
    check(tx_ok(parse_tx_line("[tx] sent=1 segs=2 timeouts=0")), "[tx] ok")
    check(not tx_ok(parse_tx_line("[tx] sent=0 segs=1 timeouts=1")), "[tx] timeout")
    check(parse_setpoint("72.0", 55, 93) == 72, "setpoint '72.0'")
    check(parse_setpoint("100", 55, 93) == 93, "setpoint clamp high")
    check(parse_setpoint("abc", 55, 93) is None, "setpoint invalid")
    check(
        is_esp_port("/dev/ttyUSB0") and not is_esp_port("/dev/ttyACM1"),
        "ESP port filter",
    )

    dec = PressDecoder()
    fixtures = [
        (
            "docs/captures/20261104-163310/nrf.log",
            [75, 76, 77, 76, 75, 74],
        ),  # l255, 1 window
        (
            "docs/captures/20261103-202852/nrf.log",  # l64, 3 windows per press
            [74, 75, 76, 77, 78, 79, 80, 79, 78, 77, 76, 75, 74, 73, 72],
        ),
    ]
    for rel, expected in fixtures:
        path = fixture_root / rel
        print(f"decoder: {rel}")
        if not path.exists():
            check(rel.startswith("docs/captures/20261103"), f"{path} not found")
            continue
        got: list[int | None] = []

        def on_burst(wins, got=got):
            d = dec.decode(wins)
            got.append(d.setpoint if d.cmd == CMD_SET else None)
            print(
                f"    burst windows={len(wins)} -> {d.setpoint} "
                f"errors={d.errors}/{d.nbits} margin={d.margin} {d.form} {d.reason}"
            )

        asm = BurstAssembler(on_burst)
        for line in path.read_text().splitlines():
            if w := parse_p_line(line):
                asm.feed(w, now=0.0)
        asm.flush(force=True)
        check(got == expected, f"sequence {got} == {expected}")

    print("decoder: synthetic l64 windows (offsets 0/768/1540 bits as captured)")

    def windows(bits: str) -> list[Window]:
        out, prev = [], 0
        for o in (0, 768, 1540):
            chunk = bits[o + 32 : o + 32 + 512]
            out.append(Window(dt=o - prev, raw=f"{int(chunk, 2):0128X}"))
            prev = o
        return out

    for sp, cmd in (
        (72, CMD_OFF),
        (72, CMD_SET),
        (55, CMD_OFF),
        (93, CMD_OFF),
        (64, CMD_SET),
    ):
        d = dec.decode(windows(encode(sp, cmd)))
        check(
            (d.setpoint, d.cmd) == (sp, cmd),
            f"{CMDS[cmd]} {sp} F -> {d.setpoint} {CMDS.get(d.cmd)} (margin {d.margin})",
        )
    check(
        packet(72, CMD_OFF)[2] == 0x50 and packet(72, CMD_OFF)[7] == 72 - 48,
        "off packet: cmd 0x50, last byte SP-48",
    )

    print("decoder rejects noise")
    d = dec.decode([Window(dt=0, raw="0123456789ABCDEF" * 8)])
    check(d.setpoint is None, f"random window rejected ({d.reason})")
    d = dec.decode([Window(dt=0, raw="D019F2E6")])
    check(d.setpoint is None, f"short window rejected ({d.reason})")

    print("selftest", "PASSED" if not fails else f"FAILED ({fails})")
    return 0 if not fails else 1


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def env(name: str, default=None):
    return os.environ.get(f"CHILIPAD_{name}", default)


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description=__doc__.split("\n\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--port",
        default=env("PORT"),
        help="dongle serial port (default: auto-detect VID 0x2fe3 / "
        "'chilipad-sniffer'; /dev/ttyUSB* is never opened)",
    )
    p.add_argument("--mqtt-host", default=env("MQTT_HOST"))
    p.add_argument("--mqtt-port", type=int, default=int(env("MQTT_PORT", "1883")))
    p.add_argument("--mqtt-user", default=env("MQTT_USER"))
    p.add_argument(
        "--mqtt-password",
        default=env("MQTT_PASSWORD"),
        help="MQTT password (prefer env CHILIPAD_MQTT_PASSWORD)",
    )
    p.add_argument("--name", default=env("NAME", "ChiliPad Cube"))
    p.add_argument("--unique-id", default=env("UNIQUE_ID", "chilipad_cube"))
    p.add_argument("--units", choices=["F"], default="F")
    p.add_argument("--min", type=int, default=HW_MIN)
    p.add_argument("--max", type=int, default=HW_MAX)
    p.add_argument(
        "--default-setpoint",
        type=int,
        metavar="DEGF",
        default=int(env("DEFAULT_SETPOINT", "72")),
        help="setpoint shown and used for mode changes while none is known "
        "(default: 72)",
    )
    p.add_argument(
        "--no-sniff",
        dest="sniff",
        action="store_false",
        help="do not capture remote presses",
    )
    p.add_argument(
        "--device-id",
        default=env("DEVICE_ID", DEVICE_ID.hex()),
        metavar="HEX",
        help="payload bytes 4-5 of the remote whose presses are decoded "
        "(default: %(default)s)",
    )
    p.add_argument(
        "--log-level",
        default=env("LOG_LEVEL", "INFO"),
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    p.add_argument(
        "--once", type=int, metavar="DEGF", help="send one setpoint and exit (no MQTT)"
    )
    p.add_argument(
        "--off",
        action="store_true",
        help="with --once: send the power-off press O<degF> instead of S<degF>",
    )
    p.add_argument(
        "--selftest",
        action="store_true",
        help="test the decoder on capture fixtures (no serial, no MQTT)",
    )
    p.add_argument(
        "--fixtures",
        type=Path,
        default=Path(__file__).resolve().parent.parent,
        help="repo root holding docs/captures/ and docs/analysis/ (selftest)",
    )
    a = p.parse_args(argv)
    if not HW_MIN <= a.min <= a.max <= HW_MAX:
        p.error(f"--min/--max must satisfy {HW_MIN} <= min <= max <= {HW_MAX}")
    if not a.min <= a.default_setpoint <= a.max:
        p.error(f"--default-setpoint must be within {a.min}..{a.max}")
    if a.port and is_esp_port(a.port):
        p.error(
            f"{a.port} looks like the ESP8266 sniffer; this bridge uses the nRF52 dongle"
        )
    if not (a.selftest or a.once is not None or a.mqtt_host):
        p.error("--mqtt-host (or CHILIPAD_MQTT_HOST) is required")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", a.unique_id):
        p.error("--unique-id may contain only letters, digits, '_' and '-'")
    if not re.fullmatch(r"[0-9A-Fa-f]{4}", a.device_id):
        p.error("--device-id must be 4 hex digits, for example 4040")
    return a


def main(argv=None) -> int:
    a = parse_args(argv)
    global DEVICE_ID
    DEVICE_ID = bytes.fromhex(a.device_id)
    logging.basicConfig(
        level=a.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    if a.selftest:
        return selftest(a.fixtures)
    if a.once is not None:
        return run_once(a, a.once)
    return Bridge(a).run()


if __name__ == "__main__":
    sys.exit(main())
