"""
logger.py — Tee stdout/stderr to a rotating daily log file.

Call setup_logging() once at startup (before any print). All subsequent
print() calls from any module are written to both the terminal and the
log file. ANSI colour codes are stripped from the file copy.

Log files: logs/YYYY-MM-DD.log  (one per calendar day, UTC)
"""

import os
import re
import sys
from datetime import datetime, timezone

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
_log_dir = os.path.join(os.path.dirname(__file__), "logs")


def _today_path() -> str:
    date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return os.path.join(_log_dir, f"{date}.log")


class _Tee:
    """Wraps a stream (stdout or stderr) and mirrors writes to a log file."""

    def __init__(self, original):
        self._original = original
        self._file = None
        self._path = None

    def _ensure_file(self):
        path = _today_path()
        if path != self._path:
            if self._file:
                self._file.close()
            os.makedirs(_log_dir, exist_ok=True)
            self._file = open(path, "a", encoding="utf-8", buffering=1)
            self._path = path

    def write(self, text):
        self._original.write(text)
        try:
            self._ensure_file()
            self._file.write(_ANSI_RE.sub("", text))
        except Exception:
            pass

    def flush(self):
        self._original.flush()
        if self._file:
            try:
                self._file.flush()
            except Exception:
                pass

    def fileno(self):
        return self._original.fileno()

    def isatty(self):
        return self._original.isatty()

    def __getattr__(self, name):
        return getattr(self._original, name)


def setup_logging():
    os.makedirs(_log_dir, exist_ok=True)
    sys.stdout = _Tee(sys.stdout)
    sys.stderr = _Tee(sys.stderr)
    path = _today_path()
    # Write a session separator so it's easy to find session starts in long files
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"\n{'='*60}\n  SESSION START  {ts}\n{'='*60}\n")
    print(f"  Logging to {path}")
