"""Markdown table: setpoint -> variable blocks, marking which cells were observed.

Variable positions (byte offsets from the first 9388B0CA):
  176.5 / 184.5  setpoint byte (degF), low nibble first
  240.5 / 248.5  setpoint-60 byte, low nibble first
  256.5 .. 280.5 CRC-16/KERMIT, low byte first, low nibble first
Cells: block's first 4 bytes in RAW view; '*' = predicted (not captured).
"""
from codebook import BLOCKS, BY_NIBBLE, NIBBLE
from decode import logical
from sweep_table import collect, combined

POS = [176.5, 184.5, 240.5, 248.5, 256.5, 264.5, 272.5, 280.5]


def main():
    data = collect()
    # at-min presses (logged as 56) carry 46 and 55; re-key them by decoded value
    fixed = {}
    for sp, lst in data.items():
        for src, k, e in lst:
            v = sp
            lo, hi = k.get(int(240.5 * 8)), k.get(int(248.5 * 8))
            if lo and hi:
                v = ((NIBBLE[hi[0]] << 4 | NIBBLE[lo[0]]) + 60) & 0xFF
                v = v if v < 200 else v - 256
            fixed.setdefault(v, []).append((src, k, e))
    comb = combined(fixed)
    print("| setpoint | set byte | set-60 | CRC (lo hi) | " + " | ".join(f"@{p}" for p in POS) + " |")
    print("|---|---|---|---|" + "---|" * len(POS))
    for sp in sorted(comb):
        known, _ = comb[sp]
        hdr, pkt = logical(sp, sp - 60)
        nibs = [pkt[3] & 15, pkt[3] >> 4, pkt[7] & 15, pkt[7] >> 4,
                pkt[8] & 15, pkt[8] >> 4, pkt[9] & 15, pkt[9] >> 4]
        cells = []
        for p, n in zip(POS, nibs):
            name = BY_NIBBLE[n]
            obs = known.get(int(p * 8))
            assert obs is None or obs[0] == name, (sp, p, obs, name)
            cells.append(f"`{BLOCKS[name][:8]}`" + ("" if obs else "*"))
        print(f"| {sp} | 0x{pkt[3]:02X} | 0x{pkt[7]:02X} | {pkt[8]:02X} {pkt[9]:02X} | " + " | ".join(cells) + " |")


if __name__ == "__main__":
    main()
