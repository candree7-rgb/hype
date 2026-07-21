"""Incremental, causal port of hl_native_shock_freq signal logic.

Backtest reference: mexc-scalper/strategies/hl_native_shock_freq.py — this
module must produce identical signals on identical candle input. Each coin
keeps a rolling window of CLOSED 1m candles; on every closed candle we test
the shock condition and may emit one entry order request.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass

from config import CFG


@dataclass
class Candle:
    t: int          # open time, epoch seconds
    open: float
    high: float
    low: float
    close: float
    vol: float


@dataclass
class EntrySignal:
    coin: str
    side: str            # "long" | "short"
    limit_price: float
    tp_dist: float       # fraction of entry price
    sl_dist: float
    ttl_min: int
    signal_t: int
    atr: float           # fractional ATR60 at signal (for logging/analysis)
    rng_atr: float


WARMUP_CANDLES = 150   # matches the backtest's warmup index


class CoinState:
    def __init__(self) -> None:
        self.candles: deque[Candle] = deque(maxlen=CFG.atr_n + 5)
        self.tr_sum: float = 0.0
        self.trs: deque[float] = deque(maxlen=CFG.atr_n)
        self.last_signal_t: int = 0
        self.seen: int = 0

    def add(self, c: Candle) -> float | None:
        """Append a closed candle; returns fractional ATR60 or None during warmup."""
        prev_close = self.candles[-1].close if self.candles else None
        self.candles.append(c)
        self.seen += 1
        if prev_close is None:
            return None
        tr = max(c.high - c.low, abs(c.high - prev_close), abs(c.low - prev_close))
        if len(self.trs) == CFG.atr_n:
            self.tr_sum -= self.trs[0]
        self.trs.append(tr)
        self.tr_sum += tr
        if len(self.trs) < CFG.atr_n:
            return None
        return (self.tr_sum / CFG.atr_n) / c.close


class SignalEngine:
    def __init__(self) -> None:
        self.state: dict[str, CoinState] = {c: CoinState() for c in CFG.coins}

    def on_closed_candle(self, coin: str, c: Candle) -> EntrySignal | None:
        st = self.state.get(coin)
        if st is None:
            return None
        atr = st.add(c)
        if atr is None or st.seen <= WARMUP_CANDLES:
            return None
        rng_atr = ((c.high - c.low) / c.close) / atr
        # order matters (backtest parity): shock detection + cooldown consumption
        # happen BEFORE the ATR gate — a gate-failed shock still resets cooldown.
        if rng_atr <= CFG.shock_atr or c.close == c.open:
            return None
        if c.t - st.last_signal_t < CFG.cooldown_min * 60:
            return None
        st.last_signal_t = c.t
        if atr < CFG.atr_min:                          # ATR gate (after cooldown)
            return None

        risk = max(CFG.sl_atr_mult * atr, CFG.sl_floor)
        if c.close < c.open:                          # down-shock -> long below low
            side = "long"
            limit = c.low * (1.0 - CFG.offset_atr * atr)
            if not (limit < c.close):
                return None
        else:                                         # up-shock -> short above high
            side = "short"
            limit = c.high * (1.0 + CFG.offset_atr * atr)
            if not (limit > c.close):
                return None
        return EntrySignal(coin=coin, side=side, limit_price=limit,
                           tp_dist=risk, sl_dist=risk, ttl_min=CFG.ttl_min,
                           signal_t=c.t, atr=atr, rng_atr=rng_atr)
