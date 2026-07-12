"""
core/logging_setup.py
=====================
Shared logging configuration for every entry point (Phase 20a).

Previously five modules (`monitor/loop.py`, `collect.py`,
`backtest/data_collector.py`, `data/deribit_feed.py`, `data/debug_viewer.py`)
each called ``logging.basicConfig`` with their own hardcoded format, level,
and rotation settings.  All of that now lives in ``config.py`` (``LOG_LEVEL``,
``LOG_FORMAT``, ``LOG_DATE_FORMAT``, ``LOG_FILE_MAX_BYTES``,
``LOG_BACKUP_COUNT``, ``LOG_DIR``, ``NOISY_LOGGERS``) and is applied through
the single :func:`setup_logging` helper below.

Public API
----------
setup_logging(level=None, log_dir=None, log_file=None, console=True, force=False)
    Wire up the root logger: console handler, optional rotating file handler,
    noisy-logger suppression, and secret redaction.

SecretRedactor
    logging.Filter that scrubs credential values from log records before they
    reach any handler.  Installed on the root logger by setup_logging().
"""

from __future__ import annotations

import logging
import logging.handlers
import os
from pathlib import Path

import config

_configured = False  # guard against double-init


class SecretRedactor(logging.Filter):
    """
    Scrubs sensitive values (API keys, tokens, chat IDs) from log records
    before they reach any handler.  Applied to the root logger so it covers
    both the console and the rotating file.

    Secrets are injected at setup time so the filter reads from config once,
    not on every log record.
    """

    def __init__(self, secrets: list[str]) -> None:
        super().__init__()
        # Drop blank/whitespace-only strings so we don't accidentally redact every log line.
        self._secrets = [s for s in secrets if s and s.strip()]

    def filter(self, record: logging.LogRecord) -> bool:
        if self._secrets:
            try:
                msg = record.getMessage()
            except Exception:
                return True
            redacted = msg
            for secret in self._secrets:
                redacted = redacted.replace(secret, "<redacted>")
            if redacted is not msg:
                # Rewrite the pre-formatted message so handlers see the
                # redacted version; clear args so Formatter doesn't re-expand.
                record.msg = redacted
                record.args = ()
        return True


def _config_secrets() -> list[str]:
    """Collect every credential value from config for redaction."""
    try:
        return [
            getattr(config, "TELEGRAM_TOKEN",             ""),
            str(getattr(config, "TELEGRAM_CHAT",          "") or ""),
            getattr(config, "DERIBIT_TEST_CLIENT_ID",     ""),
            getattr(config, "DERIBIT_TEST_CLIENT_SECRET", ""),
            getattr(config, "DERIBIT_LIVE_CLIENT_ID",     ""),
            getattr(config, "DERIBIT_LIVE_CLIENT_SECRET", ""),
            getattr(config, "SMTP_USER",     ""),
            getattr(config, "SMTP_PASSWORD", ""),
        ]
    except Exception:
        return []


def setup_logging(
    level: int | str | None = None,
    log_dir: str | Path | None = None,
    log_file: str | Path | None = None,
    console: bool = True,
    force: bool = False,
) -> None:
    """
    Configure the root logger from ``config.LOG_*`` settings.

    Parameters
    ----------
    level
        Root logger level.  Accepts an int (``logging.INFO``) or a name
        (``"INFO"``).  Defaults to ``config.LOG_LEVEL``.
    log_dir
        When given (and *log_file* is not), a rotating file handler is added
        at ``<log_dir>/bot.log``.  The ``BOT_LOG_FILE`` env var (set by
        bot.py's ``--log`` pre-parser) overrides the resulting path.
    log_file
        Explicit rotating-file path.  Takes precedence over *log_dir*.
        When both are None and BOT_LOG_FILE is unset, no file handler is
        added (console only — used by the collector and debug tools).
    console
        Add a StreamHandler (default True).
    force
        Re-run setup even if already configured (used by callers that manage
        their own guard, e.g. monitor.loop.configure_logging).

    Safe to call multiple times — extra calls are no-ops unless *force* is set.
    """
    global _configured
    if _configured and not force:
        return

    if level is None:
        level = config.LOG_LEVEL
    if isinstance(level, str):
        level = getattr(logging, level.upper(), logging.INFO)

    root = logging.getLogger()
    root.setLevel(level)

    formatter = logging.Formatter(config.LOG_FORMAT, datefmt=config.LOG_DATE_FORMAT)

    if console:
        ch = logging.StreamHandler()
        ch.setFormatter(formatter)
        root.addHandler(ch)

    # Rotating file — BOT_LOG_FILE env var lets a separate instance write to
    # its own file (e.g. logs/bot_test.log) so paper and test logs don't
    # interleave.
    log_override = os.environ.get("BOT_LOG_FILE", "")
    file_path: Path | None = None
    if log_override:
        file_path = Path(log_override)
    elif log_file is not None:
        file_path = Path(log_file)
    elif log_dir is not None:
        file_path = Path(log_dir) / "bot.log"

    if file_path is not None:
        file_path.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(
            file_path,
            maxBytes=config.LOG_FILE_MAX_BYTES,
            backupCount=config.LOG_BACKUP_COUNT,
            encoding="utf-8",
        )
        fh.setFormatter(formatter)
        root.addHandler(fh)

    # Silence high-frequency third-party loggers that add no operational value
    # (httpx logs every Telegram getUpdates poll at INFO).  Real errors still
    # surface — the suppression levels come from config.NOISY_LOGGERS.
    for name, lvl in config.NOISY_LOGGERS.items():
        logging.getLogger(name).setLevel(
            getattr(logging, lvl.upper(), logging.WARNING) if isinstance(lvl, str) else lvl
        )

    # Redact secrets from all log output.  Never let this crash logging setup.
    try:
        root.addFilter(SecretRedactor(_config_secrets()))
    except Exception:
        pass

    _configured = True
