import os
import sys
import logging
import logging.handlers

LOG_DIR = os.getenv("LOG_DIR") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
os.makedirs(LOG_DIR, exist_ok=True)
LOG_FILE = os.path.join(LOG_DIR, "bot.log")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

logger = logging.getLogger("ct_bot")
logger.setLevel(LOG_LEVEL)
logger.propagate = False

_fmt = logging.Formatter(
    fmt="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)


class _FlushStreamHandler(logging.StreamHandler):
    def emit(self, record):
        super().emit(record)
        self.flush()


_console_handler = _FlushStreamHandler(stream=sys.stdout)
_console_handler.setFormatter(_fmt)
_console_handler.setLevel(LOG_LEVEL)
logger.addHandler(_console_handler)

_file_handler = logging.handlers.RotatingFileHandler(
    LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
)
_file_handler.setFormatter(_fmt)
_file_handler.setLevel(LOG_LEVEL)
logger.addHandler(_file_handler)

logging.getLogger("telethon").setLevel(os.getenv("TELETHON_LOG_LEVEL", "WARNING"))
logging.getLogger("urllib3").setLevel("WARNING")
