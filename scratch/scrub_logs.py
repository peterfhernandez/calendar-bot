"""
scratch/scrub_logs.py
=====================
One-time log scrubber — rewrites all bot.log* rotation files in place,
replacing any occurrence of a known secret with <redacted>.

Reads secrets directly from the .env file (same key names as config.py)
so it can be run as a standalone script without importing the bot modules.

Usage
-----
    # Preview what would be replaced (no files changed):
    python -m scratch.scrub_logs --dry-run

    # Apply redactions in place:
    python -m scratch.scrub_logs

The script reports how many substitutions were made per file.  If a file
contains no secrets it is left untouched.  Files that do not exist are
silently skipped.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path


# ── Secret loading ─────────────────────────────────────────────────────────────

def _load_dotenv(env_path: Path) -> dict[str, str]:
    """Parse a .env file and return key→value pairs (no shell expansion)."""
    result: dict[str, str] = {}
    if not env_path.exists():
        return result
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key   = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            result[key] = value
    return result


_SECRET_KEYS = [
    "DERIBIT_TEST_CLIENT_ID",
    "DERIBIT_TEST_CLIENT_SECRET",
    "DERIBIT_LIVE_CLIENT_ID",
    "DERIBIT_LIVE_CLIENT_SECRET",
    "SMTP_USER",
    "SMTP_PASS",
    "TELEGRAM_TOKEN",
    "TELEGRAM_CHAT",
]


def _collect_secrets(env: dict[str, str]) -> list[str]:
    """Return non-empty secret values from the parsed .env dict."""
    secrets: list[str] = []
    for key in _SECRET_KEYS:
        # Also check OS environment so the script works when vars are exported
        value = env.get(key) or os.environ.get(key, "")
        if value and value.strip():
            secrets.append(value.strip())
    return secrets


# ── Scrubbing ──────────────────────────────────────────────────────────────────

def _scrub_text(text: str, secrets: list[str]) -> tuple[str, int]:
    """Replace all occurrences of each secret with <redacted>.

    Returns (scrubbed_text, total_replacement_count).
    """
    total = 0
    for secret in secrets:
        escaped = re.escape(secret)
        scrubbed, count = re.subn(escaped, "<redacted>", text)
        text   = scrubbed
        total += count
    return text, total


def _scrub_file(path: Path, secrets: list[str], dry_run: bool) -> int:
    """Scrub one log file.  Returns the number of replacements made (or found)."""
    try:
        original = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        print(f"  SKIP  {path.name}: {exc}", file=sys.stderr)
        return 0

    scrubbed, count = _scrub_text(original, secrets)

    if count == 0:
        print(f"  OK    {path.name}: no secrets found")
        return 0

    if dry_run:
        print(f"  DRY   {path.name}: {count} replacement(s) would be made")
    else:
        path.write_text(scrubbed, encoding="utf-8")
        print(f"  FIXED {path.name}: {count} replacement(s) applied")

    return count


# ── Entry point ────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Scrub secrets from bot.log* rotation files."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be replaced without writing any files.",
    )
    parser.add_argument(
        "--log-dir",
        default="logs",
        help="Directory containing bot.log* files (default: logs/).",
    )
    parser.add_argument(
        "--env-file",
        default=".env",
        help="Path to the .env file (default: .env in cwd).",
    )
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parent.parent
    env_path  = repo_root / args.env_file
    log_dir   = repo_root / args.log_dir

    # Load secrets
    env     = _load_dotenv(env_path)
    secrets = _collect_secrets(env)

    if not secrets:
        print(
            "No secrets found in .env or environment — nothing to redact.\n"
            f"Looked in: {env_path}"
        )
        sys.exit(0)

    print(f"Loaded {len(secrets)} secret(s) from {env_path}")

    # Find log files
    if not log_dir.exists():
        print(f"Log directory does not exist: {log_dir}")
        sys.exit(0)

    log_files = sorted(log_dir.glob("bot.log*"))
    if not log_files:
        print(f"No bot.log* files found in {log_dir}")
        sys.exit(0)

    print(f"Scanning {len(log_files)} log file(s) in {log_dir}/")
    if args.dry_run:
        print("(DRY RUN — no files will be modified)\n")
    else:
        print()

    total_replacements = 0
    for log_file in log_files:
        total_replacements += _scrub_file(log_file, secrets, dry_run=args.dry_run)

    print()
    if args.dry_run:
        print(f"Total: {total_replacements} replacement(s) would be made across all files.")
    else:
        print(f"Total: {total_replacements} replacement(s) applied across all files.")

    if total_replacements > 0 and not args.dry_run:
        print(
            "\nNote: rotation files (bot.log.1 … bot.log.5) have also been scrubbed.\n"
            "If log rotation has already compressed older files (.gz), those are not\n"
            "handled by this script — delete them manually if they may contain secrets."
        )


if __name__ == "__main__":
    main()
