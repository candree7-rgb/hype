# Strategy Research Results — 2026-07-20

Multi-agent research run: 20 ideas from 5 research personas → 9 implemented
and backtested → adversarially verified → 1 confirmed winner, hardened.

Data: 18 MEXC USDT-perps, 30 days Min1. Engine: maker entries/TPs (0% fee),
stop-market SL (0.02% taker + 0.03% slippage), TP+SL in same candle = loss,
IS/OOS split 65/35 by time, automatic lookahead check.

## Winner: `shock_candle_overshoot_catch` ✅ deploy candidate

Liquidation-cascade overshoot fade. A 1-min candle with range > 4× ATR marks a
cascade; a limit rests 3 ATR beyond the shock extreme with TTL 5 min — it fills
only if forced flow extends the cascade, then snaps back. TP = SL =
max(3×ATR, 0.3%), 1:1. Cooldown 20 min per symbol.

| Metric (out-of-sample) | Value |
|---|---|
| Winrate | **60.8%** (breakeven 53.7%, margin +7.1pp) |
| Avg R / trade | **+0.152** |
| Trades (OOS, ~10 days) | 199 |
| Profit factor | 1.34 |
| Symbols positive | 14/18 |
| 2× slippage stress | avg_r +0.117, still passes all gates |
| Lookahead violations | 0 |

**Honest caveats:** OOS PnL is back-loaded (most of it from Jul 15–20; days
8–23 were ~flat over 70 trades). Winner of ~8 candidates → family-wise
selection risk. `atr_n=60` is the most fragile parameter. Real cascade
slippage may exceed the 2× model; at 3× the edge thins to ~+0.08R but stays
positive. Recommendation: deploy small with a kill-switch (stop if realized WR
after ~100 fills < breakeven).

## Rejected in adversarial review (passed numerically, failed scrutiny)

- `breadth_flush_catch` — cross-symbol breadth-gated deep fades; verifier rejected
- `pump_fade_core` — volume-climax pump fade with ladder; verifier rejected

## Honest failures (kept as negative results)

- `stale_extreme_sweep_catch` — numbers pass but one cascade day = 96% of OOS PnL
- `trend_dip_snapback` — one day (Jul 18) = 96% of OOS PnL
- `us_session_extreme_fade` — IS edge (+0.15) died OOS (−0.15): regime, not signal
- `btc_shock_overshoot_fade` — BTC-beta residual fade: ~zero net edge
- `failed_pump_second_leg` — marginal (+0.038 OOS), below pass threshold
- `funding_magnet_fade` — clean negative: settlement-window fade loses (−0.38 OOS)

## Reproduce

```bash
python3 download_data.py --days 30            # refresh data
python3 engine.py strategies/shock_candle_overshoot_catch.py
python3 engine_stress.py strategies/shock_candle_overshoot_catch.py  # 2x slippage
```
