"""Webull Sandbox client for the SPY 0DTE paper bot.

REST is used for account, historical-bar, contract, position, and order
operations.  Live market data is supplied by Webull's Sandbox MQTT streaming
API.

The strategy uses historical 5-minute bars only to warm up indicators.
The live SPY tick stream owns the current forming 5-minute bar.
Live option QUOTE data supplies execution/risk bid/ask prices.

The module deliberately treats Webull's external order/position state as
authoritative.  A successful place_order() response means only that Webull
accepted the request; it is never treated as a fill.
"""

from __future__ import annotations

import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from webull.core.client import ApiClient
from webull.trade.trade_client import TradeClient
from webull.data.data_client import DataClient
from webull.data.data_streaming_client import DataStreamingClient
from webull.data.common.category import Category
from webull.data.common.timespan import Timespan
from webull.data.common.subscribe_type import SubscribeType

import config


REST_ENDPOINT = "api.sandbox.webull.com"
STREAM_HTTP_ENDPOINT = "api.sandbox.webull.com"
STREAM_MQTT_ENDPOINT = "data-api.sandbox.webull.com"

_last_request = 0.0


def call(fn, *args, **kwargs):
    """Serialize REST requests and retry only explicit rate-limit failures."""
    global _last_request

    last_error = None

    for attempt in range(3):
        wait = (
            config.WEBULL_MIN_REQUEST_INTERVAL
            - (time.monotonic() - _last_request)
        )

        if wait > 0:
            time.sleep(wait)

        _last_request = time.monotonic()

        try:
            return fn(*args, **kwargs)

        except Exception as exc:
            last_error = exc
            text = str(exc)

            if (
                "429" not in text
                and "TOO_MANY_REQUESTS" not in text
            ):
                raise

            time.sleep(2 * (attempt + 1))

    raise last_error


def clients():
    api = ApiClient(
        config.APP_KEY,
        config.APP_SECRET,
        config.REGION,
    )

    api.add_endpoint(
        config.REGION,
        REST_ENDPOINT,
    )

    return (
        TradeClient(api),
        DataClient(api),
    )


def connect():
    if (
        not config.APP_KEY
        or not config.APP_SECRET
        or not config.ACCOUNT_ID
    ):
        raise RuntimeError(
            "Missing WEBULL_APP_KEY/WEBULL_APP_SECRET/WEBULL_ACCOUNT_ID"
        )

    trade, data = clients()

    response = call(
        trade.account_v2.get_account_list
    )

    if response.status_code >= 300:
        raise RuntimeError(
            f"Webull account list HTTP "
            f"{response.status_code}: {response.text}"
        )

    accounts = response.json()

    account_ids = _extract_account_ids(
        accounts
    )

    if (
        account_ids
        and config.ACCOUNT_ID not in account_ids
    ):
        raise RuntimeError(
            "Configured WEBULL_ACCOUNT_ID was not "
            "returned by Webull Sandbox account list"
        )

    return trade, data


def _extract_account_ids(payload):
    items = (
        payload
        if isinstance(payload, list)
        else []
    )

    if isinstance(payload, dict):
        for key in (
            "account",
            "accounts",
            "data",
            "items",
        ):
            if isinstance(
                payload.get(key),
                list,
            ):
                items = payload[key]
                break

    return [
        x.get("account_id")
        for x in items
        if (
            isinstance(x, dict)
            and x.get("account_id")
        )
    ]


# ============================================================
# HISTORICAL DATA
# ============================================================

def bars(data, count):
    response = call(
        data.market_data.get_history_bar,
        config.SYMBOL,
        Category.US_STOCK.name,
        Timespan.M5.name,
        count,
    )

    if response.status_code >= 300:
        raise RuntimeError(
            f"Historical bars HTTP "
            f"{response.status_code}: {response.text}"
        )

    payload = response.json()

    payload = _first_list(
        payload,
        (
            "data",
            "items",
            "bars",
            "list",
        ),
    )

    out = []

    for item in payload:
        try:
            ts = item.get(
                "time",
                item.get(
                    "timestamp",
                    item.get(
                        "datetime",
                        item.get("date"),
                    ),
                ),
            )

            if isinstance(
                ts,
                (int, float),
            ):
                dt = datetime.fromtimestamp(
                    ts / 1000
                    if ts > 1e10
                    else ts,
                    tz=timezone.utc,
                )

            else:
                dt = datetime.fromisoformat(
                    str(ts).replace(
                        "Z",
                        "+00:00",
                    )
                )

                if dt.tzinfo is None:
                    dt = dt.replace(
                        tzinfo=timezone.utc
                    )

            out.append({
                "timestamp": dt,
                "open": float(item["open"]),
                "high": float(item["high"]),
                "low": float(item["low"]),
                "close": float(item["close"]),
                "volume": float(
                    item.get(
                        "volume",
                        0,
                    )
                    or 0
                ),
            })

        except (
            TypeError,
            ValueError,
            KeyError,
        ):
            continue

    out.sort(
        key=lambda x: x["timestamp"]
    )

    return out


# ============================================================
# OPTION CONTRACTS
# ============================================================

def contracts(data, option_type):
    today = datetime.now(
        timezone.utc
    ).strftime("%Y-%m-%d")

    response = call(
        data.instrument.get_option_contracts,
        category=Category.US_OPTION.name,
        underlying_symbols=config.SYMBOL,
        status="LISTING",
        start_date=today,
        end_date=today,
        option_type=option_type,
        style="AMERICAN",
        page_size=1000,
    )

    if response.status_code >= 300:
        raise RuntimeError(
            f"Option contracts HTTP "
            f"{response.status_code}: {response.text}"
        )

    return _first_list(
        response.json(),
        (
            "data",
            "items",
            "contracts",
        ),
    )


def choose(
    data,
    option_type,
    spy_price,
):
    today = datetime.now(
        timezone.utc
    ).strftime("%Y-%m-%d")

    option_type = str(
        option_type
    ).upper()

    candidates = []

    for contract in contracts(
        data,
        option_type,
    ):
        try:
            expiration = str(
                contract.get(
                    "expiration_date",
                    contract.get(
                        "expiration",
                        "",
                    ),
                )
            )[:10]

            typ = str(
                contract.get(
                    "option_type",
                    "",
                )
            ).upper()

            if (
                expiration == today
                and typ == option_type
            ):
                candidates.append(
                    contract
                )

        except (
            TypeError,
            ValueError,
        ):
            continue

    if not candidates:
        raise RuntimeError(
            f"No 0DTE {option_type} contracts available"
        )

    return min(
        candidates,
        key=lambda c: abs(
            float(
                c["strike_price"]
            )
            - spy_price
        ),
    )


# ============================================================
# REST SNAPSHOTS
# ============================================================

def option_quote(
    data,
    symbol,
):
    response = call(
        data.option_market_data.get_option_snapshot,
        symbol,
        Category.US_OPTION.name,
    )

    if response.status_code >= 300:
        raise RuntimeError(
            f"Option snapshot HTTP "
            f"{response.status_code}: {response.text}"
        )

    payload = response.json()

    item = (
        payload[0]
        if (
            isinstance(payload, list)
            and payload
        )
        else payload
    )

    def number(key):
        try:
            value = (
                item.get(key)
                if isinstance(
                    item,
                    dict,
                )
                else None
            )

            return (
                float(value)
                if value is not None
                else None
            )

        except (
            TypeError,
            ValueError,
        ):
            return None

    return {
        "bid": number("bid"),
        "ask": number("ask"),
        "premium": (
            number("price")
            or number("last_price")
            or number("mark_price")
        ),
        "raw": payload,
    }


def spy_snapshot(data):
    response = call(
        data.market_data.get_snapshot,
        config.SYMBOL,
        Category.US_STOCK.name,
    )

    if response.status_code >= 300:
        raise RuntimeError(
            f"SPY snapshot HTTP "
            f"{response.status_code}: {response.text}"
        )

    payload = response.json()

    item = (
        payload[0]
        if (
            isinstance(payload, list)
            and payload
        )
        else payload
    )

    price = None

    if isinstance(
        item,
        dict,
    ):
        for key in (
            "price",
            "last_price",
            "last",
        ):
            try:
                if item.get(key) is not None:
                    price = float(
                        item[key]
                    )
                    break

            except (
                TypeError,
                ValueError,
            ):
                pass

    return {
        "price": price,
        "raw": payload,
    }


# ============================================================
# LIVE STREAMING
# ============================================================

class LiveMarketStream:
    """Thread-safe cache for Webull Sandbox streaming data."""

    def __init__(self):
        self._lock = threading.RLock()

        self.client = None
        self.connected = False

        self.last_message_time = None

        self.stock_ticks = {}
        self.stock_quotes = {}
        self.option_quotes = {}

        self.subscribed_stocks = set()
        self.subscribed_options = set()

        self.subscribe_lock = threading.Lock()

    def start(self):
        session_id = (
            "SPY0DTE_"
            + uuid.uuid4().hex[:24]
        )

        self.client = DataStreamingClient(
            config.APP_KEY,
            config.APP_SECRET,
            config.REGION,
            session_id,
            http_host=STREAM_HTTP_ENDPOINT,
            mqtt_host=STREAM_MQTT_ENDPOINT,
        )

        self.client.on_quotes_message = (
            self._on_message
        )

        self.client.on_quotes_subscribe = (
            self._on_subscribe
        )

        self.client.connect_and_loop_async(
            timeout=1,
            thread_daemon=True,
            logger_enable=True,
        )

        # The SDK starts the MQTT connection asynchronously.
        time.sleep(2.0)

        self.connected = True

        self.subscribe_stock(
            config.SYMBOL
        )

        return session_id

    def _on_subscribe(
        self,
        client,
        api_client,
        session_id,
    ):
        self.connected = True

    def _on_message(
        self,
        client,
        payload_type,
        result,
    ):
        try:
            basic = result.get_basic()

            symbol = basic.get_symbol()

        except Exception:
            return

        now = time.monotonic()

        with self._lock:
            self.last_message_time = now

            if (
                symbol == config.SYMBOL
            ):
                self._store_stock(
                    payload_type,
                    result,
                )

            else:
                self._store_option(
                    symbol,
                    payload_type,
                    result,
                )

    def _store_stock(
        self,
        payload_type,
        result,
    ):
        if payload_type == "tick":
            price = _result_price(
                result
            )

            volume = _result_volume(
                result
            )

            timestamp = _result_timestamp(
                result
            )

            if price is not None:
                self.stock_ticks[
                    config.SYMBOL
                ] = {
                    "price": price,
                    "volume": volume or 0.0,
                    "timestamp": timestamp,
                    "received_monotonic": time.monotonic(),
                }

        elif payload_type in (
            "quote",
            "snapshot",
        ):
            self.stock_quotes[
                config.SYMBOL
            ] = {
                "bid": _best_bid(result),
                "ask": _best_ask(result),
                "price": _result_price(result),
                "timestamp": _result_timestamp(result),
                "received_monotonic": time.monotonic(),
            }

    def _store_option(
        self,
        symbol,
        payload_type,
        result,
    ):
        if payload_type == "quote":
            bid = _best_bid(result)
            ask = _best_ask(result)

            if (
                bid is not None
                or ask is not None
            ):
                self.option_quotes[
                    symbol
                ] = {
                    "bid": bid,
                    "ask": ask,
                    "premium": _result_price(
                        result
                    ),
                    "timestamp": _result_timestamp(
                        result
                    ),
                    "received_monotonic": time.monotonic(),
                }

        elif payload_type == "snapshot":
            existing = self.option_quotes.get(
                symbol,
                {},
            )

            self.option_quotes[
                symbol
            ] = {
                "bid": existing.get(
                    "bid"
                ),
                "ask": existing.get(
                    "ask"
                ),
                "premium": _result_price(
                    result
                ),
                "timestamp": _result_timestamp(
                    result
                ),
                "received_monotonic": time.monotonic(),
            }

    def subscribe_stock(
        self,
        symbol,
    ):
        symbol = str(symbol)

        with self.subscribe_lock:
            if symbol in self.subscribed_stocks:
                return

            if not self.client:
                raise RuntimeError(
                    "Live market stream is not started"
                )

            self.client.subscribe(
                [symbol],
                Category.US_STOCK.name,
                [
                    SubscribeType.QUOTE.name,
                    SubscribeType.TICK.name,
                ],
            )

            self.subscribed_stocks.add(
                symbol
            )

    def subscribe_option(
        self,
        symbol,
    ):
        symbol = str(symbol)

        with self.subscribe_lock:
            if symbol in self.subscribed_options:
                return

            if not self.client:
                raise RuntimeError(
                    "Live market stream is not started"
                )

            self.client.subscribe(
                [symbol],
                Category.US_OPTION.name,
                [
                    SubscribeType.QUOTE.name,
                    SubscribeType.TICK.name,
                ],
            )

            self.subscribed_options.add(
                symbol
            )

    def stock_tick(
        self,
        symbol=None,
    ):
        symbol = symbol or config.SYMBOL

        with self._lock:
            value = self.stock_ticks.get(
                symbol
            )

            return (
                dict(value)
                if value
                else None
            )

    def option_quote_live(
        self,
        symbol,
        max_age=5.0,
    ):
        with self._lock:
            value = self.option_quotes.get(
                symbol
            )

            if not value:
                return None

            age = (
                time.monotonic()
                - value.get(
                    "received_monotonic",
                    0,
                )
            )

            if age > max_age:
                return None

            return dict(value)

    def wait_for_stock_tick(
        self,
        timeout=10.0,
    ):
        deadline = (
            time.monotonic()
            + timeout
        )

        while time.monotonic() < deadline:
            tick = self.stock_tick()

            if tick:
                return tick

            time.sleep(0.05)

        return None

    def wait_for_option_quote(
        self,
        symbol,
        timeout=5.0,
    ):
        deadline = (
            time.monotonic()
            + timeout
        )

        while time.monotonic() < deadline:
            quote = self.option_quote_live(
                symbol
            )

            if quote:
                return quote

            time.sleep(0.05)

        return None


# ============================================================
# STREAM RESULT HELPERS
# ============================================================

def _result_price(result):
    for name in (
        "get_price",
        "price",
    ):
        try:
            value = getattr(
                result,
                name,
            )

            if callable(value):
                value = value()

            if value is not None:
                return float(value)

        except (
            AttributeError,
            TypeError,
            ValueError,
        ):
            pass

    return None


def _result_volume(result):
    for name in (
        "get_volume",
        "volume",
    ):
        try:
            value = getattr(
                result,
                name,
            )

            if callable(value):
                value = value()

            if value is not None:
                return float(value)

        except (
            AttributeError,
            TypeError,
            ValueError,
        ):
            pass

    return 0.0


def _result_timestamp(result):
    try:
        basic = result.get_basic()

        value = getattr(
            basic,
            "timestamp",
            None,
        )

        if value:
            return datetime.fromtimestamp(
                float(value) / 1000.0,
                tz=timezone.utc,
            )

    except Exception:
        pass

    return datetime.now(
        timezone.utc
    )


def _quote_side_price(item):
    for name in (
        "price",
        "get_price",
    ):
        try:
            value = getattr(
                item,
                name,
            )

            if callable(value):
                value = value()

            if value is not None:
                return float(value)

        except (
            AttributeError,
            TypeError,
            ValueError,
        ):
            pass

    try:
        value = item.__dict__.get(
            "price"
        )

        if value is not None:
            return float(value)

    except Exception:
        pass

    return None


def _best_bid(result):
    try:
        bids = result.get_bids()

        prices = [
            p
            for p in (
                _quote_side_price(x)
                for x in bids
            )
            if p is not None
            and p > 0
        ]

        return max(prices) if prices else None

    except Exception:
        return None


def _best_ask(result):
    try:
        asks = result.get_asks()

        prices = [
            p
            for p in (
                _quote_side_price(x)
                for x in asks
            )
            if p is not None
            and p > 0
        ]

        return min(prices) if prices else None

    except Exception:
        return None


# ============================================================
# REST HELPERS
# ============================================================

def _first_list(
    payload,
    keys,
):
    if isinstance(
        payload,
        list,
    ):
        return payload

    if isinstance(
        payload,
        dict,
    ):
        for key in keys:
            if isinstance(
                payload.get(key),
                list,
            ):
                return payload[key]

    return []


def _first_dict(payload):
    if isinstance(
        payload,
        dict,
    ):
        return payload

    if (
        isinstance(payload, list)
        and payload
        and isinstance(
            payload[0],
            dict,
        )
    ):
        return payload[0]

    return {}


def _nested_value(
    payload,
    keys,
):
    """Read a known field from common Webull response nesting."""

    if not isinstance(
        payload,
        dict,
    ):
        return None

    for key in keys:
        if payload.get(key) not in (
            None,
            "",
        ):
            return payload[key]

    # Webull order-detail responses have been
    # observed with order data under orders[0].
    orders = payload.get("orders")

    if (
        isinstance(orders, list)
        and orders
        and isinstance(
            orders[0],
            dict,
        )
    ):
        value = _nested_value(
            orders[0],
            keys,
        )

        if value not in (
            None,
            "",
        ):
            return value

    for parent in (
        "order",
        "data",
        "result",
        "payload",
    ):
        nested = payload.get(
            parent
        )

        if isinstance(
            nested,
            dict,
        ):
            value = _nested_value(
                nested,
                keys,
            )

            if value not in (
                None,
                "",
            ):
                return value

    return None


def _float_or_none(value):
    try:
        if value in (
            None,
            "",
        ):
            return None

        return float(value)

    except (
        TypeError,
        ValueError,
    ):
        return None


def normalize_order_detail(
    payload,
):
    """Normalize Webull order-detail fields."""

    status = _nested_value(
        payload,
        (
            "order_status",
            "status",
            "state",
        ),
    )

    filled_qty = _float_or_none(
        _nested_value(
            payload,
            (
                "filled_qty",
                "filled_quantity",
            ),
        )
    )

    filled_price = _float_or_none(
        _nested_value(
            payload,
            (
                "filled_price",
                "avg_filled_price",
                "average_filled_price",
            ),
        )
    )

    total_qty = _float_or_none(
        _nested_value(
            payload,
            (
                "qty",
                "quantity",
                "total_qty",
            ),
        )
    )

    order_id = _nested_value(
        payload,
        (
            "order_id",
            "id",
        ),
    )

    client_order_id = _nested_value(
        payload,
        (
            "client_order_id",
            "client_orderid",
        ),
    )

    return {
        "status": (
            str(status)
            if status is not None
            else None
        ),
        "status_class": classify_order_status(
            status
        ),
        "filled_qty": filled_qty,
        "filled_price": filled_price,
        "total_qty": total_qty,
        "order_id": order_id,
        "client_order_id": client_order_id,
        "raw": payload,
    }


def classify_order_status(
    status,
):
    text = str(
        status or ""
    ).upper().replace(
        " ",
        "_",
    )

    if text in {
        "FILLED",
        "FINAL_FILLED",
        "COMPLETE",
        "COMPLETED",
    }:
        return "FILLED"

    if (
        "PARTIAL" in text
        and "FILL" in text
    ):
        return "PARTIAL_FILLED"

    if (
        text in {
            "FAILED",
            "REJECTED",
            "REJECT",
            "INVALID",
        }
        or "REJECT" in text
        or "INVALID" in text
    ):
        return "REJECTED"

    if "CANCEL" in text:
        return "CANCELED"

    if (
        text in {
            "SUBMITTED",
            "NEW",
            "OPEN",
            "WORKING",
            "PENDING",
        }
        or "SUBMIT" in text
    ):
        return "PENDING"

    return "UNKNOWN"


def order_detail(
    trade,
    client_order_id,
):
    response = call(
        trade.order_v3.get_order_detail,
        config.ACCOUNT_ID,
        client_order_id,
    )

    payload = (
        response.json()
        if response.text
        else {}
    )

    result = normalize_order_detail(
        payload
    )

    result.update({
        "success": (
            200
            <= response.status_code
            < 300
        ),
        "http_status": response.status_code,
        "raw": payload,
    })

    return result


def open_orders(trade):
    response = call(
        trade.order_v3.get_order_open,
        account_id=config.ACCOUNT_ID,
    )

    payload = (
        response.json()
        if response.text
        else {}
    )

    return {
        "success": (
            200
            <= response.status_code
            < 300
        ),
        "http_status": response.status_code,
        "orders": payload,
    }


def cancel_order(
    trade,
    client_order_id,
):
    response = call(
        trade.order_v3.cancel_order,
        config.ACCOUNT_ID,
        client_order_id,
    )

    payload = (
        response.json()
        if response.text
        else {}
    )

    return {
        "success": (
            200
            <= response.status_code
            < 300
        ),
        "http_status": response.status_code,
        "response": payload,
    }


def positions(trade):
    response = call(
        trade.account_v2.get_account_position,
        config.ACCOUNT_ID,
    )

    payload = (
        response.json()
        if response.text
        else {}
    )

    return {
        "success": (
            200
            <= response.status_code
            < 300
        ),
        "http_status": response.status_code,
        "positions": payload,
    }


def _position_items(payload):
    return _first_list(
        payload,
        (
            "positions",
            "data",
            "items",
        ),
    )


def _position_leg(position):
    legs = (
        position.get("legs")
        if isinstance(
            position,
            dict,
        )
        else None
    )

    if isinstance(
        legs,
        list,
    ):
        for leg in legs:
            if (
                isinstance(
                    leg,
                    dict,
                )
                and str(
                    leg.get(
                        "instrument_type",
                        "",
                    )
                ).upper()
                == "OPTION"
            ):
                return leg

        if (
            legs
            and isinstance(
                legs[0],
                dict,
            )
        ):
            return legs[0]

    return {}


def normalize_position(
    position,
):
    """Extract fields used by the bot."""

    leg = _position_leg(
        position
    )

    quantity = _float_or_none(
        position.get(
            "quantity"
        )
    )

    strike = _float_or_none(
        leg.get(
            "option_exercise_price",
            leg.get(
                "strike_price"
            ),
        )
    )

    expiration = str(
        leg.get(
            "option_expire_date",
            leg.get(
                "expiration_date",
                "",
            ),
        )
    )[:10] or None

    option_type = str(
        leg.get(
            "option_type",
            "",
        )
    ).upper() or None

    cost_price = _float_or_none(
        position.get(
            "cost_price"
        )
    )

    last_price = _float_or_none(
        position.get(
            "last_price"
        )
    )

    return {
        "quantity": quantity,
        "symbol": position.get(
            "symbol"
        ),
        "instrument_type": str(
            position.get(
                "instrument_type",
                "",
            )
        ).upper(),
        "position_id": position.get(
            "position_id"
        ),
        "option_type": option_type,
        "strike": strike,
        "expiration": expiration,
        "cost_price": cost_price,
        "last_price": last_price,
        "unrealized_profit_loss": _float_or_none(
            position.get(
                "unrealized_profit_loss"
            )
        ),
        "raw": position,
    }


def find_matching_option_position(
    position_payload,
    contract,
    option_type,
):
    """Match option by type/strike/expiry."""

    target_strike = _float_or_none(
        contract.get(
            "strike_price"
        )
    )

    target_exp = str(
        contract.get(
            "expiration_date",
            contract.get(
                "expiration",
                "",
            ),
        )
    )[:10]

    target_symbol = contract.get(
        "symbol"
    )

    matches = []

    for raw in _position_items(
        position_payload
    ):
        if not isinstance(
            raw,
            dict,
        ):
            continue

        p = normalize_position(
            raw
        )

        if (
            p["instrument_type"]
            != "OPTION"
            or (
                p["quantity"]
                or 0
            )
            <= 0
        ):
            continue

        if (
            p["option_type"]
            != str(
                option_type
            ).upper()
        ):
            continue

        if (
            target_strike is not None
            and p["strike"] is not None
            and abs(
                p["strike"]
                - target_strike
            ) > 0.0001
        ):
            continue

        if (
            target_exp
            and p["expiration"]
            and p["expiration"]
            != target_exp
        ):
            continue

        if (
            target_symbol
            and p.get("symbol")
            == target_symbol
        ):
            matches.insert(
                0,
                p,
            )
        else:
            matches.append(
                p
            )

    if len(matches) == 1:
        return matches[0]

    if matches:
        return {
            "ambiguous": True,
            "matches": matches,
        }

    return None


# ============================================================
# ORDERS
# ============================================================

def place_option_order(
    contract,
    option_type,
    side,
    quantity,
    price,
    position_intent,
):
    if config.DRY_RUN:
        return {
            "success": True,
            "accepted": False,
            "dry_run": True,
            "client_order_id": (
                f"DRY{uuid.uuid4().hex.upper()}"
            )[:32],
            "message": (
                "DRY_RUN: order not submitted"
            ),
        }

    if quantity <= 0:
        raise ValueError(
            "Order quantity must be positive"
        )

    if (
        price is None
        or price <= 0
    ):
        raise ValueError(
            "Order price must be positive"
        )

    trade, _ = clients()

    cid = (
        f"BOT{uuid.uuid4().hex.upper()}"
    )[:32]

    expiration = str(
        contract.get(
            "expiration_date",
            contract.get(
                "expiration"
            ),
        )
    )[:10]

    order_payload = {
        "client_order_id": cid,
        "combo_type": "NORMAL",
        "option_strategy": "SINGLE",
        "instrument_type": "OPTION",
        "entrust_type": "QTY",
        "symbol": config.SYMBOL,
        "market": "US",
        "side": side,
        "order_type": "LIMIT",
        "limit_price": f"{float(price):.2f}",
        "quantity": str(
            int(quantity)
        ),
        "time_in_force": "DAY",
        "position_intent": position_intent,
        "legs": [{
            "side": side,
            "quantity": str(
                int(quantity)
            ),
            "symbol": config.SYMBOL,
            "strike_price": f"{float(contract['strike_price']):.2f}",
            "option_expire_date": expiration,
            "instrument_type": "OPTION",
            "option_type": str(
                option_type
            ).upper(),
            "market": "US",
        }],
    }

    response = call(
        trade.order_v3.place_order,
        config.ACCOUNT_ID,
        [order_payload],
    )

    body = (
        response.json()
        if response.text
        else {}
    )

    detail = normalize_order_detail(
        body
    )

    return {
        "success": (
            200
            <= response.status_code
            < 300
        ),
        "accepted": (
            200
            <= response.status_code
            < 300
        ),
        "http_status": response.status_code,
        "client_order_id": cid,
        "response": body,
        "order": order_payload,
        "status": detail["status"],
        "status_class": detail["status_class"],
        "filled_qty": detail["filled_qty"],
        "filled_price": detail["filled_price"],
    }


def entry_order(
    contract,
    option_type,
    quantity,
    price,
):
    return place_option_order(
        contract,
        option_type,
        "BUY",
        quantity,
        price,
        "BUY_TO_OPEN",
    )


def exit_order(
    contract,
    option_type,
    quantity,
    price,
):
    return place_option_order(
        contract,
        option_type,
        "SELL",
        quantity,
        price,
        "SELL_TO_CLOSE",
                    )
