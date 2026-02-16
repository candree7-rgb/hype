# Signal DCA Bot v2 - Projekt-Kontext

> **Lies diese Datei komplett bevor du irgendetwas machst.**

## Was ist das?

Ein **Python-basierter DCA-Trading-Bot** der Telegram VIP Club Signale empfängt und automatisch auf Bybit (USDT Perpetual Futures) handelt. Diverse volatile Altcoins, nicht nur HYPE (der Repo-Name ist irreführend).

**Architektur:** Telegram Signal → Parser → Batch Buffer → Bybit Orders + Exchange-Side Exits

## Aktiver Code

Der gesamte aktive Code liegt in **`signal-dca-bot/`**:

| Datei | Funktion |
|-------|----------|
| `main.py` (~2000 Zeilen) | FastAPI Server, Signal-Verarbeitung, Price Monitor (2s), Zone Refresh (15min), Neo Cloud Switch, Batch Buffer |
| `config.py` | `BotConfig` dataclass mit allen Strategy-Settings |
| `trade_manager.py` | `Trade`/`DCALevel` Klassen, TradeStatus Enum, Serialisierung |
| `bybit_engine.py` | Bybit API Wrapper (pybit), Orders, Positions, Hedge Mode |
| `database.py` | PostgreSQL: Trades, Zones, Equity, Neo Cloud, Active Trades |
| `zone_data.py` | `CoinZones`, `ZoneDataManager`, Swing H/L Berechnung, DCA Zone-Snapping |
| `telegram_listener.py` | Telethon: Hört auf VIP Club Telegram Channel |
| `telegram_parser.py` | Parst Signal-Text zu `Signal` Objekt (Symbol, Side, Entry, 4 TPs, SL) |
| `database/schema.sql` | 5 Tabellen: coin_zones, trades, daily_equity, neo_cloud_trends, active_trades |

### Dashboard (`dashboard/`)
Next.js 14 + React 18 Dashboard mit Recharts. Zeigt Equity-Kurve, Trades-Tabelle, Stats, DCA/TP-Verteilung.

## Deployment

- **Railway.app** mit PostgreSQL Add-on
- **Port:** 8000 (FastAPI + uvicorn)
- **Testnet:** Default `true` (Bybit Testnet)
- `.env.example` in `signal-dca-bot/` hat alle nötigen Env-Vars

---

# Komplette Trading-Strategie

## Entry

1. **Signal kommt** via Telegram VIP Club (Side, Entry, 4 TPs, SL)
2. **Batch Buffer:** 5 Sekunden sammeln, nach Signal-Leverage sortieren, Top N nehmen
3. **Neo Cloud Filter:** Signal wird nur ausgeführt wenn Neo Cloud Trend passt (Long = "up", Short = "down")
4. **E1 Order:** Limit @ Signal-Preis (oder Market), 1/3 des Trade-Budgets
5. **DCA1 Limit:** Gleichzeitig platziert bei Entry-5% (zone-snapped zu S1/R1 wenn >3% Abstand)

## Position Sizing

- **5% Equity pro Trade**, 20x Leverage
- **DCA Multipliers:** [1, 2] → E1 = 1/3, DCA1 = 2/3 des Budgets
- **Max 6 gleichzeitige Trades**

## Exit-Strategie: E1-Only (kein DCA gefüllt)

### Multi-TP (Signal-Targets):
| TP | Close % | SL-Aktion |
|----|---------|-----------|
| TP1 | 50% | SL → BE + 0.1% Buffer, DCA-Orders canceln |
| TP2 | 10% | **Scale-In** (1/3 dazu, wenn kein DCA) + SL = exakt neuer Avg |
| TP3 | 20% (weil doppelte Qty) | SL → TP2-Preis (Profit Lock) |
| TP4 | 10% | Trail 1% Callback |
| Trail | 10% Rest | 1% Callback |

### 2/3 Pyramiding (Scale-In bei TP2):
- Wenn TP2 gefüllt → zusätzliche 1/3 Position (gleiche Größe wie E1) als **Limit Order** am TP2-Preis
- **Nur wenn DCA NICHT gefüllt** (DCA nutzt bereits das 2/3 Budget)
- 8/10 Trades die TP2 erreichen, erreichen auch TP3 → doppelte Exposure bei minimalem Risiko
- Nach Scale-In: TP3/TP4 werden neu berechnet (neue Qtys für größere Position)
- **SL nach Scale-In = exakt neuer Avg** (gewichteter Durchschnitt aus E1 + Scale-In) → Zero Risk

### SL-Ladder (Strategie C):
```
Start:       Safety SL @ Entry - 10% (gibt DCA Raum)
Nach TP1:    SL → BE + 0.1% Buffer (Fees abgedeckt)
Nach TP2:    Scale-In + SL = exakt Avg (zero risk)
Nach TP3:    SL → TP2 Preis (Profit Lock)
Nach TP4:    Trailing 1% Callback
```

## Exit-Strategie: DCA gefüllt (Preis dippte vor TP1)

Wenn DCA1 füllt (Preis -5% vom Entry):
1. **Signal TPs canceln** → Neue TPs vom Avg-Preis:
   - DCA TP1 = +0.5% (close 50%)
   - DCA TP2 = +1.25% (close 20%)
   - Trail 30% Rest @ 1% Callback
2. **Hard SL** @ DCA-Fill + 3% (Safety Net)
3. **Quick-Trail:** Wenn Preis +0.5% steigt → SL tightened zu Avg + 0.5% (~1.1% Equity-Risiko statt ~4.7%)
4. **DCA TP1 füllt** → SL zu exakt Avg (kein Buffer)

## Neo Cloud Trend Switch

- **Was:** LuxAlgo Neo Cloud Indikator erkennt Trend-Wechsel (Lead/Lag Crossover)
- **Quelle:** Via `/zones/push` Endpoint (kombiniert Zones + Neo Cloud Daten) oder `/signal/trend-switch`
- **Logik:** Server-side Detection: `neo_lead > neo_lag` = "up", sonst "down"
- **Aktion bei Switch:** Alle gegenläufigen Positionen werden geschlossen
  - Switch zu "up" → alle SHORTs schließen
  - Switch zu "down" → alle LONGs schließen
- **Filter:** Neue Signals werden gegen gespeicherte Neo Cloud Direction gefiltert
- **DB:** `neo_cloud_trends` Tabelle speichert Direction pro Symbol

## Reversal Zones (LuxAlgo)

- **S1/S2/S3:** Support-Zonen (S1 = nächste, S3 = tiefste)
- **R1/R2/R3:** Resistance-Zonen (R1 = nächste, R3 = höchste)
- **DCA Zone-Snapping:** DCA-Level werden zu S1 (Long) / R1 (Short) gesnapped wenn >3% vom Entry
- **Auto-Fallback:** Wenn keine LuxAlgo-Zones → Swing H/L aus Bybit-Candles berechnet
- **Refresh:** Alle 15 Minuten
- **Resnap:** Aktive DCA-Orders werden bei Zone-Update automatisch resnapped
- **DB:** `coin_zones` Tabelle mit S1-S3, R1-R3 pro Symbol

## TP Qty Consolidation

TPs unter `min_qty` (Bybit Minimum) werden automatisch entfernt, ihr Anteil geht zum Trail.

## PnL-Berechnung

Queried von Bybit `get_closed_pnl` API (inkl. Fees, exakte Fills). Nicht selbst berechnet.

---

# API-Endpoints

| Endpoint | Methode | Funktion |
|----------|---------|----------|
| `/` | GET | Dashboard HTML |
| `/webhook` | POST | Legacy Signal-Eingang (JSON) |
| `/signal/trend-switch` | POST | Neo Cloud Trend Switch |
| `/zones/push` | POST | Zone + Neo Cloud Daten (LuxAlgo) |
| `/zones/discover` | POST | Debug: Alle LuxAlgo Plot-Werte loggen |
| `/zones/{symbol}` | POST | Manuelle Zone-Updates |
| `/zones` | GET | Alle Zones anzeigen |
| `/close/{symbol}` | POST | Manuell Position schließen |
| `/flush` | POST | Signal-Buffer manuell flushen |
| `/status` | GET | Bot-Status + aktive Trades |
| `/trades` | GET | Geschlossene Trades |
| `/equity` | GET | Equity-Snapshots |
| `/admin/fix-pnl` | GET | PnL-Korrektur |
| `/recovery/reset` | POST | Crash Recovery Reset |

---

# Datenbank (PostgreSQL)

5 Tabellen:
- **`coin_zones`**: S1-S3, R1-R3 pro Symbol (LuxAlgo oder Swing)
- **`trades`**: Geschlossene Trades mit vollem PnL, DCA-Details, Zone-Info
- **`daily_equity`**: Tägliche Equity-Snapshots für Dashboard
- **`neo_cloud_trends`**: Neo Cloud Direction pro Symbol ("up"/"down")
- **`active_trades`**: JSONB State für Crash Recovery (überlebt Redeploy)

---

# Tech Stack

- **Python 3.11+**, FastAPI, pybit (Bybit SDK), uvicorn, Telethon
- **PostgreSQL** (Railway Add-on)
- **Next.js 14** Dashboard (React 18, Recharts, Tailwind)
- **Railway.app** Hosting

---

# Wichtige Regeln

1. **Neo Cloud Filter NICHT deaktivieren** - filtert Counter-Trend Trades
2. **DCA Limit Buffer (0.2%)** ist absichtlich - kompensiert 1-Candle Lag von Zone-Daten
3. **Safety SL @ Entry-10%** (pre-DCA) ist absichtlich weit - gibt DCA Raum zu füllen
4. **Scale-In nur wenn kein DCA** - Budget ist entweder für Pyramiding (up) oder DCA (down)
5. **Quick-Trail** reduziert DCA-Risiko von ~4.7% auf ~1.1% Equity pro Stop-Out
6. **Alle Exits sind Exchange-Side** (Bybit TP/SL/Trail Orders) - nicht polling-basiert
7. **Hedge Mode** auf Bybit (Long + Short gleichzeitig möglich)
8. **Testnet Default = true** - nie versehentlich live handeln

---

# Was als Nächstes geplant ist

## GEPLANT: 2/3 Pyramiding (ist bereits implementiert!)
Die Scale-In Logik bei TP2 ist komplett eingebaut und konfiguriert. Needs live testing.

## Offene Punkte
- Live-Deployment auf Bybit Mainnet (aktuell Testnet)
- LuxAlgo Zone-Pusher in TradingView konfigurieren (ZonePusher_v1.pine als Referenz)
- Dashboard-Verbesserungen (Live-Updates, Alert-History)
- Monitoring / Alerting bei Bot-Ausfällen

---

# Dateistruktur (nach Cleanup)

```
hype/
├── CLAUDE.md                          ← DU BIST HIER
├── signal-dca-bot/                    ← AKTIVER BOT
│   ├── main.py                        # FastAPI + alle Logik
│   ├── config.py                      # BotConfig
│   ├── trade_manager.py               # Trade/Exit Management
│   ├── bybit_engine.py                # Bybit API
│   ├── database.py                    # PostgreSQL
│   ├── zone_data.py                   # Zones + Snapping
│   ├── telegram_listener.py           # Telethon Listener
│   ├── telegram_parser.py             # Signal Parser
│   ├── database/schema.sql            # DB Schema
│   ├── requirements.txt               # Python Deps
│   ├── Procfile                       # Railway
│   ├── nixpacks.toml                  # Railway Build
│   └── .env.example                   # Env Template
├── dashboard/                         ← NEXT.JS DASHBOARD
│   ├── app/                           # Routes + API
│   ├── components/                    # React Components
│   ├── lib/                           # DB + Utils
│   └── ...
├── LuxAlgo_Hedge_DCA_v6.pine          ← REFERENZ: Pine Script (alte TradingView Strategie)
├── ZonePusher_v1.pine                 ← REFERENZ: Zone Pusher Indicator
└── LuxAlgo_Zone_DCA_v1.pine           ← REFERENZ: Zone-Only Variante
```

> **Pine Script Dateien sind NUR REFERENZ!** Die aktive Strategie läuft komplett in Python.
> Alte Dateien (Cornix_Clone, PAC_Hedge, Hybrid_Grid, webhook/, PROJECT_BRIEFING) wurden gelöscht.
