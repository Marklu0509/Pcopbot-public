# Architecture & Technical Deep-Dive

<!-- AUTO-GENERATED sections marked inline — Last updated 2026-03-30 -->

This document covers the full system architecture, algorithms, and design decisions in Pcopbot. Intended as a technical reference for interviews and onboarding.

---

## Table of Contents

1. [System Overview](#1-system-overview)
2. [Bot Daemon — Main Event Loop](#2-bot-daemon--main-event-loop)
3. [Trade Execution Pipeline](#3-trade-execution-pipeline)
4. [Risk Management Engine](#4-risk-management-engine)
5. [Fill Aggregation](#5-fill-aggregation)
6. [Trade Tracking & API Integration](#6-trade-tracking--api-integration)
7. [Auto-Sell Strategy](#7-auto-sell-strategy)
8. [Tiered Take-Profit](#8-tiered-take-profit)
9. [On-Chain Redemption](#9-on-chain-redemption)
10. [PnL Calculation](#10-pnl-calculation)
11. [Watermark & De-duplication](#11-watermark--de-duplication)
12. [Database Schema](#12-database-schema)
13. [Dashboard Architecture](#13-dashboard-architecture)
14. [Infrastructure & Security](#14-infrastructure--security)
15. [Data Flow Diagram](#15-data-flow-diagram)
16. [Key Design Decisions](#16-key-design-decisions)
17. [Edge Cases & Solutions](#17-edge-cases--solutions)
18. [Performance Characteristics](#18-performance-characteristics)

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

<!-- AUTO-GENERATED: derived from bot/main.py run() -->

```
Each cycle:
├── For each active trader:
│   ├── Fetch new trades since watermark (Data API)
│   ├── Skip BUY trades if trader has sell_only=True
│   ├── For each trade:
│   │   ├── Fill aggregation (FillBuffer): buffer sub-threshold fills
│   │   │   ├── "buffered": record fill, wait for more
│   │   │   ├── "execute": combined value crossed threshold → run copy trade
│   │   │   └── "immediate": single fill ≥ threshold → run copy trade directly
│   │   ├── Risk checks (cap_and_check)
│   │   ├── Execute copy trade (or dry run)
│   │   └── Advance watermark
│   └── Flush expired aggregation buffer slots
│
├── Every 5 cycles:
│   └── Tiered take-profit monitor (take_profit_monitor)
│
├── Every cycle:
│   └── Auto-sell winning positions (if enabled, auto_sell_winning_positions)
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

<!-- AUTO-GENERATED: derived from bot/executor.py execute_copy_trade() -->

```
1. Calculate Copy Size
   ├── Fixed mode:  copy_size = fixed_amount / price
   └── Proportional: copy_size = original_size * (pct / 100)

2. SELL-Specific Logic
   ├── Cap to actual holdings (can't sell more than owned)
   └── Dust closeout: if remaining value < $1.50 after sell, sell everything

3. Risk Checks (risk.cap_and_check)
   ├── Hard rejects: price filter, ignore_trades_under
   ├── Soft caps: per_trade, per_market, per_outcome, position_limit
   ├── Minimum check: reject or bump (buy_at_min) if capped result below min_per_trade
   └── Slippage check: reject if price diverged too much

4. Order Placement (live mode)
   ├── Get CLOB client with Level 2 auth
   ├── Query orderbook for best available price
   ├── Choose order type and compute order price:
   │   ├── FOK BUY: MarketOrderArgs(amount=USDC, price=slippage-adjusted)
   │   │             buy_amount = floor(copy_size * order_price * 100) / 100
   │   ├── GTC BUY: OrderArgs(size=floor_2dp(copy_size), price=offset-adjusted)
   │   ├── FOK SELL: OrderArgs(size=floor_2dp(copy_size), price=slippage-adjusted)
   │   └── GTC SELL: OrderArgs(size=floor_2dp(copy_size), price=offset-adjusted)
   └── Post order to CLOB

5. GTC Timeout Logic
   ├── Poll order status every 1-3 seconds
   ├── If filled → record success
   ├── If timeout (default 30s) → cancel order, re-check for race-window fill
   └── If buy_limit_fallback / sell_limit_fallback → retry as FOK MarketOrderArgs/OrderArgs

6. Fill Price Recovery
   ├── Fetch actual fill price from order details (CLOB get_order)
   ├── Fallback: query funder wallet activity (Data API, 60s window)
   └── Periodic backfill from wallet activity (every 10 cycles, 120s window)

7. PnL Calculation (SELL trades only)
   └── realized_pnl = (sell_price - avg_buy_price) * copy_size
```

### Order Types

<!-- AUTO-GENERATED: derived from bot/executor.py -->

| Type | API Class | Behavior | Use Case |
|------|-----------|----------|----------|
| FOK BUY | `MarketOrderArgs` | Fill at market price (USDC amount, 2dp), cancel if not filled | Fast execution, accepts slippage |
| GTC BUY | `OrderArgs` | Sit in orderbook at limit price (shares floored to 2dp) until filled or cancelled | Better price, may not fill |
| FOK SELL | `OrderArgs` | Fill entire SELL order at price (shares floored to 2dp), cancel if not filled | Fast exit |
| GTC SELL | `OrderArgs` | Sit in orderbook at limit floor price until filled or cancelled | Better exit price |
| GTC + FOK Fallback | Per-side fallback toggle | Try limit first, retry as FOK on timeout | Best of both worlds |

### Fill Price Backfill Problem

FOK orders don't persist in the CLOB — `client.get_order(id)` returns nothing after execution. Solution:

1. Fetch funder wallet's activity from Data API
2. Match each CopyTrade by `token_id` + `timestamp` (within +/-10 min window)
3. Use the closest-in-time activity entry as the actual filled price
4. Run every 10 poll cycles to catch any missing prices

---

## 4. Risk Management Engine

**File: `bot/risk.py`**

<!-- AUTO-GENERATED: derived from bot/risk.py cap_and_check() -->

### Architecture: Cap then Reject (with Buy-at-Min Bump)

Unlike traditional risk systems that simply accept/reject, this engine **caps** trade sizes down to limits before deciding to reject. This maximizes trade execution while staying within risk bounds. The optional `buy_at_min` mode additionally **bumps** trades that fall below `min_per_trade` up to the minimum (as long as the bump doesn't exceed any cap ceiling).

```
Input: copy_size = $50, order_price used for all cap calculations

Step 1 - Hard Rejects (binary pass/fail):
  ├── ignore_trades_under: target trade < $5? → REJECT
  └── price_filter: price outside [min_price, max_price]? → REJECT

Step 2 - Soft Caps (reduce size, don't reject):
  ├── max_per_trade: $50 > $10 limit → cap to $10 (cap_ceiling = $10)
  ├── total_spend: already spent $90 of $100 → cap to $10
  ├── max_per_market: already $8 in market, limit $15 → cap to $7
  ├── max_per_yes_no: already $5 on Yes, limit $10 → cap to $5
  └── position_limit: net position $3, limit $8 → cap to $5

Step 3 - Minimum / Bump Check (BUY only):
  ├── capped result $5 * price < min_per_trade $1? → OK, proceed
  ├── buy_at_min=False: reject if below min_per_trade
  └── buy_at_min=True:
      ├── min_shares = min_per_trade / price
      ├── min_shares ≤ cap_ceiling? → BUMP up to min_shares
      └── min_shares > cap_ceiling? → REJECT (bump would exceed limits)

Step 4 - Hard Floor $1 USD:
  ├── order value < $1? → same bump/reject logic as Step 3
  └── buy_at_min=True: bump to $1 / price (if within cap ceiling)

Step 5 - Slippage Check (all trades):
  └── |best_price - expected_price| / expected_price > max_slippage%? → REJECT

Output: (capped_copy_size, rejection_status)
```

### All 9 Checks

<!-- AUTO-GENERATED: derived from bot/risk.py -->

| # | Check | Type | Applies To |
|---|-------|------|-----------|
| 1 | `ignore_trades_under` | Reject | All trades |
| 2 | `price_filter` | Reject | All trades |
| 3 | `max_per_trade` | Cap | BUY only |
| 4 | `total_spend_limit` | Cap | BUY only |
| 5 | `max_per_market` | Cap | BUY only |
| 6 | `max_per_yes_no` | Cap | BUY only |
| 7 | `max_position_limit` | Cap | BUY only |
| 8 | `min_per_trade` | Reject or Bump (`buy_at_min`) | After caps, BUY only |
| 9 | `slippage_check` | Reject | All trades |

### `buy_at_min` Flag

When `buy_at_min=True` on a trader and a trade would be rejected as below `min_per_trade`, the engine bumps the size up to `min_per_trade / price` shares — provided that size does not exceed the `cap_ceiling` established by the soft caps. If the bump would violate a limit, the trade is rejected instead. This prevents the common case where proportional sizing produces tiny sub-minimum trades on large tracked positions.

---

## 5. Fill Aggregation

**File: `bot/fill_buffer.py`**

<!-- AUTO-GENERATED: derived from bot/fill_buffer.py and bot/main.py -->

### Problem

Large limit orders on Polymarket often execute as many small partial fills. Each fill appears as a separate entry in the wallet's activity feed. Without aggregation, every sub-threshold fill would be rejected individually by `ignore_trades_under`, causing the bot to miss the trade entirely.

### Solution: FillBuffer with Sliding Window

```
For each incoming fill:

  fill_value = size * price

  If fill_value >= ignore_trades_under:
    → "immediate": execute right away (no buffering)

  Else (sub-threshold fill):
    → Prune fills older than window_seconds from this slot
    → Add fill to slot (trader_id, token_id, side)
    → If total_value >= ignore_trades_under:
        → "execute": build aggregated trade dict (VWAP, combined size)
        → Delete slot
    → Else:
        → "buffered": record individual fill in DB with status="buffered"
                       wait for more fills

On buffer expiry (flush_expired):
  → Slots with no activity for > window_seconds are expired
  → Buffered DB records updated to status="below_threshold"
```

### Aggregated Trade Dict

When a slot triggers, `FillBuffer` builds a synthetic trade dict:
- `size`: sum of all fill sizes
- `price`: VWAP — `sum(size * price) / total_size`
- `trade_id`: `agg_<first_trade_id>_<fill_count>`
- `_agg_fill_count`, `_agg_total_value`: persisted to `CopyTrade.agg_fill_count` / `agg_total_value`

### Configuration

| Column | Default | Description |
|--------|---------|-------------|
| `buy_agg_window_seconds` | 30 | Sliding window for BUY fills (0 = disabled) |
| `sell_agg_window_seconds` | 0 | Sliding window for SELL fills (0 = disabled by default) |
| `ignore_trades_under` | 0.0 | Aggregation threshold in USD |

Aggregation only activates when both `ignore_trades_under > 0` and `agg_window_seconds > 0`. A window of 0 means every fill is treated as immediate regardless of size.

---

## 6. Trade Tracking & API Integration

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

## 7. Auto-Sell Strategy

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
If effective >= threshold (0.999):
  ├── sell_price = threshold (or funder_price if >= 0.995)
  ├── Attempt FOK SELL with OrderArgs at sell_price
  ├── If price rejected (> 0.999 bounds) → retry at 0.999
  └── If all fail → wait for next cycle

If effective >= 0.95 but < threshold:
  └── Apply 30s cooldown before retrying (avoid FOK spam)

If effective < 0.95:
  └── Skip (not close enough to threshold)
```

### Position Source

Uses the **wallet's actual holdings** (not just DB records) as source of truth:
- Includes pre-existing positions (before bot started)
- Includes bot-copied positions
- Prevents selling more than actually held
- For dry-run traders: falls back to DB-recorded net holdings

---

## 8. Tiered Take-Profit

**File: `bot/executor.py` — `take_profit_monitor()`**

<!-- AUTO-GENERATED: derived from bot/executor.py take_profit_monitor() and _parse_tp_rules() -->

### Overview

Tiered take-profit allows different exit targets based on the average entry price. For example: trades entered below $0.30 should exit at $0.80, while trades entered above $0.30 should exit at $0.90.

### Configuration

`tp_rules` on the `Trader` model stores a JSON array:

```json
[
  {"max_entry": 0.30, "target": 0.80},
  {"max_entry": 1.00, "target": 0.90}
]
```

Rules are sorted by `max_entry` ascending. The first rule where `avg_buy_price <= max_entry` matches.

### Algorithm

```
Every 5 poll cycles:

For each trader with tp_rules configured:
  For each (trader_id, token_id) with open BUY positions:
    1. Compute avg_buy_price (weighted average across BUY trades)
    2. Match against tp_rules → target price (or skip if no match)
    3. Fetch current effective price (same 4-source logic as auto_sell)
    4. If effective >= target:
       ├── Apply 30s cooldown per (trader, token)
       ├── Live: FOK SELL with OrderArgs at min(target, 0.999)
       │         Record SELL trade and realized PnL
       └── Dry run: simulate SELL at effective price
```

### Relationship to Auto-Sell

Both systems run independently per poll cycle:
- **Auto-sell** (`auto_sell_winning_positions`) checks every cycle against the global `AUTO_SELL_THRESHOLD`
- **Tiered TP** (`take_profit_monitor`) checks every 5 cycles against per-trader, per-entry-price targets
- A position can be sold by either; whichever fires first wins

---

## 9. On-Chain Redemption

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

## 10. PnL Calculation

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

## 11. Watermark & De-duplication

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

## 12. Database Schema

**File: `db/models.py`**

### Tables

<!-- AUTO-GENERATED: derived from db/models.py -->

**`traders`** — tracked wallet configuration
```
id                      INTEGER PRIMARY KEY
wallet_address          VARCHAR UNIQUE        -- 0x-prefixed Polygon address
label                   VARCHAR               -- display name
is_active               BOOLEAN               -- enable/disable without deleting
sell_only               BOOLEAN               -- skip BUY trades
dry_run                 BOOLEAN               -- per-trader dry run mode
sizing_mode             VARCHAR               -- "fixed" or "proportional"
fixed_amount            FLOAT                 -- USD per trade (fixed mode)
proportional_pct        FLOAT                 -- % of original (proportional mode)
buy_order_type          VARCHAR               -- "market" (FOK) or "limit" (GTC)
sell_order_type         VARCHAR               -- "market" or "limit"
buy_slippage            FLOAT                 -- max slippage % for FOK BUY
sell_slippage           FLOAT                 -- max slippage % for FOK SELL
buy_price_offset_pct    FLOAT                 -- GTC BUY limit ceiling offset %
sell_price_offset_pct   FLOAT                 -- GTC SELL limit floor offset %
limit_timeout_seconds   INTEGER               -- GTC timeout before cancel
limit_fallback_market   BOOLEAN               -- legacy shared fallback toggle
buy_limit_fallback      BOOLEAN               -- per-side: retry BUY as FOK on timeout
sell_limit_fallback     BOOLEAN               -- per-side: retry SELL as FOK on timeout
buy_agg_window_seconds  INTEGER               -- BUY fill aggregation window (0=disabled)
sell_agg_window_seconds INTEGER               -- SELL fill aggregation window (0=disabled)
tp_pct                  FLOAT                 -- simple take-profit % (fallback)
sl_pct                  FLOAT                 -- stop-loss % (0=disabled)
tp_rules                VARCHAR               -- JSON tiered TP rules array
buy_at_min              BOOLEAN               -- bump sub-minimum trades up to min_per_trade
ignore_trades_under     FLOAT                 -- skip/aggregate fills below this USD
min_price               FLOAT                 -- price filter lower bound
max_price               FLOAT                 -- price filter upper bound
min_per_trade           FLOAT                 -- minimum per trade after caps
max_per_trade           FLOAT                 -- maximum per trade (cap target)
total_spend_limit       FLOAT                 -- cumulative spend cap
max_per_market          FLOAT                 -- per-market exposure cap
max_per_yes_no          FLOAT                 -- per-outcome cap
max_position_limit      FLOAT                 -- max net position per token
max_holder_market_number INTEGER              -- max markets with open positions (0=unlimited)
max_slippage            FLOAT                 -- legacy alias for slippage checks
min_trade_threshold     FLOAT                 -- legacy alias for min_per_trade
watermark_timestamp     DATETIME              -- last processed trade time
created_at              DATETIME
updated_at              DATETIME
```

**`copy_trades`** — every trade attempt (success, fail, dry run)
```
id                INTEGER PRIMARY KEY
trader_id         INTEGER FK(traders.id)
original_trade_id VARCHAR             -- source trade identifier (or synthetic key for agg/auto-sell)
original_market   VARCHAR             -- condition_id
original_token_id VARCHAR             -- asset_id (76+ digit number)
market_title      VARCHAR             -- human-readable market name
outcome           VARCHAR             -- "Yes", "No", etc.
original_side     VARCHAR             -- "BUY" or "SELL"
original_size     FLOAT               -- target wallet's share count
original_price    FLOAT               -- target wallet's fill price
original_timestamp DATETIME           -- when target traded
copy_size         FLOAT               -- our share count (may be capped or bumped)
copy_price        FLOAT               -- our actual fill price (backfilled from activity)
status            VARCHAR             -- success|failed|dry_run|slippage_exceeded|
                                      -- below_threshold|position_limit|buffered|
                                      -- skipped_sell_only|below_minimum_order
error_message     VARCHAR             -- reason for non-success or aggregation annotation
order_id          VARCHAR             -- CLOB order ID or synthetic key
agg_fill_count    INTEGER             -- number of fills aggregated (non-null for agg trades)
agg_total_value   FLOAT               -- total USD value of aggregated fills
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

## 13. Dashboard Architecture

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

## 14. Infrastructure & Security

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

## 15. Data Flow Diagram

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

## 16. Key Design Decisions

### Why Watermark Instead of Trade ID Dedup?

Trade IDs from the Data API are not guaranteed to be stable across API versions. Timestamps are monotonically increasing and universally reliable. The watermark approach also naturally handles "catch-up" after downtime — no need to query for missed IDs.

### Why Cap Instead of Reject for Risk Checks?

Rejecting a $50 trade because the limit is $10 wastes the trading signal. By capping to $10, the bot still participates in the trade at a safe size. Only hard constraints (price filters, slippage) should reject outright.

### Why `buy_at_min` Bumps Instead of Just Rejects?

Proportional sizing on a small tracked trade often produces sub-minimum amounts (e.g. 5% of a $20 trade = $1). A pure reject wastes the signal. `buy_at_min` bumps the size to the configured minimum — provided the bump doesn't violate any cap ceiling — so the bot always places the smallest allowed meaningful order.

### Why `MarketOrderArgs` for FOK BUY Instead of `OrderArgs`?

The Polymarket CLOB enforces different precision requirements per order type. For FOK (taker) BUY orders, `OrderArgs` requires the taker amount (shares) ≤ 4dp but the maker amount (USDC) must also be representable. `MarketOrderArgs` accepts USDC directly and handles precision internally, avoiding rounding errors that cause order rejection.

### Why a Sliding Window Instead of Fixed Window for Fill Aggregation?

A fixed window (e.g. "aggregate fills within the same minute") causes fills at minute boundaries to fall into different windows, splitting orders that belong together. A sliding window relative to the current fill time ensures all fills within `window_seconds` of each other are always grouped, regardless of when they arrive.

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

## 17. Edge Cases & Solutions

<!-- AUTO-GENERATED: derived from bot/ source analysis -->

| Edge Case | Problem | Solution |
|-----------|---------|----------|
| Resolved market (Gamma 422) | `_get_market_info()` returns None, skips redemption | Fallback to `fetch_prices_by_token_ids()`; if price >= 0.99, treat as resolved |
| FOK order not in CLOB history | `client.get_order()` returns None after execution | Backfill from wallet activity by token_id + timestamp (120s window, 20% size tolerance) |
| Losing tokens remain in wallet | Token has size > 0 but price = 0 | `detect_expired_losses` checks `wallet_price < 0.02` |
| Dust positions | Remaining value < $1.50 after partial sell | "Dust closeout" — sell entire remaining position |
| Rounding residuals | Net holdings = 0.005 after full sell | Filter `net < 0.1` to skip rounding noise |
| Bot restart during trade | Trade fetched but not yet executed | Watermark not advanced until after execution; trade retried on restart |
| Manual redemption via UI | Bot doesn't know about the redemption | `detect_manual_redemptions` scans wallet activity for REDEEM entries |
| GTC order never fills | Limit price too aggressive | Timeout + per-side FOK fallback (`buy_limit_fallback` / `sell_limit_fallback`) |
| GTC fill in cancel race window | Order fills between last poll and cancel call | Re-check order status after cancel attempt before marking failed |
| Multiple traders same market | Risk limits should be per-trader, not global | All risk checks filter by `trader_id` |
| Fragmented fills below threshold | Each sub-threshold fill rejected individually | `FillBuffer` accumulates fills in sliding window; executes when combined value ≥ threshold |
| Proportional trade too small for `min_per_trade` | Small % of small trade falls below minimum | `buy_at_min=True` bumps trade up to minimum if within cap ceiling |
| BUY precision on FOK orders | `py-clob-client` requires USDC maker amount ≤ 2dp | `MarketOrderArgs(amount=floor(copy_size * price * 100)/100)` |
| GTC limit order precision | CLOB requires taker (shares) ≤ 2dp for limit orders | `_snap_size_2dp()` floors shares to 2dp before `OrderArgs` |

---

## 18. Performance Characteristics

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
