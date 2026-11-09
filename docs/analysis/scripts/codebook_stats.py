"""Task 3: structure tests on the 16-block alphabet (true, 1-bit-later framing)."""
from codebook import *
from decode import stitched, true_last_bits
from rfcommon import b2h

LAST = {"S": "1", "B5": "1", "A4": "0", "F0": "1", "8A": "1", "CC": "0", "A7": "1", "C5": "0",
        "8C": "1", "81": "1", "8B": "1", "E3": "1", "93": "1", "C4": "1", "A2": "0", "A0": "1"}
TRUE = {k: BITS[k][1:] + LAST[k] for k in BITS}       # codeword as transmitted
ORDER = sorted(TRUE, key=lambda k: NIBBLE[k])


def rank(vs):
    vs, r = list(vs), 0
    for bit in reversed(range(64)):
        piv = next((v for v in vs if v >> bit & 1), None)
        if piv is None:
            continue
        vs.remove(piv)
        vs = [v ^ piv if v >> bit & 1 else v for v in vs]
        r += 1
    return r


def lin_complexity(s):
    s = [int(c) for c in s]
    n = len(s)
    C, B, L, m = [1] + [0] * n, [1] + [0] * n, 0, -1
    for N in range(n):
        d = s[N]
        for i in range(1, L + 1):
            d ^= C[i] & s[N - i]
        if d:
            T = C[:]
            for i in range(N - m, n + 1):
                if i - (N - m) < len(B):
                    C[i] ^= B[i - (N - m)]
            if L <= N // 2:
                L, m, B = N + 1 - L, N, T
    return L


def main():
    print("nibble  name  RAW-view block      true codeword (1 bit later)  weight  lin.cplx")
    for k in ORDER:
        print(f"  {NIBBLE[k]:X}     {k:>2}  {BLOCKS[k]}    {b2h(TRUE[k])}         {TRUE[k].count('1'):2d}      {lin_complexity(TRUE[k])}")
    print("\npairwise Hamming distance (true codewords), rows/cols in nibble order 0..F")
    print("     " + " ".join(f"{NIBBLE[k]:>3X}" for k in ORDER))
    ds = []
    for a in ORDER:
        row = []
        for b in ORDER:
            d = ham(TRUE[a], TRUE[b])
            row.append(d)
            if a < b:
                ds.append(d)
        print(f"  {NIBBLE[a]:X}  " + " ".join(f"{d:3d}" for d in row))
    print(f"min {min(ds)}  max {max(ds)}  mean {sum(ds)/len(ds):.1f} (random 64-bit words: mean 32)")
    rel = []
    for a in ORDER:
        for b in ORDER:
            if a >= b:
                continue
            for k in range(1, 64):
                r = TRUE[a][k:] + TRUE[a][:k]
                if ham(r, TRUE[b]) <= 6 or ham(r, TRUE[b]) >= 58:
                    rel.append(("rotation", a, b, k))
            if TRUE[a][::-1] == TRUE[b]:
                rel.append(("reversal", a, b))
    print("rotation (<=6 bit errors, any shift, incl. complement) / reversal relations:", rel or "none")
    V = [int(TRUE[k], 2) for k in ORDER]
    xs = {a ^ b for i, a in enumerate(V) for b in V[i + 1:]}
    print("distinct pairwise XORs:", len(xs), "of", 120, "(a single XOR constant/complement pairing would repeat)")
    print("GF(2) rank of codewords:", rank(V), "; rank of codewords XOR codeword0:", rank(v ^ V[0] for v in V[1:]),
          "(a linear [64,4] code would give 4)")
    print("\n16-degF recurrence: the field at 240.5 B is the low nibble of (setpoint-60),")
    print("so s and s+16 share that block, e.g.")
    for s in (65, 71):
        print(f"  {s} and {s+16}: nibble {(s-60)&15:X} -> {BLOCKS[BY_NIBBLE[(s-60)&15]]}")


if __name__ == "__main__":
    main()
