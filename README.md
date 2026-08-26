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
- CHoCH Phase 1 state is persisted separately in `state/choch_state.db`.
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

## CHoCH OANDA Practice Bot
`CHOCH_OANDA_PRACTICE_v1` is a Phase 1 infrastructure shell for a future
OANDA CHoCH bot. Phase 1 cannot place orders.

Required EC2 environment file:
`/etc/qtp/oanda-choch-practice.env`

Required environment values:
```bash
OANDA_API_KEY=...
OANDA_ENV=practice
CHOCH_EXPECTED_ACCOUNT_ID=...
CHOCH_TRADING_ENABLED=0
```

Required YAML fields live in `config/bots.yaml`: `account_id`,
`allowed_oanda_env: practice`, `trading_enabled: false`, `dry_run: true`,
named `instruments`, `s1_timeframe: H1`, `s2_timeframe: M10`, `risk_pct`,
`max_account_risk_open`, and `max_units`.

CHoCH validates configured instruments against OANDA account instruments at
startup and captures `marginRate`, `minimumTradeSize`, `tradeUnitsPrecision`,
`pipLocation`, `displayPrecision`, and `type`. Effective leverage is logged as
`1 / marginRate`; leverage is not authoritative in YAML.

Startup guardrails hard-fail unless the configured account matches
`CHOCH_EXPECTED_ACCOUNT_ID`, OANDA resolves to the practice REST host, all
configured instruments exist in the account, `EUR_USD` is available, and both
trading flags remain disabled.

Run tests:
```bash
PYTHONPATH=. pytest -q
```

Manual dry-run startup:
```bash
PYTHONPATH=. python -m bots.runner CHOCH_OANDA_PRACTICE_v1
```

Logs for the dedicated service go to:
`/home/ubuntu/logs/bots/choch-oanda-practice.log`

Before installing the service on EC2, verify the log directory exists:
```bash
ls -ld /home/ubuntu/logs/bots
```

If it is missing, create it manually:
```bash
sudo mkdir -p /home/ubuntu/logs/bots
sudo chown ubuntu:ubuntu /home/ubuntu/logs/bots
```

Eventual EC2 service installation:
```bash
sudo cp deploy/choch-oanda-practice.service /etc/systemd/system/choch-oanda-practice.service
sudo systemctl daemon-reload
```

Do not enable or start the service until the dedicated practice account and
instrument values are configured. Deploying shared runtime changes through
`deploy.sh` can still restart existing generic `trading-bot@` and
`trading-bot-practice@` services; `choch-oanda-practice.service` must be
started or restarted explicitly.
