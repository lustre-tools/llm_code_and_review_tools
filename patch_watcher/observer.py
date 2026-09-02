"""Independent bounded polling loop for Patch Watcher services."""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterable
from typing import Any


class BackgroundObserver:
    """Refresh watched patches and run deterministic automation without a browser.

    The callbacks stay injectable so this class owns no Gerrit, Maloo, database,
    or process policy.  A non-blocking tick lock coalesces concurrent browser,
    scheduler, and test invocations instead of overlapping remote reads.
    """

    def __init__(
        self,
        patches_provider: Callable[[], Iterable[dict[str, Any]]],
        refresh_patch: Callable[[dict[str, Any]], Any],
        evaluate_patch: Callable[[dict[str, Any]], Any],
        *,
        interval_seconds: float,
        error_handler: Callable[[dict[str, Any], Exception], Any] | None = None,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        self.patches_provider = patches_provider
        self.refresh_patch = refresh_patch
        self.evaluate_patch = evaluate_patch
        self.interval_seconds = float(interval_seconds)
        self.error_handler = error_handler or (lambda patch, error: None)
        self._tick_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def tick(self) -> bool:
        """Run one bounded poll, returning false when another poll owns it."""
        if not self._tick_lock.acquire(blocking=False):
            return False
        try:
            for patch in list(self.patches_provider()):
                try:
                    error = self.refresh_patch(patch)
                    if error is None:
                        self.evaluate_patch(patch)
                except Exception as exc:  # isolate one patch from the rest
                    self.error_handler(patch, exc)
            return True
        finally:
            self._tick_lock.release()

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="patch-watcher-observer",
            daemon=True,
        )
        self._thread.start()

    def stop(self, timeout: float = 5) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=timeout)

    def _run(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            self.tick()


__all__ = ["BackgroundObserver"]
