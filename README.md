# ORB Trading Bots

OANDA trading bots driven by `config/bots.yaml`.

## Local setup
1. Copy `.env.example` to `.env` and fill in OANDA credentials.
2. Activate virtualenv and install dependencies (`pandas`, `requests`, `pyyaml`).
3. Run a configured bot:
   - `python -m bots.runner ORB_LDN_M15_v1`
   - `python -m bots.runner ORB_US_M15_v1`
   - `python -m bots.runner CHANNEL_BREAKOUT`
   - `python -m bots.runner REVERSAL_DAYS`
   - `python -m bots.runner CONQUEROR_FX`
   - `python -m bots.runner INSIDE_DAYS`
   - `python -m bots.runner SLINGSHOT`

## Config
- Bot profiles live in `config/bots.yaml`.
- Supported strategies: `orb_m15`, `channel_breakout_daily`, `conqueror_fx_daily`, `inside_days_daily`, `slingshot_daily`.
- Runtime equity/state is persisted in `state/bot_state.db`.
- Per-bot OANDA account routing is configured via `account_id` in `config/bots.yaml`.
- Strategy `timeframe` is used as OANDA candle granularity (for daily strategies this defaults to `D` if omitted).

## OANDA Trade Attribution Tags
- All placed orders now include OANDA `clientExtensions.tag` and `tradeClientExtensions.tag`.
- Tag format is: `{bot_id}|{timeframe}`.
- To attribute fills/trades in OANDA, inspect transactions/trades for `clientExtensions.tag` (or `tradeClientExtensions.tag`) and group by that value.

## Exporting Trades To CSV
- Script: `tools/export_oanda_trades.py`
- Purpose: read-only export of closed-trade performance rows from OANDA transactions.
- Safety: this script never places/cancels orders; it only performs `GET` requests.

Example `.env` values:
```bash
OANDA_API_KEY=your_api_key_here
OANDA_ENV=practice
OANDA_ACCOUNT_IDS=101-xxx-xxx-002,101-xxx-xxx-003
# Optional:
OANDA_SINCE_DAYS=30
```

Run examples:
```bash
python tools/export_oanda_trades.py
python tools/export_oanda_trades.py --since-days 14
python tools/export_oanda_trades.py --env live --accounts "101-...-004,101-...-005"
python tools/export_oanda_trades.py --outdir exports
```

Output:
- CSV saved to `exports/oanda_trades_YYYY-MM-DD_HHMMSS.csv`.
- Includes tag split columns (`bot_id`, `timeframe`) using `<bot_id>|<timeframe>`.

## Deployment
- Run under `systemd` with:
  - `ExecStart=/home/ubuntu/trading-bots/.venv/bin/python -m bots.runner <BOT_ID>`
  - `WorkingDirectory=/home/ubuntu/trading-bots`
