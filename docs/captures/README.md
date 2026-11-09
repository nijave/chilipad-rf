# Capture sessions

One directory per session, named `YYYYmmdd-HHMMSS`. `meta.txt` holds the
start time, receiver configuration and a note; `events.log` holds marks.
Receivers: `nrf` = nRF52840 dongle, `esp` = ESP8266 + nRF24L01+ sniffer,
`iq` = HackRF IQ (the `.cs8` files are not committed).

| Session | Receivers | Purpose |
|---|---|---|
| 20261103-194341 | nrf+esp | Trigger-order test: esp address variant A 0-22 s, B 22-44 s; nrf default/r/o/r every 11 s |
| 20261103-194711 | nrf+esp | Trigger-order test: esp address variant B the whole run; nrf default/r/o/r every 11 s |
| 20261103-195123 | nrf+esp | Trigger-order test: esp ch50 variant B; nrf ch50 capture 0-15 s, then 25 s energy scan |
| 20261103-202203 | nrf+esp | Channel-scan comparison: idle 0-20 s, presses with the remote close 22-42 s |
| 20261103-202434 | nrf+esp | Remote close, presses throughout; nrf trigger AAAAAA then 555555, esp ch80 AAAA then 5555 (source of the off-frequency remote-header frames) |
| 20261103-202852 | nrf+esp | Sync-word trigger `9388B0CA` on both radios; UP presses 0-15 s, DOWN 17-32 s |
| 20261103-203934 | nrf+esp | Filmed setpoint sweep: slow single presses, remote display and clock in video (source of the codebook's 1748 blocks) |
| 20261103-205157 | nrf+esp | Who sends what: remote far, radios near the Cube; single presses about every 10 s including wake from idle |
| 20261104-162759 | nrf+esp | Channel-offset test: remote far; nrf on ch51, ch49, then ch50; a press every about 5 s |
| 20261104-163310 | nrf+esp | Full burst at l255: UP x3 then DOWN x3, one every about 5 s |
| 20261104-163708 | nrf+esp | Replay test: capture a real UP, replay it 20x; did the Cube change? |
| 20261104-164149 | nrf+esp | Burst tail: nrf trigger `A7E03171` l255; UP/DOWN presses |
| 20261104-172417 | nrf+esp | Wake sequence: idle first, wake press ~5 s, UP ~15 s, DOWN ~25 s; source of the family A/B analysis |
| 20261104-173550 | nrf+esp | Awake-window test: DOWN at ~3 s, then dongle sends 76F x5 at 8 s |
| 20261104-174435 | nrf+esp | Step-vs-absolute test: 80F burst once per second at 8-12 s |
| 20261104-174536 | nrf+esp | Step-vs-absolute test (repeat): 80F burst once per second at 8-12 s |
| 20261104-174754 | nrf+esp | Step-vs-absolute test (repeat): 80F burst once per second at 8-12 s |
| 20261104-175606 | nrf+esp | Carrier lead-in test: Cube display tapped on, then `k300` and 80F bursts at 8, 10, 12 s |
| 20261104-175759 | nrf+esp | One-step test: Cube at 71, dongle sends 72F at 8, 9, 10 s |
| 20261104-182034 | nrf+esp | Deviation test with `mb1` and a `j50` carrier lead-in; 72F bursts |
| 20261104-182312 | nrf+esp | Step-vs-absolute with `m1` and `j50`: 72F then 76F bursts |
| 20261104-184432 | nrf+esp | Energy vs time: dongle `e8000`, one remote press at ~4 s |
| 20261104-184855 | nrf+esp | Family A pairs: nrf trigger `632F4B` l48, esp `632F4BBA`; one press then nothing |
| 20261104-185111 | nrf+esp | Reply packets: nrf trigger `54C65E97` l24 (packet 2 RSSI), esp `632F4B`; one press |
| 20261104-194755 | nrf+esp | Clean `mb1` test: 79F bursts at 10-12 s, absolute vs step model |
| 20261104-195914 | nrf+esp | Bit-exact 2-segment test: 79F bursts at 8, 12, 16 s; no presses |
| 20261104-200059 | nrf+esp | Rate/lead-in factor test: 72F (mb1 j50), 77F (mb1 j0), 74F (m1 j50) |
| 20261104-200350 | nrf+esp | Power button: remote POWER off at ~5 s, on at ~20 s |
| 20261104-200553 | nrf+esp | Poll-pair control: remote battery out or in another room; no presses |
| 20261104-200739 | nrf+esp | Poll-pair control #2: remote battery out before start; no presses |
| 20261104-201025 | nrf+esp | `S`/`O` firmware test: S77 at 8 s, O77 at 16 s, S72 at 24 s |
| 20261107-194951-hackrf | iq | Smoke test, away from the Cube (`poll-reply.md` section 6) |
| 20261107-200343-hackrf | iq | Gain check near the Cube, no input |
| 20261107-200551-hackrf | iq | Gain check near the Cube, LNA 32, no input |
| 20261107-201142-hackrf | iq | Experiment b: off baseline, presses, OFF, unplug (ADC clipped family A packet 1; `poll-reply.md` section 7) |
| 20261107-203533-hackrf | iq | Experiment b2: laptop 2 m from the Cube, unplug at ~60 s (families A and B are not the Cube) |
