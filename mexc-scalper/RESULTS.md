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

## Venue-native validation (2026-07-21)

**Lighter (13 months of its own 1m candles, public API):** tradable universe is
tiny — post-TGE alt books are ghost towns (PEPE median 1-min volume $0, XRP
$84, ZEC $350; dead books are strongly NEGATIVE to trade, as on MEXC/ACE).
Full-size tradable: BTC ($231k/min median), ETH ($76k); reduced: SOL, HYPE.
On BTC+ETH the edge is venue-native REAL: **+0.106R, WR 62.1%, n=636 over 13
months, 8/13 months positive** (config shock_lighter_v1, stop-limit exec,
base slippage). Caveats: only ~49 trades/month, 2-coin concentration, stop
recovery rate 58% venue-native vs 91% modeled on Binance paths.

**Hyperliquid (3.5 days of own candles — API caps 1m history at ~5000):**
ATR-gate config produced n=23 (statistically void); ungated −0.02R at HL base
fees. The 12mo Binance holdout (+0.139R with gate) remains the best available
evidence; venue-native confirmation requires live/paper data collection.

**Conclusion:** Lighter = proven but small (BTC/ETH base income); Hyperliquid
= larger potential, unproven venue-native. Next step is a dual-venue paper
executor measuring real fills/slippage/frequency, then evidence-based capital
split.

## HL-native design search (2026-07-21) — new winner `hl_native_shock_freq`

Systematic search for a Hyperliquid-designed variant (55 IS variants, 4
families, IS = Jul25–Jan26 Binance 12mo @ HL base fees via engine_hl,
holdout Feb–Jun26 opened once for 2 pre-selected finalists):

- **Asymmetric TP (tp_over_sl 1.2/1.3/1.5): dead** — monotone worse; the
  snapback amplitude is bounded, wider TPs just miss (1.5 goes negative).
- **Two-rung ladders (2.0–5.0 ATR): no edge over best single rung** — the far
  rung fills too rarely to earn its half of the size.
- **5m shock detection, 1m execution: too rare on Binance paths** (best IS
  +0.113 at n=216/7mo); untestable venue-native (3.5d HL data). Shelved.
- **Vol-regime gate (24h RV top tercile): redundant with the atr_min gate** —
  raises avg_r, halves n, loses on total R.
- **Winner: frequency, not depth.** Per-trade edge is flat in offset (2.5–4.5
  ATR, cliff below 2.5) and shock threshold — so at HL fees the right design
  maximizes gated fills. `hl_native_shock_freq` = incumbent + shock_atr 3.5,
  offset_atr 2.5, cooldown 15 (gate atr_min=0.001 kept, 1:1, sl=3 ATR).
  **Holdout: n=2115, WR 62.6% (breakeven 56.3%), avg_r +0.133, +281R, PF
  1.29, 5/5 months and 18/18 symbols positive, top coin 14.7% of R.** Vs
  incumbent shock_hl_variant (+0.139, n=1233, +171R): equal per-trade edge
  (Welch t=−0.16), 1.72x trades, +64% total R. Stress (holdout): 1.5x slip
  +0.120/+254R; 2x slip +0.107/+227R; HL fee tier 1 +0.148/+313R. Lookahead
  0. Caveat: shallower limits may see worse real-world adverse selection
  than strict-trade-through modeling; incumbent avg_r ceiling (~0.14/trade)
  was NOT beaten — the gain is throughput.

## DD overlay C2 (adopted 2026-07-21) — drawdown halved, returns up

Forensics: the 68R max DD was a 17-day squeeze-rally window (Nov 17 – Dec 4,
2025) — market-wide up-shocks got faded as "cascades" but were real trend
(Nov 24: 30 of 34 trades short across coins while BTC rallied). Overlay study
(dd_study_*.py; rules designed on IS which contains the event, holdout
Feb–Jun26 evaluated once):

- **REJECTED, counter-intuitively: BTC 4h trend veto.** Makes IS DD *worse* —
  the same rallying regime also produces the strategy's snapback winners; the
  Nov failure was not ex-ante separable by market momentum.
- **ADOPTED C2:** (1) rolling-24h circuit breaker — no new entries (cancel
  resting limits) while trailing-24h realized PnL ≤ −6R; (2) streak brake —
  6 consecutive losses within 6h → pause entries 2h.
- Effect (12mo, HL tier-0): **year max DD 68.3R → ~31R (−53%)**, total
  +651R → ~+770R, WR 62.3% → 64.3%, holdout total **+17% better** (the loss-
  clustering mechanism exists year-round, so skips are net-negative trades in
  8/12 months). Nov window: −64.5R → ~−13R. Parameter plateau confirmed
  (X∈4–8, K∈5–8 all strong). Residual ~31R DD is the price of the strategy.
- Risk table with overlay (HL frequency, compounding): 1% risk → maxDD ~27%,
  worst month −13%; 2% → ~48%, worst month −26%.
- Implemented in hl-executor (breaker cancels resting limits when tripped).

## CAPACITY CEILING — this is a small-capital strategy (2026-07-22)

Measured HL shock-minute $ volumes (the minute we actually fill in) → max
equity where a 2%-risk order stays ≤10% of that volume:

| equity | @2% risk: coins / R retained | @1% risk |
|---|---|---|
| $1k | 8 / 35% | 12 / 49% |
| $5k | 6 / 28% | 6 / 28% |
| **$10k** | **4 / 12%** | 6 / 28% |
| $25k | 3 / 10% | 4 / 12% |
| $100k | 2 / 7% | 3 / 10% |

The edge lives on shocky mid-cap alts, and those exact coins have the
THINNEST HL books (max order $100–500 at 2% risk). Above ~$5–10k equity the
diversified strategy collapses to HYPE + majors — and only **HYPE (~$81k
capacity @2%)** is a robust deep vehicle; BTC/ETH barely generate signals
(BTC ~74 trades/yr). **Adding coins raises frequency at small size but adds
ZERO capacity headroom.** Levers that actually scale: (1) 1% risk ~doubles
every ceiling, (2) HYPE-concentration for deep capital, (3) **multi-venue**
(same signals on HL + Lighter + … multiplies book depth). Single-venue
diversified realistic ceiling: ~$5–10k. Data caveat: HL 1m history is only
~3.5 days, so deep-coin capacity is extrapolated (flagged in hl_capacity.py).

Universe expansion (edge-validated 12mo Binance @ HL fees, added to executor):
ENA XMR APT JUP PENGU pass (avg_r +0.10–0.16, 8–11/12 months); WIF/FET have
ghost HL books (hold); TON not on HL. Blended 23-coin avg_r +0.129 (vs +0.132).

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



## Original 30d research run (2026-07-20, MEXC data)

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
