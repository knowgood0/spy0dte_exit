# SPY 0DTE Webull Sandbox Paper Bot — exit/risk-managed build

This build preserves the existing Pine-derived strategy and Webull Sandbox-only architecture, but replaces the old "submit then assume" behavior with an explicit order/position state machine.

## State machine

- `FLAT`: no bot-managed position.
- `PENDING_ENTRY`: entry request accepted; fill and position are not yet confirmed.
- `OPEN`: a matching Webull option position is actually visible.
- `PENDING_EXIT`: an exit request was accepted; the position remains until Webull confirms it is gone.
- `RECOVERY_REQUIRED`: the external state is ambiguous or unavailable; new orders are blocked.

A successful `place_order()` response is never treated as a fill. Webull's order detail fields `order_status`, `qty`, `filled_qty`, and `filled_price` are parsed, and the account position endpoint is checked independently before an entry becomes `OPEN` or an exit becomes `FLAT`.

## Exit architecture

Strategy-derived:
- 1.5 ATR underlying stop.
- 2.0 ATR underlying target.
- 1.0 ATR underlying breakeven trigger.
- ADX Volatility Wave outer-zone exit.
- 12 completed 5-minute bars = 60-minute default time stop.

New risk controls:
- 35% option-premium maximum loss.
- 50% option-premium take profit.
- 25% option-premium breakeven arming.
- 15:55 ET forced liquidation.
- No new entries after 15:45 ET.

All new thresholds are environment variables.

## Important execution behavior

Entries remain based on the current strategy's completed 5-minute-bar signal. Once a position is confirmed OPEN, exits are monitored every worker cycle using the live option quote, so the exit checks are not restricted to bar closes.

The bot uses option `SELL` orders with `SELL_TO_CLOSE` and `DAY`, consistent with Webull's documented options order requirements.

## Safety behavior

- Sandbox endpoint is hard-coded: `api.sandbox.webull.com`.
- `DRY_RUN=true` is the default.
- Live endpoint configuration is not provided.
- Partial entries are reconciled to actual filled quantity and the remaining entry order is canceled when possible.
- Partial exits keep the remaining position open and continue monitoring it.
- Rejected/canceled exits return to `OPEN` if a position still exists.
- If order status says FILLED but the position endpoint does not yet show the position, the bot stays in `PENDING_ENTRY` rather than assuming a fill.
- Startup recovery queries Webull before allowing new entries.
- Multiple or ambiguous option positions put the bot into `RECOVERY_REQUIRED`.

## Run locally

```bash
pip install -r requirements.txt
export WEBULL_APP_KEY='...'
export WEBULL_APP_SECRET='...'
export WEBULL_ACCOUNT_ID='...'
export WEBULL_REGION='us'
export DRY_RUN=true
python worker.py
```

For Sandbox paper order submission, change only:

```bash
export DRY_RUN=false
```

Do not use production/live Webull endpoints with this build.

## Tests

```bash
python -m unittest discover -s tests -v
```

## Deployment

Run as a headless worker, not a Render web service:

```bash
python worker.py
```

The state file must live on durable storage if the host can replace its filesystem. A VPS with a persistent disk is preferred for this worker.
