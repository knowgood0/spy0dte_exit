from __future__ import annotations

import json
import logging
import os
import signal
import threading
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import config
import webull_client as wb
from strategy import Bar, analyze, levels

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("worker")
running = True
_state_lock = threading.Lock()

ET = ZoneInfo(config.TIMEZONE)

# Aggressive pricing offsets.
# Options trade in $0.01 increments.
# Entry: pay slightly above ask for fill speed.
# Exit: sell slightly below bid for fill speed.
ENTRY_PRICE_OFFSET = getattr(config, "ENTRY_PRICE_OFFSET", 0.02)
EXIT_PRICE_OFFSET = getattr(config, "EXIT_PRICE_OFFSET", 0.02)


def stop(*_):
    global running
    running = False


def load():
    try:
        with open(config.STATE_PATH, "r", encoding="utf-8") as handle:
            state = json.load(handle)
        return state if isinstance(state, dict) else default_state()
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default_state()


def default_state():
    return {
        "state": "FLAT",
        "position": None,
        "entry_order": None,
        "exit_order": None,
        "last_signal_bar": None,
        "last_trade": 0,
        "last_error": None,
    }


def save(state):
    directory = os.path.dirname(os.path.abspath(config.STATE_PATH))
    os.makedirs(directory, exist_ok=True)
    tmp = config.STATE_PATH + ".tmp"
    with _state_lock:
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(state, handle, indent=2, default=str)
        os.replace(tmp, config.STATE_PATH)


def now_et():
    return datetime.now(timezone.utc).astimezone(ET)


def parse_hhmm(value):
    hour, minute = (int(x) for x in value.split(":", 1))
    return hour, minute


def at_or_after(hhmm, dt=None):
    dt = dt or now_et()
    h, m = parse_hhmm(hhmm)
    return (dt.hour, dt.minute, dt.second) >= (h, m, 0)


def in_rth(dt=None):
    dt = dt or now_et()
    sh, sm = parse_hhmm(config.RTH_START)
    eh, em = parse_hhmm(config.RTH_END)
    return (sh, sm) <= (dt.hour, dt.minute) < (eh, em)


def new_entries_allowed(dt=None):
    dt = dt or now_et()
    return in_rth(dt) and not at_or_after(config.NO_NEW_ENTRIES_AFTER, dt) and not at_or_after(config.FORCE_EXIT_TIME, dt)


def force_exit_due(dt=None):
    return at_or_after(config.FORCE_EXIT_TIME, dt) and at_or_after(config.RTH_START, dt)


def position_contract(pos):
    return pos.get("contract") or {}


def position_age_seconds(pos):
    try:
        started = datetime.fromisoformat(pos["entry_time"])
        return max(0.0, (datetime.now(timezone.utc) - started.astimezone(timezone.utc)).total_seconds())
    except (KeyError, TypeError, ValueError):
        return float("inf")


def aggressive_entry_price(quote):
    ask = quote.get("ask")
    if ask is None or ask <= 0:
        return None

    return round(ask + ENTRY_PRICE_OFFSET, 2)


def aggressive_exit_price(quote):
    bid = quote.get("bid")
    if bid is None or bid <= 0:
        return None

    # Sell below the current bid to aggressively cross the market.
    # Never submit below the minimum valid option price.
    return max(0.01, round(bid - EXIT_PRICE_OFFSET, 2))


def option_exit_price(quote):
    return aggressive_exit_price(quote)


def order_is_open(open_orders_result, client_order_id):
    """Return True if the specified client order is still working."""
    if not open_orders_result.get("success"):
        return None

    for group in open_orders_result.get("orders", []) or []:
        if group.get("client_order_id") == client_order_id:
            return True

        for order in group.get("orders", []) or []:
            if order.get("client_order_id") == client_order_id:
                status = str(order.get("status") or "").upper()
                if status not in ("FILLED", "CANCELED", "REJECTED", "EXPIRED"):
                    return True

    return False


def verify_order_canceled(trade, client_order_id):
    """
    Verify that a canceled order is no longer present as a working order.
    This prevents a replacement SELL from accidentally interacting with an
    older still-working order and triggering Webull's reverse-position error.
    """
    for _ in range(3):
        try:
            result = wb.open_orders(trade)
            status = order_is_open(result, client_order_id)

            if status is False:
                return True

            if status is None:
                log.warning(
                    "EXIT CANCEL VERIFY: unable to determine status for %s",
                    client_order_id,
                )
            else:
                log.warning(
                    "EXIT CANCEL VERIFY: order %s still appears open; waiting",
                    client_order_id,
                )
        except Exception:
            log.exception(
                "EXIT CANCEL VERIFY: open-order check failed for %s",
                client_order_id,
            )

        time.sleep(0.5)

    return False


def entry_fill_state(trade, state):
    """Reconcile a pending entry against order detail AND actual position."""
    order = state.get("entry_order") or {}
    cid = order.get("client_order_id")
    if not cid:
        return state, False

    detail = wb.order_detail(trade, cid)
    order.update({
        "status": detail.get("status"),
        "status_class": detail.get("status_class"),
        "filled_qty": detail.get("filled_qty"),
        "filled_price": detail.get("filled_price"),
        "total_qty": detail.get("total_qty"),
        "order_id": detail.get("order_id"),
    })

    position_result = wb.positions(trade)
    if not position_result.get("success"):
        log.warning("ENTRY RECONCILE: position query failed; keeping PENDING_ENTRY")
        return state, False

    pos = wb.find_matching_option_position(
        position_result.get("positions"),
        state["position"]["contract"],
        state["position"]["side"],
    ) if state.get("position") else None

    if isinstance(pos, dict) and pos.get("ambiguous"):
        state["state"] = "RECOVERY_REQUIRED"
        state["last_error"] = "Multiple matching Webull option positions; refusing to guess"
        log.error("ENTRY RECONCILE: ambiguous position match; trading halted")
        return state, True

    status_class = detail.get("status_class")
    filled_qty = detail.get("filled_qty") or 0
    filled_price = detail.get("filled_price")

    submitted_at = order.get("submitted_at")
    if status_class == "PENDING" and submitted_at:
        try:
            age = (
                datetime.now(timezone.utc)
                - datetime.fromisoformat(submitted_at).astimezone(timezone.utc)
            ).total_seconds()
        except (TypeError, ValueError):
            age = 0

        if age >= config.ENTRY_ORDER_TIMEOUT_SECONDS:
            try:
                cancel = wb.cancel_order(trade, cid)
                log.warning("ENTRY TIMEOUT: cancel %s -> %s", cid, cancel)
            except Exception:
                log.exception("ENTRY TIMEOUT: cancel failed; keeping PENDING_ENTRY")
            return state, True

    if pos and (pos.get("quantity") or 0) > 0:
        actual_qty = int(pos["quantity"])
        actual_entry = filled_price or pos.get("cost_price")

        state["position"].update({
            "quantity": actual_qty,
            "entry_premium": actual_entry,
            "filled_qty": filled_qty,
            "entry_order_status": detail.get("status"),
            "position_cost_price": pos.get("cost_price"),
        })

        state["state"] = "OPEN"

        log.info(
            "STATE PENDING_ENTRY -> OPEN: actual_qty=%s entry_premium=%s order_status=%s",
            actual_qty,
            actual_entry,
            detail.get("status"),
        )

        if status_class == "PARTIAL_FILLED" and filled_qty < (
            detail.get("total_qty") or filled_qty
        ):
            try:
                cancel = wb.cancel_order(trade, cid)
                log.info("ENTRY PARTIAL: cancel remainder result=%s", cancel)
            except Exception:
                log.exception("ENTRY PARTIAL: failed to cancel unfilled remainder")

        return state, True

    if status_class in ("REJECTED", "CANCELED"):
        log.warning(
            "ENTRY FINAL WITHOUT POSITION: status=%s filled_qty=%s",
            detail.get("status"),
            filled_qty,
        )

        state["state"] = "FLAT"
        state["position"] = None
        state["entry_order"] = None
        state["last_error"] = (
            f"Entry order ended {detail.get('status')} without a Webull position"
        )
        return state, True

    if status_class == "FILLED":
        state["state"] = "PENDING_ENTRY"
        state["last_error"] = (
            "Order reports FILLED but matching Webull position is not yet visible"
        )
        log.warning(
            "ENTRY FILLED/NO POSITION: keeping PENDING_ENTRY for safe recovery"
        )
        return state, True

    return state, False


def reconcile_exit(trade, state):
    order = state.get("exit_order") or {}
    cid = order.get("client_order_id")
    if not cid:
        return state, False

    detail = wb.order_detail(trade, cid)

    order.update({
        "status": detail.get("status"),
        "status_class": detail.get("status_class"),
        "filled_qty": detail.get("filled_qty"),
        "filled_price": detail.get("filled_price"),
        "total_qty": detail.get("total_qty"),
        "order_id": detail.get("order_id"),
    })

    position_result = wb.positions(trade)
    if not position_result.get("success"):
        log.warning(
            "EXIT RECONCILE: position query failed; retaining PENDING_EXIT"
        )
        return state, False

    pos = wb.find_matching_option_position(
        position_result.get("positions"),
        state["position"]["contract"],
        state["position"]["side"],
    ) if state.get("position") else None

    if isinstance(pos, dict) and pos.get("ambiguous"):
        state["state"] = "RECOVERY_REQUIRED"
        state["last_error"] = (
            "Multiple matching positions after exit; refusing to guess"
        )
        return state, True

    remaining = int(pos.get("quantity") or 0) if pos else 0
    filled_qty = int(detail.get("filled_qty") or 0)
    status_class = detail.get("status_class")

    if remaining == 0:
        state["state"] = "FLAT"
        state["position"] = None
        state["exit_order"] = None
        state["last_trade"] = time.time()

        log.info(
            "STATE PENDING_EXIT -> FLAT: exit_fill_qty=%s exit_avg=%s status=%s",
            filled_qty,
            detail.get("filled_price"),
            detail.get("status"),
        )
        return state, True

    if status_class in ("REJECTED", "CANCELED"):
        state["position"]["quantity"] = remaining
        state["state"] = "OPEN"
        state["exit_order"] = None
        state["last_error"] = (
            f"Exit order ended {detail.get('status')} with "
            f"{remaining} contracts remaining"
        )

        log.warning(
            "STATE PENDING_EXIT -> OPEN: remaining=%s status=%s",
            remaining,
            detail.get("status"),
        )
        return state, True

    state["position"]["quantity"] = remaining
    return state, False


def recover_from_webull(trade, state):
    """On startup, prefer actual Webull state and safely unwind stale entry orders."""
    result = wb.positions(trade)

    if not result.get("success"):
        log.error(
            "STARTUP RECOVERY: cannot query Webull positions; trading is halted"
        )
        state["state"] = "RECOVERY_REQUIRED"
        state["last_error"] = (
            "Webull position query failed during startup recovery"
        )
        return state

    items = wb._position_items(result.get("positions"))
    option_positions = []

    for raw in items:
        p = wb.normalize_position(raw)

        if (
            p.get("instrument_type") == "OPTION"
            and (p.get("quantity") or 0) > 0
        ):
            option_positions.append(p)

    if not option_positions:
        if (
            state.get("state") == "PENDING_ENTRY"
            and state.get("entry_order", {}).get("client_order_id")
        ):
            cid = state["entry_order"]["client_order_id"]

            try:
                detail = wb.order_detail(trade, cid)

                if detail.get("status_class") == "PENDING":
                    cancel = wb.cancel_order(trade, cid)
                    log.warning(
                        "STARTUP RECOVERY: canceled stale pending entry %s -> %s",
                        cid,
                        cancel,
                    )

                elif detail.get("status_class") == "FILLED":
                    state["last_error"] = (
                        "Entry order reports FILLED but no position is visible yet"
                    )
                    state["state"] = "PENDING_ENTRY"
                    return state

            except Exception:
                log.exception(
                    "STARTUP RECOVERY: could not safely inspect/cancel pending entry"
                )
                state["state"] = "RECOVERY_REQUIRED"
                state["last_error"] = (
                    "Could not reconcile pending entry order after restart"
                )
                return state

        if state.get("state") in ("OPEN", "PENDING_EXIT"):
            log.warning(
                "STARTUP RECOVERY: local active state but Webull has no option "
                "position; clearing after successful position query"
            )

        state["state"] = "FLAT"
        state["position"] = None
        state["entry_order"] = None
        state["exit_order"] = None
        return state

    if len(option_positions) > 1:
        state["state"] = "RECOVERY_REQUIRED"
        state["last_error"] = (
            "More than one open option position exists in Webull Sandbox"
        )
        log.error(
            "STARTUP RECOVERY: multiple option positions; no automatic trading"
        )
        return state

    p = option_positions[0]
    old = state.get("position") or {}
    contract = old.get("contract")

    if not contract:
        contract = {
            "symbol": None,
            "strike_price": p.get("strike"),
            "expiration_date": p.get("expiration"),
            "option_type": p.get("option_type"),
        }

    state["position"] = {
        **old,
        "side": p.get("option_type"),
        "symbol": contract.get("symbol") or p.get("symbol"),
        "contract": contract,
        "quantity": int(p.get("quantity") or 0),
        "entry_premium": old.get("entry_premium") or p.get("cost_price"),
        "entry_underlying": old.get("entry_underlying"),
        "entry_atr": old.get("entry_atr"),
        "entry_time": old.get("entry_time")
        or datetime.now(timezone.utc).isoformat(),
    }

    if state["state"] != "PENDING_EXIT":
        state["state"] = "OPEN"

    log.warning(
        "STARTUP RECOVERY: adopted Webull position qty=%s type=%s "
        "strike=%s expiry=%s state=%s",
        p.get("quantity"),
        p.get("option_type"),
        p.get("strike"),
        p.get("expiration"),
        state["state"],
    )

    return state


def retry_stale_exit(trade, data, state):
    """
    Cancel/reprice a working exit after a short timeout.

    The replacement price is based on the CURRENT bid, minus the aggressive
    exit offset. The old order must be verified as no longer working before
    a replacement SELL is submitted.
    """
    order = state.get("exit_order") or {}
    submitted = order.get("submitted_at")

    if not submitted:
        return state

    try:
        age = (
            datetime.now(timezone.utc)
            - datetime.fromisoformat(submitted).astimezone(timezone.utc)
        ).total_seconds()
    except (TypeError, ValueError):
        age = 0

    if age < config.EXIT_ORDER_TIMEOUT_SECONDS:
        return state

    retries = int(order.get("retries") or 0)

    if retries >= config.MAX_EXIT_RETRIES:
        state["state"] = "RECOVERY_REQUIRED"
        state["last_error"] = (
            "Exit remained unresolved after maximum retries"
        )
        log.error(
            "EXIT EMERGENCY: max retries reached; automatic trading halted"
        )
        return state

    cid = order.get("client_order_id")

    if cid:
        try:
            cancel = wb.cancel_order(trade, cid)

            log.warning(
                "EXIT RETRY: cancel %s -> %s",
                cid,
                cancel,
            )

        except Exception:
            log.exception(
                "EXIT RETRY: cancel failed; will not submit replacement"
            )
            return state

        if not verify_order_canceled(trade, cid):
            state["last_error"] = (
                f"Could not verify cancellation of exit order {cid}"
            )
            log.error(
                "EXIT RETRY: old order %s could not be verified canceled; "
                "NO replacement SELL submitted",
                cid,
            )
            return state

    # Verify the actual position before submitting another SELL.
    pos_result = wb.positions(trade)

    if not pos_result.get("success"):
        log.warning(
            "EXIT RETRY: position verification failed; no replacement submitted"
        )
        return state

    pos = state.get("position") or {}

    actual = wb.find_matching_option_position(
        pos_result.get("positions"),
        pos.get("contract") or {},
        pos.get("side"),
    )

    if not actual or actual.get("ambiguous"):
        state["state"] = "RECOVERY_REQUIRED"
        state["last_error"] = (
            "Could not uniquely verify remaining position before exit retry"
        )
        return state

    remaining = int(actual.get("quantity") or 0)

    if remaining <= 0:
        state["state"] = "FLAT"
        state["position"] = None
        state["exit_order"] = None
        return state

    quote = wb.option_quote(data, pos["symbol"])
    bid = quote.get("bid")

    if bid is None or bid <= 0:
        log.warning(
            "EXIT RETRY: no valid bid for %s; keeping position protected",
            pos["symbol"],
        )
        return state

    price = aggressive_exit_price(quote)

    if price is None or price <= 0:
        return state

    try:
        result = wb.exit_order(
            pos["contract"],
            pos["side"],
            remaining,
            price,
        )
    except Exception as exc:
        log.exception(
            "EXIT RETRY: Webull rejected replacement SELL: %s",
            exc,
        )

        order["retries"] = retries + 1
        order["last_retry_error"] = str(exc)
        order["submitted_at"] = datetime.now(timezone.utc).isoformat()
        return state

    if not result.get("success"):
        log.error(
            "EXIT RETRY REJECTED: %s",
            result,
        )

        order["retries"] = retries + 1
        order["last_retry_error"] = str(result)
        order["submitted_at"] = datetime.now(timezone.utc).isoformat()
        return state

    state["exit_order"] = {
        **order,
        "client_order_id": result.get("client_order_id"),
        "status": result.get("status"),
        "status_class": result.get("status_class"),
        "requested_qty": remaining,
        "submitted_at": datetime.now(timezone.utc).isoformat(),
        "retries": retries + 1,
        "limit_price": price,
    }

    log.warning(
        "EXIT RETRY %s: new order=%s qty=%s bid=%.2f limit=%.2f",
        retries + 1,
        result.get("client_order_id"),
        remaining,
        bid,
        price,
    )

    return state


def risk_reason(pos, signal_snapshot, option_quote, dt):
    """Return the highest-priority exit reason, or None."""
    if force_exit_due(dt):
        return "FORCED_EOD_LIQUIDATION"

    if position_age_seconds(pos) >= config.MAX_HOLD_MINUTES * 60:
        return "TIME_STOP"

    premium = option_quote.get("bid")
    entry = pos.get("entry_premium")

    if config.USE_OPTION_PREMIUM_RISK and premium is not None and entry:
        change = premium / entry - 1.0

        if change <= -config.OPTION_STOP_LOSS_PCT:
            return "OPTION_MAX_LOSS"

        if change >= config.OPTION_TAKE_PROFIT_PCT:
            return "OPTION_TAKE_PROFIT"

        if config.USE_BE and change >= config.OPTION_BREAKEVEN_TRIGGER_PCT:
            pos["option_breakeven_armed"] = True

        if (
            pos.get("option_breakeven_armed")
            and change <= config.OPTION_BREAKEVEN_FLOOR_PCT
        ):
            return "OPTION_BREAKEVEN"

    if (
        signal_snapshot.get("atr") is not None
        and pos.get("entry_underlying") is not None
    ):
        stop, target, be = levels(pos)

        if config.USE_BE and pos.get("side") == "CALL":
            if signal_snapshot["close"] >= be:
                stop = max(stop, pos["entry_underlying"])

        if config.USE_BE and pos.get("side") == "PUT":
            if signal_snapshot["close"] <= be:
                stop = min(stop, pos["entry_underlying"])

        if pos.get("side") == "CALL":
            if signal_snapshot["close"] <= stop:
                return "UNDERLYING_STOP"

            if signal_snapshot["close"] >= target:
                return "UNDERLYING_TARGET"

            if (
                config.USE_ZONE
                and signal_snapshot.get("upper") is not None
                and signal_snapshot["close"] >= signal_snapshot["upper"]
            ):
                return "WAVE_ZONE"

        else:
            if signal_snapshot["close"] >= stop:
                return "UNDERLYING_STOP"

            if signal_snapshot["close"] <= target:
                return "UNDERLYING_TARGET"

            if (
                config.USE_ZONE
                and signal_snapshot.get("lower") is not None
                and signal_snapshot["close"] <= signal_snapshot["lower"]
            ):
                return "WAVE_ZONE"

    return None


def submit_exit(trade, data, state, reason):
    pos = state.get("position") or {}

    quantity = int(pos.get("quantity") or 0)

    if quantity <= 0:
        raise RuntimeError(
            "Cannot exit: actual position quantity is zero"
        )

    # Verify actual position immediately before every SELL.
    positions_result = wb.positions(trade)

    if not positions_result.get("success"):
        raise RuntimeError(
            "Cannot verify Webull position before exit"
        )

    actual = wb.find_matching_option_position(
        positions_result.get("positions"),
        pos.get("contract") or {},
        pos.get("side"),
    )

    if not actual or actual.get("ambiguous"):
        raise RuntimeError(
            "Cannot uniquely verify Webull position before exit"
        )

    quantity = int(actual.get("quantity") or 0)

    if quantity <= 0:
        raise RuntimeError(
            "Webull position disappeared before exit submission"
        )

    # Check for a working order for this position before submitting another.
    # This protects against duplicate SELLs after a restart or transient state issue.
    try:
        open_result = wb.open_orders(trade)

        if open_result.get("success"):
            for group in open_result.get("orders", []) or []:
                for order in group.get("orders", []) or []:
                    if (
                        order.get("symbol") == pos.get("symbol")
                        and str(order.get("side", "")).upper() == "SELL"
                        and str(order.get("status", "")).upper()
                        not in ("FILLED", "CANCELED", "REJECTED", "EXPIRED")
                    ):
                        log.warning(
                            "EXIT BLOCKED: existing working SELL detected "
                            "for %s order=%s status=%s",
                            pos.get("symbol"),
                            order.get("order_id") or order.get("client_order_id"),
                            order.get("status"),
                        )
                        state["state"] = "PENDING_EXIT"
                        state["exit_order"] = {
                            "client_order_id": order.get("client_order_id"),
                            "order_id": order.get("order_id"),
                            "status": order.get("status"),
                            "status_class": "PENDING",
                            "requested_qty": int(
                                order.get("total_quantity") or quantity
                            ),
                            "reason": reason,
                            "submitted_at": datetime.now(
                                timezone.utc
                            ).isoformat(),
                            "filled_qty": int(
                                order.get("filled_quantity") or 0
                            ),
                            "filled_price": None,
                        }
                        return state

    except Exception:
        log.exception(
            "EXIT PRECHECK: open-order check failed; refusing duplicate SELL"
        )
        raise RuntimeError(
            "Could not verify existing Webull orders before exit"
        )

    quote = wb.option_quote(data, pos["symbol"])

    bid = quote.get("bid")
    ask = quote.get("ask")

    if bid is None or bid <= 0:
        raise RuntimeError(
            "No valid option bid available for exit"
        )

    price = option_exit_price(quote)

    if price is None or price <= 0:
        raise RuntimeError(
            "No valid aggressive option exit price available"
        )

    log.info(
        "EXIT PRICE: %s bid=%.2f ask=%s offset=%.2f limit=%.2f",
        pos["symbol"],
        bid,
        f"{ask:.2f}" if ask is not None else "n/a",
        EXIT_PRICE_OFFSET,
        price,
    )

    try:
        result = wb.exit_order(
            pos["contract"],
            pos["side"],
            quantity,
            price,
        )
    except Exception as exc:
        raise RuntimeError(
            f"Webull exit submission failed: {exc}"
        ) from exc

    if config.DRY_RUN:
        log.info(
            "DRY_RUN EXIT reason=%s symbol=%s qty=%s bid=%.2f limit=%.2f",
            reason,
            pos["symbol"],
            quantity,
            bid,
            price,
        )

        state["state"] = "FLAT"
        state["position"] = None
        state["exit_order"] = None
        state["last_trade"] = time.time()

        return state

    if not result.get("success"):
        raise RuntimeError(
            f"Webull exit was not accepted: {result}"
        )

    state["state"] = "PENDING_EXIT"

    state["exit_order"] = {
        "client_order_id": result.get("client_order_id"),
        "status": result.get("status"),
        "status_class": result.get("status_class"),
        "requested_qty": quantity,
        "reason": reason,
        "submitted_at": datetime.now(timezone.utc).isoformat(),
        "filled_qty": result.get("filled_qty") or 0,
        "filled_price": result.get("filled_price"),
        "limit_price": price,
        "retries": 0,
    }

    log.info(
        "STATE OPEN -> PENDING_EXIT reason=%s qty=%s order=%s "
        "bid=%.2f limit=%.2f status=%s",
        reason,
        quantity,
        result.get("client_order_id"),
        bid,
        price,
        result.get("status"),
    )

    return state


def maybe_enter(trade, data, state, snapshot):
    if not snapshot.get("signal") or not new_entries_allowed():
        return state

    if state.get("state") != "FLAT":
        return state

    if time.time() - state.get("last_trade", 0) < config.COOLDOWN:
        return state

    if snapshot.get("bar_time") == state.get("last_signal_bar"):
        return state

    option_type = snapshot["signal"]

    contract = wb.choose(
        data,
        option_type,
        snapshot["close"],
    )

    symbol = contract.get("symbol")

    quote = wb.option_quote(
        data,
        symbol,
    )

    bid = quote.get("bid")
    ask = quote.get("ask")

    if bid is None or ask is None or ask <= 0:
        log.info(
            "ENTRY SKIP: no bid/ask for %s",
            symbol,
        )
        return state

    mid = (bid + ask) / 2

    spread = (
        (ask - bid) / mid
        if mid
        else float("inf")
    )

    if not (
        config.MIN_PREMIUM <= mid <= config.MAX_PREMIUM
        and spread <= config.MAX_SPREAD
    ):
        log.info(
            "ENTRY SKIP: %s premium=%.2f spread=%.1f%%",
            symbol,
            mid,
            spread * 100,
        )
        return state

    price = aggressive_entry_price(quote)

    if price is None:
        log.info(
            "ENTRY SKIP: no valid ask for %s",
            symbol,
        )
        return state

    log.info(
        "ENTRY PRICE: %s bid=%.2f ask=%.2f offset=%.2f limit=%.2f",
        symbol,
        bid,
        ask,
        ENTRY_PRICE_OFFSET,
        price,
    )

    result = wb.entry_order(
        contract,
        option_type,
        config.OPTION_QUANTITY,
        price,
    )

    if not result.get("success"):
        log.error(
            "ENTRY REJECTED: %s",
            result,
        )
        state["last_error"] = str(result)
        return state

    state["last_signal_bar"] = snapshot["bar_time"]

    if config.DRY_RUN:
        state["state"] = "OPEN"

        state["entry_order"] = {
            "client_order_id": result.get("client_order_id"),
            "status": "DRY_RUN_FILLED",
            "status_class": "FILLED",
            "requested_qty": config.OPTION_QUANTITY,
            "filled_qty": config.OPTION_QUANTITY,
            "filled_price": price,
            "simulated": True,
        }

        state["position"] = {
            "side": option_type,
            "symbol": symbol,
            "contract": contract,
            "quantity": config.OPTION_QUANTITY,
            "entry_underlying": snapshot["close"],
            "entry_atr": snapshot["atr"],
            "entry_premium": price,
            "entry_time": datetime.now(timezone.utc).isoformat(),
            "option_breakeven_armed": False,
        }

        log.info(
            "DRY_RUN ENTRY FILLED (SIMULATED): %s %s qty=%s premium=%.2f",
            option_type,
            symbol,
            config.OPTION_QUANTITY,
            price,
        )

        return state

    state["state"] = "PENDING_ENTRY"

    state["entry_order"] = {
        "client_order_id": result.get("client_order_id"),
        "status": result.get("status"),
        "status_class": result.get("status_class"),
        "requested_qty": config.OPTION_QUANTITY,
        "submitted_at": datetime.now(timezone.utc).isoformat(),
        "filled_qty": result.get("filled_qty") or 0,
        "filled_price": result.get("filled_price"),
        "limit_price": price,
    }

    state["position"] = {
        "side": option_type,
        "symbol": symbol,
        "contract": contract,
        "quantity": 0,
        "entry_underlying": snapshot["close"],
        "entry_atr": snapshot["atr"],
        "entry_premium": None,
        "entry_time": datetime.now(timezone.utc).isoformat(),
        "option_breakeven_armed": False,
    }

    log.info(
        "ENTRY SUBMITTED: %s %s qty=%s order=%s limit=%.2f",
        option_type,
        symbol,
        config.OPTION_QUANTITY,
        result.get("client_order_id"),
        price,
    )

    return state


def main():
    global running

    trade, data = wb.connect()

    state = recover_from_webull(
        trade,
        load(),
    )

    save(state)

    log.info(
        "Connected to Webull SANDBOX. DRY_RUN=%s state=%s",
        config.DRY_RUN,
        state.get("state"),
    )

    while running:
        try:
            # Always reconcile outstanding entry orders first.
            if state.get("state") == "PENDING_ENTRY":
                state, _ = entry_fill_state(
                    trade,
                    state,
                )

                save(state)

                if state.get("state") == "PENDING_ENTRY":
                    time.sleep(
                        config.RECOVERY_POLL_SECONDS
                    )
                    continue

            # Always reconcile outstanding exit orders first.
            if state.get("state") == "PENDING_EXIT":
                state, _ = reconcile_exit(
                    trade,
                    state,
                )

                if state.get("state") == "PENDING_EXIT":
                    state = retry_stale_exit(
                        trade,
                        data,
                        state,
                    )

                save(state)

                if state.get("state") == "PENDING_EXIT":
                    time.sleep(
                        config.RECOVERY_POLL_SECONDS
                    )
                    continue

            if state.get("state") == "RECOVERY_REQUIRED":
                log.error(
                    "RECOVERY_REQUIRED: %s",
                    state.get("last_error"),
                )

                time.sleep(
                    config.RECOVERY_POLL_SECONDS
                )

                state = recover_from_webull(
                    trade,
                    state,
                )

                save(state)
                continue

            raw = wb.bars(
                data,
                config.HISTORY_COUNT,
            )

            bs = [
                Bar(**x)
                for x in raw
            ]

            if len(bs) < 60:
                raise RuntimeError(
                    f"Only {len(bs)} usable bars returned"
                )

            # Entry engine uses completed 5-minute bars.
            # Exits use live option quotes every worker cycle.
            s = analyze(bs[:-1])

            log.info(
                "SPY %.2f trend=%s signal=%s ATR=%s ADX=%s compressed=%s",
                s["close"],
                "UP" if s["trend"] == 1 else "DOWN",
                s["signal"] or "-",
                f"{s['atr']:.3f}" if s["atr"] else "n/a",
                f"{s['adx']:.1f}" if s["adx"] else "n/a",
                s["compressed"],
            )

            pos = state.get("position")

            if state.get("state") == "OPEN" and pos:
                # Refresh actual position quantity before risk decisions.
                p_result = wb.positions(trade)

                if not p_result.get("success"):
                    log.warning(
                        "POSITION MONITOR: Webull position lookup failed; "
                        "no exit submitted this cycle"
                    )

                else:
                    actual = wb.find_matching_option_position(
                        p_result.get("positions"),
                        pos.get("contract") or {},
                        pos.get("side"),
                    )

                    if (
                        isinstance(actual, dict)
                        and actual.get("ambiguous")
                    ):
                        state["state"] = "RECOVERY_REQUIRED"
                        state["last_error"] = (
                            "Ambiguous live position during monitoring"
                        )
                        save(state)
                        continue

                    if actual is None:
                        log.warning(
                            "POSITION MONITOR: expected position is absent; "
                            "re-querying before changing state"
                        )
                        time.sleep(
                            config.RECOVERY_POLL_SECONDS
                        )
                        continue

                    pos["quantity"] = int(
                        actual.get("quantity") or 0
                    )

                    pos["position_cost_price"] = actual.get(
                        "cost_price"
                    )

                    if not pos.get("entry_premium"):
                        pos["entry_premium"] = actual.get(
                            "cost_price"
                        )

                    # Live option quote makes stop/target checks intrabar.
                    quote = wb.option_quote(
                        data,
                        pos["symbol"],
                    )

                    reason = risk_reason(
                        pos,
                        s,
                        quote,
                        now_et(),
                    )

                    if reason:
                        state = submit_exit(
                            trade,
                            data,
                            state,
                            reason,
                        )

                        save(state)
                        continue

            elif state.get("state") == "FLAT":
                if force_exit_due():
                    pass
                else:
                    state = maybe_enter(
                        trade,
                        data,
                        state,
                        s,
                    )
                    save(state)

            state["last_bar"] = s["bar_time"]
            save(state)

            time.sleep(
                config.POLL_SECONDS
                if state.get("state") == "OPEN"
                else config.IDLE_POLL_SECONDS
            )

        except Exception as exc:
            state["last_error"] = str(exc)
            save(state)

            log.exception(
                "Loop error: %s",
                exc,
            )

            time.sleep(
                config.IDLE_POLL_SECONDS
            )


if __name__ == "__main__":
    signal.signal(
        signal.SIGTERM,
        stop,
    )

    signal.signal(
        signal.SIGINT,
        stop,
    )

    main()
