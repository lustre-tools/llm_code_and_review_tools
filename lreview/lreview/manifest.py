"""Locked, atomic access to the summary.json manifest.

Both the runner and the poster mutate summary.json, possibly from
concurrent lreview invocations (a long `run` overlapping a `post`
of earlier results). Every load-modify-save cycle therefore holds an
exclusive flock on a sidecar lock file, and saves go through a
temporary file + os.replace so readers never observe a truncated
manifest. A corrupt manifest is backed up, never silently discarded.
"""

import fcntl
import json
import os
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

SUMMARY_NAME = "summary.json"
LOCK_NAME = ".summary.lock"


def _summary_path(results_dir: Path) -> Path:
    return results_dir / SUMMARY_NAME


def _load(results_dir: Path) -> dict:
    path = _summary_path(results_dir)
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        backup = path.with_name(
            f"{SUMMARY_NAME}.corrupt-{int(time.time())}")
        path.rename(backup)
        print(f"warning: {path} was corrupt; backed up to {backup.name} "
              "and starting a fresh manifest")
        return {}


def _save(results_dir: Path, summary: dict) -> None:
    path = _summary_path(results_dir)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(results_dir), prefix=".summary.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as tmp:
            json.dump(summary, tmp, indent=2)
            tmp.write("\n")
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


@contextmanager
def locked_summary(results_dir: Path, save: bool = True):
    """Yield the manifest dict under an exclusive inter-process lock.

    The (possibly modified) dict is saved atomically when the block
    exits without an exception; pass save=False for read-only access.
    """
    results_dir.mkdir(parents=True, exist_ok=True)
    lock_path = results_dir / LOCK_NAME
    with open(lock_path, "w") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        try:
            summary = _load(results_dir)
            yield summary
            if save:
                _save(results_dir, summary)
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)


def read_summary(results_dir: Path) -> dict:
    """Read the manifest without keeping a lock (snapshot only)."""
    with locked_summary(results_dir, save=False) as summary:
        return summary
