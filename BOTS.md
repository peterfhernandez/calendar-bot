# Bots setup

## Non cash setuo

We run 2 bots in parallel currently to do stress testing and edge case testing in a non-cash setup. The two setups are represented by the paper mode and the testnet.

### Paper mode

We run a paper mode bot to test strategies. No connections to a broker exist. This tests logic and gives a sense of what the strategy could do.

#### Files

The important files that define this paper mode are:

- `.env` holds Keys and credentials
- `config.py` contains configuration variables
- `db\calendar_bot.db` is the database file
- `logs\bot.logs` a rotating log file

#### Command

We launch this bot using a service setup with servy. The launch command is `python -m bot --portfolio=10000`

### Test mode

We run a test mode bot to test the behaviour of the bot when interaction with the broker. We get a sense for trade execution, trading fees and margin managemenet.

#### Files

The important files that define this paper mode are:

- `.env.test` holds Keys and credentials
- `config_test.py` contains configuration variables
- `db\calendar_bot_test.db` is the database file
- `logs\bot_test.logs` a rotating log file

#### Command

We launch this bot using a service setup with servy. The launch command is `python -m bot --env .env.test --db db\calendar_bot_test.db --log logs\bot_test.log`

## Cash setup

This is the live trading bot, which is not setup yet.
