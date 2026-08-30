import sys
import types
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

# Stub the SDK modules so state-machine tests don't require the real package.
for name in [
    "webull", "webull.core", "webull.core.client", "webull.trade",
    "webull.trade.trade_client", "webull.data", "webull.data.data_client",
    "webull.data.common", "webull.data.common.category", "webull.data.common.timespan",
]:
    sys.modules.setdefault(name, types.ModuleType(name))
sys.modules["webull.core.client"].ApiClient = object
sys.modules["webull.trade.trade_client"].TradeClient = object
sys.modules["webull.data.data_client"].DataClient = object
sys.modules["webull.data.common.category"].Category = types.SimpleNamespace(
    US_STOCK=types.SimpleNamespace(name="US_STOCK"),
    US_OPTION=types.SimpleNamespace(name="US_OPTION"),
)
sys.modules["webull.data.common.timespan"].Timespan = types.SimpleNamespace(M5=types.SimpleNamespace(name="M5"))

import worker


def position_payload(qty=1, cost="2.00", strike="600", expiration="2026-08-28", typ="CALL"):
    return [{
        "instrument_type": "OPTION",
        "quantity": str(qty),
        "symbol": "SPY",
        "cost_price": cost,
        "legs": [{
            "instrument_type": "OPTION",
            "option_type": typ,
            "option_exercise_price": strike,
            "option_expire_date": expiration,
        }],
    }]


def base_state(state="PENDING_ENTRY"):
    return {
        "state": state,
        "position": {
            "side": "CALL",
            "symbol": "SPY260828C00600000",
            "contract": {"symbol": "SPY260828C00600000", "strike_price": "600", "expiration_date": "2026-08-28"},
            "quantity": 0 if state == "PENDING_ENTRY" else 1,
            "entry_underlying": 600,
            "entry_atr": 1,
            "entry_premium": 2.0,
            "entry_time": datetime.now(timezone.utc).isoformat(),
        },
        "entry_order": {"client_order_id": "ENTRY1"} if state == "PENDING_ENTRY" else None,
        "exit_order": {"client_order_id": "EXIT1", "submitted_at": datetime.now(timezone.utc).isoformat()} if state == "PENDING_EXIT" else None,
        "last_trade": 0,
    }


class WorkerStateMachineTests(unittest.TestCase):
    def test_successful_entry_becomes_open_only_with_position(self):
        state = base_state()
        with patch.object(worker.wb, "order_detail", return_value={"status": "FILLED", "status_class": "FILLED", "filled_qty": 1, "filled_price": 2.25, "total_qty": 1, "order_id": "OID"}), \
             patch.object(worker.wb, "positions", return_value={"success": True, "positions": position_payload()}):
            state, changed = worker.entry_fill_state(object(), state)
        self.assertTrue(changed)
        self.assertEqual(state["state"], "OPEN")
        self.assertEqual(state["position"]["quantity"], 1)
        self.assertEqual(state["position"]["entry_premium"], 2.25)

    def test_rejected_entry_returns_flat(self):
        state = base_state()
        with patch.object(worker.wb, "order_detail", return_value={"status": "FAILED", "status_class": "REJECTED", "filled_qty": 0}), \
             patch.object(worker.wb, "positions", return_value={"success": True, "positions": []}):
            state, _ = worker.entry_fill_state(object(), state)
        self.assertEqual(state["state"], "FLAT")
        self.assertIsNone(state["position"])

    def test_unfilled_entry_stays_pending(self):
        state = base_state()
        with patch.object(worker.wb, "order_detail", return_value={"status": "SUBMITTED", "status_class": "PENDING", "filled_qty": 0}), \
             patch.object(worker.wb, "positions", return_value={"success": True, "positions": []}):
            state, _ = worker.entry_fill_state(object(), state)
        self.assertEqual(state["state"], "PENDING_ENTRY")

    def test_partial_entry_tracks_actual_quantity(self):
        state = base_state()
        with patch.object(worker.wb, "order_detail", return_value={"status": "PARTIAL_FILLED", "status_class": "PARTIAL_FILLED", "filled_qty": 1, "filled_price": 2.10, "total_qty": 2, "order_id": "OID"}), \
             patch.object(worker.wb, "positions", return_value={"success": True, "positions": position_payload(qty=1, cost="2.10")} ), \
             patch.object(worker.wb, "cancel_order", return_value={"success": True}):
            state, _ = worker.entry_fill_state(object(), state)
        self.assertEqual(state["state"], "OPEN")
        self.assertEqual(state["position"]["quantity"], 1)

    def test_filled_order_without_position_is_not_assumed_filled(self):
        state = base_state()
        with patch.object(worker.wb, "order_detail", return_value={"status": "FILLED", "status_class": "FILLED", "filled_qty": 1, "filled_price": 2.10, "total_qty": 1}), \
             patch.object(worker.wb, "positions", return_value={"success": True, "positions": []}):
            state, _ = worker.entry_fill_state(object(), state)
        self.assertEqual(state["state"], "PENDING_ENTRY")

    def test_rejected_exit_with_position_returns_open(self):
        state = base_state("PENDING_EXIT")
        with patch.object(worker.wb, "order_detail", return_value={"status": "FAILED", "status_class": "REJECTED", "filled_qty": 0}), \
             patch.object(worker.wb, "positions", return_value={"success": True, "positions": position_payload(qty=1)}):
            state, _ = worker.reconcile_exit(object(), state)
        self.assertEqual(state["state"], "OPEN")
        self.assertEqual(state["position"]["quantity"], 1)

    def test_partial_exit_keeps_remaining_position_open_pending(self):
        state = base_state("PENDING_EXIT")
        with patch.object(worker.wb, "order_detail", return_value={"status": "PARTIAL_FILLED", "status_class": "PARTIAL_FILLED", "filled_qty": 1, "filled_price": 2.50, "total_qty": 2}), \
             patch.object(worker.wb, "positions", return_value={"success": True, "positions": position_payload(qty=1)}):
            state, _ = worker.reconcile_exit(object(), state)
        self.assertEqual(state["state"], "PENDING_EXIT")
        self.assertEqual(state["position"]["quantity"], 1)

    def test_position_disappearing_after_exit_closes(self):
        state = base_state("PENDING_EXIT")
        with patch.object(worker.wb, "order_detail", return_value={"status": "FILLED", "status_class": "FILLED", "filled_qty": 1, "filled_price": 2.50, "total_qty": 1}), \
             patch.object(worker.wb, "positions", return_value={"success": True, "positions": []}):
            state, _ = worker.reconcile_exit(object(), state)
        self.assertEqual(state["state"], "FLAT")
        self.assertIsNone(state["position"])

    def test_restart_recovers_open_position(self):
        state = base_state("FLAT")
        state["position"] = None
        with patch.object(worker.wb, "positions", return_value={"success": True, "positions": position_payload(qty=1, cost="2.10")}):
            state = worker.recover_from_webull(object(), state)
        self.assertEqual(state["state"], "OPEN")
        self.assertEqual(state["position"]["quantity"], 1)
        self.assertEqual(state["position"]["entry_premium"], 2.10)

    def test_duplicate_signal_cannot_enter_when_open(self):
        state = base_state("OPEN")
        snapshot = {"signal": "PUT", "bar_time": "new", "close": 600, "atr": 1}
        with patch.object(worker.wb, "choose") as choose:
            out = worker.maybe_enter(object(), object(), state, snapshot)
        self.assertIs(out, state)
        choose.assert_not_called()

    def test_eod_forces_exit_reason(self):
        state = base_state("OPEN")
        pos = state["position"]
        snapshot = {"close": 600, "atr": 1, "upper": None, "lower": None}
        # Directly test the same rule at 15:55 New York time.
        from zoneinfo import ZoneInfo
        dt = datetime(2026, 8, 28, 15, 55, tzinfo=ZoneInfo("America/New_York"))
        self.assertEqual(worker.risk_reason(pos, snapshot, {"bid": 2.0}, dt), "FORCED_EOD_LIQUIDATION")

    def test_dry_run_exit_does_not_submit_real_order(self):
        state = base_state("OPEN")
        old = worker.config.DRY_RUN
        worker.config.DRY_RUN = True
        try:
            with patch.object(worker.wb, "positions", return_value={"success": True, "positions": position_payload(qty=1)}), \
                 patch.object(worker.wb, "option_quote", return_value={"bid": 2.00, "ask": 2.05, "premium": 2.02}), \
                 patch.object(worker.wb, "exit_order", return_value={"success": True, "dry_run": True}) as exit_order:
                state = worker.submit_exit(object(), object(), state, "TEST_EXIT")
            self.assertEqual(state["state"], "FLAT")
            exit_order.assert_called_once()
        finally:
            worker.config.DRY_RUN = old

if __name__ == "__main__":
    unittest.main()
