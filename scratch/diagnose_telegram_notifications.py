#!/usr/bin/env python3
"""
diagnose_telegram_notifications.py
===================================
Diagnostic script to identify why Telegram notifications may not be sending.

Checks:
1. .env file exists and has TELEGRAM_TOKEN and TELEGRAM_CHAT
2. config.TELEGRAM_TOKEN and config.TELEGRAM_CHAT are loaded
3. Notifier can be instantiated
4. Test alert can be sent to Telegram API
5. Bot is/was running and has sent notifications
6. Database contains notification events
"""

import asyncio
import sys
from pathlib import Path

def main():
    print("=" * 80)
    print("TELEGRAM NOTIFICATION DIAGNOSTIC")
    print("=" * 80)

    # Check 1: .env file exists
    print("\n[1] Checking .env file...")
    env_path = Path(".env")
    if env_path.exists():
        print(f"    ✓ .env file exists at {env_path.absolute()}")
        with open(env_path) as f:
            content = f.read()
            has_token = "TELEGRAM_TOKEN=" in content
            has_chat = "TELEGRAM_CHAT=" in content
            print(f"    - TELEGRAM_TOKEN present: {has_token}")
            print(f"    - TELEGRAM_CHAT present: {has_chat}")

            # Show redacted values
            for line in content.split('\n'):
                if line.startswith('TELEGRAM_TOKEN='):
                    token = line.split('=', 1)[1].strip().strip('"').strip("'")
                    if token:
                        print(f"    - TELEGRAM_TOKEN value: {token[:10]}...{token[-5:]}")
                    else:
                        print(f"    - TELEGRAM_TOKEN value: (empty or missing)")
                elif line.startswith('TELEGRAM_CHAT='):
                    chat = line.split('=', 1)[1].strip().strip('"').strip("'")
                    if chat:
                        print(f"    - TELEGRAM_CHAT value: {chat}")
                    else:
                        print(f"    - TELEGRAM_CHAT value: (empty or missing)")
    else:
        print(f"    ✗ .env file NOT FOUND at {env_path.absolute()}")
        print(f"    ! Copy .env.example to .env and fill in credentials")
        return False

    # Check 2: Config loads Telegram credentials
    print("\n[2] Checking config.py Telegram settings...")
    try:
        import config
        token = getattr(config, 'TELEGRAM_TOKEN', None)
        chat = getattr(config, 'TELEGRAM_CHAT', None)

        print(f"    - config.TELEGRAM_TOKEN: {repr(token)[:50]}")
        print(f"    - config.TELEGRAM_CHAT: {chat}")

        if not token or not chat:
            print(f"    ✗ TELEGRAM_TOKEN or TELEGRAM_CHAT not properly configured!")
            return False
        print(f"    ✓ Both credentials are configured")
    except Exception as e:
        print(f"    ✗ Error loading config: {e}")
        return False

    # Check 3: Notifier instantiation
    print("\n[3] Checking Notifier class...")
    try:
        from alerts.notifier import Notifier
        notifier = Notifier()
        print(f"    ✓ Notifier instantiated successfully")
    except Exception as e:
        print(f"    ✗ Error instantiating Notifier: {e}")
        return False

    # Check 4: Test Telegram API connectivity
    print("\n[4] Testing Telegram API connectivity...")
    try:
        async def test_telegram():
            import aiohttp
            url = f"https://api.telegram.org/bot{token}/getMe"
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    data = await resp.json()
                    if resp.status == 200:
                        print(f"    ✓ Telegram API reachable")
                        print(f"    - Bot username: @{data.get('result', {}).get('username', 'unknown')}")
                        return True
                    else:
                        print(f"    ✗ Telegram API error {resp.status}: {data}")
                        return False

        result = asyncio.run(test_telegram())
        if not result:
            return False
    except Exception as e:
        print(f"    ✗ Error testing Telegram API: {e}")
        return False

    # Check 5: Test sending an actual message
    print("\n[5] Testing Telegram message send...")
    try:
        async def send_test_message():
            import aiohttp
            url = f"https://api.telegram.org/bot{token}/sendMessage"
            payload = {
                "chat_id": chat,
                "text": "🤖 Telegram notification diagnostic test - if you see this, notifications are working!"
            }
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if data.get('ok'):
                            print(f"    ✓ Test message sent successfully!")
                            print(f"    - Message ID: {data.get('result', {}).get('message_id')}")
                            return True
                        else:
                            print(f"    ✗ Telegram rejected message: {data.get('description')}")
                            return False
                    else:
                        body = await resp.text()
                        print(f"    ✗ HTTP {resp.status}: {body}")
                        return False

        result = asyncio.run(send_test_message())
        if not result:
            print(f"\n    ! Check:")
            print(f"      - TELEGRAM_TOKEN is valid (not expired or revoked)")
            print(f"      - TELEGRAM_CHAT is correct (you may have blocked the bot)")
            print(f"      - Chat ID is a number, not a string")
            return False
    except Exception as e:
        print(f"    ✗ Error sending test message: {e}")
        return False

    # Check 6: Database and past notifications
    print("\n[6] Checking database for past events...")
    db_path = Path("db/calendar_bot.db")
    if not db_path.exists():
        print(f"    ! Database not found at {db_path}")
        print(f"    ! Bot may not have run yet, or database is at a different path")
    else:
        try:
            import sqlite3
            conn = sqlite3.connect(str(db_path))
            cursor = conn.cursor()

            # Check if calendar_trades table exists
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='calendar_trades'")
            if cursor.fetchone():
                cursor.execute("SELECT COUNT(*) FROM calendar_trades WHERE date_closed IS NOT NULL")
                closed_count = cursor.fetchone()[0]
                cursor.execute("SELECT COUNT(*) FROM calendar_trades")
                total_count = cursor.fetchone()[0]
                print(f"    ✓ Database found")
                print(f"    - Total trades: {total_count}")
                print(f"    - Closed trades: {closed_count}")

                if closed_count > 0:
                    print(f"    → {closed_count} trades were closed (should have triggered notifications)")
            else:
                print(f"    ! calendar_trades table not found in database")

            conn.close()
        except Exception as e:
            print(f"    ✗ Error reading database: {e}")

    # Check 7: Bot process status
    print("\n[7] Checking bot process status...")
    import subprocess
    try:
        result = subprocess.run(
            ["pgrep", "-f", "python.*bot.py"],
            capture_output=True,
            text=True,
            timeout=5
        )
        if result.stdout.strip():
            pids = result.stdout.strip().split('\n')
            print(f"    ✓ Bot is running (PID(s): {', '.join(pids)})")
        else:
            print(f"    ✗ Bot is NOT running")
            print(f"    ! Notifications are only sent when bot is running")
            print(f"    ! To start the bot: python bot.py")
    except Exception as e:
        print(f"    ! Could not check process status: {e}")

    # Check 8: Logs directory
    print("\n[8] Checking logs...")
    logs_path = Path("logs")
    if logs_path.exists():
        log_files = list(logs_path.glob("bot.log*"))
        if log_files:
            print(f"    ✓ Logs found ({len(log_files)} file(s))")
            latest = max(log_files, key=lambda p: p.stat().st_mtime)
            print(f"    - Latest: {latest.name} (modified {latest.stat().st_mtime})")
        else:
            print(f"    ! Logs directory exists but no bot.log files")
    else:
        print(f"    ! Logs directory not found at {logs_path}")

    print("\n" + "=" * 80)
    print("DIAGNOSTIC COMPLETE")
    print("=" * 80)
    return True

if __name__ == "__main__":
    try:
        success = main()
        sys.exit(0 if success else 1)
    except KeyboardInterrupt:
        print("\n\nDiagnostic interrupted by user")
        sys.exit(1)
    except Exception as e:
        print(f"\n\nFatal error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
