"""Exact, read-only Gerrit source preparation for Patch Watcher runs."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import subprocess
from typing import Callable, Sequence


_PROJECT_RE = re.compile(r"^[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*$")
_REVISION_RE = re.compile(r"^[0-9a-f]{40,64}$")
_REF_RE = re.compile(r"^refs/changes/[0-9]{2}/[0-9]+/[0-9]+$")


class CheckoutError(RuntimeError):
    """A revision-pinned source checkout could not be prepared safely."""


@dataclass(frozen=True)
class GerritRevision:
    change_number: int
    project: str
    patchset: int
    revision_sha: str
    revision_ref: str

    def __post_init__(self) -> None:
        if isinstance(self.change_number, bool) or self.change_number <= 0:
            raise ValueError("change_number must be positive")
        if isinstance(self.patchset, bool) or self.patchset <= 0:
            raise ValueError("patchset must be positive")
        if not _PROJECT_RE.fullmatch(self.project) or ".." in self.project.split("/"):
            raise ValueError("project is not a safe Gerrit project path")
        if not _REVISION_RE.fullmatch(self.revision_sha):
            raise ValueError("revision_sha must be a 40-64 digit lowercase hex digest")
        if not _REF_RE.fullmatch(self.revision_ref):
            raise ValueError("revision_ref is not a Gerrit patchset ref")
        expected_suffix = f"/{self.change_number}/{self.patchset}"
        if not self.revision_ref.endswith(expected_suffix):
            raise ValueError("revision_ref does not match change_number and patchset")

    @property
    def repository_url(self) -> str:
        return f"https://review.whamcloud.com/{self.project}"


Runner = Callable[..., subprocess.CompletedProcess]


def _run(
    command: Sequence[str],
    *,
    runner: Runner,
    timeout: int = 120,
) -> subprocess.CompletedProcess:
    try:
        result = runner(
            list(command),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise CheckoutError(f"source preparation failed: {type(exc).__name__}") from exc
    if result.returncode:
        # Git output can contain credential-bearing URLs. Preserve only the
        # command stage and exit status, never arbitrary stderr.
        stage = command[0] if len(command) == 1 else " ".join(command[:2])
        raise CheckoutError(
            f"source preparation command {stage!r} exited with status {result.returncode}"
        )
    return result


def prepare_revision_checkout(
    destination: Path,
    revision: GerritRevision,
    *,
    runner: Runner = subprocess.run,
) -> Path:
    """Create one private detached checkout at exactly ``revision``.

    The controller performs only Git reads against the fixed Whamcloud host.
    It never invokes a shell, consults a user's global Git configuration, or
    checks out a branch whose target can move after admission.
    """

    target = Path(destination).resolve()
    if not target.is_dir():
        raise CheckoutError("destination must be a pre-created directory")
    if any(target.iterdir()):
        raise CheckoutError("destination must be empty")

    common = [
        "git",
        "-c", "credential.helper=",
        "-c", "core.hooksPath=/dev/null",
        "-c", "protocol.file.allow=never",
    ]
    _run([*common, "init", "--quiet", str(target)], runner=runner)
    _run(
        [
            *common,
            "-C", str(target),
            "fetch", "--quiet", "--depth=1", "--no-tags",
            revision.repository_url, revision.revision_ref,
        ],
        runner=runner,
    )
    _run(
        [*common, "-C", str(target), "checkout", "--detach", "--quiet", revision.revision_sha],
        runner=runner,
    )
    head = _run(
        [*common, "-C", str(target), "rev-parse", "HEAD"],
        runner=runner,
    ).stdout.decode("utf-8", errors="replace").strip()
    if head != revision.revision_sha:
        raise CheckoutError("prepared checkout does not match the pinned revision")
    dirty = _run(
        [*common, "-C", str(target), "status", "--porcelain", "--untracked-files=all"],
        runner=runner,
    ).stdout.decode("utf-8", errors="replace").strip()
    if dirty:
        raise CheckoutError("prepared checkout is not initially clean")
    return target


__all__ = ["CheckoutError", "GerritRevision", "prepare_revision_checkout"]
