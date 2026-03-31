# Pcopbot

Polymarket copy-trading bot that automatically mirrors trades from tracked wallets with configurable sizing, full risk management, auto-redemption, and a real-time Streamlit dashboard.

## Features

- **Copy Trading** — polls the Polymarket Data API for new trades from tracked wallets and replicates them via the CLOB API
- **Flexible Sizing** — fixed dollar budget (auto-converted to shares) or proportional to the original trade size
- **Risk Management** — 9 configurable checks: per-trade limits, total spend caps, per-market/outcome caps, position limits, price filters, slippage checks
- **Buy at Min** — when a capped or proportional trade falls below `min_per_trade`, optionally bump it up to the minimum instead of rejecting
- **Fill Aggregation** — accumulates fragmented fills in a sliding-window buffer and executes once the combined value crosses `ignore_trades_under`
- **Tiered Take-Profit** — per-trader JSON rules map entry price ranges to custom exit targets (e.g. buy ≤ 0.30 → sell at 0.80)
- **Auto-Sell** — automatically sells positions when price reaches threshold (default $0.999)
- **Auto-Redemption** — redeems winning tokens on-chain when markets resolve (gasless via Relayer API)
- **Loss Detection** — records expired losing positions, manual redemptions, and OTC sells
- **PnL Tracking** — realized + unrealized PnL with per-trader breakdown, win rate, and ROI
- **Watermarking** — per-trader timestamp tracking to prevent duplicate trades
- **Per-Trader Dry Run** — each trader can trade live or simulate independently; global override available
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

<!-- AUTO-GENERATED: derived from config/settings.py -->

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `POLYMARKET_PRIVATE_KEY` | Yes | — | Proxy wallet private key for CLOB signing |
| `POLYMARKET_API_KEY` | Yes | — | CLOB API key |
| `POLYMARKET_API_SECRET` | Yes | — | CLOB API secret |
| `POLYMARKET_API_PASSPHRASE` | Yes | — | CLOB API passphrase |
| `POLYMARKET_FUNDER_ADDRESS` | Yes | — | Funder wallet address that holds funds and tokens |
| `POLYMARKET_FUNDER_PRIVATE_KEY` | No | — | Funder wallet key for on-chain redemptions (Gnosis Safe setups only; omit if proxy key IS the funder key) |
| `POLYMARKET_RELAYER_API_KEY` | No | — | Relayer API key for gasless Safe transactions (also accepted as `RELAYER_API_KEY`) |
| `RELAYER_API_KEY_ADDRESS` | No | — | Address that owns the Relayer API key (also accepted as `POLYMARKET_RELAYER_API_KEY_ADDRESS`) |
| `POLYMARKET_CHAIN_ID` | No | `137` | Polygon chain ID |
| `DATABASE_URL` | No | `sqlite:///./data/pcopbot.db` | SQLAlchemy database URL |
| `POLL_INTERVAL_SECONDS` | No | `15` | Seconds between poll cycles (overridable at runtime via dashboard) |
| `DRY_RUN` | No | `true` | Global dry-run override — set to `false` for live trading |
| `AUTO_SELL_THRESHOLD` | No | `0.999` | Price threshold for auto-selling (set to `0` to disable) |
| `LOG_LEVEL` | No | `INFO` | Logging verbosity (`DEBUG`, `INFO`, `WARNING`, `ERROR`) |
| `DASHBOARD_PASSWORD` | No | — | Dashboard login password (empty = no auth) |
| `DOMAIN` | No | — | Domain name for nginx SSL certificate |
| `POLYGON_RPC_URL` | No | `https://polygon-rpc.com` | Polygon RPC endpoint for on-chain transactions |

### Per-Trader Settings (via Dashboard)

<!-- AUTO-GENERATED: derived from db/models.py Trader columns -->

| Setting | DB Column | Description |
|---------|-----------|-------------|
| Dry Run | `dry_run` | Per-trader simulate mode; global `DRY_RUN` env overrides all |
| Sizing Mode | `sizing_mode` | `fixed` (dollar budget) or `proportional` (% of original) |
| Fixed Amount ($) | `fixed_amount` | Dollar amount per copy trade (fixed mode) |
| Copy Percentage (%) | `proportional_pct` | Percentage of original trade size (proportional mode) |
| Sell Only | `sell_only` | Only copy SELL trades, skip BUY |
| Buy Order Type | `buy_order_type` | `market` (FOK via `MarketOrderArgs`) or `limit` (GTC via `OrderArgs`) |
| Sell Order Type | `sell_order_type` | `market` (FOK) or `limit` (GTC) |
| Buy Slippage (%) | `buy_slippage` | Max slippage tolerance for FOK BUY orders |
| Sell Slippage (%) | `sell_slippage` | Max slippage tolerance for FOK SELL orders |
| Buy Price Offset (%) | `buy_price_offset_pct` | Limit order ceiling: `trader_price * (1 + offset/100)` |
| Sell Price Offset (%) | `sell_price_offset_pct` | Limit order floor: `trader_price * (1 - offset/100)` |
| Limit Timeout (s) | `limit_timeout_seconds` | GTC order timeout before cancellation |
| Buy Limit Fallback | `buy_limit_fallback` | Retry as FOK if GTC BUY times out |
| Sell Limit Fallback | `sell_limit_fallback` | Retry as FOK if GTC SELL times out |
| Buy Agg Window (s) | `buy_agg_window_seconds` | Sliding window for BUY fill aggregation (0 = disabled) |
| Sell Agg Window (s) | `sell_agg_window_seconds` | Sliding window for SELL fill aggregation (0 = disabled) |
| Tiered TP Rules | `tp_rules` | JSON array `[{"max_entry": 0.30, "target": 0.80}, ...]` — sell when price hits target for the entry bracket |
| Take-Profit % | `tp_pct` | Simple take-profit percentage (fallback when `tp_rules` not set) |
| Stop-Loss % | `sl_pct` | Stop-loss percentage (0 = disabled) |
| Ignore Trades Under ($) | `ignore_trades_under` | Skip fills below this USD value; combined with aggregation window to merge fragmented fills |
| Buy at Min | `buy_at_min` | When capped/proportional trade falls below `min_per_trade`, bump up to minimum instead of rejecting |
| Min / Max Price | `min_price` / `max_price` | Only copy trades within this price range |
| Min Per Trade ($) | `min_per_trade` | Reject (or bump with `buy_at_min`) trades below this after capping |
| Max Per Trade ($) | `max_per_trade` | Cap individual trade size to this dollar limit |
| Total Spend Limit ($) | `total_spend_limit` | Maximum cumulative BUY spend across all trades |
| Max Per Market ($) | `max_per_market` | Maximum BUY exposure per market (condition_id) |
| Max Per Yes/No ($) | `max_per_yes_no` | Maximum BUY exposure per outcome (token_id) |
| Max Position Limit ($) | `max_position_limit` | Maximum net position per token |
| Max Holder Markets | `max_holder_market_number` | Maximum number of markets with open positions (0 = unlimited) |

## Project Structure

<!-- AUTO-GENERATED: derived from repository structure -->

```
bot/                 Core trading logic
  main.py              Daemon entry point (poll loop, fill aggregation, PnL updates,
                       auto-sell, tiered TP, auto-redeem)
  tracker.py           Fetches trades & positions from Polymarket APIs
  executor.py          Executes copy trades, auto_sell_winning_positions(),
                       take_profit_monitor() with tiered TP rules
  fill_buffer.py       FillBuffer — sliding-window aggregation for fragmented fills
  risk.py              9 pre-trade risk checks (cap + reject); buy_at_min bump logic
  redeemer.py          On-chain redemption, loss detection, manual trade sync
  watermark.py         Per-trader watermark management

config/              Configuration
  settings.py          Environment-based settings loader (python-dotenv)

db/                  Database layer
  models.py            SQLAlchemy ORM: Trader, CopyTrade, Position, BotLog, BotSetting
  database.py          Engine and session factory

dashboard/           Streamlit web UI
  app.py               Multi-page entry point with timing-safe password auth gate
  _pages/              Page modules: traders, add_trader, history, pnl, logs,
                       settings, wallet

scripts/             Maintenance utilities (python -m scripts.<name>)
  refresh_prices.py       Backfill fill prices from wallet activity
  fix_historical_pnl.py   Recalculate all PnL records
  check_approval.py       Verify on-chain ERC-1155 approvals
  diagnose_sizing.py      Debug copy-trade sizing calculations
  diagnose_pnl.py         Debug PnL discrepancies
  analyze_trader.py       Inspect a specific trader's trade history
  cleanup_duplicate_sells.py  Remove duplicate SELL records

nginx/               Reverse proxy configuration
  nginx.conf           SSL + rate limiting + security headers
  http-only.conf       HTTP-only fallback (no SSL)
  entrypoint.sh        Auto-selects SSL or HTTP config at container start

tests/               Pytest test suite
  test_executor.py     Order execution, auto-sell, tiered TP tests
  test_risk.py         Risk check tests (cap, reject, buy_at_min)
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
