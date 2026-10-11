"""Virtual compounding shadow ledger for the 0DTE SPY paper bot.

Tracks a *virtual* sub-account alongside the real 1-contract paper trades so the
milestone compounding ladder can be watched live without touching the strategy
record:

- starts at the paper account's realized profit since 2026-09-14 ($465.46 net)
- sizes each trade as floor(virtual_balance / contract_cost); skips when a
  single contract is unaffordable
- on each virtual exit, compounds P&L into the virtual balance
- when the virtual balance crosses a milestone ($1k/$5k/$10k/$20k/$30k, then
  doubling), skims half: 35% of the *gains since last skim* is reserved for
  taxes (from the skim), the rest of the skim is "banked", and the remaining
  half keeps compounding

Everything here is virtual. No orders are placed, resized, or withdrawn.
The real bot keeps trading 1 contract; this module only reads entry/exit
premiums via on_entry()/on_exit() hooks. All failures are swallowed by the
caller so the ledger can never break the live loop.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone

import config

log = logging.getLogger("virtual_ledger")

_HERE = os.path.dirname(os.path.abspath(__file__))


def _resolve(name: str, default: str) -> str:
    p = getattr(config, name, default) or default
    return p if os.path.isabs(p) else os.path.join(_HERE, p)


def state_path() -> str:
    return _resolve("VIRTUAL_STATE_PATH", "virtual_state.json")


def ledger_path() -> str:
    return _resolve("VIRTUAL_LEDGER_PATH", "virtual_ledger.jsonl")


def default_state() -> dict:
    start = float(getattr(config, "VIRTUAL_START_BALANCE", 465.46))
    milestones = sorted(
        float(m)
        for m in getattr(
            config, "VIRTUAL_MILESTONES", [1000.0, 5000.0, 10000.0, 20000.0, 30000.0]
        )
    )
    return {
        "balance": start,
        "banked": 0.0,
        "tax_reserve": 0.0,
        "basis": start,
        "next_milestone": milestones[0] if milestones else None,
        "virtual_position": None,
        "trades": 0,
        "skips": 0,
        "skims": 0,
        "start_balance": start,
        "started_at": datetime.now(timezone.utc).isoformat(),
    }


def load() -> dict:
    try:
        with open(state_path(), "r", encoding="utf-8") as fh:
            st = json.load(fh)
        if isinstance(st, dict):
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
        event["virtual"] = True
        with open(ledger_path(), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, default=str) + "\n")
    except Exception:
        log.exception("virtual ledger write failed")


def _advance_milestone(st: dict, hit: float) -> None:
    milestones = sorted(
        float(m)
        for m in getattr(
            config, "VIRTUAL_MILESTONES", [1000.0, 5000.0, 10000.0, 20000.0, 30000.0]
        )
    )
    nxt = None
    for m in milestones:
        if m > hit + 1e-9:
            nxt = m
            break
    if nxt is None:
        nxt = hit * 2.0  # keep doubling past the configured list
    st["next_milestone"] = nxt


def _maybe_skim(st: dict) -> None:
    rate = float(getattr(config, "VIRTUAL_TAX_RATE", 0.35))
    while True:
        nxt = st.get("next_milestone")
        balance = float(st.get("balance", 0.0) or 0.0)
        if not nxt or balance < float(nxt):
            break
        hit = float(nxt)
        skim = balance / 2.0
        gains = balance - float(st.get("basis", balance) or balance)
        tax = rate * max(gains, 0.0)
        banked = skim - tax
        st["banked"] = float(st.get("banked", 0.0) or 0.0) + banked
        st["tax_reserve"] = float(st.get("tax_reserve", 0.0) or 0.0) + tax
        st["balance"] = skim
        st["basis"] = skim
        st["skims"] = int(st.get("skims", 0) or 0) + 1
        log.info(
            "VIRTUAL SKIM (milestone $%.0f): move $%.2f to bank, "
            "$%.2f to tax reserve; compounding continues with $%.2f",
            hit,
            banked,
            tax,
            skim,
        )
        _record(
            {
                "event": "skim",
                "milestone": hit,
                "to_bank": round(banked, 2),
                "to_tax_reserve": round(tax, 2),
                "compounding": round(skim, 2),
            }
        )
        _advance_milestone(st, hit)


def on_entry(symbol, entry_premium) -> None:
    """Record a virtual position sized off the virtual balance."""
    if not getattr(config, "VIRTUAL_ENABLED", True):
        return
    try:
        entry_premium = float(entry_premium)
    except (TypeError, ValueError):
        return
    if entry_premium <= 0:
        return
    st = load()
    vp = st.get("virtual_position")
    if vp and vp.get("symbol") == symbol:
        return  # already tracking (e.g. recovery re-fire)
    balance = float(st.get("balance", 0.0) or 0.0)
    cost = entry_premium * 100.0
    contracts = int(balance // cost) if cost > 0 else 0
    if contracts <= 0:
        st["skips"] = int(st.get("skips", 0) or 0) + 1
        st["virtual_position"] = None
        log.info(
            "VIRTUAL SKIP: %s entry $%.2f unaffordable at balance $%.2f",
            symbol,
            entry_premium,
            balance,
        )
        _record(
            {
                "event": "skip",
                "symbol": symbol,
                "entry_premium": entry_premium,
                "balance": round(balance, 2),
            }
        )
    else:
        st["virtual_position"] = {
            "symbol": symbol,
            "entry_premium": entry_premium,
            "contracts": contracts,
            "entry_ts": datetime.now(timezone.utc).isoformat(),
        }
        log.info(
            "VIRTUAL ENTRY: %s x%d @ $%.2f (balance $%.2f)",
            symbol,
            contracts,
            entry_premium,
            balance,
        )
        _record(
            {
                "event": "entry",
                "symbol": symbol,
                "entry_premium": entry_premium,
                "contracts": contracts,
                "balance": round(balance, 2),
            }
        )
    save(st)


def on_exit(exit_premium, reason=None) -> None:
    """Close the virtual position and compound its P&L."""
    if not getattr(config, "VIRTUAL_ENABLED", True):
        return
    st = load()
    vp = st.get("virtual_position")
    if not vp:
        return
    st["virtual_position"] = None
    contracts = int(vp.get("contracts", 0) or 0)
    entry_premium = float(vp.get("entry_premium", 0.0) or 0.0)
    estimated = False
    try:
        exit_px = float(exit_premium)
        if exit_px <= 0:
            raise ValueError
    except (TypeError, ValueError):
        exit_px = entry_premium
        estimated = True
    pnl = 0.0
    if contracts > 0 and entry_premium > 0:
        pnl = contracts * (exit_px - entry_premium) * 100.0
        st["balance"] = float(st.get("balance", 0.0) or 0.0) + pnl
    st["trades"] = int(st.get("trades", 0) or 0) + 1
    log.info(
        "VIRTUAL EXIT: %s x%d in $%.2f out $%.2f%s pnl $%+.2f balance $%.2f",
        vp.get("symbol"),
        contracts,
        entry_premium,
        exit_px,
        " (exit price estimated)" if estimated else "",
        pnl,
        float(st.get("balance", 0.0) or 0.0),
    )
    _record(
        {
            "event": "exit",
            "symbol": vp.get("symbol"),
            "entry_premium": entry_premium,
            "exit_premium": round(exit_px, 4),
            "exit_estimated": estimated,
            "contracts": contracts,
            "pnl": round(pnl, 2),
            "balance": round(float(st.get("balance", 0.0) or 0.0), 2),
            "reason": reason,
        }
    )
    _maybe_skim(st)
    save(st)


def summary() -> dict:
    """One-line status for health checks."""
    st = load()
    return {
        "balance": round(float(st.get("balance", 0.0) or 0.0), 2),
        "banked": round(float(st.get("banked", 0.0) or 0.0), 2),
        "tax_reserve": round(float(st.get("tax_reserve", 0.0) or 0.0), 2),
        "next_milestone": st.get("next_milestone"),
        "trades": st.get("trades", 0),
        "skips": st.get("skips", 0),
        "skims": st.get("skims", 0),
    }
