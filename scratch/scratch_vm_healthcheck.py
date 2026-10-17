"""
scratch/scratch_vm_healthcheck.py
=================================
Post-migration health check for the Linux VM deployment (deploy/linux/).

Run on the VM from the repo root after deploy/linux/install.sh:
    .venv/bin/python -m scratch.scratch_vm_healthcheck

For each bot (paper, test) it reports:
  1. systemd state — active?, enabled at boot?, restart count, start time
  2. the env file exists and which TRADING_MODE it sets
  3. the SQLite DB opens, passes integrity_check, and how many trades it holds
  4. the log file's size, last-write age and its last line

Read-only: it places no orders, opens no exchange connections and changes no
files.  Refuses to run if either env file sets TRADING_MODE=live.
"""

from __future__ import annotations

import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

BOTS = {
    "paper": {"unit": "calendar-bot-paper", "env": ".env",
              "db": "db/calendar_bot.db", "log": "logs/bot.log"},
    "test":  {"unit": "calendar-bot-test", "env": ".env.test",
              "db": "db/calendar_bot_test.db", "log": "logs/bot_test.log"},
}


def _env_value(path: Path, key: str) -> str | None:
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if line.startswith(f"{key}="):
            return line.partition("=")[2].strip().strip('"').strip("'")
    return None


def _systemd(unit: str) -> None:
    if not shutil.which("systemctl"):
        print("  systemd     : systemctl not found (not on the VM?)")
        return
    props = subprocess.run(
        ["systemctl", "show", unit, "--no-pager",
         "-p", "ActiveState,SubState,UnitFileState,NRestarts,ActiveEnterTimestamp"],
        capture_output=True, text=True,
    ).stdout
    p = dict(l.split("=", 1) for l in props.splitlines() if "=" in l)
    print(f"  systemd     : {p.get('ActiveState')}/{p.get('SubState')}"
          f"  boot={p.get('UnitFileState')}  restarts={p.get('NRestarts')}"
          f"  since={p.get('ActiveEnterTimestamp') or '-'}")


def _db(path: Path) -> None:
    if not path.exists():
        print(f"  db          : MISSING {path}")
        return
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        ok = con.execute("PRAGMA integrity_check").fetchone()[0]
        tables = [r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        counts = []
        for t in ("calendar_trades",):
            if t in tables:
                counts.append(f"{t}={con.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]}")
        print(f"  db          : integrity={ok}  tables={len(tables)}  {' '.join(counts)}")
    finally:
        con.close()


def _log(path: Path) -> None:
    if not path.exists():
        print(f"  log         : MISSING {path}")
        return
    st = path.stat()
    age = time.time() - st.st_mtime
    with path.open("rb") as f:
        f.seek(max(0, st.st_size - 4096))
        tail = f.read().decode("utf-8", errors="replace").strip().splitlines()
    print(f"  log         : {st.st_size / 1e6:.1f} MB, last write {age:.0f}s ago")
    if tail:
        print(f"  last line   : {tail[-1][:160]}")


def main() -> int:
    for name, b in BOTS.items():
        env = ROOT / b["env"]
        if env.exists() and (_env_value(env, "TRADING_MODE") or "").lower() == "live":
            print(f"{b['env']} sets TRADING_MODE=live — refusing to run.")
            return 1

    for name, b in BOTS.items():
        print(f"\n[{name}]  unit={b['unit']}")
        _systemd(b["unit"])
        env = ROOT / b["env"]
        if env.exists():
            print(f"  env         : {b['env']}  TRADING_MODE={_env_value(env, 'TRADING_MODE')}")
        else:
            print(f"  env         : MISSING {b['env']}")
        _db(ROOT / b["db"])
        _log(ROOT / b["log"])
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
