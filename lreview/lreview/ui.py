"""Terminal output for lreview.

On a TTY, the periodic status line is drawn in place (\\r + clear-line)
and event lines are printed above it; colors are enabled unless
NO_COLOR is set. When stdout is not a TTY (piped, logged), everything
degrades to plain appended lines with no escape codes.
"""

import os
import sys
import threading

def elapsed(seconds: float) -> str:
    minutes, secs = divmod(int(seconds), 60)
    return f"{minutes}m{secs:02d}s"


def format_tokens(count):
    if count is None:
        return None
    if count < 1000:
        return str(count)
    if count < 1_000_000:
        return f"{count / 1000:.0f}k"
    return f"{count / 1_000_000:.1f}M"


_CODES = {
    "red": "31",
    "green": "32",
    "yellow": "33",
    "blue": "34",
    "magenta": "35",
    "cyan": "36",
    "dim": "2",
    "bold": "1",
}


class Console:
    """Serialized writer that keeps a redrawable status line at the
    bottom of the output."""

    def __init__(self):
        self._lock = threading.Lock()
        self._status = ""

    @property
    def _stream(self):
        # Resolved per call so pytest's capsys and redirections work.
        return sys.stdout

    @property
    def is_tty(self) -> bool:
        try:
            return self._stream.isatty()
        except (AttributeError, ValueError):
            return False

    @property
    def colors_enabled(self) -> bool:
        return self.is_tty and not os.environ.get("NO_COLOR")

    def color(self, name: str, text: str) -> str:
        if not self.colors_enabled:
            return text
        return f"\x1b[{_CODES[name]}m{text}\x1b[0m"

    def event(self, msg: str) -> None:
        """Print a permanent line, keeping the status line below it."""
        with self._lock:
            stream = self._stream
            if self.is_tty and self._status:
                stream.write("\r\x1b[2K")
            stream.write(msg + "\n")
            if self.is_tty and self._status:
                stream.write(self._status)
            stream.flush()

    def status(self, msg: str) -> None:
        """Draw/replace the transient status line."""
        with self._lock:
            self._status = msg
            stream = self._stream
            if self.is_tty:
                stream.write("\r\x1b[2K" + msg)
            else:
                stream.write(msg + "\n")
            stream.flush()

    def clear_status(self) -> None:
        with self._lock:
            if self.is_tty and self._status:
                self._stream.write("\r\x1b[2K")
                self._stream.flush()
            self._status = ""


console = Console()
