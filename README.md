# Pcopbot

Polymarket copy-trading bot that automatically mirrors trades from tracked wallets with configurable sizing, full risk management, auto-redemption, and a real-time Streamlit dashboard.

## Features

- **Copy Trading** — polls the Polymarket Data API for new trades from tracked wallets and replicates them via the CLOB API
- **Flexible Sizing** — fixed dollar budget (auto-converted to shares) or proportional to the original trade size
- **Risk Management** — 9 configurable checks: per-trade limits, total spend caps, per-market/outcome caps, position limits, price filters, slippage checks
- **Auto-Sell** — automatically sells positions when price reaches threshold (default $0.999)
- **Auto-Redemption** — redeems winning tokens on-chain when markets resolve (gasless via Relayer API)
- **Loss Detection** — records expired losing positions, manual redemptions, and OTC sells
- **PnL Tracking** — realized + unrealized PnL with per-trader breakdown, win rate, and ROI
- **Watermarking** — per-trader timestamp tracking to prevent duplicate trades
- **Dry Run Mode** — simulate trades without placing real orders (default: enabled)
- **Dashboard** — password-protected Streamlit UI with live portfolio metrics
- **Production-Ready** — Docker Compose deployment with Nginx SSL, rate limiting, and security headers

## Quick Start

### Prerequisites

- Python 3.11+
- Polymarket API credentials ([get them here](https://docs.polymarket.com))
- A funded wallet on Polygon

### Local Development

```bash
# 1. Clone and install
git clone https://github.com/your-username/Pcopbot.git
cd Pcopbot
pip install -r requirements.txt

# 2. Configure environment
cp .env.example .env
# Edit .env with your Polymarket credentials

# 3. Run the bot (dry-run mode by default)
python -m bot.main

# 4. Launch the dashboard (separate terminal)
streamlit run dashboard/app.py
```

### Production Deployment (Docker)

```bash
# 1. Configure environment
cp .env.example .env
cp .env.dashboard.example .env.dashboard
# Edit both files — set credentials, DASHBOARD_PASSWORD, and DOMAIN

# 2. Build and start all services
docker compose up -d --build

# 3. Obtain SSL certificate (first time only)
docker compose run --rm certbot certonly \
  --webroot -w /var/www/certbot \
  -d your-domain.com \
  --agree-tos --no-eff-email -m your@email.com

# 4. Restart nginx to load the certificate
docker compose restart nginx

# Dashboard available at https://your-domain.com
```

### Common Operations

```bash
# View bot logs
docker compose logs -f bot

# Restart after code changes
git pull && docker compose up -d --build && docker compose restart nginx

# Run tests
python -m pytest tests/ -v

# Backfill fill prices from wallet activity
python -m scripts.refresh_prices

# Recalculate historical PnL
python -m scripts.fix_historical_pnl
```

## Dashboard Pages

| Page | Description |
|------|-------------|
| **Traders** | Per-trader settings, current positions, copy-trade holdings, realized PnL history |
| **Add Trader** | Add a new wallet address to track |
| **History** | Filterable trade history across all traders with pagination |
| **PnL** | Overall and per-trader PnL summary with cumulative chart |
| **Logs** | Real-time bot log viewer with level filtering |
| **Settings** | Poll interval, dry run toggle, auto-sell toggle, log level |

## Configuration

### Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `POLYMARKET_PRIVATE_KEY` | Yes | — | Wallet private key for trading |
| `POLYMARKET_API_KEY` | Yes | — | CLOB API key |
| `POLYMARKET_API_SECRET` | Yes | — | CLOB API secret |
| `POLYMARKET_API_PASSPHRASE` | Yes | — | CLOB API passphrase |
| `POLYMARKET_FUNDER_ADDRESS` | Yes | — | Funder wallet address (holds funds) |
| `POLYMARKET_FUNDER_PRIVATE_KEY` | No | — | Funder key for on-chain redemptions (Gnosis Safe setups) |
| `POLYMARKET_RELAYER_API_KEY` | No | — | Relayer API key for gasless Safe transactions |
| `POLYMARKET_CHAIN_ID` | No | `137` | Polygon chain ID |
| `DATABASE_URL` | No | `sqlite:///./data/pcopbot.db` | Database connection string |
| `POLL_INTERVAL_SECONDS` | No | `15` | Seconds between poll cycles |
| `DRY_RUN` | No | `true` | Set to `false` for live trading |
| `AUTO_SELL_THRESHOLD` | No | `0.999` | Price threshold for auto-selling |
| `LOG_LEVEL` | No | `INFO` | Logging verbosity |
| `DASHBOARD_PASSWORD` | No | — | Dashboard login password (empty = no auth) |
| `DOMAIN` | No | — | Domain name for nginx SSL |

### Per-Trader Settings (via Dashboard)

| Setting | Description |
|---------|-------------|
| Sizing Mode | `fixed` (dollar budget) or `proportional` (% of original) |
| Fixed Amount ($) | Dollar amount per copy trade |
| Copy Percentage (%) | Percentage of original trade size |
| Sell Only | Only copy SELL trades (skip BUY) |
| Buy/Sell Order Type | `market` (FOK) or `limit` (GTC with timeout) |
| Buy/Sell Slippage (%) | Maximum allowed slippage |
| Limit Timeout (s) | GTC order timeout before cancellation |
| Limit Fallback Market | Fall back to FOK if GTC times out |
| Ignore Trades Under ($) | Skip target trades below this value |
| Min / Max Price | Only copy trades within this price range |
| Min / Max Per Trade ($) | Per-trade dollar limits (caps to max, rejects below min) |
| Total Spend Limit ($) | Maximum cumulative spend |
| Max Per Market ($) | Maximum exposure per market |
| Max Per Yes/No ($) | Maximum exposure per outcome |
| Max Position Limit ($) | Maximum net position per token |

## Project Structure

```
bot/                 Core trading logic
  main.py              Daemon entry point (poll loop, PnL updates, auto-sell, auto-redeem)
  tracker.py           Fetches trades & positions from Polymarket APIs
  executor.py          Executes copy trades with order management
  risk.py              9 pre-trade risk checks (cap + reject)
  redeemer.py          On-chain redemption, loss detection, manual trade sync
  watermark.py         Per-trader watermark management

config/              Configuration
  settings.py          Environment-based settings loader

db/                  Database layer
  models.py            SQLAlchemy ORM models
  database.py          Engine and session factory

dashboard/           Streamlit web UI
  app.py               Multi-page entry point with auth gate
  _pages/              Page modules (traders, history, pnl, logs, settings)
  components/          Reusable UI components (charts, wallet metrics)

scripts/             Maintenance utilities
  refresh_prices.py    Backfill fill prices from wallet activity
  fix_historical_pnl.py  Recalculate all PnL records
  check_approval.py    Verify on-chain ERC-1155 approvals

nginx/               Reverse proxy configuration
  nginx.conf           SSL + rate limiting + security headers
  http-only.conf       HTTP-only fallback (no SSL)
  entrypoint.sh        Auto-selects SSL or HTTP config

tests/               Pytest test suite
  test_executor.py     Order execution and auto-sell tests
  test_risk.py         Risk check tests
  test_tracker.py      API parsing and watermark tests
  test_watermark.py    Watermark advancement tests
```

## Architecture

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for a comprehensive technical deep-dive covering:
- System architecture and data flow
- Trade execution pipeline
- Risk management algorithms
- Auto-sell price discovery
- On-chain redemption via Gnosis Safe + Relayer
- PnL calculation methodology
- Watermark and de-duplication strategy

## License

MIT
