"""Match every dongle capture triggered on 9388B0CA against the encoder for
setpoints 40..100 degF; prints which setpoint(s) explain each press bit-for-bit."""
import re
from rfcommon import *
from decode import encode
from codebook_stats import LAST

# "full": first window triggered on the first 9388B0CA (byte 0).
# "from32": first window triggered on the second 9388B0CA (byte 32): either the
# dongle missed the first sync (weak signal) or the remote sent a lead-in 32 bytes
# shorter; the captures cannot tell these apart.
ENC = {(sp, form): encode(sp, LAST)[cut:] for sp in range(40, 101)
       for form, cut in (("full", 0), ("from32", 256))}


def score(sp, grp):
    e, off, bad, n = ENC[sp], 0, 0, 0
    for i, p in enumerate(grp):
        if i:
            off += p["dt"]
        o = round(off / 4) * 4
        b = onair(p["raw"])
        lim = min(len(b), len(e) - o)
        bad += ham(b[:lim], e[o:o + lim])
        n += lim
    return bad, n


for log in sorted(CAP.glob("*/nrf.log")):
    if "trigger 9388B0CA" not in log.read_text():
        continue
    print(f"== {log.parent.name}")
    for grp in bursts(parse_nrf(log)):
        if len(grp[0]["raw"]) < 40:
            continue
        res = sorted((score(sp, grp), sp) for sp in ENC)
        best = [f"{sp}F/{form}" for (b, n), (sp, form) in res if b == res[0][0][0]]
        print(f"  t={grp[0]['t']:8.3f} windows={len(grp)} best={best} errors={res[0][0][0]}/{res[0][0][1]} bits")
