# Known attempts to control a ChiliPad without the stock app

A survey of public work, as of November 2026. The short version: this
repository is the only complete, published RF implementation. Everything else
either wraps the vendor cloud API, uses Bluetooth on the Ooler, is a mechanical
hack, or was never published.

## RF / protocol level (ChiliPad Cube)

| Work | Transport | State |
|------|-----------|-------|
| This repository (`chilipad-rf`) | 2.4 GHz, nRF52840 replaces the remote | Complete: decoded protocol, firmware, HA bridge |
| r/AskElectronics, "Hack ChiliPad?" (2019–) | 2.4 GHz, unknown | No published code; one commenter claimed working sniff/send with no repo |
| r/RTLSDR, "elusive remote signal" (2020) | 433.92 MHz via HackRF | Failed; never locked onto the signal |
| r/AskElectronics, "esp32 + harvested RF module" | 2.4 GHz, donor transceiver over SPI | Concept only; suggests reusing the remote's own radio |

The 433 MHz vs 2.4 GHz split is the recurring source of confusion. See the FCC
table in the top-level `README.md`: the 2014 remote is 433.92 MHz, the 2019
Cube is 2450 MHz.

## Cloud / REST API (Dock Pro, newer units)

These ride the sleep.me developer API rather than the radio, so they need a
token and are subject to per-account rate limits.

* [rsampayo/sleepme_thermostat](https://github.com/rsampayo/sleepme_thermostat) — Home Assistant custom integration
* [RainbowsHouse/ha-sleepme](https://github.com/RainbowsHouse/ha-sleepme)
* [dresslerc/sleepmehomeassistant](https://github.com/dresslerc/sleepmehomeassistant)
* [jcfield-boop/homebridge-SleepMeSimple](https://github.com/jcfield-boop/homebridge-SleepMeSimple) and [homebridge-dockpro](https://github.com/jcfield-boop/homebridge-dockpro)
* [zmitchell/bedctl](https://codeberg.org/zmitchell/bedctl) — Zig API client ([write-up](https://tinkering.xyz/bedctl/))
* [estebansolis/chilipad-mcp](https://github.com/estebansolis/chilipad-mcp) — local MCP server

## Bluetooth LE (Ooler)

* [PostLogical/ooler](https://github.com/PostLogical/ooler) — Home Assistant BLE integration
* [turmoni/ooler-mqtt-bridge](https://github.com/turmoni/ooler-mqtt-bridge) — BLE to MQTT

## Mechanical

* [runnedrun/chili_cookoff](https://github.com/runnedrun/chili_cookoff) — 2013 Arduino + servos pressing the remote's buttons. Not RF; included for contrast with the protocol approach.

## Method references

Not ChiliPad-specific, but the techniques this project used:

* Travis Goodspeed, [promiscuous-mode nRF24L01+ sniffing](https://travisgoodspeed.blogspot.com/2011/02/promiscuity-is-nrf24l01s-duty.html)
* Hackaday.io, [MiLight on-air protocol](https://hackaday.io/project/5888-reverse-engineering-the-milight-on-air-protocol) — same transceiver family, SPI-side decode
* dlaw.me, [RF Remote Reverse Engineering](http://dlaw.me/rf-remote-reverse-engineering/) — HackRF IQ to FSK bits
