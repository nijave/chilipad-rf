#!/usr/bin/env bash
# cap.sh <name> <seconds> <note>  -- RX only, same settings for every run
set -u
name=$1; secs=$2; note=$3
freq=2446000000; rate=20000000; lna=${LNA:-24}; vga=${VGA:-20}; amp=0
dir=~/chilipad-rf/iq-compare-20261111/$name
mkdir -p "$dir"
samples=$(( secs * rate ))
start_ns=$(date +%s%N)
printf 'start_epoch_ns %s\nfreq_hz %s\nrate_sps %s\nlna_gain_db %s\nvga_gain_db %s\namp %s\nsamples %s\nnote %s\n' \
  "$start_ns" $freq $rate $lna $vga $amp $samples "$note" > "$dir/meta.txt"
hackrf_transfer -r "$dir/iq.cs8" -f $freq -s $rate -a $amp -l $lna -g $vga -n $samples > "$dir/transfer.log" 2>&1
rc=$?
printf 'end_epoch_ns %s\nexit_code %s\n' "$(date +%s%N)" $rc >> "$dir/meta.txt"
