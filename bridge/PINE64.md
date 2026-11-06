# Running the bridge on the Pine A64+

Host: PINE A64-DB-2G rev B, Fedora Minimal aarch64 (flashed with
`arm-image-installer --target=pine64_plus`), rootless Podman with quadlets
auto-updating from `registry.example.com`. OS choice and board notes are
in the knowledge base (`personal/knowledge/systems/pine-a64-podman-host.md`).

## Deploy

1. Create a service user, add it to `dialout`, `loginctl enable-linger` it.
2. Plug in the nRF52840 dongle; find it under `/dev/serial/by-id/`
   (`usb-chilipad-rf_chilipad-sniffer_<serial>-if00`; the prefix is the USB
   manufacturer string "chilipad-rf") and put that path in
   `chilipad-bridge.container` (`AddDevice=`).
3. Copy `chilipad-bridge.container` to
   `~/.config/containers/systemd/` and the env file (from
   `chilipad-bridge.env.example`) to `~/.config/chilipad-bridge.env`
   (mode 0600; holds `CHILIPAD_MQTT_PASSWORD`).
4. `systemctl --user daemon-reload && systemctl --user start chilipad-bridge`
   and `systemctl --user enable --now podman-auto-update.timer`.

## nRF24L01+ (optional second sniffer)

The dongle is the controller; an nRF24 on the Pine can only sniff (its
32-byte payload limit cannot carry the 290-byte burst). Wire it to SPI0 on
the 40-pin Pi-2 header:

| nRF24 | Pi-2 pin | A64 pad | GPIO line |
|---|---|---|---|
| GND | 20 | | |
| VCC | 17 (3.3 V) | | |
| MOSI | 19 | PC0 | 64 |
| MISO | 21 | PC1 | 65 |
| SCK | 23 | PC2 | 66 |
| CSN | 24 | PC3 (CS0) | 67 |
| CE | 22 | PC15 | 79 |
| IRQ | 29 | PH5 | 229 |

3.3 V only, never the 5 V pins; 10 to 100 µF across VCC/GND at the module.
The mainline Pine64+ device tree leaves `spi0` off, so enable it with
a small overlay (`&spi0 { status = "okay"; spidev@0 {...} }`) or
`driver_override`; details in the knowledge-base note.
