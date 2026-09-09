"""Private run directories and the pool of numbered Lustre checkouts.

An agent works in a checkout claimed from `CheckoutPool`; the checkout's index
is its VM ownership prefix (`co<N>-*`), which is how its VMs are attributed and
cleaned up.

This module deliberately holds no policy about *what* an agent may do. Those
instructions live in its prompt.

It also used to declare a `Workspace` protocol intended as the seam a container
backend would slot into. Nothing ever went through it -- every real process
launch goes through `claude_runner` -- so it was a claim, not a seam, and has
been removed. When containerization happens, the seam belongs where processes
are actually created: `claude_runner.build_read_only_claude_command` plus the
spawn in `ClaudeHost`, with this module supplying the directory to mount.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import sqlite3
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

HASH_PREFIX = "sha256:"
RUN_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
DEFAULT_POOL_CONFIG = Path.home() / ".config" / "patch-watcher" / "checkout-pool.json"
DEFAULT_POOL_DATABASE = (
    Path.home() / ".local" / "state" / "patch-watcher" / "checkout-pool.sqlite3"
)
CANDIDATE_POOL_ROOTS = (
    Path.home() / "lustre_checkouts" / "master_checkouts",
    Path.home() / "code_shared" / "master_checkouts",
)


class WorkspaceError(RuntimeError):
    """A run directory could not be created, or a path escaped its run."""


def hash_text(text: str) -> str:
    return HASH_PREFIX + hashlib.sha256(text.encode("utf-8")).hexdigest()


def validate_run_id(run_id: str) -> str:
    if not isinstance(run_id, str) or not RUN_ID_RE.fullmatch(run_id):
        raise WorkspaceError(f"invalid run id: {run_id!r}")
    return run_id


def _assert_beneath(target: Path, root: Path) -> None:
    """Resolve ``target`` and require it to sit under ``root``."""

    _assert_resolved_beneath(target.resolve(), root)


def _assert_resolved_beneath(resolved: Path, root: Path) -> None:
    """Check an ALREADY-resolved path.

    Callers that resolve first and then check must not hand an unresolved path
    to a checker that resolves again: the two resolutions can disagree if a
    component becomes a symlink in between, and the path that was validated is
    then not the path that gets used.
    """

    if not resolved.is_relative_to(root.resolve()):
        raise WorkspaceError(f"path escapes its run root: {resolved}")


@dataclass(frozen=True)
class RunDirectoryMap:
    """Logical-to-physical paths for one run.

    The logical names are stable across execution backends: a directory run
    resolves them under a private host root, and a future container run mounts
    them at the same logical locations.  Callers name paths logically so that
    stays true.
    """

    run_id: str
    root: Path
    logical_paths: Mapping[str, Path]

    def resolve(self, logical_path: str) -> Path:
        pure = PurePosixPath(logical_path)
        if not pure.is_absolute() or ".." in pure.parts or str(pure) != logical_path:
            raise WorkspaceError(f"logical path must be absolute and normalized: {logical_path}")
        for prefix in sorted(self.logical_paths, key=len, reverse=True):
            if logical_path == prefix or logical_path.startswith(prefix.rstrip("/") + "/"):
                relative = PurePosixPath(logical_path).relative_to(PurePosixPath(prefix))
                target = self.logical_paths[prefix].joinpath(*relative.parts)
                # Resolve ONCE, check that, return that. Resolving separately
                # inside the check and again for the return value meant the
                # path handed back was not the path that had been validated: a
                # concurrent symlink swap between the two calls returned a path
                # outside the run root having passed the check. Callers chmod
                # and write to what they get back.
                resolved = target.resolve()
                _assert_resolved_beneath(resolved, self.root)
                return resolved
        raise WorkspaceError(f"path is not declared by this run: {logical_path}")

    def to_dict(self) -> dict[str, str]:
        return {logical: str(path) for logical, path in sorted(self.logical_paths.items())}


def create_run_directories(base_directory: Path, run_id: str) -> RunDirectoryMap:
    """Create the private physical layout for one run."""

    safe_run_id = validate_run_id(run_id)
    base = Path(base_directory).expanduser().resolve()
    base.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(base, 0o700)
    root = (base / safe_run_id).resolve()
    _assert_beneath(root, base)
    root.mkdir(mode=0o700, exist_ok=False)

    directory_map = {
        "/work/source": root / "work" / "source",
        "/work/input": root / "work" / "input",
        "/work/scratch": root / "work" / "scratch",
        "/work/output": root / "work" / "output",
        "/work/output/artifacts": root / "work" / "output" / "artifacts",
        "/work/output/logs": root / "work" / "output" / "logs",
    }
    for directory in sorted(set(directory_map.values()), key=lambda path: len(path.parts)):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(directory, 0o700)
    return RunDirectoryMap(run_id=safe_run_id, root=root, logical_paths=directory_map)


@dataclass(frozen=True)
class Checkout:
    """One numbered checkout from the pool.

    The index is the ownership model: an agent holding checkout N names its
    VMs ``co<N>-<role>``, which is already the mandatory convention in
    CLAUDE.md.  Attribution, cleanup, and collision avoidance all follow from
    the name prefix rather than from a separate owner token.
    """

    index: int
    path: Path

    @property
    def vm_prefix(self) -> str:
        return f"co{self.index}-"

    def owns_vm(self, vm_name: str) -> bool:
        return str(vm_name).startswith(self.vm_prefix)


class CheckoutPoolError(RuntimeError):
    """The pool is misconfigured, exhausted, or double-allocated."""


def discover_pool_root() -> Path | None:
    """Return the checkouts root, honoring $CO when it is set."""

    configured = os.environ.get("CO")
    candidates = ([Path(configured)] if configured else []) + list(CANDIDATE_POOL_ROOTS)
    for candidate in candidates:
        # $CO conventionally points at a checkout root whose children are
        # numbered; accept either the root itself or a numbered child's parent.
        expanded = candidate.expanduser()
        if expanded.is_dir():
            return expanded.resolve()
    return None


class CheckoutPool:
    """Durable allocation of numbered checkouts to sessions.

    Membership is deliberately explicit rather than "every numbered directory
    under the root": a developer is usually working in one of those checkouts,
    and an agent must never be handed it.  An unconfigured pool is empty, so
    the failure mode is "no agent can start" rather than "an agent clobbered
    your working tree".
    """

    def __init__(
        self,
        root: Path,
        indices: Sequence[int],
        *,
        database: Path = DEFAULT_POOL_DATABASE,
    ) -> None:
        self.root = Path(root).expanduser().resolve()
        self.indices = tuple(sorted({int(index) for index in indices}))
        self.database = Path(database).expanduser()
        self.database.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._migrate()

    @staticmethod
    def parse_config(
        path: Path = DEFAULT_POOL_CONFIG,
    ) -> tuple[Path, tuple[int, ...]]:
        """Read the pool declaration without touching any database.

        Constructing a `CheckoutPool` creates the state directory and
        checkout-pool.sqlite3, which is why `pw-doctor` -- whose docstring
        promises every check is read-only -- could not honestly inspect the
        pool. Splitting the parse out lets a reader ask what is declared
        without becoming a writer, and keeps `from_config` the single place
        that knows the file format.

        A missing file yields an empty pool rather than an error: the tool is
        usable for watching and reporting before any agent may run.
        """

        path = Path(path).expanduser()
        if not path.exists():
            root = discover_pool_root()
            if root is None:
                raise CheckoutPoolError(
                    "no checkout root found; set $CO or write " + str(path)
                )
            return root, ()
        try:
            declared = json.loads(path.read_text())
        except (OSError, ValueError) as exc:
            raise CheckoutPoolError(f"invalid pool configuration: {exc}") from exc
        if not isinstance(declared, Mapping):
            raise CheckoutPoolError("pool configuration must be an object")
        raw_root = declared.get("root")
        root = Path(raw_root).expanduser() if raw_root else discover_pool_root()
        if root is None:
            raise CheckoutPoolError("pool configuration declares no usable root")
        indices = declared.get("checkouts") or ()
        if not isinstance(indices, Sequence) or isinstance(indices, (str, bytes)):
            raise CheckoutPoolError("pool 'checkouts' must be a list of integers")
        try:
            parsed = tuple(int(index) for index in indices)
        except (TypeError, ValueError) as exc:
            raise CheckoutPoolError("pool 'checkouts' must be integers") from exc
        return root, parsed

    @classmethod
    def from_config(
        cls, path: Path = DEFAULT_POOL_CONFIG, *, database: Path = DEFAULT_POOL_DATABASE
    ) -> CheckoutPool:
        """Load the operator's explicit pool declaration."""

        root, indices = cls.parse_config(path)
        return cls(root, indices, database=database)

    @contextlib.contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database, isolation_level=None, timeout=30)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA foreign_keys=ON")
            yield connection
        finally:
            connection.close()

    def _migrate(self) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS pw_checkout_allocation (
                    checkout_index INTEGER PRIMARY KEY,
                    owner TEXT NOT NULL UNIQUE,
                    claimed_at TEXT NOT NULL
                )
                """
            )
        os.chmod(self.database, 0o600)

    def checkout(self, index: int) -> Checkout:
        if index not in self.indices:
            raise CheckoutPoolError(f"checkout {index} is not in the pool")
        return Checkout(index=index, path=self.root / str(index))

    def allocations(self) -> dict[int, str]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT checkout_index, owner FROM pw_checkout_allocation"
            ).fetchall()
        return {row["checkout_index"]: row["owner"] for row in rows}

    def allocation_for(self, owner: str) -> Checkout | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT checkout_index FROM pw_checkout_allocation WHERE owner = ?",
                (str(owner),),
            ).fetchone()
        return self.checkout(row["checkout_index"]) if row is not None else None

    def free(self) -> tuple[int, ...]:
        claimed = set(self.allocations())
        return tuple(index for index in self.indices if index not in claimed)

    def allocate(self, owner: str) -> Checkout:
        """Claim one checkout for ``owner``, or raise when the pool is full.

        Re-allocating for an owner that already holds one returns the same
        checkout, so a retried start cannot consume two.
        """

        owner = str(owner)
        if not owner:
            raise CheckoutPoolError("an allocation needs a non-empty owner")
        existing = self.allocation_for(owner)
        if existing is not None:
            return existing
        for index in self.indices:
            path = self.root / str(index)
            if not path.is_dir():
                continue
            try:
                with self._connection() as connection:
                    connection.execute(
                        "INSERT INTO pw_checkout_allocation "
                        "(checkout_index, owner, claimed_at) "
                        "VALUES (?, ?, datetime('now'))",
                        (index, owner),
                    )
            except sqlite3.IntegrityError:
                # Two different constraints land here and mean opposite things.
                # A checkout_index collision means someone else won this index,
                # so try the next. An owner collision means this owner already
                # holds one -- racing itself from another process -- and
                # walking on would exhaust the pool and report a false
                # "no free checkout" for a request that should have succeeded.
                existing = self.allocation_for(owner)
                if existing is not None:
                    return existing
                continue
            return Checkout(index=index, path=path)
        raise CheckoutPoolError("no free checkout in the pool")

    def release(self, owner: str) -> None:
        with self._connection() as connection:
            connection.execute(
                "DELETE FROM pw_checkout_allocation WHERE owner = ?", (str(owner),)
            )


__all__ = [
    "CANDIDATE_POOL_ROOTS",
    "Checkout",
    "CheckoutPool",
    "CheckoutPoolError",
    "RunDirectoryMap",
    "WorkspaceError",
    "create_run_directories",
    "discover_pool_root",
    "hash_text",
    "validate_run_id",
]
