# Strategy Research Guide — MEXC Futures Scalping @ 1:1 RR

## Goal

Find strategies with **stable WR > breakeven-WR out-of-sample** at ~1:1 RR on
MEXC USDT perpetuals, exploiting **0% maker fees** (entries and TPs are limit
orders). High leverage is irrelevant to the math — it only affects margin, not
expectancy. What matters: net expectancy in R after fees/slippage.

## Data

`data/*_Min1.parquet` — 30 days × 43,200 1-minute candles for 18 liquid perps.
Columns: `time, open, high, low, close, vol, amount, dt` (UTC).
`data/contracts.json` has per-contract fees (`makerFeeRate` is 0 everywhere,
`takerFeeRate` ≤ 0.02%).

## Strategy contract

Create `strategies/<name>.py` exposing:

```python
DEFAULT_PARAMS = {...}
def generate_signals(df, params) -> pd.DataFrame:
    # columns: idx, side ("long"/"short"), limit_price, tp_dist, sl_dist, ttl
```

Rules:
- The signal at row `i` may use **only rows ≤ i** (no lookahead — the engine
  runs an automatic truncation check and reports `lookahead_violations`).
- `limit_price` must be below `close[idx]` for longs / above for shorts
  (resting maker order). Orders expire after `ttl` candles unfilled.
- `tp_dist`/`sl_dist` are fractions of entry price. 1:1 means equal.

Run: `python3 engine.py strategies/<name>.py [--params '{...}'] [--symbols A,B]`

## Execution model (conservative by design)

- Entry limit fills only on strict trade-through. TP is a maker limit (0% fee,
  strict trade-through). SL is stop-market: taker 0.02% + 0.03% slippage.
- If TP and SL are both reachable in one candle → counted as **loss**.
- Unclosed after `max_hold` (default 240 min) → time-stop at close.

## The cost trap (learn from the smoke test)

With SL cost ≈ 0.05% (taker+slippage), a stop distance of 0.05% makes every
loss ≈ **-2R**, so 60% WR still loses money. Rule of thumb: keep
`sl_dist ≥ 0.3%` so losses stay ≤ ~1.17R and breakeven-WR ≈ 52%. Wider is
cheaper per-R but slower. Report `breakeven_wr` vs `wr` from the metrics.

## What counts as a PASS

- `out_of_sample.wr > out_of_sample.breakeven_wr` with margin (≥ 3pp)
- `out_of_sample.avg_r > 0.03` and `n ≥ 150` OOS trades (no cherry-picking
  symbols after the fact — symbol filters must be rule-based)
- No lookahead violations; positive in ≥ 60% of symbols; profit factor > 1.1 OOS
- Stability: does not depend on one lucky day/coin (check per_symbol)

## Performance tips

- Vectorize `generate_signals` with pandas/numpy (boolean masks + `np.where`),
  avoid per-row `.iloc` loops — 43k rows × 18 symbols adds up.
- While iterating on params, test on a 5-6 symbol subset (`--symbols`); run all
  18 only for the final numbers.
- Don't emit a signal every minute — space entries (e.g. cooldown after each
  signal) or your backtest drowns in overlapping trades.

## Honesty rules

- Never tune on the OOS window. Tune on in-sample, report OOS untouched.
- If you try N parameter sets, say so — 1 winner out of 40 tries is noise.
- Prefer fewer parameters. Every added filter must earn its place on IS data.
