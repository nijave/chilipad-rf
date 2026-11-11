#!/bin/sh
# Diff the C encoder (src/chili_encoder.h) against the Python reference for
# every setpoint 55..93: gen_burst2.py itself for the setpoint press (cmd 0x44),
# tools/gen_burst2_cmd.py (gen_burst2.py with the command byte patched) for
# 0x44 and the power OFF press (0x50).  A second build with the device ID
# 12 34 (-DCHILI_ID0/1) is compared with gen_burst2_cmd.py's idhex argument.
# Run from anywhere; needs cc, python3.
set -eu
here=$(cd "$(dirname "$0")" && pwd)
scripts="$here/../../docs/analysis/scripts"
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
cc -std=c99 -Wall -Wextra -Werror -O2 -o "$tmp/encoder_check" "$here/encoder_check.c"
cc -std=c99 -Wall -Wextra -Werror -O2 -DCHILI_ID0=0x12 -DCHILI_ID1=0x34 \
	-o "$tmp/encoder_check_id" "$here/encoder_check.c"
fail=0
bin=$tmp/encoder_check
check() { # label sp cmd python...
	label=$1 sp=$2 cmd=$3
	shift 3
	"$bin" "$sp" "$sp" "$cmd" > "$tmp/c.txt"
	"$@" > "$tmp/py.txt"
	if cmp -s "$tmp/c.txt" "$tmp/py.txt"; then
		echo "$label $sp OK"
	else
		echo "$label $sp MISMATCH"
		fail=1
	fi
}
for sp in $(seq 55 93); do
	check set "$sp" 44 python3 "$scripts/gen_burst2.py" "$sp"
	check set-cmd "$sp" 44 python3 "$here/gen_burst2_cmd.py" "$sp" 44
	check off "$sp" 50 python3 "$here/gen_burst2_cmd.py" "$sp" 50
done
bin=$tmp/encoder_check_id
for sp in 55 72 93; do
	check id-set "$sp" 44 python3 "$here/gen_burst2_cmd.py" "$sp" 44 1234
	check id-off "$sp" 50 python3 "$here/gen_burst2_cmd.py" "$sp" 50 1234
done
exit $fail
