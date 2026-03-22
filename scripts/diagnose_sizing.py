"""Diagnose copy-trade sizing for specific trade IDs.

Run on server:
    docker exec pcopbot-bot python3 scripts/diagnose_sizing.py 214247 218190 218293
"""

import sys
sys.path.insert(0, "/app")

from db.database import get_session_factory, init_db
from db.models import CopyTrade, Trader

init_db()
Session = get_session_factory()

ids = [int(x) for x in sys.argv[1:]] if len(sys.argv) > 1 else []
if not ids:
    print("Usage: python3 scripts/diagnose_sizing.py <id1> <id2> ...")
    sys.exit(1)

with Session() as s:
    for cid in ids:
        ct = s.query(CopyTrade).filter(CopyTrade.id == cid).first()
        if not ct:
            print(f"ID {cid}: NOT FOUND\n")
            continue
        t = s.query(Trader).filter(Trader.id == ct.trader_id).first()
        orig_val = (ct.original_size or 0) * (ct.original_price or 0)
        copy_val = (ct.copy_size or 0) * (ct.copy_price or 0)
        ratio = (copy_val / orig_val * 100) if orig_val > 0 else 0

        print(f"=== ID {cid} ===")
        print(f"  executed_at:      {ct.executed_at}")
        print(f"  market:           {ct.market_title}")
        print(f"  outcome:          {ct.outcome}")
        print(f"  side:             {ct.original_side}")
        print(f"  status:           {ct.status}")
        print()
        print(f"  orig_shares:      {ct.original_size}")
        print(f"  orig_price:       ${ct.original_price}")
        print(f"  orig_value:       ${orig_val:.2f}")
        print()
        print(f"  copy_shares:      {ct.copy_size}")
        print(f"  copy_price:       ${ct.copy_price}")
        print(f"  copy_value:       ${copy_val:.2f}")
        print(f"  actual_ratio:     {ratio:.4f}%")
        print()
        print(f"  order_id:         {ct.order_id}")
        print(f"  pnl:              {ct.pnl}")
        print(f"  agg_fill_count:   {ct.agg_fill_count}")
        print(f"  agg_total_value:  {ct.agg_total_value}")

        if t:
            print()
            print(f"  --- Trader #{t.id} ({t.label}) ---")
            print(f"  sizing_mode:      {t.sizing_mode}")
            print(f"  proportional_pct: {t.proportional_pct}")
            print(f"  fixed_amount:     ${t.fixed_amount}")
            print(f"  buy_slippage:     {t.buy_slippage}%")
            print(f"  buy_order_type:   {getattr(t, 'buy_order_type', None)}")
            print(f"  max_per_trade:    ${t.max_per_trade}")
            print(f"  max_per_market:   ${t.max_per_market}")
            print(f"  max_per_yes_no:   ${getattr(t, 'max_per_yes_no', None)}")
            print(f"  ignore_under:     ${t.ignore_trades_under}")
            print(f"  min_price:        {t.min_price}")
            print(f"  max_price:        {t.max_price}")

            # Simulate the calculation
            print()
            print("  --- Expected Calculation ---")
            if t.sizing_mode == "proportional":
                expected_shares = ct.original_size * (t.proportional_pct / 100.0)
                expected_value = expected_shares * ct.original_price
                print(f"  expected_shares:  {ct.original_size} × {t.proportional_pct}/100 = {expected_shares:.4f}")
                print(f"  expected_value:   {expected_shares:.4f} × ${ct.original_price} = ${expected_value:.2f}")
            else:
                expected_shares = t.fixed_amount / ct.original_price if ct.original_price > 0 else 0
                print(f"  expected_shares:  ${t.fixed_amount} / ${ct.original_price} = {expected_shares:.4f}")
                print(f"  expected_value:   ${t.fixed_amount:.2f}")

            diff = copy_val - (expected_shares * ct.original_price if t.sizing_mode == "proportional" else t.fixed_amount)
            print(f"  diff:             ${diff:+.2f}")
            if abs(diff) > 0.02:
                print(f"  ⚠️  Significant deviation — check risk caps or price slippage")

        print()
