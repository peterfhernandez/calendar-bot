# Telegram Notification Debugging Guide

## Issue Summary

You were not receiving Telegram notifications when positions were closed overnight, even though the bot was operational (evidenced by `/positions` command responses).

## Root Causes Fixed

### 1. **Fire-and-Forget Without Error Tracking**
**Before:** Telegram messages were sent asynchronously without any way to know if they succeeded or failed.
```python
# OLD - no way to know if this actually sent
loop.create_task(self._post_telegram(...))
```

**After:** Added callback-based error tracking and retry logic.
```python
# NEW - tracks completion and logs errors
task = loop.create_task(self._post_telegram(...))
task.add_done_callback(lambda t: self._log_telegram_result(t, subject))
```

### 2. **No Automatic Retry on Network Errors**
**Before:** If Telegram API was temporarily unavailable, the message failed silently.

**After:** Now retries up to 2 times with 1-second delays between attempts.

### 3. **Missing Startup Verification**
**Before:** If TELEGRAM_TOKEN or TELEGRAM_CHAT was missing from `.env`, you wouldn't know until a position closed.

**After:** Bot now logs a prominent warning at startup:
```
⚠️  Telegram notifications DISABLED: TELEGRAM_TOKEN or TELEGRAM_CHAT not configured
```

### 4. **Weak Error Logging on Close Events**
**Before:** Notification failures only logged at WARNING level and didn't identify which position was affected.
```
WARNING Notification failed on close: ...
```

**After:** Now logs at ERROR level with clear identification:
```
ERROR ⚠️  NOTIFICATION FAILED on close of trade_id=42: Telegram API error: ...
INFO Notification queued for position close: type=close trade_id=42
```

## Verification Checklist

### 1. Check .env Configuration
```bash
# Verify .env exists and has Telegram credentials
cat .env | grep TELEGRAM

# You should see:
# TELEGRAM_TOKEN=123456789:ABCdefGHIjklmnoPQRstuvWXYZabcdefg
# TELEGRAM_CHAT=987654321
```

**If missing:** Create `.env` from `.env.example`:
```bash
cp .env.example .env
# Then edit .env and add your Telegram credentials
```

### 2. Get Telegram Credentials

**TELEGRAM_TOKEN:**
1. Open Telegram and search for `@BotFather`
2. Create a new bot or get existing bot token
3. Copy the token (looks like: `123456789:ABCdefGHIjklmnoPQRstuvWXYZabcdefg`)

**TELEGRAM_CHAT:**
1. Start a chat with your new bot
2. Send any message to it
3. Go to: `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates`
4. Replace `<YOUR_TOKEN>` with your actual token
5. Find the `"id"` field under `"chat"` (looks like: `987654321`)

### 3. Run Diagnostic Script
```bash
python3 scratch/diagnose_telegram_notifications.py
```

This checks:
- ✓ .env file exists
- ✓ Credentials are loaded in config
- ✓ Notifier can be instantiated
- ✓ Telegram API is reachable
- ✓ Test message can be sent
- ✓ Bot process status
- ✓ Database integrity

### 4. Start Bot with Credentials
```bash
# Make sure .env has TELEGRAM_TOKEN and TELEGRAM_CHAT
python bot.py

# You should see:
# INFO Telegram notifications enabled for chat 987654321
# 🤖 Bot started (paper mode)
```

### 5. Trigger a Test Notification
You have several options:

**Option A - Run test script:**
```bash
python3 scratch/scratch_notify_live.py
```

**Option B - Manually test via Telegram:**
1. `/positions` — should return current positions
2. If bot responds, Telegram is connected
3. Close a position (or let one close via stop/TP) and watch for notification

**Option C - Check logs:**
```bash
tail -50 logs/bot.log | grep -E "(notification|Telegram|telegram)"
```

Should see:
```
INFO Notification queued for position close: type=close trade_id=42
INFO Telegram message sent to chat 987654321 (subject: Position closed...)
```

Or errors like:
```
ERROR ⚠️  NOTIFICATION FAILED on close of trade_id=42: Telegram timeout (both attempts)
```

## Common Failure Modes and Solutions

### Failure: "Telegram API error 401: Unauthorized"
**Cause:** Invalid token
```
ERROR Telegram API error 401: {"ok":false,"error_code":401,"description":"Unauthorized"}
```
**Fix:** Get a new token from @BotFather and update `.env`

### Failure: "Telegram API error 400: Bad Request - chat not found"
**Cause:** Invalid chat ID or you've blocked the bot
```
ERROR Telegram API error 400: {"ok":false,"error_code":400,"description":"Bad Request: chat not found"}
```
**Fix:** 
1. Get correct chat ID from `https://api.telegram.org/bot<TOKEN>/getUpdates`
2. Unblock the bot if you've blocked it

### Failure: "Telegram HTTP 429: Too Many Requests"
**Cause:** Rate limited by Telegram (usually OK, auto-retries)
```
WARNING Telegram HTTP 429 (retrying): {"ok":false,"error_code":429,"parameters":{"retry_after":1}}
```
**Note:** The retry logic handles this; message should succeed on retry

### Failure: "⚠️  NOTIFICATION FAILED on close of trade_id=42"
**Causes:** Various - check the error message that follows
- "Telegram API timeout" → network is slow, will retry
- "Telegram API error: ..." → invalid token/chat, see above
- "Connection refused" → Telegram API unreachable (rare)

**Fix:** Check logs and verify credentials

### Failure: "Telegram notifications DISABLED: TELEGRAM_TOKEN or TELEGRAM_CHAT not configured"
**Cause:** Missing or empty values in `.env`
```
WARNING ⚠️  Telegram notifications DISABLED...
```
**Fix:** Update `.env` with valid credentials and restart bot

## Log Format

Successful notification flow in logs:
```
INFO Entry notification queued for trade_id=42
INFO Notification queued for position close: type=close trade_id=42
INFO Telegram message sent to chat 987654321 (subject: Position closed...)
```

Failed notification flow in logs:
```
INFO Entry notification queued for trade_id=42
ERROR ⚠️  NOTIFICATION FAILED on entry of trade_id=42: Telegram timeout (both attempts)
```

## Testing End-to-End

### Manual Test via Python REPL
```python
import asyncio
from alerts.notifier import Notifier
import config

notifier = Notifier()

# Test entry notification
notifier.notify_entry(
    trade_id=999,
    asset="BTC",
    option_type="Put",
    strike=60000.0,
    qty=1.0,
    net_debit=0.0100,
)

# Wait for task to complete
asyncio.sleep(2)

# Check logs for: "Telegram message sent to chat ..."
```

### Automated Test Script
```bash
python3 scratch/diagnose_telegram_notifications.py
```

## Performance Notes

- Notifications are **async and non-blocking** — they don't slow down position closes
- Retries have **1-second delays** between attempts (configurable)
- Timeout is **10 seconds** total per attempt (configurable)
- Messages up to **4096 characters** are supported by Telegram

## When to Suspect Telegram Issues

1. **Positions close but no notification arrives** → Check logs for ERROR messages
2. **Position closes take longer than usual** → Probably waiting for notification retry
3. **Bot startup log doesn't mention Telegram** → Credentials not configured
4. **`/positions` command works but alerts don't** → Outgoing vs. incoming channels differ

## Additional Debugging

### Check if Bot is Running
```bash
ps aux | grep "python.*bot.py"

# Or:
pgrep -f "python.*bot.py"
```

### Monitor Logs in Real-Time
```bash
tail -f logs/bot.log | grep -E "(notification|Telegram|telegram|ERROR)"
```

### Check Telegram API Status
```bash
python3 -c "
import asyncio
import aiohttp

async def test():
    async with aiohttp.ClientSession() as s:
        async with s.get('https://api.telegram.org/bot1/getMe') as r:
            print(f'Telegram API: {r.status}')

asyncio.run(test())
"
```

## Related Code Locations

- **Notifier class:** `alerts/notifier.py`
- **Telegram dispatch:** `alerts/notifier.py` lines 303-350
- **Entry notification call:** `strategy/decision.py` line 607-615
- **Close notification call:** `strategy/decision.py` line 901-915
- **Roll notification call:** `strategy/decision.py` line 1042-1048
- **Startup notification:** `bot.py` line 119
- **Startup warning:** `bot.py` line 126-129

## Summary

The improved notification system now:
✓ Logs every notification attempt clearly
✓ Retries automatically on transient failures
✓ Reports detailed error information
✓ Warns at startup if credentials are missing
✓ Tracks notification completion with callbacks
✓ Distinguishes between network and API errors

If you're still not receiving notifications:
1. Run `python3 scratch/diagnose_telegram_notifications.py`
2. Check `logs/bot.log` for ERROR and notification-related lines
3. Verify `.env` has both TELEGRAM_TOKEN and TELEGRAM_CHAT
4. Ensure bot is running: `pgrep -f "python.*bot.py"`
