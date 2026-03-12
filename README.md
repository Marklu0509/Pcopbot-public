# Pcopbot

Polymarket copy-trading bot — automatically mirrors trades from tracked wallets with configurable sizing, full risk management, and a real-time Streamlit dashboard.

## Features

- **Copy Trading** — polls the Polymarket Data API (`/activity`) for new trades from tracked wallets and replicates them via `py-clob-client`
- **Flexible Sizing** — fixed dollar budget (auto-converted to shares) or proportional to the original trade size
- **Full Risk Management** — 9 configurable checks:
  - Ignore target wallet trades under a USD threshold
  - Min / Max price filter
  - Min / Max per trade ($)
  - Total spend limit ($)
  - Max per market ($)
  - Max per Yes/No outcome ($)
  - Max position limit per token ($, net BUY − SELL)
  - Slippage check
- **PnL Tracking** — unrealized PnL auto-updated from current market prices; realized PnL computed on SELL trades with win rate & ROI
- **Pre-existing Position Sync** — fetches the target trader's current holdings on startup via `/positions`
- **Watermarking** — tracks the last processed trade per wallet to avoid duplicates
- **Dry Run Mode** — simulate trades without placing real orders (default: enabled)
- **Password-Protected Dashboard** — Streamlit multi-page UI with login gate
- **Docker Deployment** — single `docker compose up -d` for bot + dashboard

## Dashboard Pages

| Page | Description |
|------|-------------|
| **Traders** | Per-trader tabs with settings editor, current positions, copy-trade holdings, realized PnL, and trade history |
| **Add Trader** | Add a new wallet to track |
| **History** | Filterable copy-trade history across all traders with pagination |
| **PnL** | Overall and per-trader PnL summary with cumulative PnL chart |
| **Logs** | Real-time bot logs stored in the database |
| **Settings** | Poll interval, dry run toggle, log level |
| **Wallet / API** | View configured API credentials |

## Quick Start

### Local

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

### Docker (recommended for production)

```bash
# 1. Configure environment
cp .env.example .env
# Edit .env with your credentials and set DASHBOARD_PASSWORD

# 2. Set your domain in nginx configs
# Replace YOUR_DOMAIN in nginx/nginx.conf, nginx/http-only.conf,
# and nginx/entrypoint.sh with your actual domain (e.g. bot.example.com)

# 3. Build and start
docker compose up -d --build

# 4. Obtain SSL certificate (first time only)
docker compose run --rm --entrypoint "certbot" certbot certonly \
  --webroot -w /var/www/certbot \
  -d your-domain.com \
  --agree-tos --no-eff-email -m your@email.com

# 5. Restart nginx to load the certificate
docker compose up -d --force-recreate nginx

# Dashboard available at https://your-domain.com
```

## Project Structure

```
bot/               Core trading logic
  main.py            Daemon entry point (poll loop, PnL updates)
  tracker.py         Fetches trades & positions from Polymarket APIs
  executor.py        Executes (or simulates) copy trades
  risk.py            9 pre-trade risk checks
  watermark.py       Per-trader watermark management

config/            Configuration
  settings.py        Environment-based settings

db/                Database layer
  models.py          SQLAlchemy ORM models (Trader, CopyTrade, Position, BotLog, BotSetting)
  database.py        Engine / session factory

dashboard/         Streamlit web UI
  app.py             Multi-page app entry point (with password gate)
  _pages/            Traders, Add Trader, History, PnL, Logs, Settings, Wallet/API
  components/        Reusable chart helpers

tests/             Pytest test suite
```

## Configuration

All configuration is via environment variables (or `.env` file):

| Variable | Default | Description |
|---|---|---|
| `POLYMARKET_PRIVATE_KEY` | — | Wallet private key for trading |
| `POLYMARKET_API_KEY` | — | CLOB API key |
| `POLYMARKET_API_SECRET` | — | CLOB API secret |
| `POLYMARKET_API_PASSPHRASE` | — | CLOB API passphrase |
| `POLYMARKET_FUNDER_ADDRESS` | — | Funder wallet address |
| `POLYMARKET_CHAIN_ID` | `137` | Polygon chain ID |
| `DATABASE_URL` | `sqlite:///./data/pcopbot.db` | Database connection string |
| `POLL_INTERVAL_SECONDS` | `15` | Seconds between poll cycles |
| `DRY_RUN` | `true` | Set to `false` for live trading |
| `LOG_LEVEL` | `INFO` | Logging verbosity |
| `STREAMLIT_PORT` | `8501` | Dashboard port |
| `DASHBOARD_PASSWORD` | — | Password for dashboard login (empty = no auth) |

## Per-Trader Settings (via Dashboard)

| Setting | Description |
|---------|-------------|
| Sizing Mode | `fixed` (dollar budget) or `proportional` (% of original) |
| Fixed Amount ($) | Dollar amount per copy trade |
| Copy Percentage (%) | Percentage of original trade size |
| Ignore Trades Under ($) | Skip target wallet trades below this USD value |
| Min / Max Price | Only copy trades within this price range |
| Min / Max Per Trade ($) | Per-trade dollar limits |
| Total Spend Limit ($) | Maximum total spend across all copy trades |
| Max Per Market ($) | Maximum exposure per market |
| Max Per Yes/No ($) | Maximum exposure per outcome |
| Max Position Limit ($) | Maximum net position per token |
| Buy / Sell Slippage (%) | Maximum allowed slippage |
| TP / SL (%) | Take-profit / stop-loss (planned) |

## Testing

```bash
python -m pytest tests/ -v
```

## License

MIT