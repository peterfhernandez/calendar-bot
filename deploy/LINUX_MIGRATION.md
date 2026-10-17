# Moving the bots to the Linux VM (Proxmox)

This replaces the Windows/servy setup (`CalendarBotPaper`, `CalendarBotTest`)
with two systemd services on a Linux VM. Both start at boot and restart
automatically if they crash.

| Bot   | systemd unit         | Env file    | Database                  | Log file             |
|-------|----------------------|-------------|---------------------------|----------------------|
| paper | `calendar-bot-paper` | `.env`      | `db/calendar_bot.db`      | `logs/bot.log`       |
| test  | `calendar-bot-test`  | `.env.test` | `db/calendar_bot_test.db` | `logs/bot_test.log`  |

Files involved:

- [`deploy/export_data.ps1`](export_data.ps1) packs the env files, DBs and logs on Windows
- [`deploy/linux/install.sh`](linux/install.sh) installs the venv, data, units and sudoers rule on the VM
- [`deploy/linux/calendar-bot-paper.service`](linux/calendar-bot-paper.service), [`calendar-bot-test.service`](linux/calendar-bot-test.service) are the unit templates
- [`.github/workflows/deploy.yml`](../.github/workflows/deploy.yml) handles push-to-master auto-deploy on the VM runner
- [`scratch/scratch_vm_healthcheck.py`](../scratch/scratch_vm_healthcheck.py) is a read-only post-install check

The examples below use `calbot` as the VM user and `calbot-vm` as its
hostname. Replace them with your own.

---

## 1. Stop the bots on Windows

Only one copy of each bot may run at a time. Two copies would fight over the
Telegram bot (`getUpdates` conflict) and, in test mode, over the Deribit
account. Stop the Windows services and disable them:

```powershell
Stop-Service CalendarBotPaper, CalendarBotTest
Set-Service  CalendarBotPaper -StartupType Disabled
Set-Service  CalendarBotTest  -StartupType Disabled
```

Do this on whichever machine runs them.

## 2. Export the data (Windows)

From the repo root on the machine that holds the current `db/` and `logs/`:

```powershell
powershell -ExecutionPolicy Bypass -File deploy\export_data.ps1
```

This writes `~\calendar-bot-data.tar.gz` and prints its sha256. Add
`-SkipArchivedLogs` to leave out `logs\paper`, `logs\test` and `logs\old logs`,
which are about 500 MB of old servy stderr captures. The script refuses to run
while a bot process is still running.

Copy it to the VM:

```powershell
scp $HOME\calendar-bot-data.tar.gz calbot@calbot-vm:~/
```

## 3. Clone the repo on the VM

Log in as the user that will own the bots (not root):

```bash
sudo apt-get install -y git
git clone https://github.com/peterfhernandez/calendar-bot.git ~/calendar-bot
```

If the repo is private, use a read-only deploy key or `gh auth login`. The
auto-deploy workflow runs `git fetch` in this checkout, so it needs credentials
that keep working without you.

## 4. Install

```bash
cd ~/calendar-bot
sudo bash deploy/linux/install.sh --data ~/calendar-bot-data.tar.gz
```

The script:

1. installs `python3-venv`, `sqlite3` and `git`
2. creates `.venv` and installs the requirements
3. unpacks the bundle and runs `integrity_check` on each DB
4. writes both units to `/etc/systemd/system/` and enables them so they start at boot
5. adds `/etc/sudoers.d/calendar-bot`, which lets `calbot` start, stop and restart only these two units without a password (the GitHub runner needs this)
6. starts both bots

The script is safe to re-run. Use `--force` to overwrite DBs that already exist
from a new bundle. Use `--no-start` to install without starting.

Optional: set the VM's timezone so log timestamps match what you're used to.
All trading logic uses UTC, so this only changes how the logs read.

```bash
sudo timedatectl set-timezone Australia/Sydney
```

## 5. Verify

```bash
.venv/bin/python -m scratch.scratch_vm_healthcheck
```

For each bot this shows the systemd state, whether it starts at boot, its
restart count, its trading mode, DB integrity and trade count, and the last log
line. Then check Telegram: `/status` on both chatbots should answer.

Also run the test suite once on the VM:

```bash
.venv/bin/python -m pytest -q
```

Test a reboot:

```bash
sudo reboot
# after it comes back:
systemctl status calendar-bot-paper calendar-bot-test
```

## 6. Move auto-deploy to the VM

The workflow now targets a runner labelled `calendar-bot-vm`. The old laptop
runner (label `calendar-bot`) will no longer pick up jobs.

1. On GitHub, go to repo **Settings → Actions → Runners → New self-hosted
   runner → Linux x64** and follow the download and `config.sh` steps on the
   VM as `calbot`. When it asks for labels, enter `calendar-bot-vm`.
2. Install the runner as a service so it also survives reboots:
   ```bash
   cd ~/actions-runner
   sudo ./svc.sh install calbot
   sudo ./svc.sh start
   ```
3. If the checkout is not at `~/calendar-bot`, set a repository variable
   `BOT_DIR` (**Settings → Secrets and variables → Actions → Variables**) to its
   path.
4. Remove the old runner from the laptop. On GitHub, remove it under
   **Settings → Actions → Runners**. On the laptop, run
   `.\config.cmd remove` in the runner folder.

On every push to `master` the workflow runs `git reset --hard origin/master`
in the checkout. It reinstalls requirements if `requirements.txt` changed,
restarts both units and fails the run if either unit is not active 15 s later.
`.env*`, `db/` and `logs/` are git-ignored, so the reset leaves them alone.

If you change a unit template in `deploy/linux/`, re-run `install.sh` (no
`--data`) to apply it. The workflow only restarts the units; it does not
reinstall them.

---

## Day-to-day commands

```bash
systemctl status calendar-bot-paper calendar-bot-test
sudo systemctl restart calendar-bot-test
sudo systemctl stop calendar-bot-paper        # stays stopped until start or reboot
journalctl -u calendar-bot-paper -f           # stdout/stderr (crash tracebacks)
journalctl -u calendar-bot-test --since "1 hour ago"
tail -f logs/bot.log                          # paper bot log file
tail -f logs/bot_test.log                     # test bot log file
```

To keep a bot from starting at boot, use `sudo systemctl disable <unit>`.

## Rolling back to Windows

Stop and disable the units on the VM
(`sudo systemctl disable --now calendar-bot-paper calendar-bot-test`). Copy
`db/` and `logs/` back with `scp`, then re-enable the Windows services.
