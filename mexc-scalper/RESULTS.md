# Strategy Research Results — 2026-07-20/21

> **VENUE UPDATE (2026-07-21):** MEXC's 0% maker fee does NOT apply to API
> orders. Since the API-futures launch (Mar 31, 2026) API trading has a
> separate fee schedule — maker 0.04% / taker 0.06% since Jun 1, 2026 — which
> "takes precedence over any rates or promotional offers displayed on the
> website and app". At those fees the strategy's breakeven WR is ~63% vs our
> 58–61% achieved: **not deployable on MEXC via API.** The research below
> (strategy, floor filter, liquidity rules) is venue-agnostic and remains
> valid; candidate venues by fee model: zero-fee perp DEXs (full edge),
> Hyperliquid (thin positive), Bybit (~breakeven). This also explains why the
> edge persists: API bots cannot profitably harvest it on MEXC.

## Trade-level floor filter (verified, the single biggest improvement)

Skip any signal where 3×ATR60/close < 0.30% (i.e. where the sl_floor would
bind: the entry offset degenerates to noise-level ~0.1% while the stop stays
floored at 0.3%). Adversarially verified on 12mo Binance:

- Floor-bound trades: **−0.015R** (n=2624, WR 50.9%) — no edge
- Non-floor trades: **+0.213R** (n=2797, WR 62.7%), Welch t = 8.1
- Filtered portfolio: **+0.213R/trade, +597R/yr, 11/12 months positive** —
  more total R than unfiltered (+557R) from HALF the trades
- Near-monotone gradient in ATR deciles; holds at 0.25%/0.35% thresholds;
  works within mid-caps, not just as a BTC proxy. BTC self-filters (94% of
  its signals are floor-bound) — no hardcoded exclusion needed.
- Caveats: discovered on the same 12mo set (mechanism + t≈8 argue it's real);
  MEXC 30d pointed mildly the other way (+0.124 floor vs +0.105 non-floor,
  t=−0.31, noise) — plausibly venue-dependent (thin books). Deploy as
  monitored A/B or deep-book-conditional, not as a blind hard skip.

## Coin policy (verified)

- **Liquidity floor:** median 1-min dollar volume ≥ $27k for full size
  (~2700 USDT orders ≤10% of the median minute), reduced size down to ~$2.6k,
  exclude below (dead books produce fake backtest edges — ACE: $37/min,
  −0.37R, WR 36%). 24h volume alone is misleading; always check candle medians.
- **Dynamic trailing-performance selector:** walk-forward validated (+0.142R
  top-6, 10/10 months positive, beats trade-everything t=2.9) BUT largely
  redundant once the floor filter is in (floor-filter-only: +0.209R, +512R
  vs selector's +309R). Keep as monitoring/kill-switch layer, not primary.
- **Leverage rule:** per-coin min(50, maxLeverage); even 20x coins are safe
  (liq ~4.5% vs max SL 0.97%).
- The full 0-maker universe (808 MEXC perps at the time) is queryable in one
  call: contract.mexc.com/api/v1/contract/detail (fees change — refresh live).



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

## 12-month cross-venue validation (Binance data, MEXC cost model)

Same strategy, unchanged params, 12 months of Binance USDT-M Min1 data
(Jul 2025 – Jun 2026, same 18 coins, `download_binance.py` + `validate_binance.py`):

- **5,421 trades, WR 58.1%** vs breakeven 53.3%, avg +0.103R, PF 1.22
- **Positive in 10 of 12 months**; the two others ~flat (Sep-25 +3R, May-26 −4R),
  never a blow-up month; monthly WR range 53.5–60.5%
- 17/18 symbols positive over the year. The exception is **BTC (−72R,
  WR 48.7%)** — mechanically plausible: Binance BTC is the deepest book in
  crypto, overshoots get absorbed before they reach a 3-ATR limit. The edge
  lives on thin books. (On MEXC's 30d, BTC was positive — MEXC's BTC book is
  thinner; monitor it live rather than excluding it a priori.)
- Note: viability depends on 0% maker. With Binance's own fees (0.02% maker /
  0.05% taker) the per-trade edge of ~+0.10R would shrink by ~0.11–0.19R —
  i.e. the strategy is NOT profitable on Binance itself. That asymmetry is
  plausibly why the inefficiency persists.

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
