"""Risk-model audit tests (sizing, rounding, margin, overlay parity).

Offline by default: venue metadata is a hardcoded snapshot of the MAINNET
metaAndAssetCtxs response (fetched read-only 2026-07-21). One optional
network test re-checks the snapshot against mainnet and skips on failure.

Documented findings (xfail = known gap, not yet fixed):
  F1  main._size divides by CFG.leverage_cap (20) flat, but 13/18 coins are
      max 10x and TAO is 5x on HL mainnet -> true isolated margin is 2-4x
      what the budget check models. The venue, not the bot, becomes the
      real margin limiter (silent entry rejections live that paper takes).
  F2  Paper-mode restart (main._save_state/_load_state) drops closed_r,
      streak_pause_until, day_r/day_key -> 24h breaker window, streak pause
      and the daily -20R counter reset on every redeploy. Live persists all
      of these.

Run: python3 -m pytest tests/test_risk.py -q     (from hl-executor/)
"""
from __future__ import annotations

import math
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import CFG                                  # noqa: E402
from strategy import Candle, EntrySignal                # noqa: E402
from paper import PaperBroker                           # noqa: E402
from live import LiveBroker, round_px, round_sz         # noqa: E402

T0 = 1_800_000_000

# --- MAINNET meta snapshot, fetched read-only 2026-07-21 -------------------
# {coin: (szDecimals, maxLeverage, markPx)}
META = {
    "BTC":   (5, 40, 66762.0),
    "ETH":   (4, 25, 1933.99),
    "SOL":   (2, 20, 78.079),
    "XRP":   (0, 20, 1.1475),
    "HYPE":  (2, 10, 62.01),
    "ZEC":   (2, 10, 544.14),
    "kPEPE": (0, 10, 0.002916),
    "AVAX":  (2, 10, 6.5937),
    "TAO":   (3, 5, 201.42),
    "DOGE":  (0, 10, 0.073271),
    "SUI":   (1, 10, 0.76729),
    "NEAR":  (1, 10, 1.96712),
    "WLD":   (1, 10, 0.38066),
    "PUMP":  (0, 10, 0.001957),
    "LTC":   (2, 10, 47.845),
    "BNB":   (3, 10, 575.03),
    "ADA":   (0, 10, 0.17594),
    "LINK":  (1, 10, 8.6944),
}

EQUITIES = (500.0, 1000.0, 2000.0)
SL_DISTS = (0.003, 0.0035, 0.005, 0.0075, 0.01)


def intended_notional(eq: float, sl_dist: float) -> float:
    return eq * CFG.risk_per_trade / sl_dist


# ------------------------------------------------------------- minimal mocks
def ok_statuses(*statuses):
    return {"status": "ok",
            "response": {"type": "order", "data": {"statuses": list(statuses)}}}


class RiskMockExchange:
    def __init__(self):
        self.calls: list[tuple] = []

    def order(self, name, is_buy, sz, limit_px, order_type, reduce_only=False,
              cloid=None, builder=None):
        self.calls.append(("order", name, sz, limit_px))
        return ok_statuses({"resting": {"oid": 1}})

    def bulk_orders(self, order_requests, builder=None, grouping="na"):
        self.calls.append(("bulk_orders", list(order_requests)))
        return ok_statuses({"resting": {"oid": 2}}, {"resting": {"oid": 3}})

    def cancel(self, name, oid):
        self.calls.append(("cancel", name, oid))
        return ok_statuses("success")

    def cancel_by_cloid(self, name, cloid):
        return ok_statuses("success")

    def bulk_cancel(self, cancel_requests):
        return ok_statuses("success")

    def market_close(self, coin, sz=None, px=None, slippage=0.05,
                     cloid=None, builder=None):
        self.calls.append(("market_close", coin))
        return ok_statuses({"filled": {"oid": 99, "totalSz": "1", "avgPx": "1"}})

    def update_leverage(self, leverage, name, is_cross=True):
        self.calls.append(("update_leverage", leverage, name, is_cross))
        return {"status": "ok"}


class RiskMockInfo:
    def __init__(self, account_value=1000.0):
        self.account_value = account_value

    def user_state(self, address, dex=""):
        return {"assetPositions": [],
                "marginSummary": {"accountValue": str(self.account_value)},
                "withdrawable": str(self.account_value)}

    def open_orders(self, address, dex=""):
        return []

    def meta_and_asset_ctxs(self):
        universe = [{"name": c, "szDecimals": szd, "maxLeverage": lev}
                    for c, (szd, lev, _px) in META.items()]
        return [{"universe": universe}, []]

    def user_fills_by_time(self, address, start_time, end_time=None,
                           aggregate_by_time=False):
        return []


@pytest.fixture()
def live_broker(tmp_path, monkeypatch):
    monkeypatch.setattr(CFG, "state_file", str(tmp_path / "state.json"))
    b = LiveBroker(exchange=RiskMockExchange(), info=RiskMockInfo(1000.0),
                   address="0x" + "ab" * 20)
    b.day_key = time.strftime("%Y-%m-%d", time.gmtime(T0))
    return b


@pytest.fixture()
def paper_broker(tmp_path, monkeypatch):
    monkeypatch.setattr(CFG, "state_file", str(tmp_path / "state.json"))
    b = PaperBroker()
    b.equity = 1000.0
    return b


def sig(coin="ETH", side="long", px=2000.0, dist=0.01, t=T0):
    return EntrySignal(coin=coin, side=side, limit_price=px, tp_dist=dist,
                       sl_dist=dist, ttl_min=CFG.ttl_min, signal_t=t,
                       atr=dist / CFG.sl_atr_mult, rng_atr=4.0)


def fill(oid, px, sz, tid, t_ms=(T0 + 30) * 1000, fee="0.5", pnl="0"):
    return {"coin": "ETH", "px": str(px), "sz": str(sz), "side": "B",
            "time": t_ms, "startPosition": "0", "dir": "Open Long",
            "closedPnl": str(pnl), "hash": "0x0", "oid": oid, "crossed": False,
            "fee": str(fee), "tid": tid, "feeToken": "USDC"}


# ============================================================ 1. sizing math
def test_notional_formula_at_small_equity():
    """notional = eq*risk/sl_dist -> $1k..$13.3k across the audit grid; the
    canonical $1000-equity / 0.35%-stop trade is a $5,714 position."""
    for eq in EQUITIES:
        for sl in SL_DISTS:
            n = intended_notional(eq, sl)
            assert n == pytest.approx(eq * 0.02 / sl)
            assert 2.0 * eq <= n <= (0.02 / CFG.sl_floor) * eq + 1e-9
    assert intended_notional(1000, 0.0035) == pytest.approx(5714.29, abs=0.01)
    assert intended_notional(500, 0.01) == pytest.approx(1000.0)
    assert intended_notional(2000, 0.003) == pytest.approx(13333.33, abs=0.01)


def test_universe_effective_leverage_snapshot():
    """13/18 coins cap at 10x, TAO at 5x — the '/CFG.leverage_cap' comment in
    main._size ('venue max per coin is >= 10 for universe') is wrong for TAO."""
    lev = {c: m[1] for c, m in META.items()}
    assert set(META) == set(CFG.coins)
    assert lev["TAO"] == 5 < 10
    ten_x = [c for c, v in lev.items() if v == 10]
    assert len(ten_x) == 13
    assert all(lev[c] >= CFG.leverage_cap for c in ("BTC", "ETH", "SOL", "XRP"))


def test_margin_model_understates_true_margin_on_low_leverage_coins():
    """F1: main._size books notional/20 as margin; the venue takes
    notional/min(20, maxLev). For every 10x coin the true margin is 2x the
    model, for TAO 4x. At sl=0.35% one 10x position = 57% of equity in
    REAL margin (model: 29%); one TAO position = 114% of equity, i.e.
    unplaceable at any equity."""
    for eq in EQUITIES:
        n = intended_notional(eq, 0.0035)
        model_margin = n / CFG.leverage_cap
        for coin, (_szd, max_lev, _px) in META.items():
            eff = min(CFG.leverage_cap, max_lev)
            true_margin = n / eff
            if max_lev >= CFG.leverage_cap:
                assert true_margin == pytest.approx(model_margin)
            else:
                assert true_margin >= 2 * model_margin - 1e-9, coin
        # TAO alone busts the entire margin budget at typical stops:
        assert n / 5 > CFG.margin_budget * eq
        # any single 10x coin already eats ~57% of equity:
        assert n / 10 == pytest.approx(0.5714 * eq, rel=1e-3)


def test_real_concurrency_limit_is_below_max_concurrent():
    """The validated study allows up to 6 concurrent trades; on 10x coins at
    typical stops the 0.8-equity margin budget physically fits only 1-2.
    This holds at ANY equity (everything scales with eq)."""
    for sl, expect in ((0.003, 1), (0.0035, 1), (0.005, 2), (0.01, 4)):
        margin_frac = (0.02 / sl) / 10          # 10x coin
        fit = int(CFG.margin_budget // margin_frac)
        assert fit == expect
        assert fit < CFG.max_concurrent


@pytest.mark.xfail(reason="F1: main._size uses CFG.leverage_cap flat; it "
                          "approves positions whose true isolated margin on "
                          "10x/5x coins exceeds the budget (venue will reject "
                          "them live; paper takes them)", strict=True)
def test_size_margin_check_respects_venue_leverage(tmp_path, monkeypatch):
    monkeypatch.setattr(CFG, "state_file", str(tmp_path / "state.json"))
    monkeypatch.setattr(CFG, "record_dir", str(tmp_path / "candles"))
    monkeypatch.setattr(CFG, "paper", True)
    from main import Executor
    ex = Executor()
    ex.broker.equity = 1000.0
    # sl=0.35% -> notional 5714; on a 10x coin the true margin is 571 (57% of
    # equity). A correct check would leave room for at most one such position
    # inside the 0.8 budget; the flat model books 286 and allows a second.
    n1 = ex._size(0.0035)
    assert n1 > 0
    from types import SimpleNamespace
    ex.broker.positions["HYPE"] = SimpleNamespace(notional=n1)
    n2 = ex._size(0.0035)
    # True margin for two = 1.14x equity -> a leverage-aware check must refuse.
    assert n2 == 0.0


def test_update_leverage_never_exceeds_venue_max(tmp_path, monkeypatch):
    """update_leverage(20) on a 10x/5x coin would error; the broker must send
    min(cap, venue max) per coin — TAO gets 5, HYPE 10, BTC 20."""
    monkeypatch.setattr(CFG, "state_file", str(tmp_path / "state.json"))
    ex = RiskMockExchange()
    LiveBroker(exchange=ex, info=RiskMockInfo(), address="0x" + "ab" * 20)
    sent = {c[2]: c[1] for c in ex.calls if c[0] == "update_leverage"}
    assert set(sent) == set(CFG.coins)
    for coin, (_szd, max_lev, _px) in META.items():
        assert sent[coin] == min(CFG.leverage_cap, max_lev)
        assert sent[coin] <= max_lev


# ==================================================== 2. rounding / min size
def test_rounding_distorts_risk_under_5pct_everywhere():
    """Floor-to-szDecimals must not distort the intended 2% risk by >5% for
    any coin at any audit equity/stop. Worst unit is ZEC ($5.44) — still
    ~0.1% on the canonical $5.7k position."""
    worst = 0.0
    for coin, (szd, _lev, px) in META.items():
        for eq in EQUITIES:
            for sl in SL_DISTS:
                n = intended_notional(eq, sl)
                rpx = round_px(px, szd)
                sz = round_sz(n / rpx, szd)
                assert sz > 0, (coin, eq, sl)
                actual = sz * rpx
                distortion = abs(n - actual) / n
                worst = max(worst, distortion)
                assert distortion < 0.05, (coin, eq, sl, distortion)
    assert worst < 0.02      # in fact never worse than 2% on this universe


def test_min_notional_never_binds_at_audit_equities():
    """notional < $10 requires eq < 10*sl/risk = $5 at a 1% stop — the $10
    floor cannot silently skip trades at $500+. Also true post-flooring."""
    for sl in SL_DISTS:
        eq_threshold = CFG.min_notional * sl / CFG.risk_per_trade
        assert eq_threshold <= 5.0
    for coin, (szd, _lev, px) in META.items():
        for eq in EQUITIES:
            for sl in SL_DISTS:
                rpx = round_px(px, szd)
                sz = round_sz(intended_notional(eq, sl) / rpx, szd)
                assert sz * rpx >= CFG.min_notional, (coin, eq, sl)


def test_min_viable_equity_per_coin_is_trivial():
    """min equity for one size-unit at sl=0.35% is < $1 on every coin (ZEC
    worst at ~$0.95): granularity does not constrain the $500 account."""
    for coin, (szd, _lev, px) in META.items():
        unit_value = px * 10 ** -szd
        eq_min = unit_value * 0.0035 / CFG.risk_per_trade
        assert eq_min < 1.0, (coin, eq_min)


# ================================================= 3. paper/live risk parity
def test_paper_risk_is_exactly_two_percent(paper_broker):
    b = paper_broker
    notional = intended_notional(b.equity, 0.01)      # what main._size returns
    b.place(sig(px=2000.0, dist=0.01), notional)
    b.on_closed_candle("ETH", Candle(t=T0 + 60, open=2001, high=2003,
                                     low=1990.0, close=2001, vol=1.0))
    pos = b.positions["ETH"]
    assert pos.risk_usd == pytest.approx(notional * 0.01)
    assert pos.risk_usd == pytest.approx(b_equity_risk := 1000.0 * 0.02)
    assert b_equity_risk == 20.0


def test_live_risk_matches_paper_within_rounding(live_broker):
    b = live_broker
    notional = intended_notional(1000.0, 0.01)        # 2000 USD
    b.place(sig(px=2000.0, dist=0.01), notional)
    e = next(iter(b.entries.values()))
    b.on_user_fills({"isSnapshot": False, "user": "0x",
                     "fills": [fill(1, 2000.0, e.sz, tid=201)]})
    pos = b.positions["ETH"]
    # sz floored to szDecimals -> risk_usd within 0.5% of the paper-intended $20
    assert pos.risk_usd == pytest.approx(20.0, rel=0.005)
    assert pos.risk_usd <= 20.0 + 1e-9                # flooring never oversizes


def test_live_partial_fill_risk_is_prorated(live_broker):
    """Partial fill: risk_usd = filled_notional*sl_dist, so R stays PnL per
    dollar actually risked — same semantics as paper on the filled fraction."""
    b = live_broker
    b.place(sig(px=2000.0, dist=0.01), 2000.0)        # sz 1.0
    b.on_user_fills({"isSnapshot": False, "user": "0x",
                     "fills": [fill(1, 2000.0, 0.25, tid=202)]})
    pos = b.positions["ETH"]
    assert pos.sz == 0.25
    assert pos.risk_usd == pytest.approx(0.25 * 2000.0 * 0.01)   # $5, not $20
    b.on_user_fills({"isSnapshot": False, "user": "0x",
                     "fills": [fill(1, 2000.0, 0.75, tid=203)]})
    assert pos.risk_usd == pytest.approx(20.0)


# ============================================= 4. overlay / kill-switch parity
def test_overlay_and_kill_constants_match_validated_study():
    """Frozen numbers from RESULTS.md (DD overlay C2 + kill-switches)."""
    assert CFG.breaker_r24 == 6.0
    assert CFG.streak_k == 6
    assert CFG.streak_window_h == 6.0
    assert CFG.streak_pause_h == 2.0
    assert CFG.kill_min_fills == 200
    assert CFG.kill_wr_threshold == 0.56
    assert CFG.kill_slippage_mult == 2.0
    assert CFG.kill_daily_loss_r == 20.0


@pytest.mark.parametrize("make", ["paper", "live"])
def test_breaker_window_is_trailing_24h_on_exit_timestamps(
        make, paper_broker, live_broker):
    b = paper_broker if make == "paper" else live_broker
    now = T0
    b.closed_r = [(now - 86401, -7.0)]                # just outside the window
    assert b._overlay_blocked(now) == ""
    b.closed_r = [(now - 86400, -3.0), (now - 100, -3.0)]   # sums to -6 inside
    assert b._overlay_blocked(now).startswith("breaker_24h")
    b.closed_r = [(now - 100, -5.9)]
    assert b._overlay_blocked(now) == ""


@pytest.mark.parametrize("make", ["paper", "live"])
def test_streak_brake_needs_six_losses_within_six_hours(
        make, paper_broker, live_broker):
    b = paper_broker if make == "paper" else live_broker
    # six consecutive non-wins spanning MORE than 6h (5 gaps * 4400s) -> no pause
    b.closed_r = [(T0 + i * 4400, -0.1) for i in range(CFG.streak_k)]
    b._update_streak(b.closed_r[-1][0])
    assert b.streak_pause_until == 0
    # same six inside 6h -> pause exactly 2h from last exit
    b.closed_r = [(T0 + i * 3000, -0.1) for i in range(CFG.streak_k)]
    last = b.closed_r[-1][0]
    b._update_streak(last)
    assert b.streak_pause_until == last + int(CFG.streak_pause_h * 3600)
    assert b._overlay_blocked(last + 100) == "streak_pause"
    assert b._overlay_blocked(b.streak_pause_until + 1) == ""


def test_live_overlay_and_halt_state_survive_restart(tmp_path, monkeypatch):
    monkeypatch.setattr(CFG, "state_file", str(tmp_path / "state.json"))
    b1 = LiveBroker(exchange=RiskMockExchange(), info=RiskMockInfo(),
                    address="0x" + "ab" * 20)
    b1.closed_r = [(T0, -3.0), (T0 + 60, -3.5)]
    b1.streak_pause_until = T0 + 7200
    b1.day_r = -4.2
    b1.halted = "manual test halt"
    b1.save_state()
    b2 = LiveBroker(exchange=RiskMockExchange(), info=RiskMockInfo(),
                    address="0x" + "ab" * 20)
    assert b2.closed_r == [(T0, -3.0), (T0 + 60, -3.5)]
    assert b2.streak_pause_until == T0 + 7200
    assert b2.day_r == -4.2
    assert b2.halted == "manual test halt"
    assert b2._overlay_blocked(T0 + 120).startswith("breaker_24h")


@pytest.mark.xfail(reason="F2: main._save_state/_load_state (paper mode) drop "
                          "closed_r, streak_pause_until and day_r — a restart "
                          "clears the 24h breaker, streak pause and daily -20R "
                          "counter in the paper phase", strict=True)
def test_paper_overlay_state_survives_restart(tmp_path, monkeypatch):
    monkeypatch.setattr(CFG, "state_file", str(tmp_path / "state.json"))
    monkeypatch.setattr(CFG, "record_dir", str(tmp_path / "candles"))
    monkeypatch.setattr(CFG, "paper", True)
    from main import Executor
    ex1 = Executor()
    ex1.broker.closed_r = [(T0, -7.0)]
    ex1.broker.streak_pause_until = T0 + 7200
    ex1.broker.day_r = -12.5
    ex1._save_state()
    ex2 = Executor()
    assert ex2.broker.closed_r == [(T0, -7.0)]
    assert ex2.broker.streak_pause_until == T0 + 7200
    assert ex2.broker.day_r == -12.5


def test_paper_halt_flag_does_survive_restart(tmp_path, monkeypatch):
    monkeypatch.setattr(CFG, "state_file", str(tmp_path / "state.json"))
    monkeypatch.setattr(CFG, "record_dir", str(tmp_path / "candles"))
    monkeypatch.setattr(CFG, "paper", True)
    from main import Executor
    ex1 = Executor()
    ex1.broker.halted = "WR 0.500 < 0.56 after 200 fills"
    ex1._save_state()
    ex2 = Executor()
    assert ex2.broker.halted == "WR 0.500 < 0.56 after 200 fills"


# =================================================== 5. live meta cross-check
def test_mainnet_meta_snapshot_is_current():
    """Read-only mainnet check that the hardcoded META snapshot (szDecimals,
    maxLeverage) is still accurate. Skips without network."""
    import json
    import urllib.request
    try:
        req = urllib.request.Request(
            "https://api.hyperliquid.xyz/info",
            data=json.dumps({"type": "meta"}).encode(),
            headers={"Content-Type": "application/json"})
        meta = json.loads(urllib.request.urlopen(req, timeout=15).read())
    except Exception as e:                      # pragma: no cover
        pytest.skip(f"no network: {e!r}")
    venue = {a["name"]: a for a in meta["universe"]}
    for coin, (szd, lev, _px) in META.items():
        assert coin in venue, f"{coin} missing on mainnet"
        assert venue[coin]["szDecimals"] == szd, coin
        assert venue[coin]["maxLeverage"] == lev, (
            f"{coin} maxLeverage changed: snapshot {lev} vs "
            f"venue {venue[coin]['maxLeverage']} — re-run the margin audit")
        assert not venue[coin].get("isDelisted", False), coin
