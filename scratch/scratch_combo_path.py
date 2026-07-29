"""
scratch/scratch_combo_path.py
==============================
Offline demonstration of the Phase 27c combo-order fixes.

Symptom (2026-07-27 test-mode run): every entry logged "Falling back to
individual legs" roughly **two seconds** after RANK approval — far too fast for
``COMBO_FILL_TIMEOUT_SEC = 30`` — so the combo was not timing out, it was
failing outright.  Both failure branches logged at DEBUG, and the root
``LOG_LEVEL`` is WARNING, so nothing said why.  BOT_PLAN §5 designates the
individual-leg path a last resort; it had silently become the only path.

Two defects, both confirmed against the live exchange via ``public/get_combos``
(a real combo: ``{"id": "BTC-CCAL-25DEC26_31JUL26-115000", "legs":
[{"instrument_name": "BTC-31JUL26-115000-C", "amount": -1}, ...]}``):

1. **The combo identifier was read from the wrong field.**  Deribit returns it
   as ``id``; the executor looked for ``combo_id`` / ``instrument_name`` and
   found neither, so every *successful* creation was discarded as "no combo_id".

2. **The sized quantity was sent as the leg ratio.**  A combo's legs define its
   ratio in small integers, not the trade size — the size belongs on the order
   placed against the combo instrument.  A fractional ratio (BTC ``0.2``) is not
   a valid combo definition.

Also demonstrated: failures now log at WARNING naming the reason, and a filled
combo whose order state carries no per-leg breakdown (the normal Deribit shape)
attributes leg prices so ``far - near`` equals the net actually filled, instead
of the old "far = twice the net debit" placeholder.

No network, no live orders — runs against an in-memory async mock.

Run from the repo root:
    python -m scratch.scratch_combo_path

Aborts if TRADING_MODE == "live".
"""

import asyncio
import logging
import sys

import config

if getattr(config, "TRADING_MODE", "paper") == "live":
    sys.exit("ERROR: scratch scripts must not run in live mode.")

from execution.executor import (
    _async_enter_spread_combo,
    _combo_id_from_result,
    _combo_leg_fills,
    _combo_ratio_legs,
    _index_price,
)
from execution.order_manager import OrderManager
from strategy.scanner import CalendarCandidate

PASS, FAIL = "PASS", "FAIL"
results: list[tuple[str, str]] = []


def check(label: str, condition: bool) -> None:
    results.append((label, PASS if condition else FAIL))
    print(f"  {'✓' if condition else '✗'}  {label}")


# A real create_combo response, as returned by Deribit (shape verified against
# public/get_combos on both test.deribit.com and www.deribit.com).
REAL_COMBO_RESPONSE = {
    "id": "BTC-CCAL-28AUG26_31JUL26-64000",
    "state": "active",
    "instrument_id": 667427,
    "creation_timestamp": 1785320578000,
    "legs": [
        {"instrument_name": "BTC-31JUL26-64000-P", "amount": -1},
        {"instrument_name": "BTC-28AUG26-64000-P", "amount": 1},
    ],
}


def _candidate() -> CalendarCandidate:
    return CalendarCandidate(
        asset="BTC", strike=64_000.0, option_type="Put", spot=64_000.0,
        near_instrument="BTC-31JUL26-64000-P",
        far_instrument="BTC-28AUG26-64000-P",
        near_days=7, far_days=30,
        near_iv=0.55, far_iv=0.50, iv_contango=0.05,
        near_ask=220.0, near_bid=200.0,
        far_ask=460.0,  far_bid=440.0,
        net_debit=260.0,
        near_oi=500, far_oi=500,
        pop=0.55, be_lo=60_000.0, be_hi=68_000.0, ev_score=0.15,
        qty=0.2,
    )


class _ComboClient:
    """Minimal async stand-in for _DeribitRPCClient."""

    def __init__(self, create_result, order_state=None):
        self._create_result = create_result
        self._order_state = order_state or {
            "order_id": "combo-1", "order_state": "filled",
            "amount": 0.2, "filled_amount": 0.2, "average_price": 0.00406,
        }
        self.combo_legs = None
        self.placed = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        pass

    async def create_combo(self, legs):
        self.combo_legs = legs
        if isinstance(self._create_result, Exception):
            raise self._create_result
        return self._create_result

    async def place_order(self, instrument, direction, amount, price, label="", validate_amount=True):
        self.placed.append({"instrument": instrument, "amount": amount, "price": price})
        return {"order": {"order_id": "combo-1", "order_state": "open", "price": price}}

    async def get_order_state(self, order_id):
        return self._order_state

    async def cancel_order(self, order_id):
        return {"order_id": order_id, "order_state": "cancelled"}


async def _enter(client, reasons=None, amount=0.2):
    import execution.executor as executor_mod

    original = executor_mod._DeribitRPCClient
    executor_mod._DeribitRPCClient = lambda *a, **k: client
    try:
        return await _async_enter_spread_combo(
            candidate=_candidate(),
            client_id="", client_secret="",
            order_manager=OrderManager(),
            amount=amount,
            net_debit_limit_index=0.004,
            combo_timeout=5,
            reason_sink=reasons,
        )
    finally:
        executor_mod._DeribitRPCClient = original


def demo_identifier_field() -> None:
    print("\n1. The combo identifier field")
    old = REAL_COMBO_RESPONSE.get("combo_id") or REAL_COMBO_RESPONSE.get("instrument_name")
    new = _combo_id_from_result(REAL_COMBO_RESPONSE)
    print(f"     old lookup (combo_id / instrument_name) → {old!r}")
    print(f"     new lookup (id)                         → {new!r}")
    check("old lookup found nothing on a real Deribit response", old is None)
    check("new lookup resolves the combo instrument name",
          new == "BTC-CCAL-28AUG26_31JUL26-64000")


def demo_leg_ratio() -> None:
    print("\n2. Combo legs are a ratio, not a size")
    legs = _combo_ratio_legs("BTC-31JUL26-64000-P", "BTC-28AUG26-64000-P")
    for leg in legs:
        print(f"     {leg['direction']:>4}  {leg['instrument_name']:<26} amount={leg['amount']}")
    check("legs are a 1:1 integer ratio", [l["amount"] for l in legs] == [1, 1])
    check("near is sold and far is bought",
          [l["direction"] for l in legs] == ["sell", "buy"])


async def demo_end_to_end_fill() -> None:
    print("\n3. End-to-end: the combo path now completes")
    client = _ComboClient(REAL_COMBO_RESPONSE)
    fill = await _enter(client)
    check("combo entry returns a fill (previously always None)", fill is not None)
    if fill:
        check("fill is flagged via_combo", fill["via_combo"] is True)
        check("order was placed against the combo instrument",
              client.placed[0]["instrument"] == "BTC-CCAL-28AUG26_31JUL26-64000")
        check("order carries the sized qty, legs carry the ratio",
              client.placed[0]["amount"] == 0.2 and client.combo_legs[0]["amount"] == 1)
        print(f"     near=${fill['near_prem']:.2f}  far=${fill['far_prem']:.2f}  "
              f"net_debit=${fill['net_debit']:.2f}")


async def demo_failure_is_visible(caplog_records: list[logging.LogRecord]) -> None:
    print("\n4. A failing combo now says why, at WARNING")
    reasons: list[str] = []
    client = _ComboClient(RuntimeError("Deribit error -32602: Invalid params"))
    fill = await _enter(client, reasons=reasons)
    check("a raising create_combo still falls back", fill is None)
    check("the reason is captured for the caller's fallback warning",
          bool(reasons) and "-32602" in reasons[0])
    warnings = [r for r in caplog_records
                if r.levelno >= logging.WARNING and "COMBO:" in r.getMessage()]
    check("the reason is logged at WARNING (was DEBUG, hence invisible)", bool(warnings))
    for record in warnings:
        print(f"     {record.getMessage()}")


def demo_leg_fill_attribution() -> None:
    print("\n5. Leg prices when Deribit reports only the net fill")
    cand = _candidate()
    near, far = _combo_leg_fills(
        {"order_state": "filled", "average_price": 0.00406}, cand, 0.004
    )
    old_near, old_far = 0.004, 0.008          # previous placeholder behaviour
    print(f"     old placeholder → near={old_near:.5f}  far={old_far:.5f}  "
          f"net={old_far - old_near:.5f}")
    print(f"     new attribution → near={near:.5f}  far={far:.5f}  net={far - near:.5f}")
    check("net debit equals the price actually filled",
          abs((far - near) - 0.00406) < 1e-9)
    check("near leg keeps its intended limit price",
          abs(near - _index_price(cand.near_bid, cand.spot, cand.asset)) < 1e-9)


class _Collector(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record):
        self.records.append(record)


async def main() -> None:
    print("=" * 72)
    print("  Phase 27c — combo order path (offline demo)")
    print("=" * 72)

    collector = _Collector()
    exec_logger = logging.getLogger("execution.executor")
    exec_logger.addHandler(collector)
    exec_logger.setLevel(logging.DEBUG)

    demo_identifier_field()
    demo_leg_ratio()
    await demo_end_to_end_fill()
    await demo_failure_is_visible(collector.records)
    demo_leg_fill_attribution()

    exec_logger.removeHandler(collector)

    print("\n" + "=" * 72)
    failed = [lbl for lbl, r in results if r == FAIL]
    if failed:
        print(f"  {len(failed)} CHECK(S) FAILED:")
        for lbl in failed:
            print(f"    ✗ {lbl}")
        sys.exit(1)
    print(f"  ALL {len(results)} CHECKS PASSED")
    print("=" * 72)


if __name__ == "__main__":
    asyncio.run(main())
