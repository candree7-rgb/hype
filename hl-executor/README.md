# Hyperliquid Executor (Paper-First)

Executor für die validierte Strategie `hl_native_shock_freq`
(Liquidations-Kaskaden-Fade, 1:1 RR, Maker-Entries). Forschung + Validierung:
`../mexc-scalper/RESULTS.md`.

## Warum Paper zuerst

Die 12-Monats-Validierung (WR 62.3% vs Breakeven 56.1%, +0.132R/Trade,
11/12 Monate positiv) basiert auf Binance-Preispfaden mit vermessener
Übertragung auf Hyperliquid (±0.16R Unsicherheitsband). Das Band verengt nur
mit echten venue-nativen Fills. Paper-Mode:

- verbindet sich mit dem echten HL-Websocket (1m-Candles, 18 Coins)
- simuliert Orders mit **backtest-identischen konservativen Regeln**
  (Fill nur bei striktem Durchhandeln, TP+SL in derselben Candle = Verlust)
- zeichnet alle Candles auf (baut die HL-Historie, die die API nicht hergibt)
- Kill-Switches wie live: WR < 56% nach 200 Fills, −20R-Tag → Halt

## Start

```bash
pip install -r requirements.txt
python3 main.py                 # PAPER=true ist Default, keine Keys nötig
curl localhost:8000             # Status: Equity, Fills, WR, offene Positionen
```

Railway: Procfile vorhanden. Env-Vars: `PAPER_EQUITY` (1000), `RISK_PER_TRADE`
(0.02), `MAX_CONCURRENT` (6), `PORT`.

## Erfolgs-/Abbruchkriterien der Paper-Phase (vorab festgelegt)

Nach **≥300 Fills** (~3–4 Wochen erwartet):

| Metrik | Erwartung | Abbruch wenn |
|---|---|---|
| Winrate | ≥ 58% | < 56% (Breakeven) |
| Fills/Monat | 200–350 | < 100 (Transfer-Modell falsch) |
| avg_r | ≥ +0.08 | < +0.03 |

Bestanden → Phase 2: Mikro-Live ($1–2k, 2% Risiko) mit dem
`hyperliquid-python-sdk` (ALO/Post-Only-Entries, native Trigger-Stops,
reduceOnly-Exits). Live-Order-Routing wird erst dann implementiert und gegen
die Paper-Ergebnisse abgenommen — bewusst nicht vorher.

## Risiko-Fahrplan (aus der Validierung)

- 2% Risiko/Trade bis ~$20k Equity (erwarteter Max-DD ~57%, schlechtester
  Backtest-Monat −28%)
- ab $20k auf 1% schalten (Max-DD ~34%)
- Fee-Tiers verbessern sich automatisch: Tier 1 ab ~$4k, Tier 2 ab ~$15–20k
