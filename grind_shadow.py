"""Continuation ("grind") shadow ledger.

Catches sustained trends WITHOUT a flip: when the SuperTrend has been
established for 30+ minutes (no recent transition) and price keeps making
new 3-bar lows (puts) or highs (calls), open a hypothetical 1-contract
paper trade. Same exits as the main strategy (+50% TP, -35% SL, arm +25%
-> floor +10%, 11% trail). After any close, a fresh new-low/high
re-enters while the grind persists.

This is the counterfactual for "what if we traded trend continuation,
not just reversals?" Live trading is untouched.
"""

import json
import logging
import os
import time

log = logging.getLogger("grind_shadow")

# A grind is continuation, not a flip: the trend must be this old.
GRIND_MIN_TREND_AGE_S = 30 * 60
# Entry on a new N-bar low (puts) / high (calls).
GRIND_LOOKBACK_BARS = 3
# Don't re-enter on the very next tick after a close.
GRIND_REENTRY_COOLDOWN_S = 60

_TP_PCT = 0.50
_SL_PCT = 0.35
_ARM_PCT = 0.25
_FLOOR_PCT = 0.10
_TRAIL_PCT = 0.11


def _state_path() -> str:
    return os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "grind_state.json",
    )


def _diary_path() -> str:
    return os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "grind_diary.jsonl",
    )


def default_state() -> dict:
    return {
        "open": None,
        "paper_pnl": 0.0,
        "paper_trades": 0,
        "by_day": {},
        "last_trend": None,
        "last_flip_ts": 0.0,
        "last_close_ts": 0.0,
    }


def load() -> dict:
    try:
        with open(_state_path(), "r", encoding="utf-8") as fh:
            st = json.load(fh)
        if isinstance(st, dict):
            for k, v in default_state().items():
                st.setdefault(k, v)
            return st
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    return default_state()


def save(st: dict) -> None:
    tmp = _state_path() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(st, fh, indent=2, default=str)
    os.replace(tmp, _state_path())


def _record(event: dict) -> None:
    try:
        from datetime import datetime, timezone

        event = dict(event)
        event.setdefault(
            "ts", datetime.now(timezone.utc).isoformat()
        )
        event["grind"] = True
        with open(_diary_path(), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, default=str) + "\n")
    except Exception:
        log.exception("grind diary write failed")


def _day_key() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _close_position(st: dict, bid, reason: str) -> None:
    rec = st.get("open")
    if not rec:
        return
    st["open"] = None
    st["last_close_ts"] = time.time()
    try:
        exit_px = float(bid)
        entry = float(rec["entry_ask"])
        pnl = round((exit_px - entry) * 100.0, 2) if exit_px > 0 else None
    except (TypeError, ValueError):
        pnl = None
    if pnl is not None:
        st["paper_pnl"] = round(
            float(st.get("paper_pnl", 0.0) or 0.0) + pnl, 2
        )
        st["paper_trades"] = int(st.get("paper_trades", 0) or 0) + 1
        day = _day_key()
        bd = st.setdefault("by_day", {}).setdefault(
            day, {"paper_pnl": 0.0, "trades": 0}
        )
        bd["paper_pnl"] = round(bd["paper_pnl"] + pnl, 2)
        bd["trades"] += 1
    _record(
        {
            "event": "close",
            "id": rec.get("id"),
            "direction": rec.get("direction"),
            "entry_ask": rec.get("entry_ask"),
            "exit_bid": bid,
            "reason": reason,
            "pnl_1c": pnl,
            "paper_pnl": round(
                float(st.get("paper_pnl", 0.0) or 0.0), 2
            ),
        }
    )
    save(st)
    log.info(
        "GRIND CLOSE: %s %s reason=%s pnl=%s",
        rec.get("direction"),
        rec.get("symbol"),
        reason,
        ("%+.2f" % pnl) if pnl is not None else "n/a",
    )


def on_tick(
    trend,
    lows,
    highs,
    spy,
    pick_quote,
    can_enter,
    now_ts=None,
) -> None:
    """One tick of the grind shadow.

    trend: +1 / -1 / 0 from analyze().
    lows/highs: recent bar lows/highs (list, oldest->newest).
    pick_quote(direction): -> (symbol, bid, ask) or None.
    can_enter: entry window open (hours/cutoff) and no live position.
    """
    now = now_ts or time.time()
    st = load()

    # Track flips: a grind must NOT be a fresh flip.
    last_trend = st.get("last_trend")
    if trend != last_trend:
        st["last_trend"] = trend
        st["last_flip_ts"] = now
        # A flip kills any open grind position (trend changed).
        if st.get("open"):
            _close_position(st, None, "GRIND_FLIP")
        save(st)
        return

    rec = st.get("open")
    if rec:
        # Manage the open hypothetical: need a fresh bid.
        direction = rec.get("direction")
        pq = pick_quote(direction)
        if not pq:
            return
        _symbol, bid, _ask = pq
        try:
            bid_f = float(bid)
            if bid_f <= 0:
                return
        except (TypeError, ValueError):
            return
        entry = float(rec["entry_ask"])
        peak = float(rec.get("peak", entry))
        if bid_f > peak:
            peak = bid_f
            rec["peak"] = peak
        change = bid_f / entry - 1.0

        if change <= -_SL_PCT:
            _close_position(st, bid_f, "GRIND_MAX_LOSS")
            return
        if change >= _TP_PCT:
            _close_position(st, bid_f, "GRIND_TAKE_PROFIT")
            return
        if change >= _ARM_PCT:
            rec["armed"] = True
        if rec.get("armed"):
            trail_level = peak * (1.0 - _TRAIL_PCT)
            floor_level = entry * (1.0 + _FLOOR_PCT)
            if bid_f <= max(trail_level, floor_level):
                _close_position(st, bid_f, "GRIND_ARMED_EXIT")
                return
        save(st)
        return

    # Flat: look for a grind entry.
    if not can_enter:
        return
    if now - float(st.get("last_close_ts", 0.0) or 0.0) < GRIND_REENTRY_COOLDOWN_S:
        return
    if now - float(st.get("last_flip_ts", 0.0) or 0.0) < GRIND_MIN_TREND_AGE_S:
        return
    if not lows or len(lows) < GRIND_LOOKBACK_BARS + 1:
        return

    direction = None
    if trend == -1 and lows[-1] < min(lows[-GRIND_LOOKBACK_BARS - 1 : -1]):
        direction = "PUT"
    elif trend == 1 and highs and len(highs) >= GRIND_LOOKBACK_BARS + 1:
        if highs[-1] > max(highs[-GRIND_LOOKBACK_BARS - 1 : -1]):
            direction = "CALL"
    if not direction:
        return

    pq = pick_quote(direction)
    if not pq:
        return
    symbol, _bid, ask = pq
    try:
        ask_f = float(ask)
        if ask_f <= 0:
            return
    except (TypeError, ValueError):
        return

    rec_id = "%d:%s@%.2f" % (int(now), direction, spy or 0)
    st["open"] = {
        "id": rec_id,
        "direction": direction,
        "symbol": symbol,
        "entry_ask": ask_f,
        "peak": ask_f,
        "armed": False,
        "spy": spy,
    }
    _record(
        {
            "event": "open",
            "id": rec_id,
            "direction": direction,
            "symbol": symbol,
            "ask": ask_f,
            "spy": spy,
        }
    )
    save(st)
    log.info(
        "GRIND OPEN: %s %s @ $%.2f (spy %.2f)",
        direction,
        symbol,
        ask_f,
        spy or 0,
    )


def summary() -> dict:
    st = load()
    o = st.get("open")
    return {
        "paper_pnl": round(float(st.get("paper_pnl", 0.0) or 0.0), 2),
        "paper_trades": st.get("paper_trades", 0),
        "open": bool(o),
        "open_direction": (o or {}).get("direction"),
    }
