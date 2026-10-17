# Bots setup

## Non cash setuo

We run 2 bots in parallel currently to do stress testing and edge case testing in a non-cash setup. The two setups are represented by the paper mode and the testnet.

### Paper mode

We run a paper mode bot to test strategies. No connections to a broker exist. This tests logic and gives a sense of what the strategy could do.

#### Files

The important files that define this paper mode are:

- `.env` holds Keys and credentials
- `config.py` contains configuration variables
- `db/calendar_bot.db` is the database file
- `logs/bot.log` a rotating log file

#### Command

The bot runs on the Linux VM as the systemd service `calendar-bot-paper` (`deploy/linux/calendar-bot-paper.service`). The launch command is `python -m bot --portfolio=10000`

#### Telegram

The paper bot interacts with a telegram chatbot:

- telegram chatbot named: `PFH Notifications`
- Chatbot username is `pfh_notifications_bot`
- Chatbot Id: `8909494838`
- Url is `https://web.telegram.org/a/#8909494838`

### Test mode

We run a test mode bot to test the behaviour of the bot when interaction with the broker. We get a sense for trade execution, trading fees and margin managemenet.

#### Test mode files

The important files that define this paper mode are:

- `.env.test` holds Keys and credentials
- `config_test.py` contains configuration variables
- `db/calendar_bot_test.db` is the database file
- `logs/bot_test.log` a rotating log file

#### Test mode Command

The bot runs on the Linux VM as the systemd service `calendar-bot-test` (`deploy/linux/calendar-bot-test.service`). The launch command is `python -m bot --env .env.test --db db/calendar_bot_test.db --log logs/bot_test.log`

#### Test Telegram

The paper bot interacts with a telegram chatbot:

- telegram chatbot named: `PFH Test Notifications`
- Chatbot username is `pfh_test_notifications_bot`
- Chatbot Id: `8719768449`
- Url is `https://web.telegram.org/a/#8719768449`

## Hosting

Both bots run on a Linux VM in Proxmox under systemd. They start on boot and restart on failure. Pushes to `master` auto-deploy through a self-hosted GitHub runner (label `calendar-bot-vm`). See [deploy/LINUX_MIGRATION.md](deploy/LINUX_MIGRATION.md) for setup and day-to-day commands. The earlier Windows/servy services `CalendarBotPaper` and `CalendarBotTest` are retired.

## Cash setup

This is the live trading bot, which is not setup yet.
