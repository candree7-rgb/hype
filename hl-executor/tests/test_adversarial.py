"""Adversarial tests: races, stuck state machines, error paths.

Each test here documents a real failure scenario found in review; the
matching minimal fix lives in live.py / main.py. Run with test_live.py:
    python3 -m pytest tests/ -q
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import CFG                                    # noqa: E402
from live import LiveBroker                               # noqa: E402
import main as main_mod                                   # noqa: E402
from paper import PaperBroker                             # noqa: E402

from test_live import (                                   # noqa: E402
    MockExchange, MockInfo, ok_statuses, fill, sig, candle, T0,
)


@pytest.fixture()
def broker(tmp_path, monkeypatch):
    monkeypatch.setattr(CFG, "state_file", str(tmp_path / "state.json"))
    monkeypatch.setattr(CFG, "live_adopt", "close")
    ex, info = MockExchange(), MockInfo()
    b = LiveBroker(exchange=ex, info=info, address="0x" + "ab" * 20)
    b.day_key = time.strftime("%Y-%m-%d", time.gmtime(T0))
    ex.calls.clear()
    return b


def open_position(b):
    """Standard long ETH 2.0 @ 2000, exits resting at oids 2 (tp) / 3 (sl)."""
    b.place(sig(), 4000.0)
    b.on_user_fills({"isSnapshot": False, "user": "0x",
                     "fills": [fill(1, 2000.0, 2.0, tid=101)]})
    return b.positions["ETH"]


# ------------------------------------------------- BUG 1: stuck `closing`
def test_time_stop_ioc_reject_does_not_strand_position(broker):
    """market_close returns an error status (IOC could not match). Exits were
    already cancelled. Pre-fix, pos.closing stayed True forever and BOTH the
    candle tick and reconcile skip closing positions -> a live position with
    no SL, no TP, and no retry path. Post-fix the close is retried."""
    pos = open_position(broker)
    broker.exchange.close_responses.append(
        ok_statuses({"error": "Order could not immediately match against any "
                              "resting orders"}))
    broker.on_closed_candle("ETH", candle(pos.max_hold_until))
    assert len(broker.exchange.named("market_close")) == 1
    assert not pos.closing          # must remain eligible for retry
    # next candle tick retries the close
    broker.on_closed_candle("ETH", candle(pos.max_hold_until + 60))
    assert len(broker.exchange.named("market_close")) == 2


def test_partial_ioc_close_remainder_reclosed_by_reconcile(broker):
    """market_close IOC partially fills (slippage cap). Remainder stays open
    with closing=True and zero exit orders. Pre-fix, nothing ever touched it
    again. Post-fix reconcile re-fires the reduce-only close."""
    pos = open_position(broker)
    broker.on_closed_candle("ETH", candle(pos.max_hold_until))     # close, oid 99
    broker.on_user_fills({"isSnapshot": False, "user": "0x", "fills": [
        dict(fill(99, 2001.0, 1.0, tid=201, pnl="1", fee="0.9"),
             dir="Close Long")]})
    assert "ETH" in broker.positions                     # remainder open
    broker.info.positions = [{"position": {"coin": "ETH", "szi": "1.0"}}]
    broker.info.orders = []
    n = len(broker.exchange.named("market_close"))
    broker.reconcile()
    assert len(broker.exchange.named("market_close")) == n + 1


# ------------------------------------- BUG 2: fill racing a cancel loses pos
def test_entry_fill_after_ttl_cancel_is_still_managed(broker):
    """TTL tick sends the cancel, but the entry had already filled on the
    venue; the fill event arrives just after. Pre-fix the entry record was
    popped, the fill hit 'entry_fill_untracked' and a REAL position sat on
    the venue with no SL (adopt policy: forever)."""
    broker.place(sig(), 4000.0)
    broker.on_closed_candle("ETH", candle(T0 + CFG.ttl_min * 60))  # cancel sent
    broker.on_user_fills({"isSnapshot": False, "user": "0x",
                          "fills": [fill(1, 2000.0, 2.0, tid=210)]})
    assert "ETH" in broker.positions
    pos = broker.positions["ETH"]
    assert pos.sl_oid is not None and pos.tp_oid is not None   # protected
    assert broker.exchange.named("bulk_orders")


def test_entry_fill_after_venue_cancel_update_is_still_managed(broker):
    """Same race via the orderUpdates channel ('canceled' seen before the
    partial fill event that actually preceded it)."""
    broker.place(sig(), 4000.0)
    broker.on_order_updates([{"order": {"oid": 1, "coin": "ETH"},
                              "status": "canceled"}])
    broker.on_user_fills({"isSnapshot": False, "user": "0x",
                          "fills": [fill(1, 2000.0, 2.0, tid=211)]})
    assert "ETH" in broker.positions
    assert broker.positions["ETH"].sl_oid is not None


def test_entry_gone_in_reconcile_then_fill_is_still_managed(broker):
    """Reconcile sees the entry missing from open orders (it filled between
    the fills fetch and the open-orders fetch). The fill then arrives."""
    broker.place(sig(), 4000.0)
    broker.info.orders = []
    broker.info.positions = []
    broker.reconcile()
    broker.on_user_fills({"isSnapshot": False, "user": "0x",
                          "fills": [fill(1, 2000.0, 2.0, tid=212)]})
    assert "ETH" in broker.positions
    assert broker.positions["ETH"].sl_oid is not None


def test_stale_cancelled_entries_are_pruned(broker):
    """The race-grace records must not accumulate forever."""
    now = int(time.time())
    broker.place(sig(t=now - 3600), 4000.0)
    broker.on_closed_candle("ETH", candle(now - 3600 + CFG.ttl_min * 60))
    assert broker.entries                    # kept for the race grace window
    broker.reconcile()
    assert broker.entries == {}              # pruned: >15min past expiry


# --------------------------------------- BUG 3: network error during place()
def test_place_survives_order_exception(broker, monkeypatch):
    """exchange.order raising (timeout) must not propagate into the candle
    pipeline (pre-fix it tore down the shared websocket for all coins)."""
    def boom(*a, **k):
        raise TimeoutError("gateway timeout")
    monkeypatch.setattr(broker.exchange, "order", boom)
    broker.place(sig(), 4000.0)              # must not raise
    assert broker.entries == {}
    assert broker.snapshot()["pending"] == 0


# ------------------------------------------- BUG 4: non-atomic state writes
def test_save_state_is_atomic_on_crash_mid_write(broker, monkeypatch):
    """A crash mid-write must never corrupt live_state.json: a corrupt file
    is silently discarded on boot and every open position then looks
    'untracked' -> force-closed at market (close) or unmanaged (adopt)."""
    open_position(broker)
    broker.save_state()
    good = broker.state_path.read_text()
    orig = Path.write_text

    def torn_write(self, text, *a, **kw):     # simulate crash mid-write
        orig(self, text[: len(text) // 2])
        raise OSError("simulated crash mid-write")
    monkeypatch.setattr(Path, "write_text", torn_write)
    with pytest.raises(OSError):
        broker.save_state()
    monkeypatch.setattr(Path, "write_text", orig)
    data = json.loads(broker.state_path.read_text())     # still valid JSON
    assert data == json.loads(good)
    assert "ETH" in data["positions"]


# --------------------------------- BUG 5: seen_tids trim drops RECENT tids
def test_seen_tids_trim_keeps_most_recent(broker):
    """Trimming via an unordered set can evict just-seen tids; reconcile's
    watermark overlap then re-ingests the same fill -> double-counted size.
    The structure must preserve insertion order."""
    pos = open_position(broker)
    seed = range(1, 20_001)
    broker.seen_tids = (set(seed) if isinstance(broker.seen_tids, set)
                        else dict.fromkeys(seed))
    new_tid = (1 << 20) * 19073              # slot-engineered: first set slot
    broker.on_user_fills({"isSnapshot": False, "user": "0x",
                          "fills": [fill(1, 2000.0, 0.5, tid=new_tid)]})
    assert pos.sz == 2.5
    assert len(broker.seen_tids) <= 5001
    assert new_tid in broker.seen_tids       # most recent tid must survive
    # replayed by reconcile's overlap window -> must dedup, not double-count
    broker.on_user_fills({"isSnapshot": False, "user": "0x",
                          "fills": [fill(1, 2000.0, 0.5, tid=new_tid)]})
    assert pos.sz == 2.5


# --------------------------- BUG 6: unlocked cross-thread read in main._size
def test_size_acquires_broker_lock_when_present(broker):
    """reconcile() mutates broker.positions from a worker thread; main._size
    iterates it from the event loop. Without taking the broker lock this can
    raise 'dictionary changed size during iteration' mid-candle."""
    class RecLock:
        def __init__(self):
            self.entered = 0

        def __enter__(self):
            self.entered += 1

        def __exit__(self, *a):
            return False
    rec = RecLock()
    ex = main_mod.Executor.__new__(main_mod.Executor)
    broker._lock = rec
    ex.broker = broker
    ex._size("BTC", 0.01)
    assert rec.entered == 1


def test_size_unchanged_for_paper(tmp_path, monkeypatch):
    monkeypatch.setattr(CFG, "state_file", str(tmp_path / "state.json"))
    ex = main_mod.Executor.__new__(main_mod.Executor)
    ex.broker = PaperBroker()
    assert ex._size("BTC", 0.01) == pytest.approx(
        CFG.equity_start_paper * CFG.risk_per_trade / 0.01)
