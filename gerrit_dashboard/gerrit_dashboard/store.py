"""JSON persistence: snapshot, enrichment cache, watchlist.

All writes are atomic (tmp + rename) and guarded by a module lock so
the Flask request handlers and the background refresher can share them.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

_lock = threading.Lock()

SNAPSHOT_FILE = "snapshot.json"
ENRICH_CACHE_FILE = "enrich_cache.json"
WATCHLIST_FILE = "watchlist.json"
SETTINGS_FILE = "settings.json"
HIDDEN_FILE = "hidden.json"
# Tiny sidecar for the board index: snapshots run to megabytes, and the
# landing page would otherwise parse every one of them on each hit.
SUMMARY_FILE = "summary.json"

# Bump when the snapshot layout changes: a persisted snapshot from
# another code version must be discarded, not rendered (missing keys
# 500 the page until the next successful refresh).
SNAPSHOT_SCHEMA = 22

# Usernames become URL tokens, Gerrit query terms and directory names, so
# they are restricted to a conservative charset AND must contain at least
# one alphanumeric — "." / ".." would otherwise navigate the data dir.
SAFE_USERNAME_RE = re.compile(
    r"^(?=[A-Za-z0-9._-]{1,64}$)[A-Za-z0-9._-]*[A-Za-z0-9][A-Za-z0-9._-]*$")

_CHANGE_URL_RE = re.compile(r"/(?:c/[^+]+\+/)?(\d+)(?:/\d+)?/?$")


class Store:
    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)

    # -- generic helpers ---------------------------------------------------

    def _path(self, name: str) -> Path:
        return self.data_dir / name

    def _load(self, name: str, default: Any) -> Any:
        path = self._path(name)
        try:
            with open(path) as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return default

    def _save_locked(self, name: str, data: Any) -> None:
        """Write atomically; caller must hold _lock.  Unique tmp names so
        two processes sharing the data dir cannot corrupt each other."""
        path = self._path(name)
        fd, tmp = tempfile.mkstemp(dir=self.data_dir, prefix=name + ".", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(data, f)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def _save(self, name: str, data: Any) -> None:
        with _lock:
            self._save_locked(name, data)

    # -- snapshot ----------------------------------------------------------

    def load_snapshot(self, projects: list[str] | None = None) -> dict | None:
        """Load the persisted snapshot; None if unusable.

        `projects` is the current project allowlist: a snapshot built
        under a DIFFERENT allowlist is discarded, so a restricted
        (public) instance can never serve rows that a previous
        unrestricted run persisted into the same data dir.
        """
        snap = self._load(SNAPSHOT_FILE, None)
        if not isinstance(snap, dict) or snap.get("schema") != SNAPSHOT_SCHEMA:
            return None
        if snap.get("projects", []) != sorted(projects or []):
            return None
        return snap

    def save_snapshot(self, snapshot: dict) -> None:
        self._save(SNAPSHOT_FILE, snapshot)
        self._save(SUMMARY_FILE, summarize_snapshot(snapshot))

    def load_summary(self) -> dict | None:
        """Board summary for the index, derived from the last snapshot.

        Backfilled from the snapshot for boards written before summaries
        existed, so an old data dir indexes without a refresh.  A
        snapshot from an older schema cannot be summarized; that verdict
        is cached too, so the index never re-parses a multi-megabyte
        file it already knows it cannot use.
        """
        summary = self._load(SUMMARY_FILE, None)
        if isinstance(summary, dict) and summary.get("schema") == SNAPSHOT_SCHEMA:
            # Summaries written before `state` existed carry kpis and
            # nothing else; infer it rather than calling them unusable.
            summary.setdefault("state", "ok" if summary.get("kpis") else "outdated")
            return summary
        snap = self._load(SNAPSHOT_FILE, None)
        if not isinstance(snap, dict):
            return None
        if snap.get("schema") != SNAPSHOT_SCHEMA:
            summary = {"schema": SNAPSHOT_SCHEMA, "state": "outdated",
                       "self": snap.get("self", {}) if isinstance(snap.get("self"), dict) else {}}
        else:
            summary = summarize_snapshot(snap)
        self._save(SUMMARY_FILE, summary)
        return summary

    # -- enrichment cache --------------------------------------------------

    def load_enrich_cache(self) -> dict:
        cache = self._load(ENRICH_CACHE_FILE, {})
        return cache if isinstance(cache, dict) else {}

    def save_enrich_cache(self, cache: dict) -> None:
        self._save(ENRICH_CACHE_FILE, cache)

    # -- settings ----------------------------------------------------------

    def load_settings(self) -> dict:
        s = self._load(SETTINGS_FILE, {})
        return s if isinstance(s, dict) else {}

    def save_settings(self, settings: dict) -> None:
        self._save(SETTINGS_FILE, settings)

    def update_settings(self, **values) -> dict:
        """Read-modify-write under the lock (no lost updates)."""
        with _lock:
            settings = self.load_settings()
            settings.update(values)
            self._save_locked(SETTINGS_FILE, settings)
            return settings

    # -- hidden patches ----------------------------------------------------

    def load_hidden(self) -> list[int]:
        h = self._load(HIDDEN_FILE, [])
        if not isinstance(h, list):
            return []
        return [n for n in h if isinstance(n, int)]

    def hidden_add(self, change: str, limit: int = 0) -> int:
        number = parse_change_ref(change)
        with _lock:
            hidden = self.load_hidden()
            if number not in hidden:
                if limit and len(hidden) >= limit:
                    raise ValueError(f"hidden list is full ({limit} entries)")
                hidden.append(number)
                self._save_locked(HIDDEN_FILE, hidden)
        return number

    def hidden_remove(self, number: int) -> bool:
        with _lock:
            hidden = self.load_hidden()
            if number not in hidden:
                return False
            hidden.remove(number)
            self._save_locked(HIDDEN_FILE, hidden)
        return True

    # -- watchlist ---------------------------------------------------------

    def load_watchlist(self) -> list[dict]:
        wl = self._load(WATCHLIST_FILE, [])
        if not isinstance(wl, list):
            return []
        # One hand-edited bad entry must not break every refresh.
        return [e for e in wl if isinstance(e, dict) and isinstance(e.get("number"), int)]

    def save_watchlist(self, entries: list[dict]) -> None:
        self._save(WATCHLIST_FILE, entries)

    def watchlist_add(self, change: str, note: str = "", limit: int = 0) -> dict:
        """Add a change (number or Gerrit URL) to the watchlist.

        Returns the new entry.  Raises ValueError on unparsable input,
        duplicates, or when the list is at its cap — every entry costs a
        Gerrit fetch on every refresh, so the list has to stay bounded.
        """
        number = parse_change_ref(change)
        with _lock:
            entries = self.load_watchlist()
            if any(e.get("number") == number for e in entries):
                raise ValueError(f"change {number} is already on the watchlist")
            if limit and len(entries) >= limit:
                raise ValueError(f"watchlist is full ({limit} entries)")
            entry = {
                "number": number,
                "note": note.strip()[:200],
                "added": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()),
            }
            entries.append(entry)
            self._save_locked(WATCHLIST_FILE, entries)
        return entry

    def watchlist_remove(self, number: int) -> bool:
        with _lock:
            entries = self.load_watchlist()
            kept = [e for e in entries if e.get("number") != number]
            if len(kept) == len(entries):
                return False
            self._save_locked(WATCHLIST_FILE, kept)
        return True


def summarize_snapshot(snapshot: dict) -> dict:
    """The handful of fields the board index needs from a snapshot."""
    return {
        "schema": snapshot.get("schema"),
        "state": "ok",
        "self": snapshot.get("self", {}),
        "generated_at": snapshot.get("generated_at", ""),
        "fetched_at": snapshot.get("fetched_at", 0),
        "kpis": snapshot.get("kpis", {}),
        "projects": snapshot.get("projects", []),
        "branches": [b.get("name") for b in snapshot.get("branches", [])],
    }


def list_boards(data_root: Path) -> list[dict]:
    """Every board that exists on disk, newest refresh first.

    A board directory with no usable snapshot yet (just created, or
    written by an older schema) is still listed, with summary None, so
    the index shows it as pending rather than hiding it.
    """
    users_dir = Path(data_root) / "users"
    if not users_dir.is_dir():
        return []
    boards = []
    for entry in sorted(users_dir.iterdir()):
        if not entry.is_dir() or not SAFE_USERNAME_RE.match(entry.name):
            continue
        try:
            summary = Store(entry).load_summary()
        except Exception:  # noqa: BLE001 - a broken board must not break the index
            summary = None
        boards.append({"username": entry.name, "summary": summary})
    boards.sort(key=lambda b: (b["summary"] or {}).get("fetched_at", 0), reverse=True)
    return boards


def user_store(data_root: Path, username: str, migrate_legacy: bool = False) -> Store:
    """Per-user store under <data_root>/users/<username>/.

    migrate_legacy moves the original single-user files from the data
    root into the user's directory (for the default/owner user only).

    The username must be a safe path component: an empty or
    path-navigating name would resolve to the users/ container (or above
    it) and, with migrate_legacy, strand the legacy files there.
    """
    if not SAFE_USERNAME_RE.match(username or ""):
        raise ValueError(f"invalid username for a data directory: {username!r}")
    udir = Path(data_root) / "users" / username
    store = Store(udir)
    if migrate_legacy:
        for name in (SNAPSHOT_FILE, ENRICH_CACHE_FILE, WATCHLIST_FILE, SETTINGS_FILE, HIDDEN_FILE):
            legacy = Path(data_root) / name
            target = udir / name
            if legacy.is_file() and not target.exists():
                try:
                    os.replace(legacy, target)
                except FileNotFoundError:
                    pass  # concurrent migrator won the race — idempotent
    return store


def parse_change_ref(ref: str) -> int:
    """Parse a change number from a bare number or any Gerrit change URL."""
    ref = str(ref).strip()
    if ref.isdigit():
        return int(ref)
    m = _CHANGE_URL_RE.search(ref.split("?")[0])
    if m:
        return int(m.group(1))
    raise ValueError(f"cannot parse change number from {ref!r}")
