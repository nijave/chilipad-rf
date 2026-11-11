/*
 * chili_encoder.h: header-only encoder for the ChiliPad Cube setpoint burst.
 *
 * C port of encode() in docs/analysis/scripts/decode.py and of the two-segment
 * split in docs/analysis/scripts/gen_burst2.py.  Shared by src/main.c (firmware)
 * and tools/encoder_check.c (host check against the Python reference), so it
 * uses only <stdint.h>, <stdbool.h> and <string.h>.
 *
 * Every byte array here is in the RAW view: the first on-air bit is the MSB
 * of byte 0.  That is the convention of the hex strings gen_burst2.py prints
 * and of the 't', 'w' and 'v' commands, so the output can be loaded into the
 * TX segments and address slots unchanged.
 *
 * Stream (decode.py encode(), cut to FILE_BITS = 2320 bits):
 *   "1" (last preamble bit)
 *   16 codewords: nibbles of 80 03 80 03 5A 5A 80 03, low nibble first
 *   "1010" (TRAILER)
 *   20 codewords: nibbles of 07 A5 CMD SP ID0 ID1 00 X CRCL CRCH,
 *     X = (CMD + SP + ID0 + ID1) & 0xFF.  CMD 0x44 = set temperature (also
 *     power ON), CMD 0x50 = power OFF (SP = the current setpoint).
 *     ID0 ID1 = 40 40 on the one remote captured, so X = SP-60 for 0x44.
 *     Reading them as a per-remote device ID, and X as a byte sum, is a
 *     hypothesis from one remote (docs/analysis/burst-structure.md).
 *   END_TAIL "0010011" "0000000"
 * A codeword is the 64-bit block value shifted left by one bit plus the
 * block's true last bit (BITS[name][1:] + lastbit[name]).
 *
 * Split (gen_burst2.py): 211 bits of 01 alternation + stream bits 0..1052
 * = addr1 (32 bits) + seg1; stream bits 1053..1060 are the nRF preamble of
 * packet 2, 1061..1092 are addr2, 1093..2319 are seg2.  Both payloads are
 * zero padded to 154 bytes.
 */

#ifndef CHILI_ENCODER_H
#define CHILI_ENCODER_H

#include <stdbool.h>
#include <stdint.h>
#include <string.h>

#define CHILI_SP_MIN     55
#define CHILI_SP_MAX     93
#define CHILI_ADDR_LEN   4
#define CHILI_SEG_LEN    154

#define CHILI_DATA_END   (1 + 16 * 64 + 4 + 20 * 64)  /* 2309 */
#define CHILI_FILE_BITS  2320
#define CHILI_SPLIT      1053
#define CHILI_LEAD       211

#define CHILI_CMD_SET    0x44   /* setpoint press, also power ON */
#define CHILI_CMD_OFF    0x50   /* power OFF press */

/* Payload bytes 4 and 5: probably the remote's device ID.  Override with
 * -DCHILI_ID0=0x.. -DCHILI_ID1=0x.. for a remote that sends other values. */
#ifndef CHILI_ID0
#define CHILI_ID0        0x40
#endif
#ifndef CHILI_ID1
#define CHILI_ID1        0x40
#endif

struct chili_burst {
	uint8_t addr1[CHILI_ADDR_LEN];
	uint8_t seg1[CHILI_SEG_LEN];
	uint8_t addr2[CHILI_ADDR_LEN];
	uint8_t seg2[CHILI_SEG_LEN];
};

/* Block value (codebook.py BLOCKS) and true last bit (decode-output.txt
 * "true last bits"), indexed by nibble value (codebook.py NIBBLE). */
static const uint64_t chili_block[16] = {
	0x9388B0CAD019F2E6ull, /* 0 S  */
	0xC4F30304A5ECD55Cull, /* 1 C4 */
	0xA2E1F402A42EF39Aull, /* 2 A2 */
	0xA4717A73025C4516ull, /* 3 A4 */
	0xA7E03171410A772Cull, /* 4 A7 */
	0x8A27C039D2C6C844ull, /* 5 8A */
	0x81C0514D66CA15F8ull, /* 6 81 */
	0xCC18FE2A95645252ull, /* 7 CC */
	0xB52BE101838EE2C9ull, /* 8 B5 */
	0xA00C1ED3888E902Eull, /* 9 A0 */
	0xF020764CA1565672ull, /* A F0 */
	0xC53AD4112D89E197ull, /* B C5 */
	0xE33124028D105F97ull, /* C E3 */
	0x93035E1B4395C402ull, /* D 93 */
	0x8B18AA986C3302DEull, /* E 8B */
	0x8CB0A48A7E244638ull, /* F 8C */
};
static const uint8_t chili_lastbit[16] = {
	1, 1, 0, 0, 1, 1, 1, 0, 1, 1, 1, 0, 1, 1, 1, 1,
};

/* CRC-16/KERMIT: poly 0x1021 reflected (0x8408), init 0, xorout 0. */
static inline uint16_t chili_crc16_kermit(const uint8_t *d, unsigned n)
{
	uint16_t crc = 0;

	for (unsigned i = 0; i < n; i++) {
		crc ^= d[i];
		for (int b = 0; b < 8; b++) {
			crc = (crc & 1u) ? (uint16_t)((crc >> 1) ^ 0x8408u)
					 : (uint16_t)(crc >> 1);
		}
	}
	return crc;
}

struct chili_bitbuf {
	uint8_t  *buf;
	unsigned pos;
};

static inline void chili_put(struct chili_bitbuf *w, unsigned bit)
{
	if (bit) {
		w->buf[w->pos >> 3] |= (uint8_t)(0x80u >> (w->pos & 7u));
	}
	w->pos++;
}

static inline unsigned chili_get(const uint8_t *buf, unsigned pos)
{
	return (buf[pos >> 3] >> (7u - (pos & 7u))) & 1u;
}

static inline void chili_put_codeword(struct chili_bitbuf *w, unsigned nib)
{
	uint64_t cw = (chili_block[nib] << 1) | chili_lastbit[nib];

	for (int i = 63; i >= 0; i--) {
		chili_put(w, (unsigned)((cw >> i) & 1u));
	}
}

static inline void chili_put_bytes(struct chili_bitbuf *w, const uint8_t *b,
				   unsigned n)
{
	for (unsigned i = 0; i < n; i++) {
		chili_put_codeword(w, b[i] & 0xFu);   /* low nibble first */
		chili_put_codeword(w, b[i] >> 4);
	}
}

static inline void chili_put_str(struct chili_bitbuf *w, const char *s)
{
	for (; *s != '\0'; s++) {
		chili_put(w, (unsigned)(*s == '1'));
	}
}

/* decode.py encode(sp)[:FILE_BITS] into stream[] (290 bytes, MSB first),
 * with the command byte as a parameter. */
static inline void chili_encode_stream(uint8_t cmd, int sp,
				       uint8_t stream[CHILI_FILE_BITS / 8])
{
	static const uint8_t hdr[8] = {0x80, 0x03, 0x80, 0x03, 0x5A, 0x5A, 0x80, 0x03};
	uint8_t pkt[10] = {0x07, 0xA5, cmd, (uint8_t)sp, CHILI_ID0, CHILI_ID1,
			   0x00, (uint8_t)(cmd + sp + CHILI_ID0 + CHILI_ID1),
			   0, 0};
	uint16_t crc = chili_crc16_kermit(pkt, 8);
	/* The full stream is 2323 bits; the extra byte takes the 3 cut bits. */
	uint8_t tmp[CHILI_FILE_BITS / 8 + 1];
	struct chili_bitbuf w = {tmp, 0};

	pkt[8] = (uint8_t)(crc & 0xFFu);
	pkt[9] = (uint8_t)(crc >> 8);
	memset(tmp, 0, sizeof(tmp));

	chili_put(&w, 1u);                 /* last preamble bit */
	chili_put_bytes(&w, hdr, sizeof(hdr));
	chili_put_str(&w, "1010");         /* TRAILER */
	chili_put_bytes(&w, pkt, sizeof(pkt));
	chili_put_str(&w, "00100110000000"); /* END_TAIL */
	memcpy(stream, tmp, CHILI_FILE_BITS / 8);
}

/* gen_burst2.py: addr1 seg1 addr2 seg2 for command cmd at setpoint sp (degF).
 * Returns false when sp is out of range or the split point is not an nRF
 * preamble.  The split (bit 1053) lies inside the codeword of packet byte 0
 * (07), so it does not depend on cmd or sp; the check stays as a guard. */
static inline bool chili_build_burst_cmd(uint8_t cmd, int sp,
					 struct chili_burst *out)
{
	uint8_t s[CHILI_FILE_BITS / 8];
	uint8_t full1[(CHILI_LEAD + CHILI_SPLIT + 7) / 8];
	struct chili_bitbuf w = {full1, 0};
	unsigned i;

	if (sp < CHILI_SP_MIN || sp > CHILI_SP_MAX) {
		return false;
	}
	chili_encode_stream(cmd, sp, s);

	/* s[SPLIT:SPLIT+9] must be 101010101 or 010101010. */
	for (i = 1; i < 9; i++) {
		if (chili_get(s, CHILI_SPLIT + i) == chili_get(s, CHILI_SPLIT + i - 1u)) {
			return false;
		}
	}

	/* full1 = lead + s[:SPLIT]; lead bit i = 0 when (LEAD - i) is odd. */
	memset(full1, 0, sizeof(full1));
	for (i = 0; i < CHILI_LEAD; i++) {
		chili_put(&w, ((CHILI_LEAD - i) % 2u) == 1u ? 0u : 1u);
	}
	for (i = 0; i < CHILI_SPLIT; i++) {
		chili_put(&w, chili_get(s, i));
	}

	memset(out, 0, sizeof(*out));
	/* LEAD + SPLIT = 1264 = 32 + 154 * 8: addr1 + seg1 exactly. */
	memcpy(out->addr1, full1, CHILI_ADDR_LEN);
	memcpy(out->seg1, full1 + CHILI_ADDR_LEN, CHILI_SEG_LEN);

	w.buf = out->addr2;
	w.pos = 0;
	for (i = CHILI_SPLIT + 8u; i < CHILI_SPLIT + 40u; i++) {
		chili_put(&w, chili_get(s, i));
	}
	/* 2320 - 1093 = 1227 bits, zero padded to 154 bytes. */
	w.buf = out->seg2;
	w.pos = 0;
	for (i = CHILI_SPLIT + 40u; i < CHILI_FILE_BITS; i++) {
		chili_put(&w, chili_get(s, i));
	}
	return true;
}

/* Setpoint press (cmd 0x44). */
static inline bool chili_build_burst(int sp, struct chili_burst *out)
{
	return chili_build_burst_cmd(CHILI_CMD_SET, sp, out);
}

#endif /* CHILI_ENCODER_H */
