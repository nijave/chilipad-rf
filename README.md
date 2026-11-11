# chilipad-rf — control a ChiliPad Cube without the remote

This project reverse-engineers the ChiliPad Cube's 2.4 GHz remote. An nRF52840
USB dongle running the firmware in `nrf52/` replaces the remote, and a small
MQTT bridge exposes the Cube to Home Assistant as a climate entity.

## Status

This project decoded the remote's protocol, and the dongle sends the
remote's setpoint and off presses. See
[`docs/analysis/burst-structure.md`](docs/analysis/burst-structure.md). The Cube only
receives; it never transmits, so there is no state feedback. The remote keeps
its own on/off state. See [`docs/analysis/poll-reply.md`](docs/analysis/poll-reply.md)
sections 6–7.

## Hardware

* nRF52840 USB dongle: Nordic PCA10059, or Raytac MDBT50Q-CX-40.
* A Linux host for the MQTT bridge (anything that runs Python and `uv`).
* An MQTT broker and Home Assistant.
* Optional: an ESP8266 with an nRF24L01+ as the original sniffer
  (`esp8266/`), and a HackRF for IQ analysis (`docs/analysis/hackrf-plan.md`).

## FCC filings

The Cube's radio is not a mystery: the FCC certification exhibits for both
generations are public. They place the remote's band and identify the
transceiver, which is why this project targets 2.4 GHz while older write-ups
chase 433 MHz.

| FCC ID | Grantee | Year | Band | Notes |
|--------|---------|------|------|-------|
| [`2AUPC-CP-CUBE`](https://fcc.report/FCC-ID/2AUPC-CP-CUBE) | Kryo Inc. | 2019 | 2450 MHz | Current Cube. [Internal photos of the remote](https://fcc.report/FCC-ID/2AUPC-CP-CUBE/4506776) show an off-the-shelf LT8910-class 2.4 GHz module, matching the protocol decoded here. |
| [`2ABA9-CP-CUBE-02`](https://fccid.io/2ABA9-CP-CUBE-02) | Chili Technology LLC | 2014 | 433.92 MHz | Earlier remote. Block diagram, schematic and internal photos are public. |
| [`VNT-CHILIPAD`](https://fccid.io/VNT-CHILIPAD) | T2 Products LLC | 2007 | — | Original ChiliPad remote. |

If your remote matches the 433.92 MHz filing rather than the 2.4 GHz one, this
project does not cover it.

## Quick start

1. Flash the dongle: [`nrf52/README.md`](nrf52/README.md).
2. Run the bridge: [`bridge/README.md`](bridge/README.md).

## Repository layout

| Directory | Contents |
|-----------|----------|
| `esp8266/` | ESP8266 + nRF24L01+ promiscuous sniffer sketch (`esp8266/README.md`) |
| `nrf52/` | Zephyr firmware for the nRF52840 dongle: sniffer, replay, `S<degF>` / `O<degF>` controller commands |
| `bridge/` | serial → MQTT bridge that exposes the Cube as a Home Assistant climate entity |
| `docs/analysis/` | decoder, encoder, burst generators, analysis scripts and write-ups (`hackrf-plan.md` covers HackRF IQ captures) |
| `docs/captures/` | raw capture logs |
| `tools/` | `capgui.py` capture GUI with a visible clock for filmed runs |

## Tests

```sh
cd docs/analysis/scripts && uv run --locked --project .. python -m unittest test_iq
cd bridge && uv run --locked chilipad_bridge.py --selftest
```

## Acknowledgements

Travis Goodspeed's promiscuous-mode technique for the nRF24L01+
([promiscuity is nRF24L01+'s duty](https://travisgoodspeed.blogspot.com/2011/02/promiscuity-is-nrf24l01s-duty.html)).
The code in this repository is an independent implementation of the idea.

## License

MIT. See [LICENSE](LICENSE). The ESP8266 firmware links the RF24 library
(GPL-2.0), so distributed binaries of it fall under GPL-2.0. The dongle
firmware uses USB vendor ID `0x2FE3` (Zephyr's test VID): fine for personal
use, not for products.

## Disclaimer

This is an independent hobby project. The project is not affiliated with,
endorsed by, or supported by Sleepme Inc. or Kryo Inc.; "ChiliPad" and "Cube"
are their trademarks, used only to identify compatible hardware. The software
comes "as is" without warranty (see LICENSE). The hardware transmits on
2.4 GHz and controls a heating and cooling appliance on your bed: you are
responsible for using it only where the law allows (for example FCC Part 15:
no harmful interference, no changes that increase the transmitter's output
power) and for the safety of anyone using the pad. Unattended or automated control happens at
your own risk and may void the manufacturer's warranty.
