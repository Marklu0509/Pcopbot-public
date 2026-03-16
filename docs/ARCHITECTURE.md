# Architecture & Technical Deep-Dive

This document covers the full system architecture, algorithms, and design decisions in Pcopbot. Intended as a technical reference for interviews and onboarding.

---

## Table of Contents

1. [System Overview](#1-system-overview)
2. [Bot Daemon — Main Event Loop](#2-bot-daemon--main-event-loop)
3. [Trade Execution Pipeline](#3-trade-execution-pipeline)
4. [Risk Management Engine](#4-risk-management-engine)
5. [Trade Tracking & API Integration](#5-trade-tracking--api-integration)
6. [Auto-Sell Strategy](#6-auto-sell-strategy)
7. [On-Chain Redemption](#7-on-chain-redemption)
8. [PnL Calculation](#8-pnl-calculation)
9. [Watermark & De-duplication](#9-watermark--de-duplication)
10. [Database Schema](#10-database-schema)
11. [Dashboard Architecture](#11-dashboard-architecture)
12. [Infrastructure & Security](#12-infrastructure--security)
13. [Data Flow Diagram](#13-data-flow-diagram)
14. [Key Design Decisions](#14-key-design-decisions)
15. [Edge Cases & Solutions](#15-edge-cases--solutions)
16. [Performance Characteristics](#16-performance-characteristics)

---

## 1. System Overview

### Tech Stack

| Layer | Technology |
|-------|-----------|
| Language | Python 3.11 |
| Trading | py-clob-client (Polymarket CLOB) |
| Blockchain | Web3.py (Polygon RPC) |
| Database | SQLAlchemy + SQLite |
| Dashboard | Streamlit (multi-page) |
| Proxy | Nginx (SSL, rate limiting) |
| Deployment | Docker Compose |
| Certificates | Let's Encrypt / Certbot |

### Core Components

```
                     Polymarket APIs
                    (Data, Gamma, CLOB)
                          |
                          v
     +--------------------------------------------+
     |              Bot Daemon                     |
     |  ┌──────────┐  ┌──────────┐  ┌──────────┐  |
     |  | Tracker  |  | Executor |  | Redeemer |  |
     |  | (fetch)  |->| (trade)  |  | (redeem) |  |
     |  └──────────┘  └─────┬────┘  └──────────┘  |
     |       ^              |              |       |
     |       |         ┌────v────┐         |       |
     |       |         |  Risk   |         |       |
     |       |         | Engine  |         |       |
     |       |         └─────────┘         |       |
     +-------┼─────────────┼───────────────┼───────+
             |             |               |
             v             v               v
     +--------------------------------------------+
     |             SQLite Database                 |
     |  traders | copy_trades | positions | logs   |
     +--------------------------------------------+
             |
             v
     +--------------------------------------------+
     |          Streamlit Dashboard                |
     +--------------------------------------------+
             |
             v
     +--------------------------------------------+
     |    Nginx (SSL + Rate Limiting + Headers)    |
     +--------------------------------------------+
```

---

## 2. Bot Daemon — Main Event Loop

**File: `bot/main.py`**

The bot runs as a persistent daemon with a configurable poll interval (default 15s).

### Startup Phase

1. **Database Init** — create tables, run schema migrations
2. **Logging Setup** — attach DB handler so logs are visible in the dashboard
3. **Watermark Init** — set initial watermark for each active trader (prevents reprocessing old trades on first run)
4. **Position Sync** — fetch pre-existing holdings for each trader from Data API

### Poll Loop (every N seconds)

```
Each cycle:
├── For each active trader:
│   ├── Fetch new trades since watermark (Data API)
│   ├── Skip BUY trades if trader has sell_only=True
│   ├── For each trade:
│   │   ├── Risk checks (cap_and_check)
│   │   ├── Execute copy trade (or dry run)
│   │   └── Advance watermark
│   └── Auto-sell winning positions (if enabled)
│
├── Every 10 cycles:
│   ├── Backfill fill prices from wallet activity
│   ├── Sync pre-existing positions
│   └── Update unrealized PnL from market prices
│
└── Every 20 cycles:
    ├── Auto-redeem resolved winning positions
    ├── Detect manual redemptions from wallet
    ├── Detect manual OTC sells
    └── Detect expired losing positions
```

### Restart Safety

- **Watermark** ensures no trades are missed during downtime — the bot resumes exactly where it left off
- **Idempotent processing** — duplicate trade detection via `original_trade_id` uniqueness
- **Graceful shutdown** — catches SIGTERM/SIGINT, commits pending DB changes

---

## 3. Trade Execution Pipeline

**File: `bot/executor.py`**

### Step-by-Step Flow

```
1. Calculate Copy Size
   ├── Fixed mode:  copy_size = fixed_amount / price
   └── Proportional: copy_size = original_size * (pct / 100)

2. SELL-Specific Logic
   ├── Cap to actual holdings (can't sell more than owned)
   └── Dust closeout: if remaining < $1 after sell, sell everything

3. Risk Checks (risk.cap_and_check)
   ├── Hard rejects: price filter, ignore_trades_under
   ├── Soft caps: per_trade, per_market, per_outcome, position_limit
   ├── Minimum check: reject if capped result too small
   └── Slippage check: reject if price diverged too much

4. Order Placement (live mode)
   ├── Get CLOB client with Level 2 auth
   ├── Query orderbook for best available price
   ├── Choose order type:
   │   ├── FOK (Fill-or-Kill): immediate execution or cancel
   │   └── GTC (Good-Till-Cancelled): limit order with timeout
   └── Post order to CLOB

5. GTC Timeout Logic
   ├── Poll order status every 1-3 seconds
   ├── If filled → record success
   ├── If timeout (default 30s) → cancel order
   └── If limit_fallback_market=True → retry as FOK

6. Fill Price Recovery
   ├── Fetch actual fill price from order details
   ├── Fallback: use order_price if details unavailable
   └── Periodic backfill from wallet activity (every 10 cycles)

7. PnL Calculation (SELL trades only)
   └── realized_pnl = (sell_price - avg_buy_price) * copy_size
```

### Order Types

| Type | Behavior | Use Case |
|------|----------|----------|
| FOK (Market) | Fill entire order immediately or cancel | Fast execution, accepts slippage |
| GTC (Limit) | Sit in orderbook until filled or cancelled | Better price, may not fill |
| GTC + FOK Fallback | Try limit first, fall back to market | Best of both worlds |

### Fill Price Backfill Problem

FOK orders don't persist in the CLOB — `client.get_order(id)` returns nothing after execution. Solution:

1. Fetch funder wallet's activity from Data API
2. Match each CopyTrade by `token_id` + `timestamp` (within +/-10 min window)
3. Use the closest-in-time activity entry as the actual filled price
4. Run every 10 poll cycles to catch any missing prices

---

## 4. Risk Management Engine

**File: `bot/risk.py`**

### Architecture: Cap then Reject

Unlike traditional risk systems that simply accept/reject, this engine **caps** trade sizes down to limits before deciding to reject. This maximizes trade execution while staying within risk bounds.

```
Input: copy_size = $50

Step 1 - Hard Rejects (binary pass/fail):
  ├── ignore_trades_under: target trade < $5? → REJECT
  └── price_filter: price outside [0.05, 0.95]? → REJECT

Step 2 - Soft Caps (reduce size, don't reject):
  ├── max_per_trade: $50 > $10 limit → cap to $10
  ├── total_spend: already spent $90 of $100 → cap to $10
  ├── max_per_market: already $8 in market, limit $15 → cap to $7
  ├── max_per_yes_no: already $5 on Yes, limit $10 → cap to $5
  └── position_limit: net position $3, limit $8 → cap to $5

Step 3 - Minimum Check:
  └── capped result $5 < min_per_trade $1? → OK, proceed

Step 4 - Hard Floor:
  └── order value < $1? → REJECT

Step 5 - Slippage Check:
  └── |best_price - expected_price| / expected_price > 3%? → REJECT

Output: (capped_copy_size=$5, rejection=None)  → EXECUTE
```

### All 9 Checks

| # | Check | Type | Applies To |
|---|-------|------|-----------|
| 1 | `ignore_trades_under` | Reject | All trades |
| 2 | `price_filter` | Reject | All trades |
| 3 | `max_per_trade` | Cap | BUY only |
| 4 | `total_spend_limit` | Cap | BUY only |
| 5 | `max_per_market` | Cap | BUY only |
| 6 | `max_per_yes_no` | Cap | BUY only |
| 7 | `max_position_limit` | Cap | BUY only |
| 8 | `min_per_trade` | Reject | After caps |
| 9 | `slippage_check` | Reject | All trades |

---

## 5. Trade Tracking & API Integration

**File: `bot/tracker.py`**

### Data Sources

| API | Base URL | Purpose | Limitations |
|-----|----------|---------|-------------|
| **Data API** | `data-api.polymarket.com` | Trade activity, positions, market data | Most reliable |
| **Gamma API** | `gamma-api.polymarket.com` | Market details, token prices | Returns 422 for resolved markets |
| **CLOB REST** | `clob.polymarket.com` | Orderbook, complement tokens | Rate limits on heavy use |

### Key Functions

**`get_new_trades(wallet, watermark)`**
- Fetches trades from Data API `/activity` endpoint
- Filters: `timestamp > watermark` (strict inequality)
- Returns oldest-first for chronological processing
- Handles multiple timestamp formats (unix, ISO-8601, string)

**`fetch_prices_by_token_ids(token_ids)`**
- Batch token price lookup via Gamma API
- Works for both active and resolved markets
- Returns `{token_id: price}` mapping

**`fetch_complement_token_ids(token_ids)`**
- Two-step strategy: Gamma batch first, CLOB REST fallback
- Returns `{token_id: complement_token_id}` for binary markets
- Essential for complement-based auto-sell pricing

**`fetch_positions(wallet)`**
- Open positions from Data API `/positions`
- Returns size, curPrice, cashPnl for each position
- Used for unrealized PnL and auto-sell sizing

### Resilience

- HTTP session with retry strategy (3 retries, 0.5s backoff)
- Graceful degradation: returns empty results on API errors
- 10-15s timeout per request
- Never crashes the main loop on API failures

---

## 6. Auto-Sell Strategy

**File: `bot/executor.py` — `auto_sell_winning_positions()`**

### Price Discovery (4 sources)

The auto-sell uses the **maximum price** from four independent sources:

```
Source 1: CLOB Best Bid
  └── Direct orderbook query for the token

Source 2: Complement Price (most reliable for binary markets)
  └── effective_price = 1 - best_ask(complement_token)
  └── Example: selling Yes@0.999 = buying No@0.001

Source 3: Gamma API outcomePrices
  └── Market-wide price from Gamma

Source 4: Data API curPrice
  └── Wallet position price (accounts for complement)

Final price = max(source1, source2, source3, source4)
```

### Selling Logic

```
If max_price >= threshold (0.999):
  ├── Attempt sell at best_price
  ├── If rejected, try 0.999
  ├── If rejected, try 0.99
  └── If all fail, wait for next cycle

If max_price >= 0.95 but < threshold:
  └── Apply 30s cooldown before retrying (avoid FOK spam)

If max_price < 0.95:
  └── Skip (not close enough to threshold)
```

### Position Source

Uses the **wallet's actual holdings** (not just DB records) as source of truth:
- Includes pre-existing positions (before bot started)
- Includes bot-copied positions
- Prevents selling more than actually held

---

## 7. On-Chain Redemption

**File: `bot/redeemer.py`**

### When Markets Resolve

Polymarket markets resolve to either $1.00 (winning) or $0.00 (losing). Winners can redeem their ERC-1155 tokens for USDC on-chain.

### Two Redemption Paths

**Binary Markets (neg_risk=false)**
```
Contract: ConditionalTokens (0x4D97DCd97eC945f40cF65F87097ACe5EA0476045)
Function: redeemPositions(collateralToken, parentCollectionId, conditionId, indexSets)

indexSet calculation:
  - Binary market has 2 outcomes: index 0 and index 1
  - indexSet = [1 << winning_index]
  - Example: outcome 0 wins → indexSet = [1], outcome 1 wins → indexSet = [2]
```

**Multi-Outcome Markets (neg_risk=true)**
```
Contract: NegRiskAdapter (0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296)
Function: redeemPositions(conditionId, amount)
  - Simpler: just pass winning token amount
```

### Gnosis Safe Transaction Flow

Most production setups use a Gnosis Safe (multisig) as the funder wallet. The bot's proxy key can't sign transactions directly. Solution: **Relayer API (gasless meta-transactions)**.

```
Step 1: Encode function call
  └── ABI-encode redeemPositions(...) → call_data bytes

Step 2: Compute Safe TX Hash (EIP-712)
  ├── Domain separator = keccak256(domainSeparator_typehash, chain_id, safe_address)
  ├── Struct hash = keccak256(safeTx_typehash, to, value, data, operation, ...)
  └── TX hash = keccak256(0x1901 || domain_sep || struct_hash)

Step 3: Sign with proxy key
  └── ECDSA sign(tx_hash) → signature
  └── Append signature type byte (0x02 = contract signature via proxy)

Step 4: Submit to Relayer API
  POST https://relayer-v2.polymarket.com/submit
  Headers: RELAYER_API_KEY, RELAYER_API_KEY_ADDRESS
  Body: { from, data, signature, type: "SAFE" }

Step 5: Relayer submits on-chain
  └── Returns transactionHash
  └── No gas cost to the user
```

### Loss Detection

Three detection mechanisms run every 20 poll cycles:

| Function | Detects | How |
|----------|---------|-----|
| `detect_manual_redemptions` | Manual redemptions via Polymarket UI | Checks wallet activity for REDEEM type entries |
| `detect_manual_sells` | OTC sells outside the bot | Compares wallet holdings vs DB records |
| `detect_expired_losses` | Losing positions in resolved markets | Checks wallet tokens with price < 0.02 |

Each creates a synthetic SELL record in the database with appropriate PnL.

---

## 8. PnL Calculation

### Split Model: Realized vs Unrealized

```
Total PnL = Realized PnL + Unrealized PnL

Realized PnL (locked in, never changes):
  └── Sum of all SELL trade PnL
  └── pnl = (sell_price - avg_buy_price) * copy_size
  └── Includes: manual sells, auto-sells, redemptions, expired losses

Unrealized PnL (fluctuates with market):
  └── From wallet positions via Data API
  └── pnl = (current_price - avg_buy_price) * shares_held
  └── Falls back to DB calculation if API fails
```

### Avoiding Double-Counting

- BUY trade `pnl` field tracks unrealized PnL (updated periodically from market prices)
- When shares are sold, BUY trade `pnl` is **not** used for realized calculation
- SELL trade `pnl` is computed independently using weighted average buy price
- Once fully sold, the position contributes only realized PnL

### Average Buy Price Calculation

```python
# Weighted average across all successful BUY trades for this token
total_cost = sum(copy_size * copy_price for each BUY trade)
total_shares = sum(copy_size for each BUY trade)
avg_buy_price = total_cost / total_shares
```

### Win Rate

```
Decisive trades: trades where |ROI| >= 3% (filters out noise)
Win rate = count(pnl > 0) / count(decisive trades) * 100
```

---

## 9. Watermark & De-duplication

**File: `bot/watermark.py`**

### Problem

The Data API returns trades newest-first, and the bot polls every 15 seconds. Without tracking, trades would be processed repeatedly.

### Solution: Monotonic Timestamp Watermark

```
Initialization:
  └── New trader → watermark = now (only future trades)

Each poll cycle:
  ├── Fetch trades where timestamp > watermark
  ├── Sort oldest-first (chronological processing)
  ├── Process each trade
  └── Advance watermark to trade's timestamp

Invariant:
  └── Watermark NEVER decreases (monotonic)
  └── Even on crash, next poll picks up from last watermark
```

### Properties

- **At-least-once delivery** — a trade is guaranteed to be seen
- **Idempotent processing** — `original_trade_id` uniqueness check prevents duplicates
- **Timezone-naive** — all timestamps stored as naive UTC (matching DB storage)
- **No gaps** — watermark only advances after successful processing

---

## 10. Database Schema

**File: `db/models.py`**

### Tables

**`traders`** — tracked wallet configuration
```
id              INTEGER PRIMARY KEY
wallet_address  VARCHAR UNIQUE        -- 0x-prefixed Polygon address
label           VARCHAR               -- display name
is_active       BOOLEAN               -- enable/disable without deleting
sell_only       BOOLEAN               -- skip BUY trades
sizing_mode     VARCHAR               -- "fixed" or "proportional"
fixed_amount    FLOAT                 -- USD per trade (fixed mode)
proportional_pct FLOAT                -- % of original (proportional mode)
buy_order_type  VARCHAR               -- "market" (FOK) or "limit" (GTC)
sell_order_type VARCHAR               -- "market" or "limit"
buy_slippage    FLOAT                 -- max slippage %
sell_slippage   FLOAT                 -- max slippage %
limit_timeout_seconds INTEGER         -- GTC timeout before cancel
limit_fallback_market BOOLEAN         -- fallback to FOK on GTC timeout
ignore_trades_under   FLOAT           -- skip target trades below this
min_price       FLOAT                 -- price filter lower bound
max_price       FLOAT                 -- price filter upper bound
min_per_trade   FLOAT                 -- minimum per trade after caps
max_per_trade   FLOAT                 -- maximum per trade (cap target)
total_spend_limit     FLOAT           -- cumulative spend cap
max_per_market  FLOAT                 -- per-market exposure cap
max_per_yes_no  FLOAT                 -- per-outcome cap
watermark_timestamp   DATETIME        -- last processed trade time
created_at      DATETIME
updated_at      DATETIME
```

**`copy_trades`** — every trade attempt (success, fail, dry run)
```
id                INTEGER PRIMARY KEY
trader_id         INTEGER FK(traders.id)
original_trade_id VARCHAR             -- source trade identifier
original_market   VARCHAR             -- condition_id
original_token_id VARCHAR             -- asset_id (76+ digit number)
market_title      VARCHAR             -- human-readable market name
outcome           VARCHAR             -- "Yes", "No", etc.
original_side     VARCHAR             -- "BUY" or "SELL"
original_size     FLOAT               -- target wallet's share count
original_price    FLOAT               -- target wallet's fill price
original_timestamp DATETIME           -- when target traded
copy_size         FLOAT               -- our share count (may be capped)
copy_price        FLOAT               -- our actual fill price
status            VARCHAR             -- success|failed|dry_run|slippage_exceeded|below_threshold|position_limit
error_message     VARCHAR             -- reason for non-success
order_id          VARCHAR             -- CLOB order ID or synthetic key
pnl               FLOAT               -- unrealized (BUY) or realized (SELL)
executed_at       DATETIME            -- when we executed
```

**`positions`** — pre-existing holdings (synced from Data API)
```
id, trader_id, condition_id, asset_id, market_title, outcome,
size, avg_price, current_value, initial_value, pnl, pnl_pct,
cur_price, fetched_at
```

**`bot_logs`** — in-DB log storage for dashboard
```
id, timestamp, level, logger_name, message
```

**`bot_settings`** — runtime configuration (editable via dashboard)
```
key (PK), value, updated_at
-- Keys: poll_interval_seconds, dry_run, auto_sell_enabled, log_level
```

---

## 11. Dashboard Architecture

**File: `dashboard/app.py`**

### Design

- **Multi-page Streamlit app** with dynamic module loading (avoids circular imports)
- **Password gate** using `hmac.compare_digest` (timing-safe comparison)
- **Sidebar** with live UTC clock, wallet balance widget (auto-refresh 30s), and page navigation
- **Per-page data loading** — each page queries the DB independently

### Pages

| Page | Key Features |
|------|-------------|
| Traders | Tabbed per-trader view; inline settings editor; holdings with live prices; trade history with status icons |
| Add Trader | Wallet validation (0x + 40 hex chars); default settings; auto position sync |
| History | Cross-trader trade log; status + trader filter; pagination (50/page) |
| PnL | Invested/PnL/ROI/Win Rate metrics; cumulative PnL chart (Plotly); per-trader breakdown table |
| Logs | Level filter; pagination; confirmation dialog for bulk deletion |
| Settings | Runtime config (no restart needed): poll interval, dry run, auto-sell, log level |

---

## 12. Infrastructure & Security

### Docker Compose Services

```yaml
bot:        python -m bot.main          # Trading daemon
dashboard:  streamlit run dashboard/app.py  # Web UI (port 8501)
nginx:      nginx:alpine                # Reverse proxy (ports 80, 443)
certbot:    certbot/certbot             # SSL certificate management
```

### Nginx Security

| Feature | Configuration |
|---------|--------------|
| Rate Limiting | 5 requests/min per IP on login (burst 3) |
| HSTS | `max-age=31536000; includeSubDomains` |
| Clickjacking | `X-Frame-Options: DENY` |
| MIME Sniffing | `X-Content-Type-Options: nosniff` |
| Referrer | `strict-origin-when-cross-origin` |
| XSRF | Streamlit XSRF protection enabled |
| WebSocket | Proxy upgrade for Streamlit SSE |

### Environment Separation

| File | Used By | Contains |
|------|---------|----------|
| `.env` | Bot container | Private keys, API secrets, all credentials |
| `.env.dashboard` | Dashboard container | DB path, password, public API URLs only |

The dashboard container never sees private keys or API secrets.

### Secret Management

- All secrets in `.env` files (gitignored)
- No hardcoded secrets in source code
- API keys redacted in error messages
- Log output sanitized (no credential leakage)

---

## 13. Data Flow Diagram

```
┌─────────────────────────────────────────────────────────┐
│                  External APIs                          │
│                                                         │
│  Data API          Gamma API          CLOB API          │
│  /activity         /markets           /markets          │
│  /positions        /prices            /order            │
│                                                         │
└──────┬─────────────────┬──────────────────┬─────────────┘
       │                 │                  │
       ▼                 ▼                  ▼
┌─────────────────────────────────────────────────────────┐
│                    Bot Daemon                            │
│                                                         │
│  ┌─────────┐    ┌──────────┐    ┌───────────────┐       │
│  │ Tracker │───>│ Executor │───>│ CLOB Client   │──┐    │
│  │ (fetch) │    │ (decide) │    │ (place order) │  │    │
│  └─────────┘    └────┬─────┘    └───────────────┘  │    │
│       │              │                             │    │
│       │         ┌────▼─────┐                       │    │
│       │         │   Risk   │                       │    │
│       │         │  Engine  │                       │    │
│       │         └──────────┘                       │    │
│       │                                            │    │
│  ┌────▼────────────────────────────────────────┐   │    │
│  │              Redeemer                       │   │    │
│  │  ├─ Auto-redeem (on-chain via Relayer)     │   │    │
│  │  ├─ Detect manual redemptions              │   │    │
│  │  ├─ Detect manual sells                    │   │    │
│  │  └─ Detect expired losses                  │   │    │
│  └─────────────────────────────────────────────┘   │    │
│                                                    │    │
└────────────────────────┬───────────────────────────┘    │
                         │                                │
                         ▼                                │
              ┌──────────────────┐                        │
              │   SQLite DB      │                        │
              │                  │◄───────────────────────┘
              │  traders         │   (record trade results)
              │  copy_trades     │
              │  positions       │
              │  bot_logs        │
              │  bot_settings    │
              └────────┬─────────┘
                       │
                       ▼
              ┌──────────────────┐
              │    Streamlit     │
              │    Dashboard     │
              └────────┬─────────┘
                       │
                       ▼
              ┌──────────────────┐
              │  Nginx Reverse   │
              │  Proxy (SSL)     │
              └──────────────────┘
                       │
                       ▼
                    Browser
```

---

## 14. Key Design Decisions

### Why Watermark Instead of Trade ID Dedup?

Trade IDs from the Data API are not guaranteed to be stable across API versions. Timestamps are monotonically increasing and universally reliable. The watermark approach also naturally handles "catch-up" after downtime — no need to query for missed IDs.

### Why Cap Instead of Reject for Risk Checks?

Rejecting a $50 trade because the limit is $10 wastes the trading signal. By capping to $10, the bot still participates in the trade at a safe size. Only hard constraints (price filters, slippage) should reject outright.

### Why 4 Price Sources for Auto-Sell?

No single Polymarket API is 100% reliable:
- CLOB book can be empty or stale
- Gamma returns 422 for resolved markets
- Data API curPrice can lag
- Complement pricing is mathematically sound but requires a second API call

Using the maximum across all four ensures the most accurate price discovery.

### Why Gnosis Safe + Relayer Instead of EOA?

- **Security**: Private key never directly controls funds; Safe requires signatures
- **Gasless**: Relayer pays gas fees on behalf of the user
- **Multi-sig capable**: Can add additional signers for larger operations
- **Recovery**: Safe can be recovered; a lost EOA key means lost funds

### Why SQLite Instead of PostgreSQL?

- **Simplicity**: Single file, no separate database server
- **Performance**: More than sufficient for single-bot write volumes (~1 trade/min)
- **Portability**: Database travels with the Docker volume
- **Upgrade path**: `DATABASE_URL` config supports PostgreSQL when needed

### Why Streamlit Instead of React/Next.js?

- **Rapid development**: Full dashboard in ~1000 lines of Python
- **Same language**: No context switching between Python backend and JS frontend
- **Built-in features**: Auth, auto-refresh, charts, data tables
- **Trade-off**: Less customizable UI, but adequate for monitoring dashboard

---

## 15. Edge Cases & Solutions

| Edge Case | Problem | Solution |
|-----------|---------|----------|
| Resolved market (Gamma 422) | `_get_market_info()` returns None, skips redemption | Fallback to `fetch_prices_by_token_ids()`; if price >= 0.99, treat as resolved |
| FOK order not in CLOB history | `client.get_order()` returns None | Backfill from wallet activity by token_id + timestamp matching |
| Losing tokens remain in wallet | Token has size > 0 but price = 0 | `detect_expired_losses` checks `wallet_price < 0.02` |
| Dust positions | Remaining value < $1 after partial sell | "Dust closeout" — sell entire remaining position |
| Rounding residuals | Net holdings = 0.005 after full sell | Filter `net < 0.1` to skip rounding noise |
| Bot restart during trade | Trade fetched but not yet executed | Watermark not advanced until after execution; trade retried on restart |
| Manual redemption via UI | Bot doesn't know about the redemption | `detect_manual_redemptions` scans wallet activity for REDEEM entries |
| GTC order never fills | Limit price too aggressive | Timeout + FOK fallback (configurable per trader) |
| Multiple traders same market | Risk limits should be per-trader, not global | All risk checks filter by `trader_id` |

---

## 16. Performance Characteristics

### Latency

| Operation | Typical Latency |
|-----------|----------------|
| Data API fetch (per trader) | 1-2s |
| Risk check (9 checks) | < 5ms |
| CLOB order placement | 200-500ms |
| Orderbook query | 200-500ms |
| DB write (single trade) | < 10ms |
| Dashboard page load | < 500ms |
| On-chain redemption | 2-5s (Relayer) |

### Scaling

| Metric | Estimate |
|--------|----------|
| 1 trader | ~2-3s per poll cycle |
| 10 traders | ~20-30s per poll cycle |
| 50+ traders | Consider increasing poll interval or parallelizing API calls |
| Database | < 1GB for 100k+ trades |
| Memory (bot) | ~50-100MB |
| Memory (dashboard) | ~200-300MB |

### Bottleneck

The system is **I/O bound** — API calls dominate each poll cycle. Adding more traders increases cycle time linearly. The database and risk engine are negligible in comparison.
