# LuxAlgo Hedge DCA v6 – Projekt-Briefing für Live-Deployment

## Status: Backtest fertig → Ready für Live

---

## 1. WAS IST DAS?

Eine automatisierte Crypto-Trading-Strategie für **HYPE/USDT Perpetual Futures** auf **Bybit**.
Sie läuft als PineScript Strategy in TradingView und soll per **TradingView Alerts + Webhook** live auf Bybit Orders platzieren.

### Kern-Konzept
- **Hedge-Strategie:** Kann Long UND Short gleichzeitig offen haben
- **DCA (Dollar Cost Averaging):** Nachkaufen bei tieferen/höheren Preisen wenn Position gegen uns läuft
- **Reversal Zones:** LuxAlgo-Indikator erkennt Support/Resistance Zonen
- **Confirmation Signals:** LuxAlgo Bullish/Bearish Confirmation Signale als Entry-Filter
- **Smart Trail:** Trailing Stop Linie von LuxAlgo für Exits

### Timeframe
- **15 Minuten** (primär getestet und empfohlen)
- 5 Minuten funktioniert auch, aber 4x mehr Trades bei gleichem Profit

### Exchange
- **Bybit** – USDT Perpetual Futures
- **Pair:** HYPEUSDT
- **Hebel:** 10x (margin_long=10, margin_short=10)

---

## 2. BACKTEST-ERGEBNISSE

### Performance (12 Monate, 7. Feb 2025 – 7. Feb 2026)

| Metrik | Wert |
|--------|------|
| Startkapital | $2,400 |
| G&V | +$18,882 (+785%) |
| Max Drawdown | $2,501 (25.94%) |
| Trades | 1,656 |
| Win Rate | 57.37% (950/1656) |
| Profit Factor | 2.105 |
| Sharpe Ratio | 0.937 |
| Long P&L | +$6,193 (999 Trades) |
| Short P&L | +$12,644 (657 Trades) |
| Avg Gewinn | +5.89% |
| Avg Verlust | -4.34% |

### Regime-Robustheit (profitabel in ALLEN Marktphasen)

| Regime | Zeitraum | P&L | Trades |
|--------|----------|-----|--------|
| Seitwärts | Feb-Apr 2025 | +$1,267 | 379 |
| Uptrend | Mai-Sep 2025 | +$3,716 | 673 |
| Crash+Recovery | Okt-Dez 2025 | +$13,170 | 417 |
| Downtrend | Jan-Feb 2026 | +$729 | 188 |

---

## 3. ARCHITEKTUR

```
┌─────────────────┐     Alert JSON      ┌──────────────────┐     API Call     ┌─────────┐
│   TradingView    │ ──────────────────→ │  Webhook Server  │ ──────────────→ │  Bybit  │
│  (Pine Strategy) │                     │  (Railway/VPS)   │                 │  Perp   │
│                  │                     │  FastAPI/Flask    │                 │         │
│  - Signal Logik  │                     │  - Parse Alert    │                 │  HYPE   │
│  - Zone Detect   │                     │  - Bybit API Call │                 │  USDT   │
│  - Entry/Exit    │                     │  - Logging        │                 │         │
│  - Position Mgmt │                     │  - Error Handling │                 │         │
└─────────────────┘                     └──────────────────┘                 └─────────┘
```

### Warum diese Architektur?
- TradingView berechnet ALLES (Signals, Zones, Position Sizing, Entry/Exit)
- Der Webhook-Server ist DUMM – er empfängt JSON und platziert Orders
- Keine Signal-Logik im Server nötig = weniger Bugs, weniger Risiko
- Die Backtest-Logik IST die Live-Logik (gleicher PineScript Code)

---

## 4. PINE SCRIPT – SIGNAL MAPPING (KRITISCH!)

### "Der Swap" – Bewusste Signal-Vertauschung

Im TradingView Chart müssen die LuxAlgo Indicator Plots wie folgt an die Strategy Inputs gemappt werden:

| Strategy Input | LuxAlgo Plot (ACHTUNG: Vertauscht!) |
|---------------|--------------------------------------|
| Bullish Confirmation | → LuxAlgo **Bullish Confirmation** (korrekt) |
| **Bullish Confirmation+** | → LuxAlgo **Bearish Confirmation** (SWAP!) |
| **Bearish Confirmation** | → LuxAlgo **Bullish Confirmation+** (SWAP!) |
| Bearish Confirmation+ | → LuxAlgo **Bearish Confirmation+** (korrekt) |
| R1 (Inner Rot) | → LuxAlgo R1 |
| R3 (Outer Rot) | → LuxAlgo R3 |
| S1 (Inner Grün) | → LuxAlgo S1 |
| S3 (Outer Grün) | → LuxAlgo S3 |
| Smart Trail | → LuxAlgo Smart Trail |
| Exit Signal | → LuxAlgo Exit Signal |
| Any Bullish | → LuxAlgo Any Bullish |
| Any Bearish | → LuxAlgo Any Bearish |

### Warum der Swap?

Der Signal Filter steht auf "Nur Confirmation+". Durch den Swap passiert:
- **Long Entry** braucht "Bullish Conf+" → bekommt **Bearish Conf normal** = "Verkäufer am Support erschöpft" → besseres Timing
- **Short Entry** braucht "Bearish Conf+" → bekommt **Bearish Conf+** (korrekt)

Ergebnis: Mai-Jul 2025 (parabolischer Uptrend) macht die Strategie **0 Short Trades** statt 921 Verlust-Shorts. Das allein spart $4,889 und reduziert DD von 80% auf 25%.

**WICHTIG:** Dieser Swap ist kein Bug sondern eine getestete Konfiguration. Nicht "korrigieren"!

---

## 5. STRATEGY SETTINGS

```
Signal Filter:        "Nur Confirmation+"
DCA Signal Filter:    "Nur Confirmation+"
Conf+ = volle Size:   true
Entry Typ:            Limit
Exit Typ:             Limit
Equity pct pro Trade: 5.0%
Max Slots:            6
Max DCA Stufen:       3
DCA Size Decay:       0.7
Hedge Flip:           true
Max Loss pct:         3.0%
Max Drawdown pct:     15%
Cooldown Bars:        3
ATR Länge:            14
ATR Avg Lookback:     50
Hoch Vola Schwelle:   1.5
Extrem Vola Schwelle: 2.5
```

### Defense Layers (optional, alle default AUS)
```
S1: Trend Filter:     AUS (EMA 200, ATR Abstand 1.5)
S2: Trailing DD:      AUS (12% vom Peak)
S3: Circuit Breaker:  AUS (5 konsekutive Losses, 100 Bars Pause)
```

### Improvements (optional, alle default AUS)
```
Dynamic SL:           AUS (ATR-basiert statt fixer 3%)
Max Bars in Trade:    AUS (500 Bars Auto-Close)
Loss Streak Scaling:  AUS (3 Losses=75%, 5+=50% Size)
```

### Backtest Settings (in strategy() Header)
```
initial_capital:          2400
commission:               0.02%
slippage:                 2 Ticks
process_orders_on_close:  true
calc_on_every_tick:       true
margin_long:              10 (= 10x Hebel)
margin_short:             10
pyramiding:               20
```

---

## 6. ORDER-TYPEN DIE DER WEBHOOK HANDLEN MUSS

### Entry Orders
| Signal | Typ | Order | Kommentar-Format |
|--------|-----|-------|-----------------|
| Long Entry | Limit @ S1 | Long | `L C+` oder `L C` |
| Short Entry | Limit @ R1 | Short | `S C+` oder `S C` |
| Long Flip | Limit @ S1 | Long | `L Flip` |
| Short Flip | Limit @ R1 | Short | `S Flip` |
| Long DCA | Limit @ S3 | Long | `L D1`, `L D2`, `L D3` |
| Short DCA | Limit @ R3 | Short | `S D1`, `S D2`, `S D3` |

### Exit Orders
| Signal | Typ | Order | Kommentar-Format |
|--------|-----|-------|-----------------|
| Exit X (Long) | Limit @ close | Partial Close (40%) | `L X40%` |
| Exit X (Short) | Limit @ close | Partial Close (40%) | `S X40%` |
| Trail (Long) | Limit @ close | Partial Close (30-70%) | `L T30%` / `L T70%` |
| Trail (Short) | Limit @ close | Partial Close (30-70%) | `S T30%` / `S T70%` |
| Full Close (Long) | Market | Full Close | `L Full` |
| Full Close (Short) | Market | Full Close | `S Full` |
| Stop Loss (Long) | Stop @ SL price | Full Close | `L SL` |
| Stop Loss (Short) | Stop @ SL price | Full Close | `S SL` |
| Emergency (Long) | Market | Full Close | `EM L` |
| Emergency (Short) | Market | Full Close | `EM S` |

### Hedge-Besonderheit
Die Strategie kann **Long UND Short gleichzeitig** offen haben. Bybit Perps unterstützen das im **Hedge Mode** (One-Way Mode geht NICHT). Der Webhook muss:
1. Bybit Account auf **Hedge Mode** setzen
2. Bei Orders die `positionIdx` angeben: `1` für Long, `2` für Short

---

## 7. ALERT MESSAGE FORMAT

TradingView Alerts senden JSON. Das Pine Script muss so konfiguriert werden dass der `alert_message` Parameter die nötigen Infos enthält. 

### Vorschlag für Alert JSON Format:
```json
{
  "action": "open_long",
  "ticker": "HYPEUSDT",
  "price": 25.50,
  "qty": 4.918,
  "order_type": "limit",
  "comment": "L C+",
  "sl_price": 24.74,
  "timestamp": "2025-02-07T23:15:00Z"
}
```

### Mögliche Actions:
- `open_long`, `open_short` – Neue Position
- `close_long`, `close_short` – Position schließen (partial oder full)
- `dca_long`, `dca_short` – DCA Nachkauf
- `sl_long`, `sl_short` – Stop Loss
- `emergency_close` – Alles schließen

### PROBLEM: PineScript Alert Limitierung

PineScript `strategy.order()` hat **keinen** `alert_message` Parameter. Nur `strategy.entry()` und `strategy.exit()` haben das. Da wir `strategy.order()` nutzen (wegen Hedge), müssen wir die Alert-Messages anders lösen:

**Option A:** `alert()` Funktion separat aufrufen bei jedem Order-Event
**Option B:** Strategy Alert mit generischem Text + Order-Comment parsen
**Option C:** Pine Script umbauen um `strategy.entry/exit` wo möglich zu nutzen

**→ Das muss noch gelöst werden. Das ist die wichtigste offene Aufgabe.**

---

## 8. WAS GEBAUT WERDEN MUSS

### Phase 1: Pine Script Alert-fähig machen
- [ ] `alert()` Calls in Pine Script einbauen bei jedem Entry/Exit/DCA/SL Event
- [ ] JSON Format definieren das alle nötigen Infos enthält
- [ ] Testen dass Alerts korrekt feuern im TradingView

### Phase 2: Webhook Server
- [ ] FastAPI/Flask Server auf Railway
- [ ] POST Endpoint `/webhook` der JSON empfängt
- [ ] Authentifizierung (Secret Token im Header oder Body)
- [ ] Bybit API Integration (pybit Library)
- [ ] Hedge Mode Position Management
- [ ] Order Routing: Limit, Market, Stop Orders
- [ ] Partial Close Handling (40%, 30%, 70%)
- [ ] Error Handling + Retry Logic
- [ ] Logging (jede Order, jeder Error)
- [ ] Health Check Endpoint

### Phase 3: Bybit Setup
- [ ] API Key + Secret erstellen (nur Trading, kein Withdrawal)
- [ ] Hedge Mode aktivieren für HYPEUSDT
- [ ] 10x Leverage setzen
- [ ] Cross Margin oder Isolated Margin entscheiden

### Phase 4: Testing
- [ ] Bybit Testnet zuerst
- [ ] Paper Trading 1-2 Wochen
- [ ] Live mit $500-1000 Startkapital
- [ ] Vergleich Live vs Backtest Performance
- [ ] Wenn OK nach 4-6 Wochen → auf $2,400 skalieren

---

## 9. TECH STACK

```
Webhook Server:
  - Python 3.11+
  - FastAPI (async, schnell)
  - pybit (offizielle Bybit Python Library)
  - uvicorn (ASGI Server)
  
Hosting:
  - Railway.app (oder alternativ: Render, Fly.io, eigener VPS)
  - Braucht: persistent running, nicht serverless (muss Bybit Connection halten)
  
Logging:
  - Strukturiertes Logging (JSON)
  - Optional: Telegram Bot für Trade-Notifications
  
Monitoring:
  - Health Check Endpoint
  - Optional: Uptime Monitoring (UptimeRobot)
```

---

## 10. RISIKO-MANAGEMENT LIVE

### Position Sizing
- 5% der Equity pro Trade
- 10x Hebel = 50% der Equity als notionale Position
- Max 6 Slots gleichzeitig
- DCA: 70% Decay pro Stufe (erste DCA = 3.5%, zweite = 2.45%, dritte = 1.7%)

### Stop Loss
- 3% vom Entry Price
- Wird per Stop-Order auf Bybit platziert (nicht nur im Script!)
- **KRITISCH:** SL muss auch auf Exchange-Level existieren, nicht nur im Script

### Funding Rate
- HYPE Perps haben Funding alle 8h
- Durchschnitt ~0.01% pro 8h = ~0.03% pro Tag
- Bei $2,400 Position: ~$0.72/Tag
- Bereits im Backtest berücksichtigt

### Worst Case
- Max DD im Backtest: 25.94% = ~$623 bei $2,400
- Realistisch live: 30-40% DD möglich (Slippage, missed fills)
- Emergency Stop bei 15% DD vom Startkapital (einstellbar)

---

## 11. BEKANNTE LIMITIERUNGEN

### Backtest vs Live Unterschiede
1. **process_orders_on_close=true** – Orders füllen im Backtest auf derselben Bar. Live gibt es 1-2 Bars Delay. Für Limit Orders die vorplatziert werden kein Problem, aber Exits können leicht verzögert sein.
2. **Perfekte Limit Fills** – Im Backtest wird jede Limit Order gefüllt. Live werden einige Limits nicht gefüllt (Preis dreht vorher). Betrifft ~5-10% der Orders.
3. **Slippage bei Stops** – Stop Orders werden live als Market ausgeführt. Bei schnellen Moves kann der Fill 0.1-0.5% schlechter sein.
4. **LuxAlgo Repainting** – Manche LuxAlgo Signale können sich auf der aktuellen Bar noch ändern bis sie schliesst. Alerts sollten nur auf "Bar Close" feuern.

### Realistische Live-Erwartung
- Backtest: 785% Profit, 25% DD
- Realistisch: **300-500% Profit, 30-40% DD**
- Immer noch hervorragend wenn es hält

---

## 12. DATEIEN

| Datei | Beschreibung |
|-------|-------------|
| `LuxAlgo_Hedge_DCA_v6.pine` | Aktuelles Pine Script (MUSS alert-fähig gemacht werden) |
| Dieses MD | Projekt-Briefing |

---

## 13. NÄCHSTE SCHRITTE (Priorität)

1. **Pine Script: Alerts einbauen** – `alert()` bei jedem Order-Event mit JSON
2. **Webhook Server bauen** – FastAPI + pybit auf Railway
3. **Bybit Testnet testen** – Alles end-to-end verifizieren
4. **Live gehen** – Klein starten, beobachten, skalieren
