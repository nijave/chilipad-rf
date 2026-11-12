#!/bin/bash
# Flash a DFU package to the dongle: nrf-flash.sh <package.zip>
# Sends "!boot" if the chilipad-sniffer firmware is running; otherwise waits
# (up to 10 min) for the bootloader, which the RESET button (PCA10059) or SW1
# held while plugging in (Raytac) starts.  Needs nrfutil, uv and udevadm.
set -u
pkg=${1:?usage: nrf-flash.sh <package.zip>}
[ -r "$pkg" ] || { echo "cannot read $pkg" >&2; exit 2; }
port_of() {
	for d in /dev/ttyACM*; do
		udevadm info "$d" 2>/dev/null | grep -q "$1" && echo "$d"
	done | head -1
}
app=$(port_of "ID_MODEL=chilipad-sniffer")
if [ -n "$app" ]; then
	echo "firmware on $app: sending !boot"
	uv run --with pyserial python -c '
import serial, sys
s = serial.Serial(sys.argv[1]); s.write(b"\n!boot\n"); s.flush()' "$app" 2>/dev/null
fi
for _ in $(seq 1 300); do
	bl=$(port_of "ID_MODEL_ID=521f")
	if [ -n "$bl" ]; then
		sleep 1
		echo "bootloader on $bl"
		nrfutil nrf5sdk-tools dfu usb-serial -pkg "$pkg" -p "$bl" 2>&1 | tail -1
		rc=${PIPESTATUS[0]}
		sleep 3
		echo "app on $(port_of ID_MODEL=chilipad-sniffer)"
		exit "$rc"
	fi
	sleep 2
done
echo TIMEOUT
exit 1
