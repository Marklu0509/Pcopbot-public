"""Check actual token_id format in DB for missing markets."""
import sys
sys.path.insert(0, "/app")

from db.database import get_session_factory, init_db
from db.models import CopyTrade

init_db()
s = get_session_factory()()

markets = [
    "FC Barcelona vs. Newcastle",
    "Sunderland",
    "FC Barcelona win",
    "Bayer 04 Leverkusen",
    "FC Cincinnati",
    "SS Lazio",
    "Stade Rennais",
    "Jazz vs. Kings",
    "Newcastle United FC win on 2026-03-18",
]

for mkt in markets:
    rows = (
        s.query(CopyTrade.original_token_id, CopyTrade.original_side, CopyTrade.market_title)
        .filter(CopyTrade.market_title.like("%" + mkt + "%"))
        .limit(3)
        .all()
    )
    if rows:
        for r in rows:
            tid = r[0] or "NULL"
            print(r[1] + " tid=" + tid + " market=" + (r[2] or "")[:50])
    else:
        print("NOT FOUND: " + mkt)
    print()

s.close()
