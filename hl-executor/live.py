"""Live broker: routes the frozen strategy's orders to Hyperliquid.

Same interface as paper.PaperBroker (place / on_closed_candle / snapshot) so
main.py stays broker-agnostic. Execution model:

  entry  -> post-only (ALO) GTC limit with deterministic cloid, TTL-cancelled
            on candle-close ticks (same clock as paper)
  fill   -> immediately place TP (reduce-only GTC limit, passive by
            construction) + SL (trigger stop-market, reduceOnly); OCO is
            managed here (one leg fills -> cancel the other)
  time   -> after max_hold_min cancel exits and market_close (IOC reduce-only)

State machine is driven by WS userFills/orderUpdates (pushed in by main.py)
with a REST reconciliation pass every 60s as the fallback for missed events
and for restart recovery. Every order carries a cloid derived from
(coin, signal_t, role) so restarts are idempotent.

The private key is read from the environment in exactly ONE place
(_make_clients) and is never logged.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import threading
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path

from config import CFG
from strategy import Candle, EntrySignal

ALO = {"limit": {"tif": "Alo"}}
GTC = {"limit": {"tif": "Gtc"}}


# ---------------------------------------------------------------- rounding
def round_px(px: float, sz_decimals: int) -> float:
    """HL rule: 5 significant figures, max (6 - szDecimals) decimals for
    perps; integer prices always allowed."""
    px = float(f"{px:.5g}")
    return round(px, max(0, 6 - sz_decimals))


def round_sz(sz: float, sz_decimals: int) -> float:
    """Floor to szDecimals (never oversize)."""
    f = 10 ** sz_decimals
    return math.floor(sz * f + 1e-9) / f


def make_cloid(coin: str, signal_t: int, role: str, seq: int = 0) -> str:
    h = hashlib.md5(f"hlx:{coin}:{signal_t}:{role}:{seq}".encode()).hexdigest()
    return "0x" + h  # 32 hex chars = 16 bytes


# ---------------------------------------------------------------- records
@dataclass
class LiveEntry:
    coin: str
    side: str                # "long" | "short"
    cloid: str
    oid: int | None
    limit_px: float
    sz: float
    tp_dist: float
    sl_dist: float
    expires_t: int
    signal_t: int
    notional: float
    filled_sz: float = 0.0
    status: str = "resting"  # resting | cancelled | done


@dataclass
class LivePosition:
    coin: str
    side: str
    entry: float             # avg fill price
    sz: float                # currently opened size (grows on partial fills)
    tp: float
    sl: float
    notional: float
    risk_usd: float
    entry_t: int
    max_hold_until: int
    signal_t: int
    tp_oid: int | None = None
    sl_oid: int | None = None
    exit_seq: int = 0        # bumps every time exits are re-placed
    closed_sz: float = 0.0
    realized_pnl: float = 0.0   # sum of closedPnl from exit fills
    fees: float = 0.0           # all fees, entry + exit
    last_exit_kind: str = ""
    closing: bool = False       # market_close in flight


class LiveBroker:
    """Same public surface as PaperBroker: place / on_closed_candle /
    snapshot, plus on_user_fills / on_order_updates / reconcile / save_state
    for main.py's live wiring."""

    def __init__(self, exchange=None, info=None, address: str | None = None) -> None:
        self._lock = threading.RLock()
        Path(CFG.state_file).parent.mkdir(parents=True, exist_ok=True)
        self.log_path = Path(CFG.state_file).parent / "live_trades.jsonl"
        self.state_path = Path(CFG.state_file).parent / "live_state.json"

        if exchange is None or info is None:
            exchange, info, address = self._make_clients()
        self.exchange = exchange
        self.info = info
        self.address = address

        # coin metadata: szDecimals + maxLeverage
        self.sz_decimals: dict[str, int] = {}
        self.max_leverage: dict[str, int] = {}
        self._load_meta()

        # trading state
        self.equity: float = 0.0
        self.entries: dict[str, LiveEntry] = {}       # cloid -> entry
        self.positions: dict[str, LivePosition] = {}  # coin -> position

        self.oid_role: dict[int, tuple[str, str, str]] = {}  # oid -> (role, coin, cloid)
        # insertion-ordered so trimming keeps the MOST RECENT tids (a set's
        # arbitrary order could evict just-seen tids -> reconcile re-ingests)
        self.seen_tids: dict[int, None] = {}
        self.fill_watermark_ms: int = int(time.time() * 1000) - 60_000
        self.fills = 0
        self.wins = 0
        self.day_r = 0.0
        self.day_key = ""
        self.halted = ""
        self.closed_r: list = []          # (exit_ts, r)
        self.streak_pause_until = 0
        self.sl_slips: list[float] = []   # adverse slippage fractions on SL fills

        self._load_state()
        self._set_leverage_all()
        self._refresh_equity()
        try:
            self.reconcile()              # restart recovery
        except Exception as e:
            self._log("boot_reconcile_error", error=repr(e))

    # -------------------------------------------------- margin model (F1/F4)
    def eff_leverage(self, coin: str) -> int:
        return min(CFG.leverage_cap, self.max_leverage.get(coin, CFG.leverage_cap))

    def margin_used(self) -> float:
        """Isolated margin committed: open positions + resting entry limits.
        Caller must hold self._lock."""
        used = sum(p.notional / self.eff_leverage(p.coin)
                   for p in self.positions.values())
        used += sum(e.notional / self.eff_leverage(e.coin)
                    for e in self.entries.values() if e.status == "resting")
        return used

    # ------------------------------------------------------------ clients
    @staticmethod
    def _make_clients():
        """The ONLY place the private key is read. Never log `key`."""
        import os
        import eth_account
        from hyperliquid.exchange import Exchange
        from hyperliquid.info import Info
        from hyperliquid.utils.constants import MAINNET_API_URL, TESTNET_API_URL

        key = os.environ.get("HL_PRIVATE_KEY", "")
        if not key:
            raise SystemExit("PAPER=false requires HL_PRIVATE_KEY")
        base = TESTNET_API_URL if CFG.hl_testnet else MAINNET_API_URL
        wallet = eth_account.Account.from_key(key)
        del key
        account = CFG.hl_account_address or None   # set when using an API/agent wallet
        info = Info(base, skip_ws=True)
        exchange = Exchange(wallet, base, account_address=account)
        address = account or wallet.address
        return exchange, info, address

    def _load_meta(self) -> None:
        meta, _ctxs = self.info.meta_and_asset_ctxs()
        for a in meta["universe"]:
            self.sz_decimals[a["name"]] = a["szDecimals"]
            self.max_leverage[a["name"]] = a.get("maxLeverage", CFG.leverage_cap)
        missing = [c for c in CFG.coins if c not in self.sz_decimals]
        if missing:
            self._log("coins_missing_on_venue", coins=missing)

    def _set_leverage_all(self) -> None:
        for coin in CFG.coins:
            if coin not in self.sz_decimals:
                continue
            lev = min(CFG.leverage_cap, self.max_leverage.get(coin, CFG.leverage_cap))
            try:
                self.exchange.update_leverage(lev, coin, is_cross=False)
            except Exception as e:
                self._log("leverage_error", coin=coin, error=repr(e))

    def _refresh_equity(self) -> None:
        try:
            st = self.info.user_state(self.address)
            self.equity = float(st["marginSummary"]["accountValue"])
        except Exception as e:
            self._log("equity_error", error=repr(e))
            return
        # USDC parked in the SPOT balance is invisible to perps trading
        # (deposits/sends can land there; the unified-account UI hides the
        # split but the clearinghouses stay separate). Sweep it to perps.
        if self.equity < 5.0:
            try:
                spot = self.info.spot_user_state(self.address)
                usdc = next((float(b["total"]) for b in spot.get("balances", [])
                             if b.get("coin") == "USDC"), 0.0)
                if usdc >= 5.0:
                    self.exchange.usd_class_transfer(usdc, to_perp=True)
                    self._log("spot_to_perp_sweep", amount=usdc)
                    st = self.info.user_state(self.address)
                    self.equity = float(st["marginSummary"]["accountValue"])
            except Exception as e:
                self._log("spot_sweep_error", error=repr(e))

    # ------------------------------------------------------------ overlay C2
    # (identical semantics to paper.py — the validated ruleset)
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

    # ------------------------------------------------------------ lifecycle
    def place(self, sig: EntrySignal, notional: float) -> None:
        with self._lock:
            self._place(sig, notional)

    def _place(self, sig: EntrySignal, notional: float) -> None:
        if self.halted:
            return
        blocked = self._overlay_blocked(sig.signal_t)
        if blocked:
            self._log("skip_overlay", coin=sig.coin, reason=blocked)
            return
        live_entries = [e for e in self.entries.values() if e.status == "resting"]
        if len(self.positions) + len(live_entries) >= CFG.max_concurrent:
            self._log("skip_concurrency", coin=sig.coin)
            return
        if sig.coin in self.positions or any(e.coin == sig.coin for e in live_entries):
            self._log("skip_coin_busy", coin=sig.coin)
            return
        szd = self.sz_decimals.get(sig.coin)
        if szd is None:
            self._log("skip_no_meta", coin=sig.coin)
            return
        px = round_px(sig.limit_price, szd)
        sz = round_sz(notional / px, szd)
        if sz <= 0 or sz * px < CFG.min_notional:
            self._log("skip_min_notional", coin=sig.coin, notional=round(sz * px, 2))
            return
        cloid = make_cloid(sig.coin, sig.signal_t, "entry")
        try:
            resp = self.exchange.order(
                sig.coin, sig.side == "long", sz, px, ALO,
                reduce_only=False, cloid=self._cloid(cloid))
        except Exception as ex:
            # Timeout may still have placed the order on the venue; the
            # reconcile untracked-order sweep cancels it within 60s.
            self._log("entry_error", coin=sig.coin, error=repr(ex))
            return
        status, err = self._first_status(resp)
        if err or status is None:
            self._log("entry_error", coin=sig.coin, error=err or "no status")
            return
        if "error" in status:
            # ALO would cross => price already moved through our limit: skip.
            self._log("entry_alo_reject", coin=sig.coin, error=status["error"])
            return
        entry = LiveEntry(
            coin=sig.coin, side=sig.side, cloid=cloid, oid=None,
            limit_px=px, sz=sz, tp_dist=sig.tp_dist, sl_dist=sig.sl_dist,
            expires_t=sig.signal_t + sig.ttl_min * 60, signal_t=sig.signal_t,
            notional=sz * px)
        if "resting" in status:
            entry.oid = status["resting"]["oid"]
        elif "filled" in status:                     # defensive; ALO cannot take
            entry.oid = status["filled"]["oid"]
        if entry.oid is not None:
            self.oid_role[entry.oid] = ("entry", sig.coin, cloid)
        self.entries[cloid] = entry
        self._log("entry_placed", coin=sig.coin, side=sig.side, limit=px, sz=sz,
                  oid=entry.oid, cloid=cloid, tp_dist=sig.tp_dist, atr=sig.atr)

    def on_closed_candle(self, coin: str, c: Candle) -> None:
        with self._lock:
            self._roll_day(c.t)
            # overlay tripped -> cancel ALL resting entry limits (validated rule)
            if self._overlay_blocked(c.t):
                for e in list(self.entries.values()):
                    if e.status == "resting":
                        self._cancel_entry(e, "entry_cancelled_overlay")
            # TTL expiry for this coin's entry (candle close = clock, like paper)
            for e in list(self.entries.values()):
                if e.coin == coin and e.status == "resting" and c.t >= e.expires_t:
                    self._cancel_entry(e, "entry_expired")
            # time stop
            pos = self.positions.get(coin)
            if pos is not None and not pos.closing and c.t >= pos.max_hold_until:
                self._time_stop(pos)

    def _cancel_entry(self, e: LiveEntry, reason: str) -> None:
        try:
            if e.oid is not None:
                self.exchange.cancel(e.coin, e.oid)
            else:
                self.exchange.cancel_by_cloid(e.coin, self._cloid(e.cloid))
        except Exception as ex:
            self._log("cancel_error", coin=e.coin, oid=e.oid, error=repr(ex))
        e.status = "cancelled"
        # Keep the record: a fill can race the cancel (filled on venue, event
        # in flight) and must still open a MANAGED position with exits.
        # Stale cancelled records are pruned by reconcile after a grace window.
        self._log(reason, coin=e.coin, oid=e.oid, filled_sz=e.filled_sz)

    def _time_stop(self, pos: LivePosition) -> None:
        pos.closing = True
        self._cancel_exits(pos)
        pos.exit_seq += 1               # unique cloid per close attempt
        cloid = make_cloid(pos.coin, pos.signal_t, "time", pos.exit_seq)
        try:
            resp = self.exchange.market_close(pos.coin, cloid=self._cloid(cloid))
        except Exception as e:
            self._log("time_stop_error", coin=pos.coin, error=repr(e))
            pos.closing = False
            return
        status, err = self._first_status(resp)
        oid = None
        if status and "filled" in status:
            oid = status["filled"]["oid"]
        elif status and "resting" in status:      # cannot happen for IOC; defensive
            oid = status["resting"]["oid"]
        if oid is not None:
            self.oid_role[oid] = ("time", pos.coin, cloid)
        else:
            # IOC rejected / no response / venue already flat. Exits are
            # already cancelled — a stuck closing=True would leave the
            # position unprotected forever (candle ticks AND reconcile skip
            # closing positions). Clear the flag so both paths retry.
            pos.closing = False
        self._log("time_stop_sent", coin=pos.coin, oid=oid,
                  error=(err or (status or {}).get("error")))

    # ------------------------------------------------------------ WS events
    def on_user_fills(self, data: dict) -> None:
        if data.get("isSnapshot"):
            return                       # history; reconcile() covers gaps
        with self._lock:
            for f in data.get("fills", []):
                self._ingest_fill(f)

    def _ingest_fill(self, f: dict) -> None:
        tid = f.get("tid")
        if tid is not None:
            if tid in self.seen_tids:
                return
            self.seen_tids[tid] = None
            if len(self.seen_tids) > 20000:   # keep newest by insertion order
                self.seen_tids = dict.fromkeys(list(self.seen_tids)[-5000:])
        self.fill_watermark_ms = max(self.fill_watermark_ms, int(f.get("time", 0)))
        role = self.oid_role.get(f["oid"])
        if role is None:
            self._log("fill_unknown_oid", oid=f["oid"], coin=f.get("coin"),
                      px=f.get("px"), sz=f.get("sz"))
            return
        kind, coin, cloid = role
        px, sz = float(f["px"]), float(f["sz"])
        fee = float(f.get("fee", 0) or 0)
        pnl = float(f.get("closedPnl", 0) or 0)
        t = int(f.get("time", time.time() * 1000)) // 1000
        if kind == "entry":
            self._on_entry_fill(cloid, px, sz, fee, t)
        else:
            self._on_exit_fill(coin, kind, px, sz, fee, pnl, t)

    def on_order_updates(self, updates: list) -> None:
        with self._lock:
            for u in updates:
                o = u.get("order", {})
                st = u.get("status")
                role = self.oid_role.get(o.get("oid"))
                if role is None:
                    continue
                kind, coin, cloid = role
                if kind == "entry" and st in ("canceled", "marginCanceled", "rejected"):
                    e = self.entries.get(cloid)
                    if e is not None and e.status == "resting":
                        e.status = "cancelled"   # record kept for fill races
                        self._log("entry_cancelled_venue", coin=coin, status=st)

    # ------------------------------------------------------------ fills
    def _on_entry_fill(self, cloid: str, px: float, sz: float, fee: float, t: int) -> None:
        e = self.entries.get(cloid)
        if e is None:
            self._log("entry_fill_untracked", cloid=cloid)
            return
        e.filled_sz += sz
        unit = 10 ** -self.sz_decimals.get(e.coin, 3)
        if e.filled_sz >= e.sz - 0.5 * unit:
            e.status = "done"
        pos = self.positions.get(e.coin)
        if pos is None:
            long = e.side == "long"
            tp = round_px(px * (1 + e.tp_dist) if long else px * (1 - e.tp_dist),
                          self.sz_decimals[e.coin])
            sl = round_px(px * (1 - e.sl_dist) if long else px * (1 + e.sl_dist),
                          self.sz_decimals[e.coin])
            pos = LivePosition(
                coin=e.coin, side=e.side, entry=px, sz=sz, tp=tp, sl=sl,
                notional=sz * px, risk_usd=sz * px * e.sl_dist,
                entry_t=t, max_hold_until=t + CFG.max_hold_min * 60,
                signal_t=e.signal_t)
            pos.fees += fee
            self.positions[e.coin] = pos
            self._log("entry_filled", coin=e.coin, side=e.side, entry=px, sz=sz,
                      tp=tp, sl=sl, partial=e.status != "done")
        else:
            pos.entry = (pos.entry * pos.sz + px * sz) / (pos.sz + sz)
            pos.sz += sz
            pos.notional = pos.entry * pos.sz
            pos.risk_usd = pos.notional * e.sl_dist
            pos.fees += fee
            self._log("entry_fill_add", coin=e.coin, sz=sz, total_sz=pos.sz)
        self._refresh_exits(pos)

    def _refresh_exits(self, pos: LivePosition) -> None:
        """(Re)place TP + SL sized to the actual filled size. One batched call."""
        self._cancel_exits(pos)
        pos.exit_seq += 1
        szd = self.sz_decimals[pos.coin]
        open_sz = round_sz(pos.sz - pos.closed_sz, szd)
        if open_sz <= 0:
            return
        long = pos.side == "long"
        tp_cloid = make_cloid(pos.coin, pos.signal_t, "tp", pos.exit_seq)
        sl_cloid = make_cloid(pos.coin, pos.signal_t, "sl", pos.exit_seq)
        orders = [
            {"coin": pos.coin, "is_buy": not long, "sz": open_sz,
             "limit_px": pos.tp, "order_type": GTC, "reduce_only": True,
             "cloid": self._cloid(tp_cloid)},
            {"coin": pos.coin, "is_buy": not long, "sz": open_sz,
             "limit_px": pos.sl, "reduce_only": True,
             "order_type": {"trigger": {"triggerPx": pos.sl, "isMarket": True,
                                        "tpsl": "sl"}},
             "cloid": self._cloid(sl_cloid)},
        ]
        try:
            resp = self.exchange.bulk_orders(orders)
            statuses = resp["response"]["data"]["statuses"]
        except Exception as e:
            self._log("exit_place_error", coin=pos.coin, error=repr(e))
            statuses = [{"error": repr(e)}, {"error": repr(e)}]
        pos.tp_oid = pos.sl_oid = None
        for st, role, cl in zip(statuses, ("tp", "sl"), (tp_cloid, sl_cloid)):
            oid = (st.get("resting") or st.get("filled") or {}).get("oid")
            if oid is not None:
                self.oid_role[oid] = (role, pos.coin, cl)
                if role == "tp":
                    pos.tp_oid = oid
                else:
                    pos.sl_oid = oid
            else:
                self._log("exit_leg_error", coin=pos.coin, leg=role,
                          error=st.get("error", "unknown"))
        if pos.sl_oid is None and not pos.closing:
            # An unprotected position is not acceptable: flatten immediately.
            self._log("sl_place_failed_closing", coin=pos.coin)
            self._time_stop(pos)

    def _cancel_exits(self, pos: LivePosition) -> None:
        cancels = [{"coin": pos.coin, "oid": o}
                   for o in (pos.tp_oid, pos.sl_oid) if o is not None]
        if not cancels:
            return
        try:
            self.exchange.bulk_cancel(cancels)
        except Exception as e:
            self._log("cancel_exits_error", coin=pos.coin, error=repr(e))
        pos.tp_oid = pos.sl_oid = None

    def _on_exit_fill(self, coin: str, kind: str, px: float, sz: float,
                      fee: float, closed_pnl: float, t: int) -> None:
        pos = self.positions.get(coin)
        if pos is None:
            self._log("exit_fill_untracked", coin=coin, kind=kind)
            return
        pos.closed_sz += sz
        pos.realized_pnl += closed_pnl
        pos.fees += fee
        pos.last_exit_kind = kind
        if kind == "sl":
            # THE number the validation waits for: trigger vs actual fill.
            adverse = (pos.sl - px) / pos.sl if pos.side == "long" \
                else (px - pos.sl) / pos.sl
            self.sl_slips.append(adverse)
            self._log("sl_slippage", coin=coin, trigger=pos.sl, fill=px,
                      slip_frac=round(adverse, 6),
                      model=CFG.assumed_sl_slippage)
        unit = 10 ** -self.sz_decimals.get(coin, 3)
        if pos.closed_sz >= pos.sz - 0.5 * unit:
            self._finalize(pos, t)

    def _finalize(self, pos: LivePosition, t: int) -> None:
        # OCO: cancel the surviving leg
        self._cancel_exits(pos)
        pnl = pos.realized_pnl - pos.fees
        r = pnl / pos.risk_usd if pos.risk_usd else 0.0
        self.equity += pnl        # refreshed from user_state every 60s anyway
        self.fills += 1
        self.day_r += r
        if r > 0:
            self.wins += 1
        self.closed_r.append((t, r))
        self.closed_r = [(ts, x) for ts, x in self.closed_r if t - ts <= 2 * 86400]
        self._update_streak(t)
        del self.positions[pos.coin]
        self._log("closed", coin=pos.coin, side=pos.side,
                  outcome=pos.last_exit_kind or "unknown",
                  entry=pos.entry, r=round(r, 4), pnl=round(pnl, 4),
                  fees=round(pos.fees, 4), equity=round(self.equity, 2),
                  hold_min=(t - pos.entry_t) // 60)
        self._check_kill()

    # ------------------------------------------------------------ kill-switches
    def _check_kill(self) -> None:
        if self.fills >= CFG.kill_min_fills:
            wr = self.wins / self.fills
            if wr < CFG.kill_wr_threshold:
                self.halted = f"WR {wr:.3f} < {CFG.kill_wr_threshold} after {self.fills} fills"
        if self.day_r <= -CFG.kill_daily_loss_r:
            self.halted = f"daily loss {self.day_r:.1f}R"
        if len(self.sl_slips) >= 10:
            avg = sum(self.sl_slips) / len(self.sl_slips)
            if avg > CFG.kill_slippage_mult * CFG.assumed_sl_slippage:
                self.halted = (f"slippage {avg:.5f} > "
                               f"{CFG.kill_slippage_mult}x model ({CFG.assumed_sl_slippage})")
        if self.halted:
            self._log("KILL_SWITCH", reason=self.halted)
            self.save_state()

    def _roll_day(self, t: int) -> None:
        key = time.strftime("%Y-%m-%d", time.gmtime(t))
        if key != self.day_key:
            self.day_key = key
            self.day_r = 0.0
            if self.halted.startswith("daily loss"):
                self.halted = ""
                self._log("halt_reset")

    # ------------------------------------------------------------ reconciliation
    def reconcile(self) -> None:
        """REST fallback: refresh equity, ingest missed fills, verify orders
        and positions against the venue. Called every 60s and once at boot."""
        with self._lock:
            self._refresh_equity()
            # 1) missed fills since watermark (dedup by tid)
            try:
                fills = self.info.user_fills_by_time(self.address,
                                                     self.fill_watermark_ms - 5_000)
                for f in fills:
                    self._ingest_fill(f)
            except Exception as e:
                self._log("reconcile_fills_error", error=repr(e))
            try:
                open_orders = self.info.open_orders(self.address)
                st = self.info.user_state(self.address)
            except Exception as e:
                self._log("reconcile_state_error", error=repr(e))
                return
            open_oids = {o["oid"] for o in open_orders}
            venue_pos = {p["position"]["coin"]: float(p["position"]["szi"])
                         for p in st.get("assetPositions", [])
                         if float(p["position"]["szi"]) != 0}
            # 2) tracked resting entries that vanished without a fill
            #    (record kept: the "vanished" order may in fact have filled
            #    between the fills fetch and the open-orders fetch)
            for e in list(self.entries.values()):
                if e.status == "resting" and e.oid is not None \
                        and e.oid not in open_oids:
                    e.status = "cancelled"
                    self._log("entry_gone_on_venue", coin=e.coin, oid=e.oid)
            # 3) tracked positions
            for coin, pos in list(self.positions.items()):
                if coin not in venue_pos:
                    # flat on venue but we still track it: fills ingest above
                    # should have finalized; if not, force-close the record.
                    self._log("reconcile_force_close", coin=coin,
                              realized=pos.realized_pnl)
                    self._finalize(pos, int(time.time()))
                elif pos.closing:
                    # close in flight but venue still shows the position
                    # (partial IOC fill or lost close): re-fire the
                    # reduce-only close — idempotent, caps at position size.
                    self._log("reconcile_reclose", coin=coin,
                              szi=venue_pos[coin])
                    self._time_stop(pos)
                else:
                    # ensure both exit legs still rest
                    tp_ok = pos.tp_oid in open_oids
                    sl_ok = pos.sl_oid in open_oids
                    if not (tp_ok and sl_ok):
                        self._log("reconcile_exits_missing", coin=coin,
                                  tp_ok=tp_ok, sl_ok=sl_ok)
                        self._refresh_exits(pos)
            # 4) untracked venue positions (manual trades / lost state)
            for coin, szi in venue_pos.items():
                if coin in self.positions:
                    continue
                if CFG.live_adopt == "close":
                    self._log("untracked_position_closing", coin=coin, szi=szi)
                    try:
                        self.exchange.market_close(coin)
                    except Exception as e:
                        self._log("untracked_close_error", coin=coin, error=repr(e))
                else:
                    self._log("untracked_position_adopted", coin=coin, szi=szi)
            # 5) untracked open orders on our coins (only in 'close' policy)
            if CFG.live_adopt == "close":
                for o in open_orders:
                    if o["coin"] in CFG.coins and o["oid"] not in self.oid_role:
                        self._log("untracked_order_cancel", coin=o["coin"], oid=o["oid"])
                        try:
                            self.exchange.cancel(o["coin"], o["oid"])
                        except Exception as e:
                            self._log("untracked_cancel_error", oid=o["oid"], error=repr(e))
            # 6) prune entry records kept for the cancel/fill race grace
            now = int(time.time())
            for cl, e in list(self.entries.items()):
                if e.status == "cancelled" and e.filled_sz <= 0 \
                        and now > e.expires_t + 900:
                    self.entries.pop(cl, None)
            self.save_state()

    # ------------------------------------------------------------ persistence
    def save_state(self) -> None:
        s = {
            "entries": {k: asdict(v) for k, v in self.entries.items()},
            "positions": {k: asdict(v) for k, v in self.positions.items()},
            "oid_role": {str(k): list(v) for k, v in self.oid_role.items()},
            "fills": self.fills, "wins": self.wins,
            "day_r": self.day_r, "day_key": self.day_key,
            "halted": self.halted, "closed_r": self.closed_r,
            "streak_pause_until": self.streak_pause_until,
            "sl_slips": self.sl_slips[-500:],
            "fill_watermark_ms": self.fill_watermark_ms,
            "seen_tids": list(self.seen_tids)[-5000:],
        }
        # atomic: a crash mid-write must never corrupt the state file
        # (corrupt state => positions look untracked on restart => closed)
        tmp = self.state_path.with_name(self.state_path.name + ".tmp")
        tmp.write_text(json.dumps(s))
        os.replace(tmp, self.state_path)

    def _load_state(self) -> None:
        if not self.state_path.exists():
            return
        try:
            s = json.loads(self.state_path.read_text())
        except Exception as e:
            self._log("state_load_error", error=repr(e))
            return
        self.entries = {k: LiveEntry(**v) for k, v in s.get("entries", {}).items()}
        self.positions = {k: LivePosition(**v) for k, v in s.get("positions", {}).items()}
        self.oid_role = {int(k): tuple(v) for k, v in s.get("oid_role", {}).items()}
        self.fills = s.get("fills", 0)
        self.wins = s.get("wins", 0)
        self.day_r = s.get("day_r", 0.0)
        self.day_key = s.get("day_key", "")
        self.halted = s.get("halted", "")
        self.closed_r = [tuple(x) for x in s.get("closed_r", [])]
        self.streak_pause_until = s.get("streak_pause_until", 0)
        self.sl_slips = s.get("sl_slips", [])
        self.fill_watermark_ms = s.get("fill_watermark_ms", self.fill_watermark_ms)
        self.seen_tids = dict.fromkeys(s.get("seen_tids", []))

    # ------------------------------------------------------------ helpers/io
    @staticmethod
    def _cloid(raw: str):
        from hyperliquid.utils.types import Cloid
        return Cloid.from_str(raw)

    @staticmethod
    def _first_status(resp) -> tuple[dict | None, str | None]:
        try:
            if resp.get("status") != "ok":
                return None, json.dumps(resp)[:400]
            return resp["response"]["data"]["statuses"][0], None
        except Exception:
            return None, json.dumps(resp)[:400]

    def _log(self, event: str, **kw) -> None:
        rec = {"ts": int(time.time()), "event": event, **kw}
        with self.log_path.open("a") as f:
            f.write(json.dumps(rec) + "\n")
        # live events are rare — mirror them to stdout so the platform log
        # (Railway) shows order flow and errors without shell access
        print(f"live: {event} " + " ".join(f"{k}={v}" for k, v in kw.items()),
              flush=True)

    def snapshot(self) -> dict:
        with self._lock:
            n_slip = len(self.sl_slips)
            return {
                "equity": round(self.equity, 2), "fills": self.fills,
                "wins": self.wins,
                "wr": round(self.wins / self.fills, 4) if self.fills else None,
                "open_positions": {k: asdict(v) for k, v in self.positions.items()},
                "pending": sum(1 for e in self.entries.values()
                               if e.status == "resting"),
                "halted": self.halted,
                "avg_sl_slip": round(sum(self.sl_slips) / n_slip, 6) if n_slip else None,
                "n_sl_fills": n_slip,
            }
