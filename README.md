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

## Deployment
- Run under `systemd` with:
  - `ExecStart=/home/ubuntu/trading-bots/.venv/bin/python -m bots.runner <BOT_ID>`
  - `WorkingDirectory=/home/ubuntu/trading-bots`
