#!/usr/bin/env python3
"""DD overlay study for hl_native_shock_freq (trade-list surgery).

Tests ex-ante overlay rules on the precomputed 12mo trade list:
  1. Daily circuit-breaker (UTC-day and rolling-24h variants)
  2. Same-side concurrency cap (rolling 60 min)
  3. BTC 4h trend veto (causal, from data_binance BTC candles)
  4. Streak brake (K consecutive global losses within 6h -> 2h pause)
  5. Combos of best singles

Discipline: rules are selected on IS (Jul25-Jan26); holdout (Feb-Jun26)
reported for the table but selection criteria are IS-based.
Limitation (acknowledged): skipping an entry removes its r entirely but does
not alter other trades' fills — valid here since trades are independent per
coin and the engine has no portfolio coupling.
"""
import numpy as np
import pandas as pd

SCRATCH = "/tmp/claude-0/-home-user-hype/c47c9f5c-c8dc-549c-b753-d71584fd5d9c/scratchpad"
TRADES = f"{SCRATCH}/hlfreq_trades.parquet"
BTC = "/home/user/hype/mexc-scalper/data_binance/BTC_USDT_Min1.parquet"

HOLDOUT_START = pd.Timestamp("2026-02-01")
NOV_START = pd.Timestamp("2025-11-17")
NOV_END = pd.Timestamp("2025-12-05")  # exclusive


def load():
    t = pd.read_parquet(TRADES).copy()
    t["exit_time"] = t["entry_time"] + pd.to_timedelta(t["exit_idx"] - t["entry_idx"], unit="m")
    t["signal_time"] = t["entry_time"] - pd.to_timedelta(t["entry_idx"] - t["signal_idx"], unit="m")
    t = t.sort_values(["entry_time", "symbol"], kind="mergesort").reset_index(drop=True)

    btc = pd.read_parquet(BTC)
    btc_close = pd.Series(btc["close"].values,
                          index=btc["dt"].dt.tz_localize(None)).sort_index()
    # causal 4h trailing return at each minute
    ret4h = btc_close / btc_close.shift(240) - 1.0
    # map each trade's SIGNAL time (decision point) to BTC 4h return
    t["btc_ret4h"] = ret4h.reindex(t["signal_time"], method="ffill").values
    return t


def dd_of(r, order_time):
    if len(r) == 0:
        return 0.0
    eq = pd.Series(np.asarray(r, dtype=float)[np.argsort(np.asarray(order_time), kind="mergesort")]).cumsum()
    return float((eq.cummax() - eq).max())


def stats(t, mask=None):
    if mask is not None:
        t = t[mask]
    is_t = t[t["entry_time"] < HOLDOUT_START]
    ho_t = t[t["entry_time"] >= HOLDOUT_START]
    nov = t[(t["entry_time"] >= NOV_START) & (t["entry_time"] < NOV_END)]
    return {
        "is_n": len(is_t),
        "is_total": float(is_t["r"].sum()),
        "is_dd": dd_of(is_t["r"].values, is_t["entry_time"].values),
        "ho_n": len(ho_t),
        "ho_avg": float(ho_t["r"].mean()) if len(ho_t) else 0.0,
        "ho_total": float(ho_t["r"].sum()),
        "ho_dd": dd_of(ho_t["r"].values, ho_t["entry_time"].values),
        "nov_n": len(nov),
        "nov_total": float(nov["r"].sum()),
    }


# ---------------------------------------------------------------- simulation
def simulate(t, daily_x=None, roll24_x=None, side_cap=None, btc_t=None,
             streak_k=None):
    """Event-driven pass over entries in time order. Returns accepted mask.

    daily_x   : block new entries for rest of UTC day once realized day PnL <= -X
    roll24_x  : same but realized PnL over trailing 24h
    side_cap  : max N same-side accepted entries per rolling 60 min (global)
    btc_t     : skip shorts when BTC 4h ret > +T, longs when < -T (at signal time)
    streak_k  : after K consecutive losses (exits) within 6h -> pause 2h
    All state rules count only ACCEPTED trades (skipped trades don't exist).
    """
    n = len(t)
    accept = np.zeros(n, dtype=bool)
    et = t["entry_time"].values
    xt = t["exit_time"].values
    rr = t["r"].values
    sd = t["side"].values
    btc = t["btc_ret4h"].values

    # pending exits of accepted trades, kept sorted by exit_time via heap
    import heapq
    exits = []  # (exit_time, r)

    day = None
    day_pnl = 0.0
    breaker_until = None          # daily breaker block end
    roll_exits = []               # deque of (exit_time, r) within 24h
    roll_sum = 0.0
    side_hist = {"long": [], "short": []}   # accepted entry times, 60min window
    streak_losses = []            # exit times of current consecutive-loss run
    pause_until = None            # streak brake block end

    H1 = np.timedelta64(60, "m")
    H6 = np.timedelta64(6, "h")
    H24 = np.timedelta64(24, "h")
    H2 = np.timedelta64(2, "h")

    for i in range(n):
        now = et[i]
        # 1) realize exits up to now
        while exits and exits[0][0] <= now:
            ex_t, ex_r = heapq.heappop(exits)
            ex_day = ex_t.astype("datetime64[D]")
            if daily_x is not None:
                if ex_day != day:
                    day, day_pnl = ex_day, 0.0
                day_pnl += ex_r
                if day_pnl <= -daily_x:
                    breaker_until = (ex_day + 1).astype("datetime64[m]")
            if roll24_x is not None:
                roll_exits.append((ex_t, ex_r))
                roll_sum += ex_r
            if streak_k is not None:
                if ex_r <= 0:
                    streak_losses.append(ex_t)
                    if len(streak_losses) >= streak_k and \
                       streak_losses[-1] - streak_losses[-streak_k] <= H6:
                        pause_until = ex_t + H2
                else:
                    streak_losses = []

        # trim rolling windows
        if roll24_x is not None:
            while roll_exits and roll_exits[0][0] < now - H24:
                roll_sum -= roll_exits.pop(0)[1]
        if side_cap is not None:
            for s in side_hist:
                side_hist[s] = [x for x in side_hist[s] if x > now - H1]

        # 2) decide
        ok = True
        if daily_x is not None and breaker_until is not None and now < breaker_until:
            # breaker applies only within the day it tripped
            ok = False
        if ok and roll24_x is not None and roll_sum <= -roll24_x:
            ok = False
        if ok and streak_k is not None and pause_until is not None and now < pause_until:
            ok = False
        if ok and side_cap is not None and len(side_hist[sd[i]]) >= side_cap:
            ok = False
        if ok and btc_t is not None and not np.isnan(btc[i]):
            if (sd[i] == "short" and btc[i] > btc_t) or \
               (sd[i] == "long" and btc[i] < -btc_t):
                ok = False

        if ok:
            accept[i] = True
            heapq.heappush(exits, (xt[i], rr[i]))
            if side_cap is not None:
                side_hist[sd[i]].append(now)
    return accept


def main():
    t = load()
    base = stats(t)
    rows = [("baseline", base, len(t))]

    grids = []
    for x in (6, 8, 10, 15):
        grids.append((f"daily_breaker_X{x}", dict(daily_x=x)))
    for x in (6, 8, 10, 15):
        grids.append((f"roll24_breaker_X{x}", dict(roll24_x=x)))
    for nn in (3, 5, 8):
        grids.append((f"side_cap_N{nn}", dict(side_cap=nn)))
    for tt in (0.01, 0.015, 0.02):
        grids.append((f"btc_veto_T{tt*100:g}pct", dict(btc_t=tt)))
    for k in (6, 8, 10):
        grids.append((f"streak_brake_K{k}", dict(streak_k=k)))

    results = {}
    for name, kw in grids:
        m = simulate(t, **kw)
        s = stats(t, m)
        results[name] = (kw, s)
        rows.append((name, s, int(m.sum())))

    hdr = (f"{'rule':26s} {'n':>5s} {'IS_tot':>8s} {'IS_DD':>7s} "
           f"{'HO_avg':>8s} {'HO_tot':>8s} {'HO_DD':>7s} {'NovR':>8s} {'Nov_n':>6s}")
    print(hdr)
    for name, s, nacc in rows:
        print(f"{name:26s} {nacc:5d} {s['is_total']:8.1f} {s['is_dd']:7.1f} "
              f"{s['ho_avg']:8.4f} {s['ho_total']:8.1f} {s['ho_dd']:7.1f} "
              f"{s['nov_total']:8.1f} {s['nov_n']:6d}")

    # IS-based selection summary
    print("\nIS selection view (DD improvement %, IS total delta vs baseline):")
    for name, (kw, s) in results.items():
        ddi = 100 * (1 - s["is_dd"] / base["is_dd"])
        dtot = s["is_total"] - base["is_total"]
        print(f"{name:26s} DD_improve {ddi:6.1f}%  IS_total_delta {dtot:+7.1f}")


if __name__ == "__main__":
    main()
