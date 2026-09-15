from __future__ import annotations

import json
import logging
import os
import signal
import time
from datetime import datetime, timezone

from zoneinfo import ZoneInfo

import config
import webull_client as wb
from strategy import Bar, analyze, levels


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

log = logging.getLogger("worker")

running = True

ET = ZoneInfo(
    config.TIMEZONE
)

ENTRY_PRICE_OFFSET = 0.02
EXIT_PRICE_OFFSET = 0.03

STREAM_STALE_SECONDS = 5.0
OPTION_STREAM_WAIT_SECONDS = 5.0


def stop(*_):
    global running
    running = False


def default_state():
    return {
        "state": "FLAT",
        "position": None,
        "entry_order": None,
        "exit_order": None,
        "last_signal_bar": None,
        "last_trade": 0,
        "last_error": None,
        "last_bar": None,
    }


def load():
    try:
        with open(
            config.STATE_PATH,
            "r",
            encoding="utf-8",
        ) as handle:
            state = json.load(handle)

        return (
            state
            if isinstance(state, dict)
            else default_state()
        )

    except (
        FileNotFoundError,
        json.JSONDecodeError,
        OSError,
    ):
        return default_state()


def save(state):
    directory = os.path.dirname(
        os.path.abspath(
            config.STATE_PATH
        )
    )

    os.makedirs(
        directory,
        exist_ok=True,
    )

    tmp = (
        config.STATE_PATH
        + ".tmp"
    )

    with open(
        tmp,
        "w",
        encoding="utf-8",
    ) as handle:
        json.dump(
            state,
            handle,
            indent=2,
            default=str,
        )

    os.replace(
        tmp,
        config.STATE_PATH,
    )


def now_et():
    return datetime.now(
        timezone.utc
    ).astimezone(ET)


def parse_hhmm(value):
    hour, minute = (
        int(x)
        for x in value.split(
            ":",
            1,
        )
    )

    return hour, minute


def at_or_after(
    hhmm,
    dt=None,
):
    dt = dt or now_et()

    h, m = parse_hhmm(
        hhmm
    )

    return (
        dt.hour,
        dt.minute,
        dt.second,
    ) >= (
        h,
        m,
        0,
    )


def in_rth(dt=None):
    dt = dt or now_et()

    sh, sm = parse_hhmm(
        config.RTH_START
    )

    eh, em = parse_hhmm(
        config.RTH_END
    )

    return (
        sh,
        sm,
    ) <= (
        dt.hour,
        dt.minute,
    ) < (
        eh,
        em,
    )


def new_entries_allowed(
    dt=None,
):
    dt = dt or now_et()

    return (
        in_rth(dt)
        and not at_or_after(
            config.NO_NEW_ENTRIES_AFTER,
            dt,
        )
        and not at_or_after(
            config.FORCE_EXIT_TIME,
            dt,
        )
    )


def force_exit_due(
    dt=None,
):
    return (
        at_or_after(
            config.FORCE_EXIT_TIME,
            dt,
        )
        and at_or_after(
            config.RTH_START,
            dt,
        )
    )


def position_contract(pos):
    return (
        pos.get("contract")
        or {}
    )


def position_age_seconds(pos):
    try:
        started = datetime.fromisoformat(
            pos["entry_time"]
        )

        return max(
            0.0,
            (
                datetime.now(
                    timezone.utc
                )
                - started.astimezone(
                    timezone.utc
                )
            ).total_seconds(),
        )

    except (
        KeyError,
        TypeError,
        ValueError,
    ):
        return float("inf")


def aggressive_entry_price(
    ask,
):
    if ask is None or ask <= 0:
        return None

    return round(
        ask + ENTRY_PRICE_OFFSET,
        2,
    )


def aggressive_exit_price(
    bid,
):
    if bid is None or bid <= 0:
        return None

    return round(
        max(
            0.01,
            bid - EXIT_PRICE_OFFSET,
        ),
        2,
    )


# ============================================================
# LIVE 5-MINUTE BAR ENGINE
# ============================================================

def bar_bucket(
    timestamp,
):
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(
            tzinfo=timezone.utc
        )

    timestamp = timestamp.astimezone(
        timezone.utc
    )

    minute = (
        timestamp.minute
        - timestamp.minute % 5
    )

    return timestamp.replace(
        minute=minute,
        second=0,
        microsecond=0,
    )


def seed_history(
    data,
):
    raw = wb.bars(
        data,
        config.HISTORY_COUNT,
    )

    if len(raw) < 60:
        raise RuntimeError(
            f"Only {len(raw)} usable historical "
            "bars returned"
        )

    return [
        Bar(**x)
        for x in raw
    ]


def update_live_bar(
    history,
    tick,
):
    if not tick:
        return history

    price = tick.get(
        "price"
    )

    if price is None or price <= 0:
        return history

    timestamp = tick.get(
        "timestamp"
    )

    if not isinstance(
        timestamp,
        datetime,
    ):
        timestamp = datetime.now(
            timezone.utc
        )

    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(
            tzinfo=timezone.utc
        )

    timestamp = timestamp.astimezone(
        timezone.utc
    )

    bucket = bar_bucket(
        timestamp
    )

    volume = float(
        tick.get(
            "volume",
            0,
        )
        or 0
    )

    if not history:
        history.append(
            Bar(
                timestamp=bucket,
                open=price,
                high=price,
                low=price,
                close=price,
                volume=volume,
            )
        )

        return history

    last = history[-1]

    last_bucket = bar_bucket(
        last.timestamp
    )

    if bucket > last_bucket:
        history.append(
            Bar(
                timestamp=bucket,
                open=price,
                high=price,
                low=price,
                close=price,
                volume=volume,
            )
        )

    elif bucket == last_bucket:
        history[-1] = Bar(
            timestamp=last.timestamp,
            open=last.open,
            high=max(
                last.high,
                price,
            ),
            low=min(
                last.low,
                price,
            ),
            close=price,
            volume=max(
                last.volume,
                volume,
            ),
        )

    else:
        # Out-of-order tick. Do not corrupt the
        # current bar.
        return history

    # Keep a manageable history while preserving
    # enough bars for all indicators.
    if len(history) > config.HISTORY_COUNT + 20:
        del history[
            :-config.HISTORY_COUNT
        ]

    return history


def remove_stale_forming_bar(
    history,
    live_tick,
):
    """Remove the REST API's current/stale bar.

    The first live tick becomes authoritative for
    the new current forming bar.
    """

    if not history:
        return history

    timestamp = live_tick.get(
        "timestamp"
    )

    if not isinstance(
        timestamp,
        datetime,
    ):
        return history

    bucket = bar_bucket(
        timestamp
    )

    while (
        history
        and bar_bucket(
            history[-1].timestamp
        ) >= bucket
    ):
        history.pop()

    return history


def stream_tick_fresh(
    tick,
):
    if not tick:
        return False

    received = tick.get(
        "received_monotonic"
    )

    if received is None:
        return False

    return (
        time.monotonic()
        - received
        <= STREAM_STALE_SECONDS
    )


# ============================================================
# ORDER RECONCILIATION
# ============================================================

def entry_fill_state(
    trade,
    state,
):
    order = (
        state.get(
            "entry_order"
        )
        or {}
    )

    cid = order.get(
        "client_order_id"
    )

    if not cid:
        return state, False

    detail = wb.order_detail(
        trade,
        cid,
    )

    order.update({
        "status": detail.get(
            "status"
        ),
        "status_class": detail.get(
            "status_class"
        ),
        "filled_qty": detail.get(
            "filled_qty"
        ),
        "filled_price": detail.get(
            "filled_price"
        ),
        "total_qty": detail.get(
            "total_qty"
        ),
        "order_id": detail.get(
            "order_id"
        ),
    })

    position_result = wb.positions(
        trade
    )

    if not position_result.get(
        "success"
    ):
        log.warning(
            "ENTRY RECONCILE: position query failed; "
            "keeping PENDING_ENTRY"
        )

        return state, False

    pos = (
        wb.find_matching_option_position(
            position_result.get(
                "positions"
            ),
            state["position"]["contract"],
            state["position"]["side"],
        )
        if state.get("position")
        else None
    )

    if (
        isinstance(
            pos,
            dict,
        )
        and pos.get(
            "ambiguous"
        )
    ):
        state["state"] = (
            "RECOVERY_REQUIRED"
        )

        state["last_error"] = (
            "Multiple matching Webull option positions; "
            "refusing to guess"
        )

        return state, True

    status_class = detail.get(
        "status_class"
    )

    filled_qty = (
        detail.get(
            "filled_qty"
        )
        or 0
    )

    filled_price = detail.get(
        "filled_price"
    )

    submitted_at = order.get(
        "submitted_at"
    )

    if (
        status_class == "PENDING"
        and submitted_at
    ):
        try:
            age = (
                datetime.now(
                    timezone.utc
                )
                - datetime.fromisoformat(
                    submitted_at
                ).astimezone(
                    timezone.utc
                )
            ).total_seconds()

        except (
            TypeError,
            ValueError,
        ):
            age = 0

        if (
            age
            >= config.ENTRY_ORDER_TIMEOUT_SECONDS
        ):
            try:
                cancel = wb.cancel_order(
                    trade,
                    cid,
                )

                log.warning(
                    "ENTRY TIMEOUT: cancel %s -> %s",
                    cid,
                    cancel,
                )

            except Exception:
                log.exception(
                    "ENTRY TIMEOUT: cancel failed"
                )

            return state, True

    if (
        pos
        and (
            pos.get(
                "quantity"
            )
            or 0
        ) > 0
    ):
        actual_qty = int(
            pos["quantity"]
        )

        actual_entry = (
            filled_price
            or pos.get(
                "cost_price"
            )
        )

        state["position"].update({
            "quantity": actual_qty,
            "entry_premium": actual_entry,
            "filled_qty": filled_qty,
            "entry_order_status": detail.get(
                "status"
            ),
            "position_cost_price": pos.get(
                "cost_price"
            ),
        })

        state["state"] = "OPEN"

        log.info(
            "STATE PENDING_ENTRY -> OPEN: "
            "actual_qty=%s entry_premium=%s order_status=%s",
            actual_qty,
            actual_entry,
            detail.get("status"),
        )

        if (
            status_class
            == "PARTIAL_FILLED"
            and filled_qty
            < (
                detail.get(
                    "total_qty"
                )
                or filled_qty
            )
        ):
            try:
                cancel = wb.cancel_order(
                    trade,
                    cid,
                )

                log.info(
                    "ENTRY PARTIAL: cancel remainder result=%s",
                    cancel,
                )

            except Exception:
                log.exception(
                    "ENTRY PARTIAL: failed to cancel remainder"
                )

        return state, True

    if status_class in (
        "REJECTED",
        "CANCELED",
    ):
        state["state"] = "FLAT"
        state["position"] = None
        state["entry_order"] = None

        state["last_error"] = (
            f"Entry order ended "
            f"{detail.get('status')} "
            "without a Webull position"
        )

        return state, True

    if status_class == "FILLED":
        state["state"] = (
            "PENDING_ENTRY"
        )

        state["last_error"] = (
            "Order reports FILLED but matching Webull "
            "position is not yet visible"
        )

        return state, True

    return state, False


def reconcile_exit(
    trade,
    state,
):
    order = (
        state.get(
            "exit_order"
        )
        or {}
    )

    cid = order.get(
        "client_order_id"
    )

    if not cid:
        return state, False

    detail = wb.order_detail(
        trade,
        cid,
    )

    order.update({
        "status": detail.get(
            "status"
        ),
        "status_class": detail.get(
            "status_class"
        ),
        "filled_qty": detail.get(
            "filled_qty"
        ),
        "filled_price": detail.get(
            "filled_price"
        ),
        "total_qty": detail.get(
            "total_qty"
        ),
        "order_id": detail.get(
            "order_id"
        ),
    })

    position_result = wb.positions(
        trade
    )

    if not position_result.get(
        "success"
    ):
        log.warning(
            "EXIT RECONCILE: position query failed; "
            "retaining PENDING_EXIT"
        )

        return state, False

    pos = wb.find_matching_option_position(
        position_result.get(
            "positions"
        ),
        state["position"]["contract"],
        state["position"]["side"],
    )

    if (
        isinstance(
            pos,
            dict,
        )
        and pos.get(
            "ambiguous"
        )
    ):
        state["state"] = (
            "RECOVERY_REQUIRED"
        )

        state["last_error"] = (
            "Multiple matching positions after exit"
        )

        return state, True

    remaining = (
        int(
            pos.get(
                "quantity"
            )
            or 0
        )
        if pos
        else 0
    )

    filled_qty = int(
        detail.get(
            "filled_qty"
        )
        or 0
    )

    status_class = detail.get(
        "status_class"
    )

    if remaining == 0:
        state["state"] = "FLAT"
        state["position"] = None
        state["exit_order"] = None
        state["last_trade"] = time.time()

        log.info(
            "STATE PENDING_EXIT -> FLAT: "
            "exit_fill_qty=%s exit_avg=%s status=%s",
            filled_qty,
            detail.get(
                "filled_price"
            ),
            detail.get(
                "status"
            ),
        )

        return state, True

    if status_class in (
        "REJECTED",
        "CANCELED",
    ):
        state["position"]["quantity"] = (
            remaining
        )

        state["state"] = "OPEN"
        state["exit_order"] = None

        state["last_error"] = (
            f"Exit order ended "
            f"{detail.get('status')} "
            f"with {remaining} contracts remaining"
        )

        return state, True

    state["position"]["quantity"] = (
        remaining
    )

    return state, False


# ============================================================
# STARTUP RECOVERY
# ============================================================

def recover_from_webull(
    trade,
    state,
):
    result = wb.positions(
        trade
    )

    if not result.get(
        "success"
    ):
        state["state"] = (
            "RECOVERY_REQUIRED"
        )

        state["last_error"] = (
            "Webull position query failed during startup recovery"
        )

        return state

    items = wb._position_items(
        result.get(
            "positions"
        )
    )

    option_positions = []

    for raw in items:
        p = wb.normalize_position(
            raw
        )

        if (
            p.get(
                "instrument_type"
            )
            == "OPTION"
            and (
                p.get(
                    "quantity"
                )
                or 0
            ) > 0
        ):
            option_positions.append(
                p
            )

    if not option_positions:
        if (
            state.get(
                "state"
            )
            == "PENDING_ENTRY"
            and state.get(
                "entry_order",
                {},
            ).get(
                "client_order_id"
            )
        ):
            cid = state[
                "entry_order"
            ][
                "client_order_id"
            ]

            try:
                detail = wb.order_detail(
                    trade,
                    cid,
                )

                if (
                    detail.get(
                        "status_class"
                    )
                    == "PENDING"
                ):
                    cancel = wb.cancel_order(
                        trade,
                        cid,
                    )

                    log.warning(
                        "STARTUP RECOVERY: canceled stale "
                        "pending entry %s -> %s",
                        cid,
                        cancel,
                    )

                elif (
                    detail.get(
                        "status_class"
                    )
                    == "FILLED"
                ):
                    state["last_error"] = (
                        "Entry order reports FILLED but "
                        "no position is visible yet"
                    )

                    state["state"] = (
                        "PENDING_ENTRY"
                    )

                    return state

            except Exception:
                log.exception(
                    "STARTUP RECOVERY: could not safely inspect "
                    "pending entry"
                )

                state["state"] = (
                    "RECOVERY_REQUIRED"
                )

                state["last_error"] = (
                    "Could not reconcile pending entry order after restart"
                )

                return state

        state["state"] = "FLAT"
        state["position"] = None
        state["entry_order"] = None
        state["exit_order"] = None

        return state

    if len(option_positions) > 1:
        state["state"] = (
            "RECOVERY_REQUIRED"
        )

        state["last_error"] = (
            "More than one open option position exists in Webull Sandbox"
        )

        return state

    p = option_positions[0]

    old = (
        state.get(
            "position"
        )
        or {}
    )

    contract = old.get(
        "contract"
    )

    if not contract:
        contract = {
            "symbol": None,
            "strike_price": p.get(
                "strike"
            ),
            "expiration_date": p.get(
                "expiration"
            ),
            "option_type": p.get(
                "option_type"
            ),
        }

    state["position"] = {
        **old,
        "side": p.get(
            "option_type"
        ),
        "symbol": (
            contract.get(
                "symbol"
            )
            or p.get(
                "symbol"
            )
        ),
        "contract": contract,
        "quantity": int(
            p.get(
                "quantity"
            )
            or 0
        ),
        "entry_premium": (
            old.get(
                "entry_premium"
            )
            or p.get(
                "cost_price"
            )
        ),
        "entry_underlying": old.get(
            "entry_underlying"
        ),
        "entry_atr": old.get(
            "entry_atr"
        ),
        "entry_time": (
            old.get(
                "entry_time"
            )
            or datetime.now(
                timezone.utc
            ).isoformat()
        ),
    }

    if (
        state["state"]
        != "PENDING_EXIT"
    ):
        state["state"] = "OPEN"

    return state


# ============================================================
# EXIT RETRY
# ============================================================

def retry_stale_exit(
    trade,
    data,
    stream,
    state,
):
    order = (
        state.get(
            "exit_order"
        )
        or {}
    )

    submitted = order.get(
        "submitted_at"
    )

    if not submitted:
        return state

    try:
        age = (
            datetime.now(
                timezone.utc
            )
            - datetime.fromisoformat(
                submitted
            ).astimezone(
                timezone.utc
            )
        ).total_seconds()

    except (
        TypeError,
        ValueError,
    ):
        age = 0

    if (
        age
        < config.EXIT_ORDER_TIMEOUT_SECONDS
    ):
        return state

    retries = int(
        order.get(
            "retries"
        )
        or 0
    )

    if (
        retries
        >= config.MAX_EXIT_RETRIES
    ):
        state["state"] = (
            "RECOVERY_REQUIRED"
        )

        state["last_error"] = (
            "Exit remained unresolved after maximum retries"
        )

        return state

    cid = order.get(
        "client_order_id"
    )

    if cid:
        try:
            cancel = wb.cancel_order(
                trade,
                cid,
            )

            log.warning(
                "EXIT RETRY: cancel %s -> %s",
                cid,
                cancel,
            )

        except Exception:
            log.exception(
                "EXIT RETRY: cancel failed"
            )

            return state

    pos_result = wb.positions(
        trade
    )

    if not pos_result.get(
        "success"
    ):
        return state

    pos = (
        state.get(
            "position"
        )
        or {}
    )

    actual = wb.find_matching_option_position(
        pos_result.get(
            "positions"
        ),
        pos.get(
            "contract"
        )
        or {},
        pos.get(
            "side"
        ),
    )

    if (
        not actual
        or actual.get(
            "ambiguous"
        )
    ):
        state["state"] = (
            "RECOVERY_REQUIRED"
        )

        state["last_error"] = (
            "Could not uniquely verify remaining position "
            "before exit retry"
        )

        return state

    remaining = int(
        actual.get(
            "quantity"
        )
        or 0
    )

    if remaining <= 0:
        state["state"] = "FLAT"
        state["position"] = None
        state["exit_order"] = None

        return state

    symbol = pos.get(
        "symbol"
    )

    quote = (
        stream.option_quote_live(
            symbol
        )
        if symbol
        else None
    )

    if not quote:
        log.warning(
            "EXIT RETRY: no fresh live quote for %s",
            symbol,
        )

        return state

    bid = quote.get(
        "bid"
    )

    if bid is None or bid <= 0:
        return state

    price = aggressive_exit_price(
        bid
    )

    if price is None:
        return state

    result = wb.exit_order(
        pos["contract"],
        pos["side"],
        remaining,
        price,
    )

    if not result.get(
        "success"
    ):
        order["retries"] = (
            retries + 1
        )

        order["last_retry_error"] = str(
            result
        )

        order["submitted_at"] = (
            datetime.now(
                timezone.utc
            ).isoformat()
        )

        return state

    state["exit_order"] = {
        **order,
        "client_order_id": result.get(
            "client_order_id"
        ),
        "status": result.get(
            "status"
        ),
        "status_class": result.get(
            "status_class"
        ),
        "requested_qty": remaining,
        "submitted_at": datetime.now(
            timezone.utc
        ).isoformat(),
        "retries": retries + 1,
        "bid_at_submission": bid,
        "limit_price": price,
    }

    return state


# ============================================================
# RISK
# ============================================================

def risk_reason(
    pos,
    signal_snapshot,
    option_quote,
    dt,
):
    if force_exit_due(dt):
        return "FORCED_EOD_LIQUIDATION"

    if (
        position_age_seconds(pos)
        >= config.MAX_HOLD_MINUTES * 60
    ):
        return "TIME_STOP"

    premium = option_quote.get(
        "bid"
    )

    entry = pos.get(
        "entry_premium"
    )

    if (
        config.USE_OPTION_PREMIUM_RISK
        and premium is not None
        and entry
    ):
        change = (
            premium / entry
            - 1.0
        )

        if (
            change
            <= -config.OPTION_STOP_LOSS_PCT
        ):
            return "OPTION_MAX_LOSS"

        if (
            change
            >= config.OPTION_TAKE_PROFIT_PCT
        ):
            return "OPTION_TAKE_PROFIT"

        if (
            config.USE_BE
            and change
            >= config.OPTION_BREAKEVEN_TRIGGER_PCT
        ):
            pos[
                "option_breakeven_armed"
            ] = True

        if (
            pos.get(
                "option_breakeven_armed"
            )
            and change
            <= config.OPTION_BREAKEVEN_FLOOR_PCT
        ):
            return "OPTION_BREAKEVEN"

    if (
        signal_snapshot.get(
            "atr"
        )
        is not None
        and pos.get(
            "entry_underlying"
        )
        is not None
    ):
        stop, target, be = levels(
            pos
        )

        if (
            config.USE_BE
            and pos.get(
                "side"
            )
            == "CALL"
            and signal_snapshot[
                "close"
            ]
            >= be
        ):
            stop = max(
                stop,
                pos[
                    "entry_underlying"
                ],
            )

        if (
            config.USE_BE
            and pos.get(
                "side"
            )
            == "PUT"
            and signal_snapshot[
                "close"
            ]
            <= be
        ):
            stop = min(
                stop,
                pos[
                    "entry_underlying"
                ],
            )

        if pos.get(
            "side"
        ) == "CALL":
            if (
                signal_snapshot[
                    "close"
                ]
                <= stop
            ):
                return "UNDERLYING_STOP"

            if (
                signal_snapshot[
                    "close"
                ]
                >= target
            ):
                return "UNDERLYING_TARGET"

            if (
                config.USE_ZONE
                and signal_snapshot.get(
                    "upper"
                ) is not None
                and signal_snapshot[
                    "close"
                ]
                >= signal_snapshot[
                    "upper"
                ]
            ):
                return "WAVE_ZONE"

        else:
            if (
                signal_snapshot[
                    "close"
                ]
                >= stop
            ):
                return "UNDERLYING_STOP"

            if (
                signal_snapshot[
                    "close"
                ]
                <= target
            ):
                return "UNDERLYING_TARGET"

            if (
                config.USE_ZONE
                and signal_snapshot.get(
                    "lower"
                ) is not None
                and signal_snapshot[
                    "close"
                ]
                <= signal_snapshot[
                    "lower"
                ]
            ):
                return "WAVE_ZONE"

    return None


# ============================================================
# EXIT SUBMISSION
# ============================================================

def submit_exit(
    trade,
    stream,
    state,
    reason,
):
    pos = (
        state.get(
            "position"
        )
        or {}
    )

    quantity = int(
        pos.get(
            "quantity"
        )
        or 0
    )

    if quantity <= 0:
        raise RuntimeError(
            "Cannot exit: actual position quantity is zero"
        )

    positions_result = wb.positions(
        trade
    )

    if not positions_result.get(
        "success"
    ):
        raise RuntimeError(
            "Cannot verify Webull position before exit"
        )

    actual = wb.find_matching_option_position(
        positions_result.get(
            "positions"
        ),
        pos.get(
            "contract"
        )
        or {},
        pos.get(
            "side"
        ),
    )

    if (
        not actual
        or actual.get(
            "ambiguous"
        )
    ):
        raise RuntimeError(
            "Cannot uniquely verify Webull position before exit"
        )

    quantity = int(
        actual.get(
            "quantity"
        )
        or 0
    )

    if quantity <= 0:
        raise RuntimeError(
            "Webull position disappeared before exit submission"
        )

    symbol = pos.get(
        "symbol"
    )

    quote = (
        stream.option_quote_live(
            symbol
        )
        if symbol
        else None
    )

    if not quote:
        raise RuntimeError(
            "No fresh live option quote available for exit"
        )

    bid = quote.get(
        "bid"
    )

    if bid is None or bid <= 0:
        raise RuntimeError(
            "No valid live option bid available for exit"
        )

    price = aggressive_exit_price(
        bid
    )

    if price is None:
        raise RuntimeError(
            "Could not calculate valid aggressive exit price"
        )

    log.warning(
        "EXIT PRICING: reason=%s symbol=%s qty=%s "
        "bid=%.2f -> limit=%.2f",
        reason,
        symbol,
        quantity,
        bid,
        price,
    )

    result = wb.exit_order(
        pos["contract"],
        pos["side"],
        quantity,
        price,
    )

    if config.DRY_RUN:
        state["state"] = "FLAT"
        state["position"] = None
        state["exit_order"] = None
        state["last_trade"] = time.time()

        return state

    if not result.get(
        "success"
    ):
        raise RuntimeError(
            f"Webull exit was not accepted: {result}"
        )

    state["state"] = (
        "PENDING_EXIT"
    )

    state["exit_order"] = {
        "client_order_id": result.get(
            "client_order_id"
        ),
        "status": result.get(
            "status"
        ),
        "status_class": result.get(
            "status_class"
        ),
        "requested_qty": quantity,
        "reason": reason,
        "submitted_at": datetime.now(
            timezone.utc
        ).isoformat(),
        "filled_qty": result.get(
            "filled_qty"
        ) or 0,
        "filled_price": result.get(
            "filled_price"
        ),
        "bid_at_submission": bid,
        "limit_price": price,
        "retries": 0,
    }

    log.info(
        "STATE OPEN -> PENDING_EXIT reason=%s "
        "qty=%s order=%s status=%s "
        "bid=%.2f limit=%.2f",
        reason,
        quantity,
        result.get(
            "client_order_id"
        ),
        result.get(
            "status"
        ),
        bid,
        price,
    )

    return state


# ============================================================
# ENTRY
# ============================================================

def maybe_enter(
    trade,
    data,
    stream,
    state,
    snapshot,
):
    if not snapshot.get(
        "signal"
    ):
        return state

    if not new_entries_allowed():
        return state

    if state.get(
        "state"
    ) != "FLAT":
        return state

    if (
        time.time()
        - state.get(
            "last_trade",
            0,
        )
        < config.COOLDOWN
    ):
        return state

    # Prevent repeated entry orders from the
    # same intrabar transition.
    if (
        snapshot.get(
            "signal_event_id"
        )
        == state.get(
            "last_signal_event"
        )
    ):
        return state

    option_type = snapshot[
        "signal"
    ]

    contract = wb.choose(
        data,
        option_type,
        snapshot[
            "close"
        ],
    )

    symbol = contract.get(
        "symbol"
    )

    if not symbol:
        log.warning(
            "ENTRY SKIP: selected option has no symbol"
        )

        return state

    # Subscribe before requesting the live quote.
    stream.subscribe_option(
        symbol
    )

    quote = stream.wait_for_option_quote(
        symbol,
        OPTION_STREAM_WAIT_SECONDS,
    )

    if not quote:
        log.info(
            "ENTRY SKIP: no fresh live option quote for %s",
            symbol,
        )

        return state

    bid = quote.get(
        "bid"
    )

    ask = quote.get(
        "ask"
    )

    if (
        bid is None
        or ask is None
        or ask <= 0
        or bid <= 0
        or ask < bid
    ):
        return state

    mid = (
        bid + ask
    ) / 2

    spread = (
        (ask - bid)
        / mid
        if mid
        else float("inf")
    )

    if not (
        config.MIN_PREMIUM
        <= mid
        <= config.MAX_PREMIUM
        and spread
        <= config.MAX_SPREAD
    ):
        log.info(
            "ENTRY SKIP: %s premium=%.2f spread=%.1f%%",
            symbol,
            mid,
            spread * 100,
        )

        return state

    entry_price = aggressive_entry_price(
        ask
    )

    if entry_price is None:
        return state

    log.info(
        "ENTRY PRICING: %s bid=%.2f ask=%.2f "
        "-> limit=%.2f (+%.2f)",
        symbol,
        bid,
        ask,
        entry_price,
        ENTRY_PRICE_OFFSET,
    )

    result = wb.entry_order(
        contract,
        option_type,
        config.OPTION_QUANTITY,
        entry_price,
    )

    if not result.get(
        "success"
    ):
        log.error(
            "ENTRY REJECTED: %s",
            result,
        )

        state["last_error"] = str(
            result
        )

        return state

    event_id = snapshot.get(
        "signal_event_id"
    )

    state[
        "last_signal_event"
    ] = event_id

    if config.DRY_RUN:
        state["state"] = "OPEN"

        state["entry_order"] = {
            "client_order_id": result.get(
                "client_order_id"
            ),
            "status": "DRY_RUN_FILLED",
            "status_class": "FILLED",
            "requested_qty": config.OPTION_QUANTITY,
            "filled_qty": config.OPTION_QUANTITY,
            "filled_price": entry_price,
            "simulated": True,
        }

        state["position"] = {
            "side": option_type,
            "symbol": symbol,
            "contract": contract,
            "quantity": config.OPTION_QUANTITY,
            "entry_underlying": snapshot[
                "close"
            ],
            "entry_atr": snapshot[
                "atr"
            ],
            "entry_premium": entry_price,
            "entry_time": datetime.now(
                timezone.utc
            ).isoformat(),
            "option_breakeven_armed": False,
        }

        return state

    state["state"] = (
        "PENDING_ENTRY"
    )

    state["entry_order"] = {
        "client_order_id": result.get(
            "client_order_id"
        ),
        "status": result.get(
            "status"
        ),
        "status_class": result.get(
            "status_class"
        ),
        "requested_qty": config.OPTION_QUANTITY,
        "submitted_at": datetime.now(
            timezone.utc
        ).isoformat(),
        "filled_qty": result.get(
            "filled_qty"
        ) or 0,
        "filled_price": result.get(
            "filled_price"
        ),
        "ask_at_submission": ask,
        "limit_price": entry_price,
    }

    state["position"] = {
        "side": option_type,
        "symbol": symbol,
        "contract": contract,
        "quantity": 0,
        "entry_underlying": snapshot[
            "close"
        ],
        "entry_atr": snapshot[
            "atr"
        ],
        "entry_premium": None,
        "entry_time": datetime.now(
            timezone.utc
        ).isoformat(),
        "option_breakeven_armed": False,
    }

    log.info(
        "ENTRY SUBMITTED: %s %s qty=%s "
        "order=%s ask=%.2f limit=%.2f",
        option_type,
        symbol,
        config.OPTION_QUANTITY,
        result.get(
            "client_order_id"
        ),
        ask,
        entry_price,
    )

    return state


# ============================================================
# MAIN
# ============================================================

def main():
    global running

    trade, data = wb.connect()

    state = recover_from_webull(
        trade,
        load(),
    )

    save(state)

    log.info(
        "Connected to Webull SANDBOX. "
        "DRY_RUN=%s state=%s "
        "ENTRY_OFFSET=%.2f EXIT_OFFSET=%.2f "
        "DATA_MODE=LIVE_STREAM_INTRABAR",
        config.DRY_RUN,
        state.get(
            "state"
        ),
        ENTRY_PRICE_OFFSET,
        EXIT_PRICE_OFFSET,
    )

    # --------------------------------------------------------
    # HISTORICAL WARM-UP
    # --------------------------------------------------------

    history = seed_history(
        data
    )

    log.info(
        "Historical warm-up complete: %s "
        "five-minute bars loaded",
        len(history),
    )

    # --------------------------------------------------------
    # LIVE STREAM
    # --------------------------------------------------------

    stream = wb.LiveMarketStream()

    session_id = stream.start()

    log.info(
        "Webull Sandbox live stream started "
        "session=%s "
        "mqtt=%s",
        session_id,
        wb.STREAM_MQTT_ENDPOINT,
    )

    # --------------------------------------------------------
    # FIRST LIVE TICK
    #
    # Do not trust the historical API's current forming bar.
    # The first live tick becomes the authoritative beginning
    # of the current bar.
    # --------------------------------------------------------

    first_tick = stream.wait_for_stock_tick(
        timeout=15.0
    )

    if not first_tick:
        raise RuntimeError(
            "No live SPY tick received from Webull "
            "Sandbox stream"
        )

    history = remove_stale_forming_bar(
        history,
        first_tick,
    )

    history = update_live_bar(
        history,
        first_tick,
    )

    log.info(
        "LIVE SPY STREAM ACTIVE: "
        "price=%.2f bar=%s",
        first_tick["price"],
        history[-1].timestamp,
    )

    last_logged_bar = None
    last_trend = None
    last_signal_event = None

    # --------------------------------------------------------
    # POSITION RECONCILIATION
    #
    # The live stream drives market/risk evaluation at high
    # frequency. Webull REST position reconciliation is only
    # needed periodically because risk_reason() uses the
    # locally tracked position plus live streaming quotes.
    #
    # submit_exit() still performs an immediate Webull
    # position verification before submitting an exit.
    # --------------------------------------------------------

    next_position_reconcile = 0.0

    while running:
        try:
            # ------------------------------------------------
            # PENDING ENTRY
            # ------------------------------------------------

            if (
                state.get(
                    "state"
                )
                == "PENDING_ENTRY"
            ):
                state, _ = entry_fill_state(
                    trade,
                    state,
                )

                save(state)

                if (
                    state.get(
                        "state"
                    )
                    == "PENDING_ENTRY"
                ):
                    time.sleep(
                        config.RECOVERY_POLL_SECONDS
                    )

                    continue

            # ------------------------------------------------
            # PENDING EXIT
            # ------------------------------------------------

            if (
                state.get(
                    "state"
                )
                == "PENDING_EXIT"
            ):
                state, _ = reconcile_exit(
                    trade,
                    state,
                )

                if (
                    state.get(
                        "state"
                    )
                    == "PENDING_EXIT"
                ):
                    state = retry_stale_exit(
                        trade,
                        data,
                        stream,
                        state,
                    )

                save(state)

                if (
                    state.get(
                        "state"
                    )
                    == "PENDING_EXIT"
                ):
                    time.sleep(
                        config.RECOVERY_POLL_SECONDS
                    )

                    continue

            # ------------------------------------------------
            # RECOVERY
            # ------------------------------------------------

            if (
                state.get(
                    "state"
                )
                == "RECOVERY_REQUIRED"
            ):
                log.error(
                    "RECOVERY_REQUIRED: %s",
                    state.get(
                        "last_error"
                    ),
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

            # ------------------------------------------------
            # LIVE SPY TICK
            # ------------------------------------------------

            tick = stream.stock_tick()

            if not stream_tick_fresh(
                tick
            ):
                log.warning(
                    "LIVE DATA STALE: no fresh SPY "
                    "tick; trading paused"
                )

                time.sleep(
                    0.25
                )

                continue

            history = update_live_bar(
                history,
                tick,
            )

            if len(history) < 60:
                time.sleep(
                    0.1
                )

                continue

            # ------------------------------------------------
            # INTRABAR STRATEGY
            # ------------------------------------------------

            snapshot = analyze(
                history
            )

            current_bar = history[
                -1
            ]

            current_bar_id = (
                current_bar.timestamp.isoformat()
            )

            current_trend = snapshot.get(
                "trend"
            )

            signal = snapshot.get(
                "signal"
            )

            # A signal is a transition between the
            # previous calculated trend and the current
            # live trend.
            #
            # The same signal must not generate repeated
            # orders on every tick.
            signal_event_id = None

            if signal:
                signal_event_id = (
                    current_bar_id
                    + ":"
                    + signal
                )

            snapshot[
                "signal_event_id"
            ] = signal_event_id

            if (
                current_trend
                != last_trend
            ):
                log.info(
                    "INTRABAR TREND FLIP: "
                    "SPY=%.2f trend=%s signal=%s "
                    "bar=%s",
                    snapshot[
                        "close"
                    ],
                    (
                        "UP"
                        if current_trend
                        == 1
                        else "DOWN"
                    ),
                    signal or "-",
                    current_bar_id,
                )

                last_trend = current_trend

            if (
                signal
                and signal_event_id
                != last_signal_event
            ):
                log.info(
                    "INTRABAR SIGNAL: %s "
                    "SPY=%.2f bar=%s",
                    signal,
                    snapshot[
                        "close"
                    ],
                    current_bar_id,
                )

                last_signal_event = (
                    signal_event_id
                )

            # ------------------------------------------------
            # OPEN POSITION
            # ------------------------------------------------

            pos = state.get(
                "position"
            )

            if (
                state.get(
                    "state"
                )
                == "OPEN"
                and pos
            ):
                # Webull position reconciliation is deliberately
                # throttled. The live stream remains the source
                # for intrabar price/risk decisions.
                reconcile_now = time.monotonic()

                if (
                    reconcile_now
                    >= next_position_reconcile
                ):
                    next_position_reconcile = (
                        reconcile_now
                        + config.RECOVERY_POLL_SECONDS
                    )

                    p_result = wb.positions(
                        trade
                    )

                    if not p_result.get(
                        "success"
                    ):
                        log.warning(
                            "POSITION MONITOR: "
                            "Webull position lookup failed; "
                            "continuing live risk evaluation"
                        )

                    else:
                        actual = (
                            wb.find_matching_option_position(
                                p_result.get(
                                    "positions"
                                ),
                                pos.get(
                                    "contract"
                                )
                                or {},
                                pos.get(
                                    "side"
                                ),
                            )
                        )

                        if (
                            isinstance(
                                actual,
                                dict,
                            )
                            and actual.get(
                                "ambiguous"
                            )
                        ):
                            state[
                                "state"
                            ] = (
                                "RECOVERY_REQUIRED"
                            )

                            state[
                                "last_error"
                            ] = (
                                "Ambiguous live position during monitoring"
                            )

                            save(state)

                            continue

                        if actual is None:
                            log.warning(
                                "POSITION MONITOR: "
                                "expected position is absent; "
                                "skipping risk evaluation until next reconciliation"
                            )

                            continue

                        pos[
                            "quantity"
                        ] = int(
                            actual.get(
                                "quantity"
                            )
                            or 0
                        )

                        pos[
                            "position_cost_price"
                        ] = actual.get(
                            "cost_price"
                        )

                        if not pos.get(
                            "entry_premium"
                        ):
                            pos[
                                "entry_premium"
                            ] = actual.get(
                                "cost_price"
                            )

                symbol = pos.get(
                    "symbol"
                )

                option_quote = (
                    stream.option_quote_live(
                        symbol
                    )
                    if symbol
                    else None
                )

                if not option_quote:
                    log.warning(
                        "OPTION DATA STALE: "
                        "no fresh quote for %s; "
                        "risk decisions paused",
                        symbol,
                    )

                    time.sleep(
                        0.1
                    )

                    continue

                reason = risk_reason(
                    pos,
                    snapshot,
                    option_quote,
                    now_et(),
                )

                if reason:
                    state = submit_exit(
                        trade,
                        stream,
                        state,
                        reason,
                    )

                    save(state)

                    continue

            # ------------------------------------------------
            # FLAT / ENTRY
            # ------------------------------------------------

            elif (
                state.get(
                    "state"
                )
                == "FLAT"
            ):
                if not force_exit_due():
                    state = maybe_enter(
                        trade,
                        data,
                        stream,
                        state,
                        snapshot,
                    )

                    save(state)

            state[
                "last_bar"
            ] = current_bar_id

            save(state)

            # The live stream drives the loop.
            # There is deliberately no 15-second market
            # data polling delay anymore.
            time.sleep(
                0.05
            )

        except Exception as exc:
            state[
                "last_error"
            ] = str(exc)

            save(state)

            log.exception(
                "Loop error: %s",
                exc,
            )

            # Do not hammer Webull after an error.
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
