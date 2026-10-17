#!/usr/bin/env bash
# deploy/linux/install.sh
# =======================
# One-shot installer for the calendar bot on a Linux (systemd) VM.
#
# Run from inside a clone of the repo, as the user that will own the bots:
#
#     sudo bash deploy/linux/install.sh --data ~/calendar-bot-data.tar.gz
#
# What it does (each step is safe to re-run):
#   1. installs python3-venv / sqlite3 / git (apt-based distros only)
#   2. creates .venv and installs requirements.txt
#   3. restores .env, .env.test, db/ and logs/ from the export bundle (--data)
#   4. installs calendar-bot-paper.service and calendar-bot-test.service
#   5. installs a sudoers drop-in so the bot user (and the GitHub runner that
#      runs as that user) can start/stop/restart the two units without a password
#   6. enables both units (start on boot) and starts them unless --no-start
#
# Options:
#   --data FILE   tar.gz produced by deploy/export_data.ps1
#   --force       let --data overwrite existing db/*.db files
#   --no-start    enable the units but do not start them now
#   --user NAME   bot user (default: the user who invoked sudo)
set -euo pipefail

UNITS=(calendar-bot-paper calendar-bot-test)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BOT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
BOT_USER="${SUDO_USER:-}"
DATA=""
FORCE=0
START=1

while [[ $# -gt 0 ]]; do
    case "$1" in
        --data)     DATA="$2"; shift 2 ;;
        --force)    FORCE=1; shift ;;
        --no-start) START=0; shift ;;
        --user)     BOT_USER="$2"; shift 2 ;;
        -h|--help)  sed -n '2,25p' "$0"; exit 0 ;;
        *) echo "Unknown option: $1" >&2; exit 2 ;;
    esac
done

log()  { printf '\n==> %s\n' "$*"; }
die()  { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
as_bot() { sudo -u "$BOT_USER" -H "$@"; }

[[ $EUID -eq 0 ]]                 || die "run with sudo"
[[ -n "$BOT_USER" ]]              || die "could not determine bot user; pass --user NAME"
[[ "$BOT_USER" != "root" ]]       || die "refusing to run the bots as root; pass --user NAME"
id "$BOT_USER" >/dev/null 2>&1    || die "user '$BOT_USER' does not exist"
[[ "$BOT_DIR" != *" "* ]]         || die "repo path contains spaces: $BOT_DIR"
[[ -f "$BOT_DIR/bot.py" ]]        || die "bot.py not found in $BOT_DIR"
command -v systemctl >/dev/null   || die "systemd not found"
[[ -z "$DATA" || -f "$DATA" ]]    || die "data bundle not found: $DATA"

echo "Bot user: $BOT_USER"
echo "Bot dir:  $BOT_DIR"

# ── 1. System packages ────────────────────────────────────────────────────────
if command -v apt-get >/dev/null; then
    log "Installing system packages"
    apt-get update -qq
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3 python3-venv python3-pip sqlite3 git
else
    echo "Not an apt-based distro - make sure python3 (with venv), sqlite3 and git are installed."
fi
python3 -c 'import sys; assert sys.version_info >= (3, 11), sys.version' \
    || die "python3 >= 3.11 required (found $(python3 --version 2>&1))"

# The repo must belong to the bot user so it (and the runner) can git pull
chown -R "$BOT_USER": "$BOT_DIR"

# ── 2. Virtualenv ─────────────────────────────────────────────────────────────
log "Creating virtualenv and installing requirements"
[[ -x "$BOT_DIR/.venv/bin/python" ]] || as_bot python3 -m venv "$BOT_DIR/.venv"
as_bot "$BOT_DIR/.venv/bin/pip" install -q --upgrade pip
as_bot "$BOT_DIR/.venv/bin/pip" install -q -r "$BOT_DIR/requirements.txt"

# ── 3. Restore runtime data ───────────────────────────────────────────────────
if [[ -n "$DATA" ]]; then
    log "Restoring data from $DATA"
    if [[ $FORCE -eq 0 ]] && compgen -G "$BOT_DIR/db/*.db" >/dev/null; then
        die "db/*.db already exists - pass --force to overwrite it with the bundle"
    fi
    # Stop the units first if they are already installed and running
    for u in "${UNITS[@]}"; do systemctl stop "$u" 2>/dev/null || true; done
    as_bot tar -xzf "$DATA" -C "$BOT_DIR" --no-same-owner
    # Windows CRLF line endings in the env files are harmless to config.py,
    # but strip them so the files are clean to edit/source on Linux.
    for f in .env .env.test; do
        if [[ -f "$BOT_DIR/$f" ]]; then sed -i 's/\r$//' "$BOT_DIR/$f"; fi
    done
    chmod 600 "$BOT_DIR"/.env* 2>/dev/null || true
fi
as_bot mkdir -p "$BOT_DIR/logs" "$BOT_DIR/db"
for f in .env .env.test; do
    [[ -f "$BOT_DIR/$f" ]] || echo "WARNING: $BOT_DIR/$f is missing - that bot will not start correctly"
done

# Sanity-check the restored databases
for db in "$BOT_DIR"/db/*.db; do
    [[ -f "$db" ]] || continue
    result="$(sqlite3 "$db" 'PRAGMA integrity_check;' 2>&1 | head -1)"
    echo "  $(basename "$db"): integrity_check=$result"
done

# ── 4. systemd units ──────────────────────────────────────────────────────────
log "Installing systemd units"
for u in "${UNITS[@]}"; do
    sed -e "s|@BOT_USER@|$BOT_USER|g" -e "s|@BOT_DIR@|$BOT_DIR|g" \
        "$SCRIPT_DIR/$u.service" > "/etc/systemd/system/$u.service"
    chmod 644 "/etc/systemd/system/$u.service"
    echo "  /etc/systemd/system/$u.service"
done
systemctl daemon-reload

# ── 5. sudoers drop-in for passwordless service control ──────────────────────
log "Installing sudoers rule for $BOT_USER"
SUDOERS_TMP="$(mktemp)"
{
    echo "# Installed by calendar-bot deploy/linux/install.sh"
    echo "# Lets $BOT_USER (and the GitHub Actions runner running as $BOT_USER) control the bot units."
    for bin in /usr/bin/systemctl /bin/systemctl; do
        for action in start stop restart; do
            echo "$BOT_USER ALL=(root) NOPASSWD: $bin $action ${UNITS[*]}"
            for u in "${UNITS[@]}"; do
                echo "$BOT_USER ALL=(root) NOPASSWD: $bin $action $u"
            done
        done
    done
} > "$SUDOERS_TMP"
visudo -cf "$SUDOERS_TMP" >/dev/null || die "generated sudoers file failed validation"
install -m 440 "$SUDOERS_TMP" /etc/sudoers.d/calendar-bot
rm -f "$SUDOERS_TMP"

# ── 6. Enable (start on boot) and start ───────────────────────────────────────
log "Enabling units"
systemctl enable "${UNITS[@]}"
if [[ $START -eq 1 ]]; then
    systemctl restart "${UNITS[@]}"
    sleep 5
    systemctl --no-pager --lines=5 status "${UNITS[@]}" || true
fi

log "Done"
cat <<EOF
Useful commands:
  systemctl status ${UNITS[*]}
  journalctl -u calendar-bot-paper -f        # stdout/stderr of the paper bot
  tail -f $BOT_DIR/logs/bot.log              # paper bot log file
  tail -f $BOT_DIR/logs/bot_test.log         # test bot log file
  sudo systemctl restart calendar-bot-paper  # no password needed for $BOT_USER
EOF
