# strategy/scratch_scan.py — run from repo root: python -m strategy.scratch_scan
import asyncio, logging
logging.basicConfig(level=logging.INFO)

from data.chain_cache import ChainCache
from data.deribit_feed import DeribitFeed
from strategy.scanner import scan
from strategy.sizer import size_candidate

async def main():
    cache = ChainCache(ttl=60)

    async def on_ticker(snap):
        cache.update(snap)

    feed = DeribitFeed(assets=["BTC", "ETH"], paper=True, on_ticker=on_ticker)

    print("Connecting and waiting 15s for chain data...")
    task = asyncio.create_task(feed.start())
    await asyncio.sleep(15)

    # Deribit minimum contract size per asset (in units of the underlying)
    MIN_CONTRACT = {"BTC": 0.1, "ETH": 0.1}
    DEFAULT_MIN  = 0.1

    candidates = scan(cache, min_pop=0.01)   # relax filters for visibility
    print(f"\n{'='*72}")
    print(f"  {len(candidates)} candidates found")
    print(f"{'='*72}")
    for c in candidates[:10]:
        result   = size_candidate(c, portfolio_value=10_000, open_positions=[])
        ev_pct   = (c.ev_score / c.net_debit * 100) if c.net_debit > 0 else 0.0
        min_unit = MIN_CONTRACT.get(c.asset, DEFAULT_MIN)
        min_cost = c.net_debit * min_unit
        print(
            f"  {c.asset} {c.option_type:4} K={c.strike:>8,.0f}  "
            f"{c.near_days:2}d/{c.far_days:2}d  "
            f"contango={c.iv_contango*100:+.1f}%  "
            f"PoP={c.pop*100:.0f}%  "
            f"EV={ev_pct:+.1f}%/debit  "
            f"debit=${c.net_debit:.2f}  "
            f"min={min_unit}{c.asset}(${min_cost:.2f})  "
            f"qty={result.qty}"
        )
    task.cancel()

asyncio.run(main())