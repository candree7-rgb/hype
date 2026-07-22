"""Aggregate SMC study JSON logs into one variant table.

Reads the line-JSON outputs of smc_sweep_study / smc_limit_study /
smc_confluence_study and prints a table with, per variant:
n, WR, avg_r (HL fees), control WR/avg_r, delta_WR, and delta of the
SL-first hit rate (P(sl or ambiguous) real minus control) — the direct
measurement of the structural-stop claim.
"""
from __future__ import annotations

import json
import sys


def sl_rate(s: dict) -> float | None:
    oc = s.get("outcomes", {})
    n = s.get("n", 0)
    if not n:
        return None
    return (oc.get("sl", 0) + oc.get("ambiguous_sl", 0)) / n


def main(paths: list[str]) -> None:
    rows = []
    for path in paths:
        for line in open(path):
            line = line.strip()
            if not line.startswith("{"):
                continue
            d = json.loads(line)
            re, ct = d.get("real", {}), d.get("control", {})
            if not re.get("n"):
                continue
            r_sl, c_sl = sl_rate(re), sl_rate(ct)
            rows.append({
                "variant": d["variant"],
                "n": re["n"], "wr": re["wr"], "avg_r": re["avg_r"],
                "ctrl_wr": ct.get("wr"), "ctrl_avg_r": ct.get("avg_r"),
                "dWR_pp": round(100 * (re["wr"] - ct["wr"]), 2) if ct.get("n") else None,
                "dSLrate_pp": round(100 * (r_sl - c_sl), 2) if c_sl is not None else None,
                "med_sl": re.get("med_sl_frac", re.get("med_sl_dist")),
            })
    hdr = (f"{'variant':>28} {'n':>7} {'wr':>6} {'avg_r':>8} "
           f"{'ctrlWR':>7} {'ctrl_r':>8} {'dWR pp':>7} {'dSL pp':>7} {'medSL':>8}")
    print(hdr)
    for r in rows:
        print(f"{r['variant']:>28} {r['n']:>7} {r['wr']:>6.3f} {r['avg_r']:>8.3f} "
              f"{r['ctrl_wr']:>7.3f} {r['ctrl_avg_r']:>8.3f} "
              f"{r['dWR_pp']:>7.2f} {str(r['dSLrate_pp']):>7} {r['med_sl']:>8}")
    if rows:
        import statistics
        d = [r["dWR_pp"] for r in rows if r["dWR_pp"] is not None]
        print(f"\nvariants: {len(rows)}  dWR pp: mean {statistics.mean(d):+.2f}  "
              f"min {min(d):+.2f}  max {max(d):+.2f}")


if __name__ == "__main__":
    main(sys.argv[1:])
