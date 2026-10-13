"""Tight-stop counterfactual ledger: -20% stop on unarmed trades.

Mirrors the real (gated) strategy's entries 1:1 at 1 contract, but any
trade that dips -20% before arming (+25%) is scored as stopped at -20%.
Armed trades, and trades that never dip -20%, score their real P&L.

This answers: "where would we be if the stop were -20% for unarmed
trades?" Compare tight_pnl against the real 1-contract paper P&L.
"""

import json
import logging
import os

log = logging.getLogger("tight_stop")

_ARM_PCT = 0.25
_TIGHT_STOP_PCT = 0.20


def _state_path() -> str:
    return os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "tight_stop_state.json",
    )


def default_state() -> dict:
    return {
        "tight_pnl": 0.0,
        "trades": 0,
        "stops_triggered": 0,
        "saved_vs_real": 0.0,
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


def score_trade(entry_premium, peak_premium, low_premium, exit_premium):
    """Return (tight_pnl_1c, real_pnl_1c, stopped_early)."""
    try:
        entry = float(entry_premium)
        peak = float(peak_premium)
        low = float(low_premium)
        exit_px = float(exit_premium)
    except (TypeError, ValueError):
        return None, None, False
    if entry <= 0 or exit_px <= 0:
        return None, None, False
    real_pnl = round((exit_px - entry) * 100.0, 2)
    armed = peak >= entry * (1.0 + _ARM_PCT)
    stopped_early = (not armed) and (low <= entry * (1.0 - _TIGHT_STOP_PCT))
    if stopped_early:
        tight_pnl = round(-_TIGHT_STOP_PCT * entry * 100.0, 2)
    else:
        tight_pnl = real_pnl
    return tight_pnl, real_pnl, stopped_early


def on_trade(entry_premium, peak_premium, low_premium, exit_premium) -> None:
    """Score one closed real trade under the tight-stop rule."""
    tight_pnl, real_pnl, stopped_early = score_trade(
        entry_premium, peak_premium, low_premium, exit_premium
    )
    if tight_pnl is None:
        return
    st = load()
    st["tight_pnl"] = round(
        float(st.get("tight_pnl", 0.0) or 0.0) + tight_pnl, 2
    )
    st["trades"] = int(st.get("trades", 0) or 0) + 1
    if stopped_early:
        st["stops_triggered"] = int(st.get("stops_triggered", 0) or 0) + 1
    st["saved_vs_real"] = round(
        float(st.get("saved_vs_real", 0.0) or 0.0)
        + (tight_pnl - real_pnl),
        2,
    )
    save(st)
    log.info(
        "TIGHT-STOP: entry $%.2f peak $%.2f low $%.2f exit $%.2f -> "
        "tight $%+.2f (real $%+.2f)%s",
        float(entry_premium),
        float(peak_premium),
        float(low_premium),
        float(exit_premium),
        tight_pnl,
        real_pnl,
        " STOPPED EARLY" if stopped_early else "",
    )


def summary() -> dict:
    st = load()
    return {
        "tight_pnl": round(float(st.get("tight_pnl", 0.0) or 0.0), 2),
        "trades": st.get("trades", 0),
        "stops_triggered": st.get("stops_triggered", 0),
        "saved_vs_real": round(
            float(st.get("saved_vs_real", 0.0) or 0.0), 2
        ),
    }
