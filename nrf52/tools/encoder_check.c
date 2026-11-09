/*
 * Host check for src/chili_encoder.h.  Prints "addr1 seg1 addr2 seg2" (hex)
 * per setpoint, in the format of docs/analysis/scripts/gen_burst2.py, so the two
 * can be diffed.  See tools/encoder_check.sh.
 *
 *   cc -std=c99 -Wall -Wextra -O2 -o encoder_check tools/encoder_check.c
 *   ./encoder_check [lo [hi [cmd]]]   (default 55..93, cmd 0x44)
 * cmd is hex, e.g. 50 for the power OFF press.
 */
#include <stdio.h>
#include <stdlib.h>

#include "../src/chili_encoder.h"

static void hex(const uint8_t *b, unsigned n)
{
	for (unsigned i = 0; i < n; i++) {
		printf("%02X", b[i]);
	}
}

int main(int argc, char **argv)
{
	int lo = argc > 1 ? atoi(argv[1]) : CHILI_SP_MIN;
	int hi = argc > 2 ? atoi(argv[2]) : (argc > 1 ? lo : CHILI_SP_MAX);
	uint8_t cmd = argc > 3 ? (uint8_t)strtoul(argv[3], NULL, 16) : CHILI_CMD_SET;
	struct chili_burst b;

	for (int sp = lo; sp <= hi; sp++) {
		if (!chili_build_burst_cmd(cmd, sp, &b)) {
			fprintf(stderr, "%d: encoder refused\n", sp);
			return 1;
		}
		hex(b.addr1, sizeof(b.addr1));
		putchar(' ');
		hex(b.seg1, sizeof(b.seg1));
		putchar(' ');
		hex(b.addr2, sizeof(b.addr2));
		putchar(' ');
		hex(b.seg2, sizeof(b.seg2));
		putchar('\n');
	}
	return 0;
}
