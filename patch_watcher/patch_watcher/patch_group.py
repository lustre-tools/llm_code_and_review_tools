"""Declared sets of Gerrit changes that one agent is responsible for together.

A change rarely stands alone.  Fixing a review comment on one patch of a
series often means editing a different patch, and whether a patchset needs a
rebase is a question about the chain, not about any one link in it.  An agent
handed a single change cannot answer either question, because it cannot see
the others.

The set is DECLARED, never inferred.  Gerrit's own relation chain is a fact
about specific patchsets, not about changes: asking it about four changes that
were pushed as a series returned two of them at patchset 1 while their current
patchset was 2, because the newer patchsets had been rebased apart.  Inferring
the group from that would mean the group silently changing shape underneath a
run, and it would be wrong exactly when the rebase question matters most.  The
operator says which changes belong together; Gerrit says what state they are
in.

A group is one of two shapes, and they are not the same question.

A SERIES is stacked: the changes depend on each other, the order is the
operator's and is meaningful base-first, and rebasing one moves the ones above
it.  "Does this need a rebase" and "this fix belongs in the second patch, not
the third" are both questions about the order.

A FLOCK is merely related: several changes on one ticket, or the same bug in
different components, with no dependency between them.  There is no base and
no tip, and an agent told otherwise would invent an ordering it then reasons
from -- so a flock's members are held as a set, sorted for a stable
representation, and asking one for its base is an error rather than a guess.

A TICKET group is rooted in a JIRA issue, and its changes are DISCOVERED
rather than declared.  That is not a contradiction of the rule above: a
relation chain is an accident of which patchset was pushed on top of which,
and it drifts under a rebase, whereas the LU key in a commit subject is put
there on purpose and stays put.  Searching Gerrit for the key is therefore a
stable answer, not a guess.  The ticket may also have no changes yet, which is
a perfectly good thing to watch.

Membership is refreshed as patches appear, but a RUN pins the exact set it was
given, so a change landing mid-run cannot move the ground under it.

What all three share is that one agent is responsible for the whole set at
once.
"""

from __future__ import annotations

import contextlib
import dataclasses
import fcntl
import json
import os
import re
import tempfile
from collections.abc import Iterator, Sequence
from pathlib import Path

DEFAULT_GROUP_CONFIG = Path.home() / ".config" / "patch-watcher" / "patch-groups.json"
MAX_GROUP_MEMBERS = 32
MAX_LABEL_CHARS = 200
# "series": stacked, order meaningful, base first.
# "flock": related but independent, no order at all.
# "ticket": rooted in a JIRA issue; its changes are discovered from the key.
GROUP_KINDS = ("series", "flock", "ticket")
# A JIRA key: project, a dash, a number.  "LU-20724" and nothing looser, so a
# stray subject line or URL cannot become a group id.
TICKET_KEY_RE = re.compile(r"[A-Z][A-Z0-9_]{0,19}-[1-9][0-9]{0,9}")


class PatchGroupError(RuntimeError):
    """A group declaration is unusable."""


class PatchGroupConflict(PatchGroupError):
    """A concurrent write changed the group first."""


def _change_id(value: object) -> str:
    text = str(value).strip()
    if not text.isdigit() or int(text) <= 0:
        raise PatchGroupError(f"change number must be a positive integer: {value!r}")
    return str(int(text))


def _ticket_key(value: object) -> str:
    text = str(value or "").strip().upper()
    if not TICKET_KEY_RE.fullmatch(text):
        raise PatchGroupError(f"not a JIRA issue key: {value!r}")
    return text


def _group_id(value: object) -> str:
    """A group handle: a change number for a series or flock, a key for a ticket."""
    text = str(value or "").strip()
    if TICKET_KEY_RE.fullmatch(text.upper()):
        return text.upper()
    return _change_id(text)


def _members(values: Sequence[object], *, minimum: int) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise PatchGroupError("members must be a list of change numbers")
    seen: list[str] = []
    for value in values:
        change = _change_id(value)
        if change in seen:
            raise PatchGroupError(f"change {change} is listed twice in one group")
        seen.append(change)
    if len(seen) < minimum:
        raise PatchGroupError(f"a group of this kind needs at least {minimum} changes")
    if len(seen) > MAX_GROUP_MEMBERS:
        raise PatchGroupError(f"a group may hold at most {MAX_GROUP_MEMBERS} changes")
    return tuple(seen)


@dataclasses.dataclass(frozen=True)
class PatchGroup:
    """One declared set of changes handled together.

    A ``series`` keeps the operator's order, base first, because the order is
    a fact about the patches.  A ``flock`` is sorted, because any order it
    appeared to have would be an accident of how it was typed.
    """

    group_id: str
    members: tuple[str, ...] = ()
    kind: str = "series"
    ticket: str = ""
    label: str = ""
    version: int = 0

    def __post_init__(self) -> None:
        kind = str(self.kind or "").strip().casefold()
        if kind not in GROUP_KINDS:
            raise PatchGroupError(
                f"kind must be one of {', '.join(GROUP_KINDS)}: {self.kind!r}"
            )
        object.__setattr__(self, "kind", kind)
        if kind == "ticket":
            # The root is the issue, so that is the handle.  A ticket with no
            # patches yet is a perfectly good thing to watch, and the ones it
            # acquires are discovered, so no minimum applies.
            object.__setattr__(self, "ticket", _ticket_key(self.ticket or self.group_id))
            object.__setattr__(self, "group_id", self.ticket)
            members = tuple(sorted(_members(self.members, minimum=0), key=int))
        else:
            if self.ticket:
                raise PatchGroupError(
                    f"only a ticket group has a ticket; this one is a {kind}"
                )
            object.__setattr__(self, "group_id", _change_id(self.group_id))
            # Two is the floor: a declared group of one is just a patch.
            members = _members(self.members, minimum=2)
            if kind == "flock":
                members = tuple(sorted(members, key=int))
        object.__setattr__(self, "members", members)
        object.__setattr__(self, "label", str(self.label or "")[:MAX_LABEL_CHARS])
        if int(self.version) < 0:
            raise PatchGroupError("version must not be negative")
        object.__setattr__(self, "version", int(self.version))
        if self.kind != "ticket" and self.group_id not in self.members:
            raise PatchGroupError("a group's id must be one of its own changes")

    @property
    def ordered(self) -> bool:
        """Whether position in this group means anything."""
        return self.kind == "series"

    @property
    def discovered(self) -> bool:
        """Whether membership is found rather than declared, and so may grow."""
        return self.kind == "ticket"

    def with_members(self, members: Sequence[object]) -> PatchGroup:
        """The same group with a freshly discovered membership.

        Only a discovered group may be rewritten this way.  Silently replacing
        a declared series or flock would undo the operator's own statement of
        what belongs together, which is the one thing they asserted.
        """
        if not self.discovered:
            raise PatchGroupError(
                f"a {self.kind} group's membership is declared, not discovered"
            )
        return dataclasses.replace(self, members=tuple(members))

    def _require_series(self, what: str) -> None:
        if not self.ordered:
            raise PatchGroupError(
                f"a flock has no {what}: its changes do not depend on each other"
            )

    @property
    def base(self) -> str:
        """The change the rest are stacked on, as the operator declared it."""
        self._require_series("base")
        return self.members[0]

    @property
    def tip(self) -> str:
        """The last change in the declared order."""
        self._require_series("tip")
        return self.members[-1]

    def contains(self, change_number: object) -> bool:
        with contextlib.suppress(PatchGroupError):
            return _change_id(change_number) in self.members
        return False

    def position(self, change_number: object) -> int:
        """1-based position in a series, or 0 when not a member.

        Always 0 for a flock: there is no position to report, and returning a
        plausible-looking number is how an invented ordering gets reasoned
        from downstream.
        """
        if not self.ordered:
            return 0
        with contextlib.suppress(PatchGroupError):
            change = _change_id(change_number)
            if change in self.members:
                return self.members.index(change) + 1
        return 0

    def to_dict(self) -> dict[str, object]:
        return {
            "group_id": self.group_id,
            "members": list(self.members),
            "kind": self.kind,
            "ticket": self.ticket,
            "label": self.label,
            "version": self.version,
        }

    @classmethod
    def from_dict(cls, value: object) -> PatchGroup:
        if not isinstance(value, dict):
            raise PatchGroupError("a group must be an object")
        return cls(
            group_id=value.get("group_id", ""),
            members=value.get("members", ()),
            kind=value.get("kind", "series"),
            ticket=value.get("ticket", ""),
            label=value.get("label", ""),
            version=value.get("version", 0),
        )


class PatchGroupStore:
    """A private, atomic JSON store of declared groups.

    Deliberately the same shape as the standing-policy store: one small file,
    an exclusive lock around read-modify-write, and a version per record so a
    concurrent edit is refused rather than silently lost.
    """

    def __init__(self, path: str | Path = DEFAULT_GROUP_CONFIG) -> None:
        self.path = Path(path).expanduser()
        self.lock_path = self.path.with_name(self.path.name + ".lock")

    @contextlib.contextmanager
    def _locked(self, *, exclusive: bool) -> Iterator[None]:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.lock_path.touch(mode=0o600, exist_ok=True)
        with open(self.lock_path, "r+b") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def _read(self) -> dict[str, PatchGroup]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            raise PatchGroupError(f"group configuration is unreadable: {exc}") from exc
        groups = raw.get("groups") if isinstance(raw, dict) else None
        if not isinstance(groups, dict):
            return {}
        parsed: dict[str, PatchGroup] = {}
        for value in groups.values():
            with contextlib.suppress(PatchGroupError):
                group = PatchGroup.from_dict(value)
                parsed[group.group_id] = group
        return parsed

    def _write(self, groups: dict[str, PatchGroup]) -> None:
        payload = {
            "schema": "patch-watcher-patch-groups/v1",
            "groups": {key: group.to_dict() for key, group in sorted(groups.items())},
        }
        descriptor, temporary = tempfile.mkstemp(dir=str(self.path.parent))
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(temporary)
            raise

    def list(self) -> tuple[PatchGroup, ...]:
        with self._locked(exclusive=False):
            return tuple(sorted(self._read().values(), key=lambda item: item.group_id))

    def get(self, group_id: object) -> PatchGroup | None:
        with self._locked(exclusive=False):
            return self._read().get(_group_id(group_id))

    def for_change(self, change_number: object) -> PatchGroup | None:
        """The group this change belongs to, or None.

        A change belongs to at most one group, which `save` enforces, so this
        answer is unambiguous and callers need no tie-break.
        """
        with contextlib.suppress(PatchGroupError):
            change = _change_id(change_number)
            with self._locked(exclusive=False):
                for group in self._read().values():
                    if change in group.members:
                        return group
        return None

    def save(
        self, group: PatchGroup, *, expected_version: int | None = None
    ) -> PatchGroup:
        if not isinstance(group, PatchGroup):
            raise PatchGroupError("group must be a PatchGroup")
        with self._locked(exclusive=True):
            groups = self._read()
            current = groups.get(group.group_id)
            expected = (
                group.version if expected_version is None else int(expected_version)
            )
            if (current.version if current is not None else 0) != expected:
                raise PatchGroupConflict(
                    f"group {group.group_id} is version "
                    f"{current.version if current else 0}, expected {expected}"
                )
            # One change, one group.  Two groups sharing a change would mean
            # two agents each believing they own it, which is the invariant
            # every run's exclusive ownership rests on.
            for other in groups.values():
                if other.group_id == group.group_id:
                    continue
                overlap = sorted(set(other.members) & set(group.members))
                if overlap:
                    raise PatchGroupError(
                        f"change {overlap[0]} is already in group {other.group_id}"
                    )
            saved = dataclasses.replace(group, version=expected + 1)
            groups[saved.group_id] = saved
            self._write(groups)
            return saved

    def delete(self, group_id: object, *, expected_version: int | None = None) -> bool:
        target = _group_id(group_id)
        with self._locked(exclusive=True):
            groups = self._read()
            current = groups.get(target)
            if current is None:
                return False
            if expected_version is not None and current.version != int(expected_version):
                raise PatchGroupConflict(
                    f"group {target} is version {current.version}, "
                    f"expected {expected_version}"
                )
            del groups[target]
            self._write(groups)
            return True
