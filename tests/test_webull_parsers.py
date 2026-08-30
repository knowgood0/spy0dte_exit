import sys
import types
import unittest

# Make parser/state tests runnable without an installed Webull SDK. The real
# integration still imports the actual SDK in production.

def install_sdk_stubs():
    names = [
        "webull",
        "webull.core",
        "webull.core.client",
        "webull.trade",
        "webull.trade.trade_client",
        "webull.data",
        "webull.data.data_client",
        "webull.data.common",
        "webull.data.common.category",
        "webull.data.common.timespan",
    ]
    for name in names:
        sys.modules.setdefault(name, types.ModuleType(name))
    sys.modules["webull.core.client"].ApiClient = object
    sys.modules["webull.trade.trade_client"].TradeClient = object
    sys.modules["webull.data.data_client"].DataClient = object
    sys.modules["webull.data.common.category"].Category = types.SimpleNamespace(US_STOCK=types.SimpleNamespace(name="US_STOCK"), US_OPTION=types.SimpleNamespace(name="US_OPTION"))
    sys.modules["webull.data.common.timespan"].Timespan = types.SimpleNamespace(M5=types.SimpleNamespace(name="M5"))

install_sdk_stubs()

from webull_client import classify_order_status, normalize_order_detail, find_matching_option_position


class WebullParserTests(unittest.TestCase):
    def test_statuses(self):
        self.assertEqual(classify_order_status("SUBMITTED"), "PENDING")
        self.assertEqual(classify_order_status("PARTIAL_FILLED"), "PARTIAL_FILLED")
        self.assertEqual(classify_order_status("FILLED"), "FILLED")
        self.assertEqual(classify_order_status("FAILED"), "REJECTED")
        self.assertEqual(classify_order_status("CANCELLED"), "CANCELED")

    def test_fill_fields_are_never_invented(self):
        result = normalize_order_detail({
            "order_status": "PARTIAL_FILLED",
            "qty": "2",
            "filled_qty": "1",
            "filled_price": "2.35",
            "order_id": "OID",
            "client_order_id": "CID",
        })
        self.assertEqual(result["filled_qty"], 1.0)
        self.assertEqual(result["filled_price"], 2.35)
        self.assertEqual(result["total_qty"], 2.0)

    def test_option_position_match(self):
        payload = [{
            "instrument_type": "OPTION",
            "quantity": "1",
            "symbol": "SPY",
            "cost_price": "2.10",
            "legs": [{
                "instrument_type": "OPTION",
                "option_type": "CALL",
                "option_exercise_price": "600",
                "option_expire_date": "2026-08-28",
            }],
        }]
        contract = {
            "symbol": "SPY260828C00600000",
            "strike_price": "600",
            "expiration_date": "2026-08-28",
        }
        result = find_matching_option_position(payload, contract, "CALL")
        self.assertEqual(result["quantity"], 1.0)
        self.assertEqual(result["cost_price"], 2.10)

    def test_ambiguous_positions_are_not_guessed(self):
        raw = {
            "instrument_type": "OPTION",
            "quantity": "1",
            "symbol": "SPY",
            "legs": [{
                "instrument_type": "OPTION",
                "option_type": "CALL",
                "option_exercise_price": "600",
                "option_expire_date": "2026-08-28",
            }],
        }
        result = find_matching_option_position([raw, dict(raw)], {"strike_price": "600", "expiration_date": "2026-08-28"}, "CALL")
        self.assertTrue(result["ambiguous"])


if __name__ == "__main__":
    unittest.main()
