"""Paper broker: simulates resting orders against the live candle stream with
the SAME conservative rules as the backtest engine (strict trade-through
fills, TP+SL-in-same-candle counts as loss, taker fee + assumed slippage on
stops). Every event is appended to a JSONL trade log for later analysis.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path

from config import CFG
from strategy import Candle, EntrySignal

# Mainnet maxLeverage snapshot (2026-07-21) so paper enforces the same margin
# reality as live (F1): 13/18 coins cap at 10x, TAO at 5x.
MAX_LEVERAGE = {
    "BTC": 40, "ETH": 25, "SOL": 20, "XRP": 20, "TAO": 5,
    "HYPE": 10, "ZEC": 10, "kPEPE": 10, "AVAX": 10, "DOGE": 10, "SUI": 10,
    "NEAR": 10, "WLD": 10, "PUMP": 10, "LTC": 10, "BNB": 10, "ADA": 10,
    "LINK": 10,
}


@dataclass
class PendingOrder:
    coin: str
    side: str
    limit_price: float
    tp_dist: float
    sl_dist: float
    expires_t: int
    signal_t: int
    notional: float


@dataclass
class Position:
    coin: str
    side: str
    entry: float
    tp: float
    sl: float
    notional: float
    risk_usd: float
    entry_t: int
    max_hold_until: int


@dataclass
class PaperBroker:
    equity: float = CFG.equity_start_paper
    pending: list[PendingOrder] = field(default_factory=list)
    positions: dict[str, Position] = field(default_factory=dict)  # one per coin
    fills: int = 0
    wins: int = 0
    day_r: float = 0.0
    day_key: str = ""
    halted: str = ""     # non-empty = kill-switch reason
    closed_r: list = field(default_factory=list)   # (exit_ts, r) for overlay rules
    streak_pause_until: int = 0

    def __post_init__(self) -> None:
        Path(CFG.state_file).parent.mkdir(parents=True, exist_ok=True)
        self.log_path = Path(CFG.state_file).parent / "paper_trades.jsonl"

    # ---------- margin model (F1/F4: per-coin venue leverage, resting orders count) ----------
    def eff_leverage(self, coin: str) -> int:
        return min(CFG.leverage_cap, MAX_LEVERAGE.get(coin, CFG.leverage_cap))

    def margin_used(self) -> float:
        used = sum(p.notional / self.eff_leverage(p.coin) for p in self.positions.values())
        used += sum(o.notional / self.eff_leverage(o.coin) for o in self.pending)
        return used

    # ---------- DD overlay C2 (validated: year DD 68R -> 32R) ----------
    def _overlay_blocked(self, now: int) -> str:
        r24 = sum(r for ts, r in self.closed_r if now - ts <= 86400)
        if r24 <= -CFG.breaker_r24:
            return f"breaker_24h ({r24:.1f}R)"
        if now < self.streak_pause_until:
            return "streak_pause"
        return ""

    def _update_streak(self, exit_ts: int) -> None:
        last = self.closed_r[-CFG.streak_k:]
        if len(last) == CFG.streak_k and all(r <= 0 for _, r in last) \
                and exit_ts - last[0][0] <= CFG.streak_window_h * 3600:
            self.streak_pause_until = exit_ts + int(CFG.streak_pause_h * 3600)
            self._log("streak_brake", pause_until=self.streak_pause_until)

    # ---------- lifecycle ----------
    def place(self, sig: EntrySignal, notional: float) -> None:
        if self.halted:
            return
        blocked = self._overlay_blocked(sig.signal_t)
        if blocked:
            self._log("skip_overlay", coin=sig.coin, reason=blocked)
            return
        if len(self.positions) + len(self.pending) >= CFG.max_concurrent:
            self._log("skip_concurrency", coin=sig.coin)
            return
        self.pending.append(PendingOrder(
            coin=sig.coin, side=sig.side, limit_price=sig.limit_price,
            tp_dist=sig.tp_dist, sl_dist=sig.sl_dist,
            expires_t=sig.signal_t + sig.ttl_min * 60,
            signal_t=sig.signal_t, notional=notional))
        self._log("entry_placed", coin=sig.coin, side=sig.side,
                  limit=sig.limit_price, tp_dist=sig.tp_dist, atr=sig.atr)

    def on_closed_candle(self, coin: str, c: Candle) -> None:
        self._roll_day(c.t)
        # overlay tripped -> cancel resting entry limits (study caveat: skipping
        # their fills is exactly what was modeled)
        if self.pending and self._overlay_blocked(c.t):
            for o in self.pending:
                self._log("entry_cancelled_overlay", coin=o.coin)
            self.pending = []
        # 1) resolve open position on this coin
        pos = self.positions.get(coin)
        if pos is not None:
            self._resolve_position(pos, c)
        # 2) entry fills / expiries (skip if position just opened on this candle:
        #    conservative — do not double-enter the same coin intrabar)
        still = []
        for o in self.pending:
            if o.coin != coin:
                still.append(o)
                continue
            if c.t >= o.expires_t:
                self._log("entry_expired", coin=o.coin)
                continue
            if coin in self.positions:
                still.append(o)
                continue
            filled = (c.low < o.limit_price) if o.side == "long" else (c.high > o.limit_price)
            if not filled:
                still.append(o)
                continue
            e = o.limit_price
            tp = e * (1 + o.tp_dist) if o.side == "long" else e * (1 - o.tp_dist)
            sl = e * (1 - o.sl_dist) if o.side == "long" else e * (1 + o.sl_dist)
            pos = Position(coin=o.coin, side=o.side, entry=e, tp=tp, sl=sl,
                           notional=o.notional, risk_usd=o.notional * o.sl_dist,
                           entry_t=c.t, max_hold_until=c.t + CFG.max_hold_min * 60)
            self.positions[o.coin] = pos
            self._log("entry_filled", coin=o.coin, side=o.side, entry=e, tp=tp, sl=sl,
                      notional=o.notional)
            # same-candle resolution, conservative (SL first on ambiguity)
            self._resolve_position(pos, c)
        self.pending = still

    def _resolve_position(self, pos: Position, c: Candle) -> None:
        long = pos.side == "long"
        hit_tp = c.high > pos.tp if long else c.low < pos.tp
        hit_sl = c.low <= pos.sl if long else c.high >= pos.sl
        if hit_sl:  # includes ambiguous -> loss, same as backtest
            slip = CFG.assumed_sl_slippage
            px = pos.sl * (1 - slip) if long else pos.sl * (1 + slip)
            self._close(pos, px, "sl_ambiguous" if hit_tp else "sl",
                        exit_fee=CFG.taker_fee, t=c.t)
        elif hit_tp:
            self._close(pos, pos.tp, "tp", exit_fee=CFG.maker_fee, t=c.t)
        elif c.t >= pos.max_hold_until:
            px = c.close * (1 - CFG.assumed_sl_slippage / 2) if long \
                else c.close * (1 + CFG.assumed_sl_slippage / 2)
            self._close(pos, px, "time", exit_fee=CFG.taker_fee, t=c.t)

    def _close(self, pos: Position, px: float, outcome: str, exit_fee: float, t: int) -> None:
        long = pos.side == "long"
        gross = (px - pos.entry) / pos.entry if long else (pos.entry - px) / pos.entry
        net_frac = gross - CFG.maker_fee - exit_fee
        pnl = pos.notional * net_frac
        r = pnl / pos.risk_usd if pos.risk_usd else 0.0
        self.equity += pnl
        self.fills += 1
        self.day_r += r
        if r > 0:
            self.wins += 1
        self.closed_r.append((t, r))
        self.closed_r = [(ts, x) for ts, x in self.closed_r if t - ts <= 2 * 86400]
        self._update_streak(t)
        del self.positions[pos.coin]
        self._log("closed", coin=pos.coin, side=pos.side, outcome=outcome,
                  entry=pos.entry, exit=px, r=round(r, 4), pnl=round(pnl, 4),
                  equity=round(self.equity, 2), hold_min=(t - pos.entry_t) // 60)
        self._check_kill()

    # ---------- kill-switches ----------
    def _check_kill(self) -> None:
        if self.fills >= CFG.kill_min_fills:
            wr = self.wins / self.fills
            if wr < CFG.kill_wr_threshold:
                self.halted = f"WR {wr:.3f} < {CFG.kill_wr_threshold} after {self.fills} fills"
        if self.day_r <= -CFG.kill_daily_loss_r:
            self.halted = f"daily loss {self.day_r:.1f}R"
        if self.halted:
            self._log("KILL_SWITCH", reason=self.halted)

    def _roll_day(self, t: int) -> None:
        key = time.strftime("%Y-%m-%d", time.gmtime(t))
        if key != self.day_key:
            self.day_key = key
            self.day_r = 0.0
            if self.halted.startswith("daily loss"):
                self.halted = ""          # daily halt resets next day
                self._log("halt_reset")

    # ---------- io ----------
    def _log(self, event: str, **kw) -> None:
        rec = {"ts": int(time.time()), "event": event, **kw}
        with self.log_path.open("a") as f:
            f.write(json.dumps(rec) + "\n")

    def snapshot(self) -> dict:
        return {"equity": round(self.equity, 2), "fills": self.fills,
                "wins": self.wins,
                "wr": round(self.wins / self.fills, 4) if self.fills else None,
                "open_positions": {k: asdict(v) for k, v in self.positions.items()},
                "pending": len(self.pending), "halted": self.halted}
