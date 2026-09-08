# Roll Logic Debugging Guide

## Status

**Problem:** Roll logic has 0 successful rolls in validation phase. 5 positions failed to roll and were force-closed instead.

**Root causes identified:**
1. **Candidate search failures** (Trade #65): `Roll: no matching candidate` — scanner finds zero candidates for the position's strike/far-leg combination
2. **Gate rejections** (Trade #59, #60, #74, #76): `Roll: no acceptable roll candidate after gates` — candidates found but all rejected by liquidity, margin, or fee gates

**Investigation status:** Diagnostic tools added; ready for live debugging via enhanced logging.

---

## Diagnostic Tools

### 1. Enhanced Logging in `strategy/decision.py`

When `_try_roll()` is called:
- **If no candidates found:** Log shows:
  - Cache state: whether far_instrument is in chain
  - Chain size for the asset/strike
  - Example: `Roll: no matching candidate for trade_id=65 asset=BTC strike=88000 far=BTC-11SEP26-88000-C (far in cache: true, chain_size=142)`

- **If candidates found but all rejected:** Log shows:
  - Total candidates evaluated
  - Rejection reasons for each candidate
  - Example: `Roll: no acceptable roll candidate after gates for trade_id=59 (5 candidates evaluated, all rejected: [0] BTC-29AUG26-88000-C: liquidity — near-leg spread 12.5% > MAX_LEG_SPREAD_PCT 5% | [1] ...)`

### 2. Scanner Debug Output

When `scan()` filters in roll mode (lines 477-479 in `strategy/scanner.py`):
- Before/after count on far_instrument filter
- Example: `Roll mode: filtered 23 → 0 candidates after far_instrument filter (BTC-11SEP26-88000-C)`

This tells you whether candidates exist but don't match the exact far instrument, or if none exist at all.

### 3. Diagnostic Scripts

- **`scratch/scratch_roll_debug.py`**: Theoretical analysis of 5 failure scenarios
  - Simulates trade #65 failure conditions
  - Documents potential root causes with detailed explanations
  - Provides diagnostic checklist

- **`scratch/scratch_roll_diagnostic.py`**: Interactive diagnostics
  - Shows configuration for roll attempts (search space, gates)
  - Explains which gates apply and which are relaxed
  - Lists diagnostic checklist to run

---

## How to Debug Roll Failures

### Step 1: Identify When Roll Fails

Run the bot with `LOG_LEVEL=DEBUG` in `config.py`. Watch logs for:

```
Roll: no matching candidate for asset=BTC strike=88000.0 far=BTC-11SEP26-88000-C
  (far in cache: [true|false], chain_size=[N])
```

or

```
Roll: no acceptable roll candidate after gates for trade_id=59
  ([N] candidates evaluated, all rejected: ...)
```

### Step 2: Trace the Root Cause

#### Case A: No Candidates Found

Check the log fields:

1. **Is far_instrument in cache?**
   - If `false`: Feed subscription issue (even with Phase 18 fix)
     - Verify Phase 18 fix is active: check logs for "extra-position" or "open-position instrument"
     - Run `scratch/scratch_feed_open_position_coverage.py` to test subscription mechanism
   
   - If `true`: Candidate search issue
     - Check chain_size: do we have enough instruments at this strike?
     - Run scanner manually: `python -c "from data.chain_cache import ChainCache; from strategy.scanner import scan; ..."`

2. **Why are candidates missing?**
   - Check NEAR_DAYS_OPTIONS: does it include appropriate near-leg tenors?
   - Check FAR_DAYS_OPTIONS: are far-leg tenors still valid after position aged?
   - Check far_instrument filter: would any candidates have matched before filtering?

**Debug command:**

```bash
# Enable DEBUG logging, then run bot
LOG_LEVEL=DEBUG python bot.py 2>&1 | grep -E 'Roll:|Scan complete|filtered.*→'
```

#### Case B: All Candidates Rejected

Parse the rejection reasons:

```
[0] BTC-29AUG26-88000-C: liquidity — near-leg spread 12.5% > MAX_LEG_SPREAD_PCT 5%
[1] BTC-02SEP26-88000-C: gap_days=0 < MIN=1
[2] BTC-05SEP26-88000-C: margin — current margin utilization 87.2% > MAX 80%
```

For each type:

1. **Liquidity gate rejection**
   - Spread too wide: `(ask - bid) / mid > MAX_LEG_SPREAD_PCT`
   - Fix: Relax `MAX_LEG_SPREAD_PCT` for rolls
     - Create `ROLL_MAX_LEG_SPREAD_PCT = 0.20` (double the standard)
     - Update `_check_liquidity_gate()` to accept `is_roll` param
   
   - Entry premium too high: `net_debit > spread_mid * (1 + MAX_ENTRY_PREMIUM)`
   - Fix: Relax `MAX_ENTRY_PREMIUM` for rolls
     - Create `ROLL_MAX_ENTRY_PREMIUM = 0.20` (double the standard)

2. **Near/far gap rejection**
   - Gap too small: `gap_days < MIN_ROLL_NEAR_FAR_GAP_DAYS`
   - Cause: Far leg has aged, valid gap is shrinking
   - Fix: Relax `MIN_ROLL_NEAR_FAR_GAP_DAYS` for late-stage rolls
     - Create `ROLL_MIN_NEAR_FAR_GAP_DAYS = 0` (allow zero-width spreads as last resort)
     - Risk: zero-width spreads have zero theta and no P&L, but keep position open until expiry

3. **Margin gate rejection**
   - Current utilization too high: account can't absorb roll margin impact
   - Fix: This is a real constraint — can't override
   - Workaround: Close smaller positions first to free margin for rolls

4. **Fee gate rejection**
   - Roll fees too high: `roll_cost > theta_gain`
   - Cause: Close the old near, open new near; tight spreads make fees expensive
   - Fix: Accept higher fee gates for rolls
     - Create `ROLL_FEE_GATE_MULTIPLIER = 0.5` (allow rolls when fees = 50% of theta gain instead of 100%)

---

## Recommended Fixes

### Priority 1: Relaxed Roll-Specific Gates

Create configuration for roll-specific thresholds:

```python
# In config.py

# Standard entry gates
MAX_LEG_SPREAD_PCT = 0.05
MAX_ENTRY_PREMIUM = 0.10

# Relaxed roll gates (rolls are continuation of risk, not new risk)
ROLL_MAX_LEG_SPREAD_PCT = 0.20  # 4x looser than entry
ROLL_MAX_ENTRY_PREMIUM = 0.20   # 2x looser than entry
ROLL_MIN_NEAR_FAR_GAP_DAYS = 0  # Allow zero-width as last resort
ROLL_FEE_GATE_COST_MULTIPLIER = 0.5  # Allow when fees = 50% of theta gain
```

Update `_check_liquidity_gate()` and `_try_roll()` to use these.

### Priority 2: Better Error Diagnostics

Add to `_try_roll()` before returning False:

```python
# Log why all candidates were rejected, with rejection categories
rejection_categories = {}
for reason in rejection_reasons:
    category = reason.split(":")[1].strip().split(" ")[0]  # "liquidity", "margin", "gap_days"
    rejection_categories[category] = rejection_categories.get(category, 0) + 1

logger.warning(
    "Roll blocked: %d candidates, rejections by category: %s. "
    "Consider relaxing ROLL_* config thresholds.",
    len(candidates), rejection_categories
)
```

### Priority 3: Verify Feed Subscription

Run `scratch/scratch_feed_open_position_coverage.py` to confirm Phase 18 fix is working:

```bash
python -m scratch.scratch_feed_open_position_coverage
```

Expected output:
```
Far leg subscribed: True  [PASS]
Far leg re-subscribed after reconnect: True  [PASS]
Far leg dropped after close: True  [PASS]
```

If any fails, Phase 18 fix isn't working correctly.

---

## Next Steps

1. **Run bot with enhanced logging**
   ```bash
   LOG_LEVEL=DEBUG TRADING_MODE=paper python bot.py 2>&1 | tee logs/roll_debug.log
   ```

2. **Collect failure examples**
   - Save 3-5 roll failure logs showing full context (candidate count, rejection reasons)

3. **Analyze patterns**
   - Do failures cluster around specific strikes, assets, or gate types?
   - Is it always "no candidates" or mostly "all rejected"?

4. **Implement Priority 1 fix**
   - Add ROLL_* config thresholds
   - Update gates to use them
   - Re-test with same positions

5. **Verify Phase 18**
   - Run diagnostic script
   - Check if far_instruments are actually subscribed

6. **Measure improvement**
   - Count successful rolls after fixes
   - Track roll P&L vs forced closes
   - Confirm positions roll successfully to completion

---

## Testing

Once fixes are in place, validate with:

1. **Unit tests**: Add to `tests/test_decision.py`
   - Test roll with relaxed gate thresholds
   - Test roll rejection logging

2. **Integration tests**: Run `scratch/scratch_decision.py`
   - Simulate a position needing to roll
   - Verify roll succeeds with relaxed thresholds

3. **Paper trading**: Monitor next 2-4 weeks
   - Confirm at least 3-5 successful rolls
   - No force-closes at retry cap
   - Roll P&L reasonable

---

## Phase 18 Fix Reference

**What it does:** Ensures that open position instruments are subscribed to the WebSocket feed even if their expiry days fall outside the configured `NEAR_DAYS_OPTIONS`/`FAR_DAYS_OPTIONS` window.

**Implementation:**
- `DeribitFeed.__init__()` accepts `extra_instruments` callable
- `_subscribe_all()` calls provider on every connect/reconnect
- `_open_position_extras()` fetches open position names from DB and subscribes them

**Verification:**
- Check logs for: "Subscribing to X open-position instrument(s) outside the day window"
- Run `scratch/scratch_feed_open_position_coverage.py`
- Confirm far legs don't go "No IV for trade N" after reconnect

---

## Questions?

If roll logic remains broken after these fixes:
1. Post the rejection_reasons summary from logs
2. Include which gate is rejecting (liquidity, margin, gap, fee)
3. Provide far_instrument and near candidates found
4. Include config values for relevant MAX_* thresholds

This information will help pinpoint whether the issue is:
- Data (far instrument not in cache)
- Candidate search (no near legs found)
- Gates (thresholds too strict)
- Fee calculation (fees too high)
