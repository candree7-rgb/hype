"""LiveBroker verification without real money.

Two layers:
1. Signature parity: the mock Exchange/Info methods must have EXACTLY the
   same parameter names/defaults as the installed hyperliquid-python-sdk, so
   a scripted test cannot hide a wrong parameter name.
2. Scenario tests: normal tp exit, stop exit + slippage measurement,
   ambiguous/racy same-candle, partial fill resizing, TTL expiry, ALO
   reject, ws-miss + reconciliation, restart with open position, overlay
   trip, time stop, untracked-position policy, sizing/rounding.

Run: python3 -m pytest tests/ -q     (from hl-executor/)
"""
from __future__ import annotations

import inspect
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import CFG                                    # noqa: E402
from strategy import Candle, EntrySignal                  # noqa: E402
import live as live_mod                                   # noqa: E402
from live import LiveBroker, make_cloid, round_px, round_sz   # noqa: E402

from hyperliquid.exchange import Exchange as RealExchange     # noqa: E402
from hyperliquid.info import Info as RealInfo                 # noqa: E402

T0 = 1_800_000_000          # fixed epoch for determinism


# --------------------------------------------------------------------- mocks
def ok_statuses(*statuses):
    return {"status": "ok",
            "response": {"type": "order", "data": {"statuses": list(statuses)}}}


class MockExchange:
    """Signatures below are asserted to match the real SDK exactly."""
    DEFAULT_SLIPPAGE = 0.05

    def __init__(self):
        self.calls: list[tuple] = []
        self.order_responses: list = []       # FIFO scripted responses
        self.bulk_responses: list = []
        self.close_responses: list = []

    def _pop(self, q, default):
        return q.pop(0) if q else default

    def order(self, name, is_buy, sz, limit_px, order_type, reduce_only=False,
              cloid=None, builder=None):
        self.calls.append(("order", name, is_buy, sz, limit_px, order_type,
                           reduce_only, str(cloid) if cloid else None))
        return self._pop(self.order_responses, ok_statuses({"resting": {"oid": 1}}))

    def bulk_orders(self, order_requests, builder=None, grouping="na"):
        self.calls.append(("bulk_orders", [dict(o, cloid=str(o.get("cloid")))
                                           for o in order_requests]))
        return self._pop(self.bulk_responses,
                         ok_statuses({"resting": {"oid": 2}}, {"resting": {"oid": 3}}))

    def cancel(self, name, oid):
        self.calls.append(("cancel", name, oid))
        return ok_statuses("success")

    def cancel_by_cloid(self, name, cloid):
        self.calls.append(("cancel_by_cloid", name, str(cloid)))
        return ok_statuses("success")

    def bulk_cancel(self, cancel_requests):
        self.calls.append(("bulk_cancel", list(cancel_requests)))
        return ok_statuses("success")

    def bulk_cancel_by_cloid(self, cancel_requests):
        self.calls.append(("bulk_cancel_by_cloid", list(cancel_requests)))
        return ok_statuses("success")

    def market_close(self, coin, sz=None, px=None, slippage=DEFAULT_SLIPPAGE,
                     cloid=None, builder=None):
        self.calls.append(("market_close", coin, sz, str(cloid) if cloid else None))
        return self._pop(self.close_responses,
                         ok_statuses({"filled": {"oid": 99, "totalSz": "1", "avgPx": "1"}}))

    def update_leverage(self, leverage, name, is_cross=True):
        self.calls.append(("update_leverage", leverage, name, is_cross))
        return {"status": "ok"}

    def named(self, name):
        return [c for c in self.calls if c[0] == name]


class MockInfo:
    def __init__(self):
        self.account_value = 1000.0
        self.fills: list = []            # returned by user_fills_by_time
        self.orders: list = []           # returned by open_orders
        self.positions: list = []        # assetPositions entries

    def user_state(self, address, dex=""):
        return {"assetPositions": self.positions,
                "marginSummary": {"accountValue": str(self.account_value)},
                "withdrawable": str(self.account_value)}

    def spot_user_state(self, address):
        # unified-mode free collateral; tests keep it 0 so account_value
        # remains the single source of truth in existing scenarios
        return {"balances": [{"coin": "USDC", "token": 0,
                              "total": str(getattr(self, "spot_usdc", 0.0)),
                              "hold": "0.0"}]}

    def open_orders(self, address, dex=""):
        return self.orders

    def meta_and_asset_ctxs(self):
        universe = [{"name": c, "szDecimals": SZD.get(c, 2), "maxLeverage": 10}
                    for c in CFG.coins]
        return [{"universe": universe}, []]

    def user_fills_by_time(self, address, start_time, end_time=None,
                           aggregate_by_time=False):
        return [f for f in self.fills if f["time"] >= start_time]

    def query_order_by_cloid(self, user, cloid):
        return {"status": "unknownOid"}


SZD = {"ETH": 4, "BTC": 5, "DOGE": 0, "SOL": 2}


# ------------------------------------------------------------------ fixtures
@pytest.fixture()
def broker(tmp_path, monkeypatch):
    monkeypatch.setattr(CFG, "state_file", str(tmp_path / "state.json"))
    monkeypatch.setattr(CFG, "live_adopt", "close")
    ex, info = MockExchange(), MockInfo()
    b = LiveBroker(exchange=ex, info=info, address="0x" + "ab" * 20)
    b.day_key = time.strftime("%Y-%m-%d", time.gmtime(T0))   # avoid day-roll noise
    ex.calls.clear()
    return b


def sig(coin="ETH", side="long", px=2000.0, dist=0.01, t=T0):
    return EntrySignal(coin=coin, side=side, limit_price=px, tp_dist=dist,
                       sl_dist=dist, ttl_min=CFG.ttl_min, signal_t=t,
                       atr=0.002, rng_atr=4.0)


def candle(coin_t, px=2000.0):
    return Candle(t=coin_t, open=px, high=px, low=px, close=px, vol=1.0)


def fill(oid, px, sz, tid, t_ms=(T0 + 30) * 1000, fee="0.5", pnl="0"):
    return {"coin": "ETH", "px": str(px), "sz": str(sz), "side": "B",
            "time": t_ms, "startPosition": "0", "dir": "Open Long",
            "closedPnl": str(pnl), "hash": "0x0", "oid": oid, "crossed": False,
            "fee": str(fee), "tid": tid, "feeToken": "USDC"}


def place_and_fill_entry(b, entry_sz=None):
    """Standard long ETH entry at 2000, fully filled -> exits at oids 2/3."""
    b.place(sig(), 4000.0)
    e = next(iter(b.entries.values()))
    sz = entry_sz if entry_sz is not None else e.sz
    b.on_user_fills({"isSnapshot": False, "user": "0x",
                     "fills": [fill(1, 2000.0, sz, tid=101)]})
    return e


# --------------------------------------------------------- signature parity
@pytest.mark.parametrize("mock_cls,real_cls,methods", [
    (MockExchange, RealExchange,
     ["order", "bulk_orders", "cancel", "cancel_by_cloid", "bulk_cancel",
      "bulk_cancel_by_cloid", "market_close", "update_leverage"]),
    (MockInfo, RealInfo,
     ["user_state", "open_orders", "meta_and_asset_ctxs",
      "user_fills_by_time", "query_order_by_cloid"]),
])
def test_mock_signatures_match_real_sdk(mock_cls, real_cls, methods):
    for m in methods:
        real = inspect.signature(getattr(real_cls, m))
        mock = inspect.signature(getattr(mock_cls, m))
        real_params = [(p.name, p.default) for p in real.parameters.values()]
        mock_params = [(p.name, p.default) for p in mock.parameters.values()]
        assert mock_params == real_params, (
            f"{real_cls.__name__}.{m}: mock {mock_params} != real {real_params}")


def test_default_slippage_matches_sdk():
    assert MockExchange.DEFAULT_SLIPPAGE == RealExchange.DEFAULT_SLIPPAGE


# ------------------------------------------------------------------ rounding
def test_rounding_rules():
    assert round_px(1234.5678, 4) == 1234.6          # 5 sig figs (SDK-identical)
    assert round_px(0.00123456, 0) == 0.001235       # 6-decimal cap beats 5 sig figs
    assert round_px(117234.6, 5) == 117230.0         # BTC-style
    assert round_sz(1.23456789, 4) == 1.2345         # floor, never oversize
    assert round_sz(0.999999999999, 0) == 1.0        # fp-epsilon tolerant
    assert round_sz(2.9999, 0) == 2.0


def test_cloid_is_deterministic_and_valid():
    c1, c2 = make_cloid("ETH", T0, "entry"), make_cloid("ETH", T0, "entry")
    assert c1 == c2 and c1.startswith("0x") and len(c1) == 34
    assert make_cloid("ETH", T0, "tp", 1) != c1
    LiveBroker._cloid(c1)   # real SDK Cloid validation must accept it


# ------------------------------------------------------------------ entries
def test_entry_placed_as_alo_with_cloid(broker):
    broker.place(sig(), 4000.0)
    (name, coin, is_buy, sz, px, otype, ro, cloid) = broker.exchange.calls[0]
    assert name == "order" and coin == "ETH" and is_buy is True
    assert otype == {"limit": {"tif": "Alo"}} and ro is False
    assert cloid == make_cloid("ETH", T0, "entry")
    assert sz == 2.0 and px == 2000.0
    assert broker.snapshot()["pending"] == 1
    assert broker.oid_role[1] == ("entry", "ETH", cloid)


def test_alo_reject_is_skipped(broker):
    broker.exchange.order_responses.append(
        ok_statuses({"error": "Post only order would have immediately matched"}))
    broker.place(sig(), 4000.0)
    assert broker.entries == {} and broker.positions == {}
    assert broker.snapshot()["pending"] == 0


def test_min_notional_enforced(broker):
    broker.place(sig(px=2000.0), 8.0)     # < $10
    assert broker.exchange.named("order") == [] and broker.entries == {}


def test_ttl_expiry_cancels_entry(broker):
    broker.place(sig(), 4000.0)
    broker.on_closed_candle("ETH", candle(T0 + 60))              # still alive
    assert broker.snapshot()["pending"] == 1
    broker.on_closed_candle("ETH", candle(T0 + CFG.ttl_min * 60))
    assert ("cancel", "ETH", 1) in broker.exchange.calls
    assert broker.snapshot()["pending"] == 0


# --------------------------------------------------------------- fills/exits
def test_entry_fill_places_tp_and_sl(broker):
    place_and_fill_entry(broker)
    pos = broker.positions["ETH"]
    assert pos.sz == 2.0 and pos.entry == 2000.0
    assert pos.tp == 2020.0 and pos.sl == 1980.0
    (_, orders), = [c for c in broker.exchange.calls if c[0] == "bulk_orders"]
    tp, sl = orders
    assert tp["order_type"] == {"limit": {"tif": "Gtc"}} and tp["reduce_only"]
    assert tp["is_buy"] is False and tp["sz"] == 2.0 and tp["limit_px"] == 2020.0
    assert sl["order_type"] == {"trigger": {"triggerPx": 1980.0,
                                            "isMarket": True, "tpsl": "sl"}}
    assert sl["reduce_only"] and sl["sz"] == 2.0
    assert pos.tp_oid == 2 and pos.sl_oid == 3


def test_normal_tp_exit_cancels_sl_oco(broker):
    place_and_fill_entry(broker)
    broker.on_user_fills({"isSnapshot": False, "user": "0x", "fills": [
        dict(fill(2, 2020.0, 2.0, tid=102, pnl="40", fee="0.6"),
             dir="Close Long")]})
    assert "ETH" not in broker.positions
    assert broker.fills == 1 and broker.wins == 1
    cancels = broker.exchange.named("bulk_cancel")[-1][1]
    assert {"coin": "ETH", "oid": 3} in cancels          # SL leg cancelled
    # pnl = 40 - (0.5 entry + 0.6 exit) = 38.9 ; risk = 4000*0.01 = 40
    assert broker.closed_r[-1][1] == pytest.approx(38.9 / 40.0)


def test_stop_exit_measures_slippage(broker):
    place_and_fill_entry(broker)
    broker.on_user_fills({"isSnapshot": False, "user": "0x", "fills": [
        dict(fill(3, 1979.0, 2.0, tid=103, pnl="-42", fee="1.8"),
             dir="Close Long")]})
    assert "ETH" not in broker.positions and broker.wins == 0
    assert broker.sl_slips == [pytest.approx((1980.0 - 1979.0) / 1980.0)]
    assert broker.snapshot()["n_sl_fills"] == 1
    cancels = broker.exchange.named("bulk_cancel")[-1][1]
    assert {"coin": "ETH", "oid": 2} in cancels          # TP leg cancelled


def test_ambiguous_race_late_fill_after_close(broker):
    """SL fills the whole position; a racing TP fill arrives afterwards."""
    place_and_fill_entry(broker)
    broker.on_user_fills({"isSnapshot": False, "user": "0x", "fills": [
        dict(fill(3, 1979.0, 2.0, tid=104, pnl="-42"), dir="Close Long")]})
    assert "ETH" not in broker.positions
    # late TP fill for the already-closed trade must not crash or double-count
    broker.on_user_fills({"isSnapshot": False, "user": "0x", "fills": [
        dict(fill(2, 2020.0, 2.0, tid=105, pnl="40"), dir="Close Long")]})
    assert broker.fills == 1 and "ETH" not in broker.positions


def test_partial_entry_fill_resizes_exits(broker):
    broker.place(sig(), 4000.0)          # sz 2.0
    broker.on_user_fills({"isSnapshot": False, "user": "0x",
                          "fills": [fill(1, 2000.0, 0.5, tid=110)]})
    pos = broker.positions["ETH"]
    assert pos.sz == 0.5
    first = broker.exchange.named("bulk_orders")[-1][1]
    assert first[0]["sz"] == 0.5 and first[1]["sz"] == 0.5
    entry = next(iter(broker.entries.values()))
    assert entry.status == "resting"     # remainder keeps resting until TTL
    broker.exchange.bulk_responses.append(
        ok_statuses({"resting": {"oid": 4}}, {"resting": {"oid": 5}}))
    broker.on_user_fills({"isSnapshot": False, "user": "0x",
                          "fills": [fill(1, 2010.0, 1.5, tid=111)]})
    assert pos.sz == 2.0 and pos.entry == pytest.approx(2007.5)
    # old exits cancelled, new ones sized to full fill
    assert {"coin": "ETH", "oid": 2} in broker.exchange.named("bulk_cancel")[-1][1]
    second = broker.exchange.named("bulk_orders")[-1][1]
    assert second[0]["sz"] == 2.0 and second[1]["sz"] == 2.0
    assert pos.tp_oid == 4 and pos.sl_oid == 5


def test_duplicate_fill_tid_ignored(broker):
    place_and_fill_entry(broker)
    sz_before = broker.positions["ETH"].sz
    broker.on_user_fills({"isSnapshot": False, "user": "0x",
                          "fills": [fill(1, 2000.0, 2.0, tid=101)]})   # same tid
    assert broker.positions["ETH"].sz == sz_before


def test_snapshot_fills_ignored(broker):
    broker.place(sig(), 4000.0)
    broker.on_user_fills({"isSnapshot": True, "user": "0x",
                          "fills": [fill(1, 2000.0, 2.0, tid=120)]})
    assert broker.positions == {}


# ------------------------------------------------------------------ time stop
def test_time_stop_cancels_exits_and_market_closes(broker):
    place_and_fill_entry(broker)
    pos = broker.positions["ETH"]
    broker.on_closed_candle("ETH", candle(pos.max_hold_until))
    assert {"coin": "ETH", "oid": 2} in broker.exchange.named("bulk_cancel")[-1][1]
    mc = broker.exchange.named("market_close")[-1]
    assert mc[1] == "ETH"
    assert broker.oid_role[99][0] == "time"
    broker.on_user_fills({"isSnapshot": False, "user": "0x", "fills": [
        dict(fill(99, 2001.0, 2.0, tid=130, pnl="2", fee="1.8"),
             dir="Close Long")]})
    assert "ETH" not in broker.positions and broker.fills == 1


def test_sl_placement_failure_flattens_position(broker):
    broker.exchange.bulk_responses.append(
        ok_statuses({"resting": {"oid": 2}}, {"error": "insufficient margin"}))
    place_and_fill_entry(broker)
    assert broker.exchange.named("market_close")   # fail-safe close fired


# ------------------------------------------------------------------- overlay
def test_overlay_blocks_new_entries(broker):
    broker.closed_r = [(T0 - 1000, -7.0)]
    broker.place(sig(), 4000.0)
    assert broker.exchange.named("order") == [] and broker.entries == {}


def test_overlay_trip_cancels_resting_entries(broker):
    broker.place(sig(), 4000.0)
    assert broker.snapshot()["pending"] == 1
    broker.closed_r = [(T0 + 50, -7.0)]              # breaker trips
    broker.on_closed_candle("BTC", candle(T0 + 60, px=100000.0))
    assert ("cancel", "ETH", 1) in broker.exchange.calls
    assert broker.snapshot()["pending"] == 0


def test_streak_brake(broker):
    for i in range(CFG.streak_k):
        broker.closed_r.append((T0 + i * 60, -1.0))
    broker._update_streak(T0 + CFG.streak_k * 60)
    assert broker.streak_pause_until > T0
    broker.place(sig(t=T0 + 400), 4000.0)
    assert broker.entries == {}


# -------------------------------------------------------------- kill-switches
def test_slippage_kill_switch(broker):
    place_and_fill_entry(broker)
    broker.sl_slips = [CFG.assumed_sl_slippage * 3] * 10
    broker.on_user_fills({"isSnapshot": False, "user": "0x", "fills": [
        dict(fill(3, 1979.0, 2.0, tid=140, pnl="-42"), dir="Close Long")]})
    assert broker.halted.startswith("slippage")
    broker.place(sig(coin="SOL", t=T0 + 999), 4000.0)   # halted -> refuse
    assert "SOL" not in [e.coin for e in broker.entries.values()]


def test_daily_loss_kill_and_reset(broker):
    broker.day_r = -CFG.kill_daily_loss_r
    broker._check_kill()
    assert broker.halted.startswith("daily loss")
    broker.on_closed_candle("ETH", candle(T0 + 86400 * 2))   # next utc day
    assert broker.halted == ""


# -------------------------------------------------------------- reconciliation
def test_ws_miss_recovered_by_reconcile(broker):
    place_and_fill_entry(broker)
    # WS died: TP filled on venue, we never saw it
    broker.info.fills = [dict(fill(2, 2020.0, 2.0, tid=150, pnl="40", fee="0.6"),
                              time=(T0 + 120) * 1000, dir="Close Long")]
    broker.info.positions = []                      # flat on venue
    broker.reconcile()
    assert "ETH" not in broker.positions
    assert broker.fills == 1 and broker.wins == 1   # closed via real fill data


def test_reconcile_force_close_without_fill_data(broker):
    place_and_fill_entry(broker)
    broker.info.fills = []                          # nothing to explain it
    broker.info.positions = []                      # but venue is flat
    broker.reconcile()
    assert "ETH" not in broker.positions and broker.fills == 1


def test_reconcile_replaces_missing_exits(broker):
    place_and_fill_entry(broker)
    pos = broker.positions["ETH"]
    broker.info.positions = [{"position": {"coin": "ETH", "szi": "2.0"}}]
    broker.info.orders = [{"coin": "ETH", "oid": pos.tp_oid}]   # SL vanished
    broker.exchange.bulk_responses.append(
        ok_statuses({"resting": {"oid": 6}}, {"resting": {"oid": 7}}))
    broker.exchange.calls.clear()
    broker.reconcile()
    assert broker.exchange.named("bulk_orders")     # exits re-placed
    assert pos.tp_oid == 6 and pos.sl_oid == 7


def test_untracked_position_closed_per_policy(tmp_path, monkeypatch):
    monkeypatch.setattr(CFG, "state_file", str(tmp_path / "state.json"))
    monkeypatch.setattr(CFG, "live_adopt", "close")
    ex, info = MockExchange(), MockInfo()
    info.positions = [{"position": {"coin": "DOGE", "szi": "-500"}}]
    LiveBroker(exchange=ex, info=info, address="0x" + "ab" * 20)  # boot reconcile
    assert ("market_close", "DOGE", None, None) in ex.calls


def test_untracked_position_adopted_leaves_it_alone(tmp_path, monkeypatch):
    monkeypatch.setattr(CFG, "state_file", str(tmp_path / "state.json"))
    monkeypatch.setattr(CFG, "live_adopt", "adopt")
    ex, info = MockExchange(), MockInfo()
    info.positions = [{"position": {"coin": "DOGE", "szi": "-500"}}]
    LiveBroker(exchange=ex, info=info, address="0x" + "ab" * 20)
    assert ex.named("market_close") == []


def test_restart_recovery_readopts_position(tmp_path, monkeypatch):
    monkeypatch.setattr(CFG, "state_file", str(tmp_path / "state.json"))
    monkeypatch.setattr(CFG, "live_adopt", "close")
    ex1, info1 = MockExchange(), MockInfo()
    b1 = LiveBroker(exchange=ex1, info=info1, address="0x" + "ab" * 20)
    b1.day_key = time.strftime("%Y-%m-%d", time.gmtime(T0))
    b1.place(sig(), 4000.0)
    b1.on_user_fills({"isSnapshot": False, "user": "0x",
                      "fills": [fill(1, 2000.0, 2.0, tid=160)]})
    b1.save_state()
    pos1 = b1.positions["ETH"]
    # --- restart: venue still has the position and both exit orders
    ex2, info2 = MockExchange(), MockInfo()
    info2.positions = [{"position": {"coin": "ETH", "szi": "2.0"}}]
    info2.orders = [{"coin": "ETH", "oid": pos1.tp_oid},
                    {"coin": "ETH", "oid": pos1.sl_oid}]
    b2 = LiveBroker(exchange=ex2, info=info2, address="0x" + "ab" * 20)
    assert "ETH" in b2.positions
    p = b2.positions["ETH"]
    assert p.entry == 2000.0 and p.tp_oid == pos1.tp_oid and p.sl_oid == pos1.sl_oid
    assert ex2.named("market_close") == []          # NOT treated as untracked
    assert ex2.named("bulk_orders") == []           # exits intact, not re-placed
    # halt flag survives restarts too
    b2.halted = "manual"
    b2.save_state()
    b3 = LiveBroker(exchange=MockExchange(), info=info2, address="0x" + "ab" * 20)
    assert b3.halted == "manual"


def test_concurrency_and_busy_coin_limits(broker):
    place_and_fill_entry(broker)
    broker.place(sig(t=T0 + 1200), 4000.0)          # same coin busy
    assert sum(1 for e in broker.entries.values() if e.status == "resting") == 0
    broker.positions.clear()
    for i, coin in enumerate(["BTC", "SOL", "DOGE"]):
        broker.exchange.order_responses.append(
            ok_statuses({"resting": {"oid": 50 + i}}))
        broker.place(sig(coin=coin, px=100.0, t=T0 + 1300 + i), 4000.0)
    monkey = CFG.max_concurrent
    assert sum(1 for e in broker.entries.values()
               if e.status == "resting") == min(3, monkey)
