#!/usr/bin/env bash
# hackrf_capture.sh — record a HackRF IQ capture (cs8) with metadata.
#
# Creates captures/<YYYYMMDD-HHMMSS>-hackrf/, writes meta.txt (start epoch
# with ns, frequency, sample rate, LNA/VGA gains, amp flag, note), runs
# hackrf_transfer -r iq.cs8 — for --seconds or until Ctrl-C — then appends
# the end time and exit code.  RX only; iq.cs8 is interleaved signed int8
# I/Q ("cs8"), directly readable by analysis/scripts/iq_demod.py.
#
# usage: hackrf_capture.sh [-f Hz] [-s rate] [-l LNA_dB] [-g VGA_dB]
#        [-a 0|1] [-n seconds] [-m "note"]
# defaults: 2446 MHz (channel 2450 sits 4 MHz up, away from the DC spike), 20 MS/s, LNA 24 dB, VGA 20 dB, amp off
set -euo pipefail

usage() {
    sed -n '2,11p' "$0" | sed 's/^# \{0,1\}//'
}

freq=2446000000   # 4 MHz below the 2450 MHz channel, clear of the DC spike
rate=20000000
lna=24
vga=20
amp=0
seconds=""
note=""

while getopts "f:s:l:g:a:n:m:h" opt; do
    case "$opt" in
        f) freq=$OPTARG ;;
        s) rate=$OPTARG ;;
        l) lna=$OPTARG ;;
        g) vga=$OPTARG ;;
        a) amp=$OPTARG ;;
        n) seconds=$OPTARG ;;
        m) note=$OPTARG ;;
        h) usage; exit 0 ;;
        *) usage >&2; exit 2 ;;
    esac
done

# keep going after Ctrl-C so the end time still gets written
trap : INT

repo=$(cd "$(dirname "$0")/../.." && pwd)
stamp=$(date +%Y%m%d-%H%M%S)
dir="$repo/captures/${stamp}-hackrf"
mkdir -p "$dir"

start_ns=$(date +%s%N)
samples=""
if [ -n "$seconds" ]; then
    samples=$(awk -v s="$seconds" -v r="$rate" 'BEGIN { printf "%.0f", s * r }')
fi

{
    echo "start_epoch_ns $start_ns"
    echo "freq_hz $freq"
    echo "rate_sps $rate"
    echo "lna_gain_db $lna"
    echo "vga_gain_db $vga"
    echo "amp $amp"
    echo "samples ${samples:-unbounded_until_ctrl_c}"
    printf 'note %s\n' "$note"
} > "$dir/meta.txt"

cmd=(hackrf_transfer -r "$dir/iq.cs8" -f "$freq" -s "$rate"
     -a "$amp" -l "$lna" -g "$vga")
if [ -n "$samples" ]; then
    cmd+=(-n "$samples")
fi
printf 'running: %s\n' "${cmd[*]}"

rc=0
"${cmd[@]}" || rc=$?

end_ns=$(date +%s%N)
{
    echo "end_epoch_ns $end_ns"
    echo "exit_code $rc"
} >> "$dir/meta.txt"
echo "wrote $dir/iq.cs8 (exit code $rc)"
