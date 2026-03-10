# Pcopbot

Polymarket copy-trading bot — automatically mirrors trades from tracked wallets with configurable sizing, risk management, and a Streamlit dashboard.

## Features

- **Copy Trading** — poll the Polymarket Data API for new trades from tracked wallets and replicate them via `py-clob-client`
- **Risk Management** — min trade threshold, per-token position limits, slippage checks
- **Flexible Sizing** — fixed dollar amount or proportional to the original trade
- **Watermarking** — tracks the last processed trade per wallet to avoid duplicates
- **Dry Run Mode** — simulate trades without placing real orders (default)
- **Streamlit Dashboard** — manage traders, browse trade history, and monitor PnL

## Quick Start

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Copy and configure environment variables
cp .env.example .env
# Edit .env with your Polymarket credentials

# 3. Run the bot (dry-run by default)
python -m bot.main

# 4. Launch the dashboard (separate terminal)
streamlit run dashboard/app.py
```

## Project Structure

```
bot/            Core trading logic
  main.py         Daemon entry point (poll loop)
  tracker.py      Fetches trades from Polymarket Data API
  executor.py     Executes (or simulates) copy trades
  risk.py         Pre-trade risk checks
  watermark.py    Per-trader watermark management

config/         Configuration
  settings.py     Environment-based settings

db/             Database layer
  models.py       SQLAlchemy ORM models (Trader, CopyTrade)
  database.py     Engine / session factory

dashboard/      Streamlit web UI
  app.py          Multi-page app entry point
  pages/          Traders, History, PnL pages
  components/     Reusable chart helpers

tests/          Pytest test suite
```

## Configuration

All configuration is via environment variables (or `.env` file):

| Variable | Default | Description |
|---|---|---|
| `POLYMARKET_PRIVATE_KEY` | — | Wallet private key for trading |
| `POLYMARKET_FUNDER_ADDRESS` | — | Funder wallet address |
| `POLYMARKET_CHAIN_ID` | `137` | Polygon chain ID |
| `DATABASE_URL` | `sqlite:///./data/pcopbot.db` | Database connection string |
| `POLL_INTERVAL_SECONDS` | `15` | Seconds between poll cycles |
| `DRY_RUN` | `true` | Set to `false` for live trading |
| `LOG_LEVEL` | `INFO` | Logging verbosity |

## Testing

```bash
python -m pytest tests/ -v
```

## License

MIT