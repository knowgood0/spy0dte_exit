"""Webull Sandbox client for the SPY 0DTE paper bot.

The module deliberately treats Webull's external order/position state as
authoritative.  A successful place_order() response means only that Webull
accepted the request; it is never treated as a fill.
"""
from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone
from typing import Any

from webull.core.client import ApiClient
from webull.trade.trade_client import TradeClient
from webull.data.data_client import DataClient
from webull.data.common.category import Category
from webull.data.common.timespan import Timespan

import config

ENDPOINT = "api.sandbox.webull.com"
_last_request = 0.0


def call(fn, *args, **kwargs):
    """Serialize requests and retry only explicit rate-limit failures."""
    global _last_request
    last_error = None
    for attempt in range(3):
        wait = config.WEBULL_MIN_REQUEST_INTERVAL - (time.monotonic() - _last_request)
        if wait > 0:
            time.sleep(wait)
        _last_request = time.monotonic()
        try:
            return fn(*args, **kwargs)
        except Exception as exc:
            last_error = exc
            text = str(exc)
            if "429" not in text and "TOO_MANY_REQUESTS" not in text:
                raise
            time.sleep(2 * (attempt + 1))
    raise last_error


def clients():
    api = ApiClient(config.APP_KEY, config.APP_SECRET, config.REGION)
    api.add_endpoint(config.REGION, ENDPOINT)
    return TradeClient(api), DataClient(api)


def connect():
    if not config.APP_KEY or not config.APP_SECRET or not config.ACCOUNT_ID:
        raise RuntimeError("Missing WEBULL_APP_KEY/WEBULL_APP_SECRET/WEBULL_ACCOUNT_ID")
    trade, data = clients()
    response = call(trade.account_v2.get_account_list)
    if response.status_code >= 300:
        raise RuntimeError(f"Webull account list HTTP {response.status_code}: {response.text}")
    accounts = response.json()
    account_ids = _extract_account_ids(accounts)
    if account_ids and config.ACCOUNT_ID not in account_ids:
        raise RuntimeError("Configured WEBULL_ACCOUNT_ID was not returned by Webull Sandbox account list")
    return trade, data


def _extract_account_ids(payload):
    items = payload if isinstance(payload, list) else []
    if isinstance(payload, dict):
        for key in ("account", "accounts", "data", "items"):
            if isinstance(payload.get(key), list):
                items = payload[key]
                break
    return [x.get("account_id") for x in items if isinstance(x, dict) and x.get("account_id")]


def bars(data, count):
    response = call(
        data.market_data.get_history_bar,
        config.SYMBOL,
        Category.US_STOCK.name,
        Timespan.M5.name,
        count,
    )
    if response.status_code >= 300:
        raise RuntimeError(f"Historical bars HTTP {response.status_code}: {response.text}")
    payload = response.json()
    payload = _first_list(payload, ("data", "items", "bars", "list"))
    out = []
    for item in payload:
        try:
            ts = item.get("time", item.get("timestamp", item.get("datetime")))
            if isinstance(ts, (int, float)):
                dt = datetime.fromtimestamp(ts / 1000 if ts > 1e10 else ts, tz=timezone.utc)
            else:
                dt = datetime.fromisoformat(str(item.get("date")).replace("Z", "+00:00"))
            out.append({
                "timestamp": dt,
                "open": float(item["open"]),
                "high": float(item["high"]),
                "low": float(item["low"]),
                "close": float(item["close"]),
                "volume": float(item.get("volume", 0) or 0),
            })
        except (TypeError, ValueError, KeyError):
            continue
    out.sort(key=lambda x: x["timestamp"])
    return out


def contracts(data, option_type):
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
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
        raise RuntimeError(f"Option contracts HTTP {response.status_code}: {response.text}")
    return _first_list(response.json(), ("data", "items", "contracts"))


def choose(data, option_type, spy_price):
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    candidates = []
    for contract in contracts(data, option_type):
        try:
            expiration = str(contract.get("expiration_date", contract.get("expiration", "")))[:10]
            typ = str(contract.get("option_type", "")).upper()
            if expiration == today and typ == option_type:
                candidates.append(contract)
        except (TypeError, ValueError):
            continue
    if not candidates:
        raise RuntimeError(f"No 0DTE {option_type} contracts available")
    return min(candidates, key=lambda c: abs(float(c["strike_price"]) - spy_price))


def option_quote(data, symbol):
    response = call(data.option_market_data.get_option_snapshot, symbol, Category.US_OPTION.name)
    if response.status_code >= 300:
        raise RuntimeError(f"Option snapshot HTTP {response.status_code}: {response.text}")
    payload = response.json()
    item = payload[0] if isinstance(payload, list) and payload else payload

    def number(key):
        try:
            value = item.get(key) if isinstance(item, dict) else None
            return float(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    return {
        "bid": number("bid"),
        "ask": number("ask"),
        "premium": number("price") or number("last_price") or number("mark_price"),
        "raw": payload,
    }


def spy_snapshot(data):
    response = call(data.market_data.get_snapshot, config.SYMBOL, Category.US_STOCK.name)
    if response.status_code >= 300:
        raise RuntimeError(f"SPY snapshot HTTP {response.status_code}: {response.text}")
    payload = response.json()
    item = payload[0] if isinstance(payload, list) and payload else payload
    price = None
    if isinstance(item, dict):
        for key in ("price", "last_price", "last"):
            try:
                if item.get(key) is not None:
                    price = float(item[key])
                    break
            except (TypeError, ValueError):
                pass
    return {"price": price, "raw": payload}


def _first_list(payload, keys):
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in keys:
            if isinstance(payload.get(key), list):
                return payload[key]
    return []


def _first_dict(payload):
    if isinstance(payload, dict):
        return payload
    if isinstance(payload, list) and payload and isinstance(payload[0], dict):
        return payload[0]
    return {}


def _nested_value(payload, keys):
    """Read a known field from common Webull response nesting."""
    if not isinstance(payload, dict):
        return None
    for key in keys:
        if payload.get(key) not in (None, ""):
            return payload[key]
    for parent in ("order", "data", "result", "payload"):
        nested = payload.get(parent)
        if isinstance(nested, dict):
            value = _nested_value(nested, keys)
            if value not in (None, ""):
                return value
    return None


def _float_or_none(value):
    try:
        if value in (None, ""):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def normalize_order_detail(payload):
    """Normalize fields documented by Webull's order/trade-event API."""
    status = _nested_value(payload, ("order_status", "status", "state"))
    filled_qty = _float_or_none(_nested_value(payload, ("filled_qty",)))
    filled_price = _float_or_none(_nested_value(payload, ("filled_price",)))
    total_qty = _float_or_none(_nested_value(payload, ("qty", "quantity")))
    order_id = _nested_value(payload, ("order_id",))
    client_order_id = _nested_value(payload, ("client_order_id",))
    return {
        "status": str(status) if status is not None else None,
        "status_class": classify_order_status(status),
        "filled_qty": filled_qty,
        "filled_price": filled_price,
        "total_qty": total_qty,
        "order_id": order_id,
        "client_order_id": client_order_id,
        "raw": payload,
    }


def classify_order_status(status):
    text = str(status or "").upper().replace(" ", "_")
    if text in {"FILLED", "FINAL_FILLED", "COMPLETE", "COMPLETED"}:
        return "FILLED"
    if "PARTIAL" in text and "FILL" in text:
        return "PARTIAL_FILLED"
    if text in {"FAILED", "REJECTED", "REJECT", "INVALID"} or "REJECT" in text or "INVALID" in text:
        return "REJECTED"
    if "CANCEL" in text:
        return "CANCELED"
    if text in {"SUBMITTED", "NEW", "OPEN", "WORKING", "PENDING"} or "SUBMIT" in text:
        return "PENDING"
    return "UNKNOWN"


def order_detail(trade, client_order_id):
    response = call(trade.order_v3.get_order_detail, config.ACCOUNT_ID, client_order_id)
    payload = response.json() if response.text else {}
    result = normalize_order_detail(payload)
    result.update({
        "success": 200 <= response.status_code < 300,
        "http_status": response.status_code,
        "raw": payload,
    })
    return result


def open_orders(trade):
    response = call(trade.order_v3.get_order_open, account_id=config.ACCOUNT_ID)
    payload = response.json() if response.text else {}
    return {"success": 200 <= response.status_code < 300, "http_status": response.status_code, "orders": payload}


def cancel_order(trade, client_order_id):
    response = call(trade.order_v3.cancel_order, config.ACCOUNT_ID, client_order_id)
    payload = response.json() if response.text else {}
    return {"success": 200 <= response.status_code < 300, "http_status": response.status_code, "response": payload}


def positions(trade):
    response = call(trade.account_v2.get_account_position, config.ACCOUNT_ID)
    payload = response.json() if response.text else {}
    return {"success": 200 <= response.status_code < 300, "http_status": response.status_code, "positions": payload}


def _position_items(payload):
    return _first_list(payload, ("positions", "data", "items"))


def _position_leg(position):
    legs = position.get("legs") if isinstance(position, dict) else None
    if isinstance(legs, list):
        for leg in legs:
            if isinstance(leg, dict) and str(leg.get("instrument_type", "")).upper() == "OPTION":
                return leg
        if legs and isinstance(legs[0], dict):
            return legs[0]
    return {}


def normalize_position(position):
    """Extract only fields used by the bot; unknown fields remain in raw."""
    leg = _position_leg(position)
    quantity = _float_or_none(position.get("quantity"))
    strike = _float_or_none(leg.get("option_exercise_price", leg.get("strike_price")))
    expiration = str(leg.get("option_expire_date", leg.get("expiration_date", "")))[:10] or None
    option_type = str(leg.get("option_type", "")).upper() or None
    cost_price = _float_or_none(position.get("cost_price"))
    last_price = _float_or_none(position.get("last_price"))
    return {
        "quantity": quantity,
        "symbol": position.get("symbol"),
        "instrument_type": str(position.get("instrument_type", "")).upper(),
        "position_id": position.get("position_id"),
        "option_type": option_type,
        "strike": strike,
        "expiration": expiration,
        "cost_price": cost_price,
        "last_price": last_price,
        "unrealized_profit_loss": _float_or_none(position.get("unrealized_profit_loss")),
        "raw": position,
    }


def find_matching_option_position(position_payload, contract, option_type):
    """Match the option by type/strike/expiry; Webull position legs may use SPY as symbol."""
    target_strike = _float_or_none(contract.get("strike_price"))
    target_exp = str(contract.get("expiration_date", contract.get("expiration", "")))[:10]
    target_symbol = contract.get("symbol")
    matches = []
    for raw in _position_items(position_payload):
        if not isinstance(raw, dict):
            continue
        p = normalize_position(raw)
        if p["instrument_type"] != "OPTION" or (p["quantity"] or 0) <= 0:
            continue
        if p["option_type"] != str(option_type).upper():
            continue
        if target_strike is not None and p["strike"] is not None and abs(p["strike"] - target_strike) > 0.0001:
            continue
        if target_exp and p["expiration"] and p["expiration"] != target_exp:
            continue
        # If an OCC symbol is actually returned, prefer exact identity.
        if target_symbol and p.get("symbol") == target_symbol:
            matches.insert(0, p)
        else:
            matches.append(p)
    if len(matches) == 1:
        return matches[0]
    if matches:
        # Multiple matching positions are unsafe to guess between.
        return {"ambiguous": True, "matches": matches}
    return None


def place_option_order(contract, option_type, side, quantity, price, position_intent):
    if config.DRY_RUN:
        return {
            "success": True,
            "accepted": False,
            "dry_run": True,
            "client_order_id": f"DRY{uuid.uuid4().hex.upper()}"[:32],
            "message": "DRY_RUN: order not submitted",
        }

    if quantity <= 0:
        raise ValueError("Order quantity must be positive")
    if price is None or price <= 0:
        raise ValueError("Order price must be positive")

    trade, _ = clients()
    cid = f"BOT{uuid.uuid4().hex.upper()}"[:32]
    expiration = str(contract.get("expiration_date", contract.get("expiration")))[:10]
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
        "quantity": str(int(quantity)),
        "time_in_force": "DAY",
        "position_intent": position_intent,
        "legs": [{
            "side": side,
            "quantity": str(int(quantity)),
            "symbol": config.SYMBOL,
            "strike_price": f"{float(contract['strike_price']):.2f}",
            "option_expire_date": expiration,
            "instrument_type": "OPTION",
            "option_type": str(option_type).upper(),
            "market": "US",
        }],
    }
    response = call(trade.order_v3.place_order, config.ACCOUNT_ID, [order_payload])
    body = response.json() if response.text else {}
    detail = normalize_order_detail(body)
    return {
        "success": 200 <= response.status_code < 300,
        "accepted": 200 <= response.status_code < 300,
        "http_status": response.status_code,
        "client_order_id": cid,
        "response": body,
        "order": order_payload,
        "status": detail["status"],
        "status_class": detail["status_class"],
        "filled_qty": detail["filled_qty"],
        "filled_price": detail["filled_price"],
    }


def entry_order(contract, option_type, quantity, price):
    return place_option_order(contract, option_type, "BUY", quantity, price, "BUY_TO_OPEN")


def exit_order(contract, option_type, quantity, price):
    return place_option_order(contract, option_type, "SELL", quantity, price, "SELL_TO_CLOSE")
