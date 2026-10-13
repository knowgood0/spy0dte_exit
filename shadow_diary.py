"""No-gate shadow diary for the 0DTE SPY paper bot.

For every trend-flip signal, tracks what the ORIGINAL (pre-persistence-gate)
strategy would have done: enter immediately on the flip, exit when the trend
reverses (the underlying stop). Two tallies are kept:

- paper shadow: 1-contract P&L per signal, summed per day. This mirrors the
  real bot's 1-contract paper trades.
- virtual shadow: the compounding ladder (same milestone / half-skim / 35%
  tax-reserve rules as virtual_ledger) applied to the shadow trades, so the
  compounding comparison stays apples-to-apples.

How each signal resolves:
- BLOCKED (flip died before the persistence gate opened): the no-gate trade
  is assumed stopped at the reversal tick. Entry = live ask at signal time,
  exit = live bid at reversal. Measured, not estimated.
- DELAYED (flip survived; the real bot traded it): the shadow parallels the
  real trade. Shadow paper P&L = real 1-contract P&L adjusted by the
  entry-premium difference (shadow entered ~30s earlier at the signal-time
  ask). Flagged ESTIMATED because the exit path is assumed identical.

Everything here is hypothetical. No orders are placed. All failures are
swallowed by the caller so the diary can never break the live loop.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone

import config

log = logging.getLogger("shadow_diary")

_HERE = os.path.dirname(os.path.abspath(__file__))


def _resolve(name: str, default: str) -> str:
    p = getattr(config, name, default) or default
    return p if os.path.isabs(p) else os.path.join(_HERE, p)


def state_path() -> str:
    return _resolve("SHADOW_STATE_PATH", "shadow_state.json")


def diary_path() -> str:
    return _resolve("SHADOW_DIARY_PATH", "shadow_diary.jsonl")


def _enabled() -> bool:
    return bool(getattr(config, "SHADOW_ENABLED", True))


def _start_balance() -> float:
    return float(getattr(config, "VIRTUAL_START_BALANCE", 497.88))


def _milestones() -> list:
    return sorted(
        float(m)
        for m in getattr(
            config,
            "VIRTUAL_MILESTONES",
            [1000.0, 5000.0, 10000.0, 20000.0, 30000.0],
        )
    )


def _tax_rate() -> float:
    return float(getattr(config, "VIRTUAL_TAX_RATE", 0.35))


def default_state() -> dict:
    return {
        "open": None,
        "paper_pnl": 0.0,
        "paper_trades": 0,
        "by_day": {},
        "virtual": _new_ledger(),
        "fixed3": _new_ledger(),
    }


def load() -> dict:
    try:
        with open(state_path(), "r", encoding="utf-8") as fh:
            st = json.load(fh)
        if isinstance(st, dict):
            st.setdefault("virtual", _new_ledger())
            st.setdefault("fixed3", _new_ledger())
            return st
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    return default_state()


def save(st: dict) -> None:
    tmp = state_path() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(st, fh, indent=2, default=str)
    os.replace(tmp, state_path())


def _record(event: dict) -> None:
    try:
        event = dict(event)
        event.setdefault("ts", datetime.now(timezone.utc).isoformat())
        event["shadow"] = True
        with open(diary_path(), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, default=str) + "\n")
    except Exception:
        log.exception("shadow diary write failed")


def _day_key() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


# ------------------------------------------------------------
# Shadow virtual ledger (mirrors virtual_ledger's rules)
# ------------------------------------------------------------

def _ledger_advance_milestone(ld: dict, hit: float) -> None:
    nxt = None
    for m in _milestones():
        if m > hit + 1e-9:
            nxt = m
            break
    if nxt is None:
        nxt = hit * 2.0
    ld["next_milestone"] = nxt


def _ledger_skim(ld: dict, tag: str) -> None:
    rate = _tax_rate()
    while True:
        nxt = ld.get("next_milestone")
        balance = float(ld.get("balance", 0.0) or 0.0)
        if not nxt or balance < float(nxt):
            break
        hit = float(nxt)
        skim = balance / 2.0
        gains = balance - float(ld.get("basis", balance) or balance)
        tax = rate * max(gains, 0.0)
        banked = skim - tax
        ld["banked"] = float(ld.get("banked", 0.0) or 0.0) + banked
        ld["tax_reserve"] = float(ld.get("tax_reserve", 0.0) or 0.0) + tax
        ld["balance"] = skim
        ld["basis"] = skim
        ld["skims"] = int(ld.get("skims", 0) or 0) + 1
        log.info(
            "SHADOW %s SKIM (milestone $%.0f): bank $%.2f, tax $%.2f, "
            "compounding $%.2f",
            tag,
            hit,
            banked,
            tax,
            skim,
        )
        _record(
            {
                "event": "ledger_skim",
                "ledger": tag,
                "milestone": hit,
                "to_bank": round(banked, 2),
                "to_tax_reserve": round(tax, 2),
                "compounding": round(skim, 2),
            }
        )
        _ledger_advance_milestone(ld, hit)


def _v_advance_milestone(v: dict, hit: float) -> None:
    _ledger_advance_milestone(v, hit)


def _v_skim(v: dict) -> None:
    _ledger_skim(v, "VIRTUAL")


def _ledger_apply_pnl(ld: dict, pnl: float, tag: str) -> None:
    """Add a dollar P&L to a compounding ledger (balance + skim rules)."""
    if not pnl:
        return
    ld["balance"] = round(
        float(ld.get("balance", 0.0) or 0.0) + float(pnl), 2
    )
    ld["trades"] = int(ld.get("trades", 0) or 0) + 1
    _ledger_skim(ld, tag)


def _new_ledger() -> dict:
    ms = _milestones()
    return {
        "balance": _start_balance(),
        "banked": 0.0,
        "tax_reserve": 0.0,
        "basis": _start_balance(),
        "next_milestone": ms[0] if ms else None,
        "trades": 0,
        "skips": 0,
        "skims": 0,
        "position": None,
    }


def _v_entry(v: dict, symbol: str, premium) -> int:
    """Size a shadow-virtual position. Returns contracts (0 = skip)."""
    try:
        premium = float(premium)
    except (TypeError, ValueError):
        return 0
    if premium <= 0:
        return 0
    balance = float(v.get("balance", 0.0) or 0.0)
    cost = premium * 100.0
    contracts = int(balance // cost) if cost > 0 else 0
    if contracts <= 0:
        v["skips"] = int(v.get("skips", 0) or 0) + 1
        v["position"] = None
        _record(
            {
                "event": "v_skip",
                "symbol": symbol,
                "entry_premium": premium,
                "balance": round(balance, 2),
            }
        )
        return 0
    v["position"] = {
        "symbol": symbol,
        "entry_premium": premium,
        "contracts": contracts,
    }
    return contracts


def _v_exit(v: dict, exit_premium, estimated: bool = False) -> float:
    """Close the shadow-virtual position. Returns dollar P&L."""
    pos = v.get("position")
    v["position"] = None
    if not pos:
        return 0.0
    contracts = int(pos.get("contracts", 0) or 0)
    entry = float(pos.get("entry_premium", 0.0) or 0.0)
    try:
        exit_px = float(exit_premium)
        if exit_px <= 0:
            raise ValueError
    except (TypeError, ValueError):
        exit_px = entry
        estimated = True
    pnl = 0.0
    if contracts > 0 and entry > 0:
        pnl = contracts * (exit_px - entry) * 100.0
    v["trades"] = int(v.get("trades", 0) or 0) + 1
    if pnl:
        v["balance"] = round(
            float(v.get("balance", 0.0) or 0.0) + pnl, 2
        )
    log.info(
        "SHADOW VIRTUAL EXIT: %s x%d in $%.2f out $%.2f%s pnl $%+.2f "
        "balance $%.2f",
        pos.get("symbol"),
        contracts,
        entry,
        exit_px,
        " (estimated)" if estimated else "",
        pnl,
        float(v.get("balance", 0.0) or 0.0),
    )
    _record(
        {
            "event": "v_exit",
            "symbol": pos.get("symbol"),
            "entry_premium": entry,
            "exit_premium": round(exit_px, 4),
            "exit_estimated": estimated,
            "contracts": contracts,
            "pnl": round(pnl, 2),
            "balance": round(float(v.get("balance", 0.0) or 0.0), 2),
        }
    )
    _v_skim(v)
    return pnl


def _f3_apply(st: dict, pnl_1c) -> float:
    """Apply a 1-contract paper P&L to the fixed-3-contract ledger."""
    try:
        p1 = float(pnl_1c)
    except (TypeError, ValueError):
        return 0.0
    if p1 == 0:
        return 0.0
    f3 = st.get("fixed3")
    if not isinstance(f3, dict):
        f3 = _new_ledger()
        st["fixed3"] = f3
    pnl = round(3.0 * p1, 2)
    _ledger_apply_pnl(f3, pnl, "FIXED3")
    _record(
        {
            "event": "f3_apply",
            "pnl_1c": round(p1, 2),
            "pnl": pnl,
            "balance": round(float(f3.get("balance", 0.0) or 0.0), 2),
        }
    )
    return pnl


# ------------------------------------------------------------
# Public hooks (called from the live loop)
# ------------------------------------------------------------

def peek_open() -> dict | None:
    if not _enabled():
        return None
    return load().get("open")


def abandon_stale() -> None:
    """Clear any shadow left open across a restart (its timer is gone too)."""
    if not _enabled():
        return
    st = load()
    rec = st.get("open")
    if not rec:
        return
    st["open"] = None
    # Also clear a stranded virtual position; it can never be priced now.
    v = st.get("virtual", {})
    if v.get("position"):
        v["position"] = None
        _record({"event": "v_abandon", "symbol": rec.get("symbol")})
    _record({"event": "abandoned", "id": rec.get("id")})
    save(st)
    log.info("SHADOW: abandoned stale open record %s", rec.get("id"))


def replay_trade(day: str, symbol: str, direction: str, entry_premium, exit_premium, note: str = "") -> None:
    """Backfill one historical no-gate trade (Sep 14+ era, pre-gate).

    Paper side: exact 1-contract P&L. Virtual side: sized off the running
    shadow-virtual balance with the standard skim/tax rules. `day` is an
    explicit YYYY-MM-DD key so history doesn't land in today's bucket.
    """
    if not _enabled():
        return
    st = load()
    try:
        entry = float(entry_premium)
        exit_px = float(exit_premium)
        ok = entry > 0 and exit_px > 0
    except (TypeError, ValueError):
        ok = False
    pnl_1c = round((exit_px - entry) * 100.0, 2) if ok else None
    if pnl_1c is not None:
        st["paper_pnl"] = round(float(st.get("paper_pnl", 0.0) or 0.0) + pnl_1c, 2)
        st["paper_trades"] = int(st.get("paper_trades", 0) or 0) + 1
        bd = st.setdefault("by_day", {}).setdefault(day, {"paper_pnl": 0.0, "trades": 0})
        bd["paper_pnl"] = round(bd["paper_pnl"] + pnl_1c, 2)
        bd["trades"] += 1
    v = st["virtual"]
    v_contracts = _v_entry(v, symbol, entry if ok else 0)
    v_pnl = _v_exit(v, exit_px if ok else None, estimated=False)
    f3_pnl = _f3_apply(st, pnl_1c)
    _record(
        {
            "event": "replay",
            "day": day,
            "direction": direction,
            "symbol": symbol,
            "entry_premium": entry if ok else None,
            "exit_premium": exit_px if ok else None,
            "pnl_1c": pnl_1c,
            "v_contracts": v_contracts,
            "v_pnl": round(v_pnl, 2),
            "v_balance": round(float(v.get("balance", 0.0) or 0.0), 2),
            "f3_pnl": round(f3_pnl, 2),
            "f3_balance": round(float(st["fixed3"].get("balance", 0.0) or 0.0), 2),
            "note": note,
        }
    )
    save(st)


def on_signal(sig_id: str, direction: str, symbol: str, ask, spy, estimated: bool = False) -> None:
    """A fresh flip fired. In the no-gate universe this enters immediately."""
    if not _enabled():
        return
    st = load()
    if st.get("open"):
        log.warning("SHADOW: signal %s while shadow already open; keeping old", sig_id)
        return
    try:
        ask_f = float(ask)
        ask_ok = ask_f > 0
    except (TypeError, ValueError):
        ask_f = None
        ask_ok = False
    ask_estimated = estimated or not ask_ok
    st["open"] = {
        "id": sig_id,
        "direction": direction,
        "symbol": symbol,
        "ask": ask_f,
        "ask_estimated": ask_estimated,
        "spy": spy,
        "delayed": False,
    }
    v_contracts = _v_entry(st["virtual"], symbol, ask_f if ask_ok else 0) if ask_ok else 0
    if ask_ok and v_contracts == 0:
        log.info("SHADOW VIRTUAL SKIP: %s unaffordable", symbol)
    _record(
        {
            "event": "open",
            "id": sig_id,
            "direction": direction,
            "symbol": symbol,
            "ask": ask_f,
            "ask_estimated": ask_estimated,
            "spy": spy,
            "v_contracts": v_contracts,
        }
    )
    save(st)
    log.info(
        "SHADOW OPEN: %s %s @ ask $%s (spy %.2f)",
        direction,
        symbol,
        ("%0.2f" % ask_f) if ask_ok else "n/a",
        spy or 0,
    )


def close_blocked(bid, estimated: bool = False) -> None:
    """The flip died before the gate opened: no-gate trade stopped here."""
    if not _enabled():
        return
    st = load()
    rec = st.get("open")
    if not rec or rec.get("delayed"):
        return
def _close_open(st: dict, rec: dict, bid, bid_estimated: bool, reason: str, est_close: bool) -> None:
    """Shared settlement for an open non-delayed shadow record."""
    st["open"] = None
    try:
        bid_f = float(bid)
        bid_ok = bid_f > 0
    except (TypeError, ValueError):
        bid_f = None
        bid_ok = False
    bid_estimated = bid_estimated or not bid_ok
    ask = rec.get("ask")
    pnl_1c = None
    if ask and bid_ok:
        pnl_1c = round((bid_f - float(ask)) * 100.0, 2)
        st["paper_pnl"] = round(float(st.get("paper_pnl", 0.0) or 0.0) + pnl_1c, 2)
        st["paper_trades"] = int(st.get("paper_trades", 0) or 0) + 1
        day = _day_key()
        bd = st.setdefault("by_day", {}).setdefault(day, {"paper_pnl": 0.0, "trades": 0})
        bd["paper_pnl"] = round(bd["paper_pnl"] + pnl_1c, 2)
        bd["trades"] += 1
    v_pnl = _v_exit(st["virtual"], bid_f if bid_ok else None, estimated=bid_estimated)
    f3_pnl = _f3_apply(st, pnl_1c)
    _record(
        {
            "event": "close_blocked",
            "id": rec.get("id"),
            "direction": rec.get("direction"),
            "exit_bid": bid_f,
            "exit_estimated": bid_estimated,
            "reason": reason,
            "estimated_close": est_close,
            "pnl_1c": pnl_1c,
            "v_pnl": round(v_pnl, 2),
            "v_balance": round(float(st["virtual"].get("balance", 0.0) or 0.0), 2),
            "f3_pnl": round(f3_pnl, 2),
            "f3_balance": round(float(st["fixed3"].get("balance", 0.0) or 0.0), 2),
        }
    )
    save(st)
    log.info(
        "SHADOW CLOSE: %s reason=%s pnl_1c=%s v_pnl=%+.2f",
        rec.get("id"),
        reason,
        ("%+.2f" % pnl_1c) if pnl_1c is not None else "n/a",
        v_pnl,
    )


def close_blocked(bid, estimated: bool = False) -> None:
    """The flip died before the gate opened: no-gate trade stopped here."""
    if not _enabled():
        return
    st = load()
    rec = st.get("open")
    if not rec or rec.get("delayed"):
        return
    _close_open(st, rec, bid, estimated, "SHADOW_UNDERLYING_STOP", False)


def close_riding(exit_premium, reason: str = "SHADOW_RIDE_EXIT", estimated: bool = True) -> None:
    """Close an open shadow at a given premium (manual/seeded outcomes)."""
    if not _enabled():
        return
    st = load()
    rec = st.get("open")
    if not rec or rec.get("delayed"):
        return
    _close_open(st, rec, exit_premium, estimated, reason, True)


def mark_delayed(entry_premium=None) -> None:
    """The real bot entered on this signal: shadow parallels the real trade.

    If the signal-time ask quote was unavailable (cold cache), fall back to
    the real entry premium so the virtual side can still be sized; flagged
    estimated. If no shadow is open (e.g. entry on a carried signal, which
    has no fresh transition), record a note instead of inventing one.
    """
    if not _enabled():
        return
    st = load()
    rec = st.get("open")
    if not rec:
        _record(
            {
                "event": "carried_entry_no_shadow",
                "note": "real entry on a carried signal; no fresh flip, "
                        "so no no-gate counterpart exists",
            }
        )
        save(st)
        return
    if rec.get("delayed"):
        return
    if rec.get("ask") is None and entry_premium:
        try:
            ep = float(entry_premium)
            if ep > 0:
                rec["ask"] = ep
                rec["ask_estimated"] = True
                v = st.get("virtual", {})
                if not v.get("position"):
                    _v_entry(v, rec.get("symbol"), ep)
        except (TypeError, ValueError):
            pass
    rec["delayed"] = True
    _record({"event": "delayed", "id": rec.get("id")})
    save(st)


def resolve_delayed(exit_premium, real_entry_premium) -> None:
    """The real trade closed: settle the parallel shadow (estimated)."""
    if not _enabled():
        return
    st = load()
    rec = st.get("open")
    if not rec or not rec.get("delayed"):
        return
    st["open"] = None
    ask = rec.get("ask")
    pnl_1c = None
    try:
        real_entry = float(real_entry_premium)
        exit_px = float(exit_premium)
        real_pnl_1c = (exit_px - real_entry) * 100.0
        if ask:
            pnl_1c = round(real_pnl_1c + (real_entry - float(ask)) * 100.0, 2)
        else:
            pnl_1c = round(real_pnl_1c, 2)
    except (TypeError, ValueError):
        pass
    if pnl_1c is not None:
        st["paper_pnl"] = round(float(st.get("paper_pnl", 0.0) or 0.0) + pnl_1c, 2)
        st["paper_trades"] = int(st.get("paper_trades", 0) or 0) + 1
        day = _day_key()
        bd = st.setdefault("by_day", {}).setdefault(day, {"paper_pnl": 0.0, "trades": 0})
        bd["paper_pnl"] = round(bd["paper_pnl"] + pnl_1c, 2)
        bd["trades"] += 1
    # Shadow virtual exits at the real exit premium against the shadow entry.
    v = st["virtual"]
    pos = v.get("position")
    if pos:
        pos["entry_premium"] = ask or pos.get("entry_premium")
    v_pnl = _v_exit(v, exit_premium, estimated=True)
    f3_pnl = _f3_apply(st, pnl_1c)
    _record(
        {
            "event": "close_delayed",
            "id": rec.get("id"),
            "direction": rec.get("direction"),
            "shadow_ask": ask,
            "real_entry": real_entry_premium,
            "exit": exit_premium,
            "estimated": True,
            "pnl_1c": pnl_1c,
            "v_pnl": round(v_pnl, 2),
            "v_balance": round(float(v.get("balance", 0.0) or 0.0), 2),
            "f3_pnl": round(f3_pnl, 2),
            "f3_balance": round(float(st["fixed3"].get("balance", 0.0) or 0.0), 2),
        }
    )
    save(st)
    log.info(
        "SHADOW CLOSE (delayed, est): %s pnl_1c=%s v_pnl=%+.2f",
        rec.get("id"),
        ("%+.2f" % pnl_1c) if pnl_1c is not None else "n/a",
        v_pnl,
    )


def backfill_fixed3() -> dict:
    """Rebuild the fixed-3-contract ledger from the diary's close records.

    Each closed shadow trade contributes 3x its 1-contract paper P&L,
    applied in diary order with the standard skim/tax rules. One-time
    history reconstruction; live closes hook in automatically.
    """
    if not _enabled():
        return {}
    st = load()
    st["fixed3"] = _new_ledger()
    applied = 0
    try:
        with open(diary_path(), "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if d.get("event") not in (
                    "close_blocked",
                    "close_delayed",
                    "replay",
                ):
                    continue
                pnl_1c = d.get("pnl_1c")
                try:
                    p1 = float(pnl_1c)
                except (TypeError, ValueError):
                    continue
                if p1 == 0:
                    continue
                _ledger_apply_pnl(st["fixed3"], round(3.0 * p1, 2), "FIXED3")
                applied += 1
    except (FileNotFoundError, OSError):
        pass
    f3 = st["fixed3"]
    _record(
        {
            "event": "f3_backfill",
            "trades_applied": applied,
            "balance": round(float(f3.get("balance", 0.0) or 0.0), 2),
            "banked": round(float(f3.get("banked", 0.0) or 0.0), 2),
            "tax_reserve": round(float(f3.get("tax_reserve", 0.0) or 0.0), 2),
            "skims": f3.get("skims", 0),
        }
    )
    save(st)
    log.info(
        "SHADOW FIXED3 BACKFILL: %d trades -> balance $%.2f",
        applied,
        float(f3.get("balance", 0.0) or 0.0),
    )
    return summary()


def summary() -> dict:
    """One-line status for health checks."""
    st = load()
    v = st.get("virtual", {})
    f3 = st.get("fixed3", {})
    return {
        "paper_pnl": round(float(st.get("paper_pnl", 0.0) or 0.0), 2),
        "paper_trades": st.get("paper_trades", 0),
        "v_balance": round(float(v.get("balance", 0.0) or 0.0), 2),
        "v_banked": round(float(v.get("banked", 0.0) or 0.0), 2),
        "v_tax": round(float(v.get("tax_reserve", 0.0) or 0.0), 2),
        "v_trades": v.get("trades", 0),
        "v_skims": v.get("skims", 0),
        "f3_balance": round(float(f3.get("balance", 0.0) or 0.0), 2),
        "f3_banked": round(float(f3.get("banked", 0.0) or 0.0), 2),
        "f3_tax": round(float(f3.get("tax_reserve", 0.0) or 0.0), 2),
        "f3_trades": f3.get("trades", 0),
        "f3_skims": f3.get("skims", 0),
        "open": bool(st.get("open")),
    }
