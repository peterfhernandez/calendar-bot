# strategy/scratch_decision.py — run from repo root: python -m strategy.scratch_decision
"""
End-to-end dry-run of the DecisionEngine against the Deribit paper feed.

No orders are placed. The DryRunExecutor logs what would happen and the
engine prints a full status report after each tick.

Steps:
  1. Connect to Deribit paper WebSocket and collect chain data for 15 s
  2. Run scan_tick()  — finds candidates, "enters" the best one (dry-run)
  3. Run monitor_tick() — evaluates any open positions in the local DB
  4. Print a summary of engine state, daily P&L, and open positions
"""

import asyncio
import logging
import sys
from pathlib import Path

# Pretty output without noise from lower-level modules
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(name)s  %(message)s",
    datefmt="%H:%M:%S",
)
# Silence the websockets and feed debug noise
logging.getLogger("websockets").setLevel(logging.WARNING)
logging.getLogger("data.deribit_feed").setLevel(logging.WARNING)

from data.chain_cache import ChainCache
from data.deribit_feed import DeribitFeed
from db.state import DB_PATH, init_db, load_calendar_state
from strategy.decision import BotState, DecisionEngine, DryRunExecutor

import config

# Use a separate scratch DB so we don't pollute the real one
_SCRATCH_DB = Path(__file__).parent.parent / "db" / "scratch_decision.db"

_SEP  = "=" * 72
_SEP2 = "-" * 72


def _print_status(label: str, status) -> None:
    print(f"\n{_SEP}")
    print(f"  {label}")
    print(_SEP2)
    print(f"  State        : {status.state.value}")
    print(f"  Open pos     : {status.open_positions}")
    print(f"  Daily P&L    : ${status.daily_pnl:+.4f}")
    print(f"  Message      : {status.message}")
    print(_SEP)


def _print_open_positions() -> None:
    print(f"\n  Open positions in scratch DB ({_SCRATCH_DB.name}):")
    any_open = False
    for asset in config.ASSETS:
        state = load_calendar_state(asset, db_path=_SCRATCH_DB)
        if state["open"]:
            any_open = True
            pos = state["open"]
            print(
                f"    [{pos['trade_id']}] {pos['asset']} {pos.get('option_type','')} "
                f"K={pos['strike']:,.0f}  near={pos['expiry_near']}  far={pos['expiry_far']}  "
                f"qty={pos['qty']}  debit={pos['net_debit']:.4f}"
            )
    if not any_open:
        print("    (none)")


async def main() -> None:
    init_db(_SCRATCH_DB)

    cache = ChainCache(ttl=60)

    async def on_ticker(snap):
        cache.update(snap)

    feed = DeribitFeed(assets=config.ASSETS, paper=True, on_ticker=on_ticker)

    print(f"\n{_SEP}")
    print("  Calendar Spread Bot — Decision Engine Dry-Run")
    print(f"  DB : {_SCRATCH_DB}")
    print(_SEP)
    print("\nConnecting to Deribit paper feed — waiting 15 s for chain data...")

    feed_task = asyncio.create_task(feed.start())
    await asyncio.sleep(15)

    # Check we actually received data
    spot_btc = cache.get_spot("BTC")
    chain_btc = cache.get_chain("BTC")
    print(f"\nCache snapshot: BTC spot={spot_btc}  chain instruments={len(chain_btc)}")
    if not chain_btc:
        print("\n[WARN] No chain data received — check your network / API credentials.")

    # ── Wiring ────────────────────────────────────────────────────────────────
    executor = DryRunExecutor()
    engine = DecisionEngine(
        cache=cache,
        portfolio_value=10_000.0,
        executor=executor,
        db_path=_SCRATCH_DB,
        daily_loss_limit=config.DAILY_LOSS_LIMIT,
    )

    # ── Scan tick ─────────────────────────────────────────────────────────────
    print(f"\n{'─'*72}")
    print("  Running scan_tick() ...")
    print(f"{'─'*72}")
    scan_status = engine.scan_tick()
    _print_status("After scan_tick()", scan_status)

    if scan_status.state is BotState.HALTED:
        print("\n[HALT] Engine is halted. Reset scratch DB to continue.")
        feed_task.cancel()
        return

    # ── Monitor tick ──────────────────────────────────────────────────────────
    print(f"\n{'─'*72}")
    print("  Running monitor_tick() ...")
    print(f"{'─'*72}")
    mon_status = engine.monitor_tick()
    _print_status("After monitor_tick()", mon_status)

    # ── DB summary ────────────────────────────────────────────────────────────
    _print_open_positions()

    # ── Advice ───────────────────────────────────────────────────────────────
    print()
    if scan_status.open_positions == 0 and not any(
        load_calendar_state(a, db_path=_SCRATCH_DB)["open"] for a in config.ASSETS
    ):
        print("  Tip: no positions entered. Try relaxing filters in config.py")
        print("       (MIN_IV_CONTANGO, MIN_POP, MIN_OI_NEAR/FAR) or wait for")
        print("       better market conditions.")
    else:
        print("  Dry-run complete. Re-run to simulate additional ticks.")
        print(f"  To reset: delete {_SCRATCH_DB.name} in the db/ folder.")
    print()

    feed_task.cancel()


asyncio.run(main())
