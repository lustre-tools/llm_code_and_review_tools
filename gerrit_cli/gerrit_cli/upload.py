"""Upload commits to Gerrit over HTTPS, as the selected account.

`gerrit push` posts staged comment replies; this is the commit upload.
It exists because hand-built `git push` commands kept getting four
things wrong: pushing as the operator's SSH identity instead of the
account the credential set names, putting an HTTP password containing
'/' into a URL, leaving a committer email Gerrit does not accept from
that account, and pushing a HEAD whose Change-Id belongs to some other
change.

By default exactly one commit, HEAD, is uploaded.  --series uploads
every commit between the target branch and HEAD, each to the change its
own Change-Id names.
"""

import os
import re
import shlex
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote, unquote, urlsplit, urlunsplit

from .errors import ErrorCode, ExitCode

# Base URL git pushes to; the project is appended.  Defaults to
# $GERRIT_URL/a, Gerrit's authenticated HTTP path.
PUSH_URL_VAR = "GERRIT_PUSH_URL"

# The askpass script reads the credential from these, so it never appears
# in a URL, in argv or in the script file itself.
ASKPASS_USER_VAR = "GERRIT_UPLOAD_USER"
ASKPASS_PASS_VAR = "GERRIT_UPLOAD_PASS"

_ASKPASS_SCRIPT = f"""#!/bin/sh
case "$1" in
Username*) printf '%s\\n' "${ASKPASS_USER_VAR}" ;;
*) printf '%s\\n' "${ASKPASS_PASS_VAR}" ;;
esac
"""

REDACTED = "<redacted>"

CHANGE_ID_FORMAT = re.compile(r"I[0-9a-f]{40}")

# A range this long is a wrong base or a wrong branch, not a series:
# Gerrit would open a change for every commit in it.
MAX_COMMITS = 50

_KNOWN_SHA_BATCH = 10


class UploadError(Exception):
    """A refusal or failure, carrying what the JSON error needs."""

    def __init__(
        self,
        code: str,
        message: str,
        details: dict[str, Any] | None = None,
        exit_code: int = ExitCode.GENERAL_ERROR,
    ):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}
        self.exit_code = exit_code


@dataclass
class Commit:
    sha: str
    parents: list[str]
    subject: str
    committer_name: str
    committer_email: str
    change_ids: list[str] = field(default_factory=list)

    @property
    def committer(self) -> str:
        return f"{self.committer_name} <{self.committer_email}>"

    @property
    def change_id(self) -> str | None:
        """The Change-Id trailer, None if there is none.

        Raises:
            UploadError: for several different trailers or a malformed
                one, both of which Gerrit refuses.
        """
        if not self.change_ids:
            return None
        if len(set(self.change_ids)) > 1:
            raise UploadError(
                ErrorCode.MULTIPLE_CHANGE_IDS,
                f"Commit {self.sha[:12]} '{self.subject}' has "
                f"{len(self.change_ids)} Change-Id trailers "
                f"({', '.join(self.change_ids)}). Gerrit refuses a commit "
                "like that; keep the one that belongs to its change.",
            )
        change_id = self.change_ids[0]
        if not CHANGE_ID_FORMAT.fullmatch(change_id):
            raise UploadError(
                ErrorCode.INVALID_CHANGE_ID,
                f"Commit {self.sha[:12]} '{self.subject}' has a malformed "
                f"Change-Id '{change_id}'; Gerrit expects 'I' followed by "
                "40 lowercase hex digits.",
            )
        return change_id

    def label(self) -> str:
        return (
            f"{self.sha[:12]} '{self.subject}' "
            f"({self.change_ids[-1] if self.change_ids else 'no Change-Id'})"
        )


@dataclass
class Target:
    project: str
    branch: str
    change_id: str
    change_number: int
    subject: str = ""
    current_patchset: int | None = None
    revisions: dict[str, Any] = field(default_factory=dict)


@dataclass
class Account:
    username: str
    name: str
    preferred_email: str | None
    emails: set[str]


# --------------------------------------------------------------------------
# git
# --------------------------------------------------------------------------

def _git(
    repo: str,
    args: list[str],
    env: dict[str, str] | None = None,
    check: bool = True,
    stdin: bytes | None = None,
) -> subprocess.CompletedProcess:
    try:
        result = subprocess.run(
            ["git", "-C", repo, *args],
            capture_output=True,
            env=env,
            input=stdin,
            timeout=600,
        )
    except FileNotFoundError as e:
        raise UploadError(ErrorCode.GIT_ERROR, "git is not installed") from e
    except subprocess.TimeoutExpired as e:
        raise UploadError(
            ErrorCode.GIT_ERROR, f"git {args[0]} timed out after {e.timeout}s"
        ) from e
    if check and result.returncode != 0:
        raise UploadError(
            ErrorCode.GIT_ERROR,
            f"git {' '.join(args)} failed: {_text(result.stderr).strip()}",
        )
    return result


def _text(data: bytes) -> str:
    return data.decode("utf-8", errors="replace")


def _out(repo: str, args: list[str], **kwargs: Any) -> str:
    return _text(_git(repo, args, **kwargs).stdout).strip()


def _toplevel(repo: str) -> str:
    result = _git(repo, ["rev-parse", "--show-toplevel"], check=False)
    if result.returncode != 0:
        raise UploadError(
            ErrorCode.INVALID_INPUT,
            f"{os.path.abspath(repo)} is not inside a git work tree",
            exit_code=ExitCode.INVALID_INPUT,
        )
    return _text(result.stdout).strip()


# One record per commit; fields NUL-separated, records RS-terminated.
_LOG_FORMAT = (
    "%H%x00%P%x00%s%x00%cn%x00%ce%x00"
    "%(trailers:key=Change-Id,valueonly,separator=%x01)%x1e"
)


def _read_commits(repo: str, revs: list[str]) -> list[Commit]:
    """Commits named by a rev-list expression, parents before children."""
    raw = _out(repo, [
        "log", "--reverse", "--topo-order", f"--format={_LOG_FORMAT}", *revs,
    ])
    commits = []
    for record in raw.split("\x1e"):
        record = record.strip("\n")
        if not record:
            continue
        sha, parents, subject, cname, cemail, ids = record.split("\x00")
        commits.append(Commit(
            sha=sha,
            parents=parents.split(),
            subject=subject,
            committer_name=cname,
            committer_email=cemail,
            change_ids=[i.strip() for i in ids.split("\x01") if i.strip()],
        ))
    return commits


def _recommit(
    repo: str, sha: str, parent_map: dict[str, str], name: str, email: str
) -> str:
    """Copy a commit with a new committer and, if remapped, new parents.

    Author, tree and message are carried over byte for byte.  Built with
    commit-tree rather than `commit --amend`, which would also take
    whatever is staged in the index.
    """
    raw = _git(repo, ["cat-file", "commit", sha]).stdout
    header, _, message = raw.partition(b"\n\n")
    tree = None
    parents: list[str] = []
    author = None
    encoding = None
    for line in _text(header).split("\n"):
        key, _, value = line.partition(" ")
        if key == "tree":
            tree = value
        elif key == "parent":
            parents.append(parent_map.get(value, value))
        elif key == "author":
            author = value
        elif key == "encoding":
            encoding = value
    match = re.fullmatch(r"(.*) <(.*)> (\d+) ([+-]\d{4})", author or "")
    if not tree or not match:
        raise UploadError(
            ErrorCode.GIT_ERROR, f"Cannot parse commit {sha} to rewrite it"
        )

    env = dict(os.environ)
    env.update({
        "GIT_AUTHOR_NAME": match.group(1),
        "GIT_AUTHOR_EMAIL": match.group(2),
        "GIT_AUTHOR_DATE": f"@{match.group(3)} {match.group(4)}",
        "GIT_COMMITTER_NAME": name,
        "GIT_COMMITTER_EMAIL": email,
    })
    env.pop("GIT_COMMITTER_DATE", None)
    args = []
    if encoding:
        args += ["-c", f"i18n.commitEncoding={encoding}"]
    args += ["commit-tree", tree]
    for parent in parents:
        args += ["-p", parent]
    return _text(_git(repo, args, env=env, stdin=message).stdout).strip()


def _rewrite_chain(
    repo: str, commits: list[Commit], head: str, name: str, email: str
) -> dict[str, str]:
    """Recommit these (parents first) and move HEAD to the new tip."""
    mapping: dict[str, str] = {}
    for commit in commits:
        mapping[commit.sha] = _recommit(repo, commit.sha, mapping, name, email)
    # Compare-and-swap, so a HEAD that moved since it was read is not
    # silently replaced.
    _git(repo, [
        "update-ref", "-m", f"gerrit upload: committer {email}",
        "HEAD", mapping[head], head,
    ])
    return mapping


# --------------------------------------------------------------------------
# Transport
# --------------------------------------------------------------------------

def push_base_url(gerrit_url: str) -> str:
    override = os.environ.get(PUSH_URL_VAR)
    if override:
        return override.rstrip("/")
    return gerrit_url.rstrip("/") + "/a"


def push_url(gerrit_url: str, project: str, username: str) -> str:
    """The URL git pushes to, naming the account but never its password."""
    url = f"{push_base_url(gerrit_url)}/{project}"
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        return url
    if parts.password is not None:
        raise UploadError(
            ErrorCode.CONFIG_ERROR,
            f"{PUSH_URL_VAR} carries a password. Remove it; upload supplies "
            "the credential set's password through GIT_ASKPASS.",
            exit_code=ExitCode.INVALID_INPUT,
        )
    if parts.username is not None:
        if unquote(parts.username) != username:
            raise UploadError(
                ErrorCode.CONFIG_ERROR,
                f"{PUSH_URL_VAR} names user '{unquote(parts.username)}' but "
                f"the credential set is '{username}'.",
                exit_code=ExitCode.INVALID_INPUT,
            )
        return url
    netloc = f"{quote(username, safe='')}@{parts.netloc}"
    return urlunsplit(parts._replace(netloc=netloc))


def _check_url_rewrites(repo: str, url: str) -> None:
    """Refuse when git config would send this URL somewhere else.

    A url.<base>.pushInsteadOf pointing Gerrit at an ssh:// remote is a
    common operator setup, and it would push as the operator.
    """
    result = _git(
        repo,
        ["config", "--get-regexp", r"^url\..*\.(pushinsteadof|insteadof)$"],
        check=False,
    )
    for line in _text(result.stdout).splitlines():
        key, _, prefix = line.partition(" ")
        if prefix and url.startswith(prefix):
            base = key[len("url."):].rsplit(".", 1)[0]
            raise UploadError(
                ErrorCode.CONFIG_ERROR,
                f"git config {key}={prefix} rewrites the upload URL {url} "
                f"to {base}{url[len(prefix):]}, which would not push over "
                "HTTPS as the selected account. Remove that rule for this "
                "repository or push from a repository without it.",
            )


class _Askpass:
    """A GIT_ASKPASS script that answers from the environment."""

    def __init__(self, username: str, password: str):
        self.username = username
        self.password = password
        self.dir: str | None = None
        self.script = ""

    def __enter__(self) -> "_Askpass":
        self.dir = tempfile.mkdtemp(prefix="gerrit-upload-")
        self.script = os.path.join(self.dir, "askpass")
        with open(self.script, "w") as f:
            f.write(_ASKPASS_SCRIPT)
        os.chmod(self.script, 0o700)
        return self

    def __exit__(self, *exc: Any) -> None:
        if self.dir:
            shutil.rmtree(self.dir, ignore_errors=True)

    def env(self) -> dict[str, str]:
        env = dict(os.environ)
        env.update(self.overrides(redact=False))
        return env

    def overrides(self, redact: bool) -> dict[str, str]:
        return {
            "GIT_ASKPASS": self.script if not redact else
            f"<temporary script printing ${ASKPASS_USER_VAR} / "
            f"${ASKPASS_PASS_VAR}>",
            "GIT_TERMINAL_PROMPT": "0",
            ASKPASS_USER_VAR: self.username,
            ASKPASS_PASS_VAR: REDACTED if redact else self.password,
        }


# An empty credential.helper clears every configured helper, so the
# operator's stored login can neither answer for this account nor be
# overwritten with its password.
_GIT_AUTH_ARGS = ["-c", "credential.helper="]


def _transport_error(stderr: str, url: str, username: str) -> UploadError:
    lowered = stderr.lower()
    if any(s in lowered for s in (
        "authentication failed", "401", "could not read username",
        "could not read password", "invalid username or password",
    )):
        return UploadError(
            ErrorCode.AUTH_FAILED,
            f"Gerrit refused the HTTP password of '{username}' at {url}. "
            "Check GERRIT_PASS in the credential set (Gerrit: Settings > "
            "HTTP Credentials).",
            details={"git_stderr": stderr.strip()},
            exit_code=ExitCode.AUTH_ERROR,
        )
    if any(s in lowered for s in (
        "not found", "does not appear to be a git repository",
        "repository not found",
    )):
        return UploadError(
            ErrorCode.NOT_FOUND,
            f"No git repository at {url}",
            details={"git_stderr": stderr.strip()},
        )
    return UploadError(
        ErrorCode.GIT_ERROR,
        f"git could not reach {url}: {stderr.strip()}",
        details={"git_stderr": stderr.strip()},
    )


def _branch_tip(
    repo: str, url: str, branch: str, askpass: _Askpass
) -> str:
    """The server's tip of the target branch, fetched if not local."""
    ref = f"refs/heads/{branch}"
    result = _git(
        repo, [*_GIT_AUTH_ARGS, "ls-remote", url, ref],
        env=askpass.env(), check=False,
    )
    if result.returncode != 0:
        raise _transport_error(_text(result.stderr), url, askpass.username)
    tip = None
    for line in _text(result.stdout).splitlines():
        sha, _, name = line.partition("\t")
        if name == ref:
            tip = sha
    if tip is None:
        raise UploadError(
            ErrorCode.NOT_FOUND,
            f"Branch '{branch}' does not exist at {url}",
        )

    present = _git(
        repo, ["cat-file", "-e", f"{tip}^{{commit}}"], check=False
    )
    if present.returncode != 0:
        fetched = _git(
            repo,
            [*_GIT_AUTH_ARGS, "fetch", "--no-write-fetch-head", "--no-tags",
             "--quiet", url, ref],
            env=askpass.env(), check=False,
        )
        if fetched.returncode != 0:
            raise _transport_error(_text(fetched.stderr), url, askpass.username)
    return tip


# --------------------------------------------------------------------------
# Gerrit
# --------------------------------------------------------------------------

def _http_status(error: Exception) -> int | None:
    response = getattr(error, "response", None)
    return getattr(response, "status_code", None)


def _rest(what: str, call: Any, *args: Any, **kwargs: Any) -> Any:
    try:
        return call(*args, **kwargs)
    except UploadError:
        raise
    except Exception as e:
        status = _http_status(e)
        if status in (401, 403):
            raise UploadError(
                ErrorCode.AUTH_FAILED,
                f"Gerrit refused the credentials while {what} "
                f"(HTTP {status}). Check GERRIT_USER / GERRIT_PASS in the "
                "credential set.",
                exit_code=ExitCode.AUTH_ERROR,
            ) from e
        raise UploadError(
            ErrorCode.API_ERROR, f"Gerrit REST call failed while {what}: {e}"
        ) from e


def _account(client: Any) -> Account:
    me = _rest("reading the account", client.get_self_account)
    emails = _rest("reading the account's emails", client.get_self_emails)
    registered = {
        e["email"] for e in emails or []
        if e.get("email") and not e.get("pending_confirmation")
    }
    preferred = next(
        (e["email"] for e in emails or []
         if e.get("preferred") and e.get("email")),
        me.get("email"),
    )
    if me.get("email"):
        registered.add(me["email"])
    return Account(
        username=me.get("username") or client.username,
        name=me.get("name") or me.get("display_name")
        or me.get("username") or client.username,
        preferred_email=preferred,
        emails=registered,
    )


def _describe(change: dict[str, Any]) -> str:
    return (
        f"{change.get('_number')} ({change.get('project')} "
        f"{change.get('branch')}, {change.get('status')})"
    )


def _find_change(
    client: Any,
    change_id: str,
    project: str | None,
    branch: str | None,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """The change a Change-Id names, narrowed by project and branch.

    Returns the match (None if there is none) and every change carrying
    the Change-Id, for the error that has to say where it does exist.
    """
    matches = _rest(
        f"looking up Change-Id {change_id}", client.search_changes,
        f"change:{change_id}", limit=25, options=[],
    ) or []
    candidates = [
        m for m in matches
        if (not project or m.get("project") == project)
        and (not branch or m.get("branch") == branch)
    ]
    if not candidates:
        return None, matches
    if len(candidates) == 1:
        return candidates[0], matches
    open_changes = [m for m in candidates if m.get("status") == "NEW"]
    if len(open_changes) == 1:
        return open_changes[0], matches
    raise UploadError(
        ErrorCode.AMBIGUOUS_CHANGE,
        f"Change-Id {change_id} matches {len(candidates)} changes: "
        f"{', '.join(_describe(m) for m in candidates)}. Name the change "
        "number, or narrow it with --branch / --project.",
        details={"matches": [_describe(m) for m in candidates]},
    )


def _closed(change: dict[str, Any], commit: Commit | None = None) -> None:
    """Refuse a change Gerrit takes no patchsets on."""
    status = change.get("status")
    if status not in ("MERGED", "ABANDONED"):
        return
    number = change.get("_number")
    what = f"Change {number}"
    if commit is not None:
        what = f"Commit {commit.label()} belongs to change {number}, which"
    hint = (
        f"Restore it first: gerrit restore {number}"
        if status == "ABANDONED" else
        "To upload as a new change on another branch, name no change and "
        "pass --project and --branch."
    )
    raise UploadError(
        ErrorCode.CHANGE_CLOSED,
        f"{what} is {status}; Gerrit accepts no new patchsets on it. {hint}",
    )


def _load_target(client: Any, number: int) -> Target:
    detail = _rest(
        f"reading change {number}", client.get_change, number,
        ["CURRENT_REVISION", "ALL_REVISIONS"],
    )
    _closed(detail)
    revisions = detail.get("revisions") or {}
    current = detail.get("current_revision")
    return Target(
        project=detail.get("project", ""),
        branch=detail.get("branch", ""),
        change_id=detail.get("change_id", ""),
        change_number=int(detail.get("_number", number)),
        subject=detail.get("subject", ""),
        current_patchset=(revisions.get(current) or {}).get("_number"),
        revisions=revisions,
    )


def _same_server(a: str, b: str) -> bool:
    return (urlsplit(a).hostname or "").lower() == (
        urlsplit(b).hostname or ""
    ).lower()


def _resolve_named(
    client: Any, change: str, project: str | None, branch: str | None
) -> int:
    """The change number CHANGE names: a number, a URL or a Change-Id."""
    from .client import GerritCommentsClient

    text = change.strip()
    change_id = GerritCommentsClient.extract_change_id(text)
    if change_id is None:
        try:
            base, number = GerritCommentsClient.parse_gerrit_url(
                text, client.url
            )
        except ValueError as e:
            raise UploadError(
                ErrorCode.INVALID_INPUT, str(e),
                exit_code=ExitCode.INVALID_INPUT,
            ) from e
        if not _same_server(base, client.url):
            raise UploadError(
                ErrorCode.INVALID_INPUT,
                f"{text} is on {base}, but the credential set is for "
                f"{client.url}. Select the matching set with --user.",
                exit_code=ExitCode.INVALID_INPUT,
            )
        return number

    picked, _ = _find_change(client, change_id, project, branch)
    if picked is None:
        raise UploadError(
            ErrorCode.CHANGE_NOT_FOUND,
            f"No change on {client.url} has Change-Id {change_id}"
            + (f" in {project}" if project else "")
            + (f" on {branch}" if branch else ""),
            exit_code=ExitCode.NOT_FOUND,
        )
    return int(picked["_number"])


def _owner_of(client: Any, change_id: str) -> str | None:
    """Best effort: which change a stray Change-Id belongs to."""
    try:
        matches = client.search_changes(
            f"change:{change_id}", limit=5, options=[]
        )
    except Exception:
        return None
    if not matches:
        return None
    return ", ".join(
        f"change {m.get('_number')} ('{m.get('subject', '')}', "
        f"{m.get('branch')})"
        for m in matches
    )


def _head_mismatch(
    client: Any, repo: str, head: Commit, head_change_id: str | None,
    named: Target,
) -> UploadError:
    """The refusal for a HEAD that is not the named change."""
    if head_change_id is None:
        problem = "HEAD has no Change-Id trailer"
        consequence = "Gerrit would not add it to that change"
    else:
        problem = f"HEAD carries Change-Id {head_change_id}"
        owner = _owner_of(client, head_change_id)
        consequence = (
            f"HEAD's Change-Id belongs to {owner}, so pushing would upload "
            "this diff as a patchset of that change"
            if owner else
            "Gerrit would open a new change instead of adding a patchset"
        )
    fix = (
        "Fix HEAD's commit message so its Change-Id trailer is the "
        "change's."
    )
    ancestors = _read_commits(repo, [f"-{MAX_COMMITS}", head.sha])
    for depth, commit in enumerate(reversed(ancestors)):
        if depth and named.change_id in commit.change_ids:
            fix = (
                f"Change {named.change_number} is HEAD~{depth}, "
                f"{commit.label()}. To upload it together with the "
                "commits above it, pass --series; each goes to its own "
                "change."
            )
            break
    return UploadError(
        ErrorCode.CHANGE_ID_MISMATCH,
        f"Refusing to upload: {problem} (commit {head.sha[:12]} "
        f"'{head.subject}'), but change {named.change_number} "
        f"('{named.subject}') has Change-Id {named.change_id}; "
        f"{consequence}. Nothing was pushed. {fix}",
        details={
            "head": head.sha,
            "head_change_id": head_change_id,
            "change_number": named.change_number,
            "change_change_id": named.change_id,
        },
    )


def _known_on_gerrit(client: Any, shas: list[str]) -> set[str]:
    """Which of these commits Gerrit already has as a patchset."""
    known: set[str] = set()
    for i in range(0, len(shas), _KNOWN_SHA_BATCH):
        batch = shas[i:i + _KNOWN_SHA_BATCH]
        results = _rest(
            "checking which commits Gerrit already has",
            client.search_changes,
            " OR ".join(f"commit:{sha}" for sha in batch),
            limit=100, options=["ALL_REVISIONS"],
        )
        for change in results or []:
            known.update(set(change.get("revisions") or {}) & set(batch))
    return known


def _patchset_of(
    client: Any, change_id: str, project: str, branch: str, sha: str,
    number: int | None,
) -> tuple[int | None, int | None]:
    """(change number, patchset number) a pushed commit became."""
    try:
        if number is None:
            found = client.search_changes(
                f"change:{change_id} project:{project} branch:{branch}",
                limit=1, options=[],
            )
            if not found:
                return None, None
            number = int(found[0]["_number"])
        detail = client.get_change(number, ["ALL_REVISIONS"])
    except Exception:
        return number, None
    revision = (detail.get("revisions") or {}).get(sha) or {}
    return number, revision.get("_number")


# --------------------------------------------------------------------------
# Push result
# --------------------------------------------------------------------------

def _remote_lines(stderr: str) -> list[str]:
    lines = []
    for raw in re.split(r"[\r\n]+", stderr):
        if raw.startswith("remote:"):
            line = raw[len("remote:"):].strip()
            if line:
                lines.append(line)
    return lines


def _porcelain_status(stdout: str) -> tuple[str, str] | None:
    """(flag, summary) of the one ref update `git push --porcelain` printed."""
    for line in stdout.splitlines():
        parts = line.split("\t")
        if len(parts) >= 3 and len(parts[0]) == 1:
            return parts[0], parts[2]
    return None


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

def _resolve_destination(
    client: Any,
    repo: str,
    head: Commit,
    change: str | None,
    project: str | None,
    branch: str | None,
    series: bool,
) -> tuple[str, str, Target | None]:
    """The project and branch to upload to, and the named change if any.

    From the named change, from --project and --branch, or from the
    change HEAD's Change-Id names.  Without --series the named change
    must be HEAD's.
    """
    head_change_id = head.change_id
    if change:
        named = _load_target(
            client, _resolve_named(client, change, project, branch)
        )
        for flag, wanted, actual in (
            ("--branch", branch, named.branch),
            ("--project", project, named.project),
        ):
            if wanted and wanted != actual:
                raise UploadError(
                    ErrorCode.INVALID_INPUT,
                    f"Change {named.change_number} is in {named.project} "
                    f"on {named.branch}, not {flag} {wanted}. To upload "
                    "HEAD as a new change there, name no change and pass "
                    "--project and --branch.",
                    exit_code=ExitCode.INVALID_INPUT,
                )
        if not series:
            if head_change_id != named.change_id:
                raise _head_mismatch(client, repo, head, head_change_id, named)
            if head.sha in named.revisions:
                raise UploadError(
                    ErrorCode.NO_NEW_CHANGES,
                    f"HEAD {head.sha[:12]} is already patchset "
                    f"{named.revisions[head.sha].get('_number')} of change "
                    f"{named.change_number}; there is nothing new to "
                    "upload.",
                )
        return named.project, named.branch, named

    if project and branch:
        return project, branch, None

    if head_change_id is None:
        raise UploadError(
            ErrorCode.NO_CHANGE_ID,
            f"HEAD {head.sha[:12]} ('{head.subject}') has no Change-Id "
            "trailer, so there is no change to upload it to. Amend the "
            "commit with the Change-Id of the change it belongs to (the "
            "commit-msg hook generates one for a new change).",
        )
    picked, everywhere = _find_change(client, head_change_id, project, branch)
    if picked is None:
        where = ""
        if everywhere:
            where = (
                " matching the given --project/--branch (it is on "
                f"{', '.join(_describe(m) for m in everywhere)})"
            )
        raise UploadError(
            ErrorCode.CHANGE_NOT_FOUND,
            f"No change on {client.url} has HEAD's Change-Id "
            f"{head_change_id}{where}. To upload HEAD as a new change, "
            "pass both --project and --branch.",
            exit_code=ExitCode.NOT_FOUND,
        )
    _closed(picked)
    return picked["project"], picked["branch"], None


def _check_range(
    commits: list[Commit],
    new: list[Commit],
    head: Commit,
    series: bool,
    named: Target | None,
    project: str,
    branch: str,
) -> None:
    """Refuse a range this mode cannot upload."""
    if not series and (len(new) > 1 or new[0].sha != head.sha):
        below = len(commits) - len(new)
        raise UploadError(
            ErrorCode.MULTIPLE_COMMITS,
            f"HEAD is {len(new)} commits ahead of {project} {branch} "
            "that Gerrit does not have yet: "
            + "; ".join(c.label() for c in new)
            + ". `gerrit upload` pushes one commit by default. Pass "
            f"--series to push all {len(new)} as a series, each to its "
            "own change by its Change-Id."
            + (f" ({below} commit(s) below them are already on Gerrit "
               "and are not counted.)" if below else ""),
            details={"commits": [
                {"sha": c.sha, "subject": c.subject,
                 "change_id": c.change_ids[-1] if c.change_ids else None}
                for c in new
            ]},
        )
    if not series:
        return

    missing = [c for c in commits if c.change_id is None]
    if missing:
        raise UploadError(
            ErrorCode.NO_CHANGE_ID,
            "Every commit in a series needs a Change-Id trailer, and "
            "Gerrit would refuse the push without one. Missing on: "
            + "; ".join(c.label() for c in missing) + ".",
            details={"commits": [c.sha for c in missing]},
        )
    if named and named.change_id not in {c.change_id for c in commits}:
        raise UploadError(
            ErrorCode.CHANGE_ID_MISMATCH,
            f"Refusing to upload: change {named.change_number} "
            f"('{named.subject}', Change-Id {named.change_id}) is not any "
            f"of the {len(commits)} commits between {branch} and HEAD: "
            + "; ".join(c.label() for c in commits)
            + ". Nothing was pushed.",
            details={"change_change_id": named.change_id},
        )


def _plan(
    client: Any,
    commits: list[Commit],
    known: set[str],
    project: str,
    branch: str,
) -> list[dict[str, Any]]:
    """What each commit will do on Gerrit: none, update or create."""
    base = client.url.rstrip("/")
    plan = []
    for c in commits:
        entry: dict[str, Any] = {
            "sha": c.sha,
            "subject": c.subject,
            "change_id": c.change_ids[-1] if c.change_ids else None,
            "committer": c.committer,
        }
        if c.sha in known:
            entry["action"] = "none"
        else:
            existing, _ = _find_change(
                client, c.change_id or "", project, branch
            )
            if existing is None:
                entry["action"] = "create"
                entry["change_number"] = None
            else:
                _closed(existing, c)
                number = int(existing["_number"])
                entry["action"] = "update"
                entry["change_number"] = number
                entry["url"] = f"{base}/c/{project}/+/{number}"
        plan.append(entry)
    return plan


def _committer_rewrites(
    commits: list[Commit], known: set[str], account: Account, amend: bool
) -> list[Commit]:
    """The commits to recommit, parents first.

    One with a committer the account has not registered, and everything
    above it, since its parent changes.
    """
    rewrite: list[Commit] = []
    rewritten: set[str] = set()
    for c in commits:
        if c.sha in known:
            continue
        if c.committer_email not in account.emails or (
            set(c.parents) & rewritten
        ):
            rewrite.append(c)
            rewritten.add(c.sha)
    if rewrite and (not amend or not account.preferred_email):
        foreign = [c for c in rewrite if c.committer_email not in account.emails]
        to = f"{account.name} <{account.preferred_email}>"
        reason = (
            "--no-amend was given" if account.preferred_email
            else "the account has no email address to set"
        )
        raise UploadError(
            ErrorCode.COMMITTER_NOT_REGISTERED,
            "The committer email of "
            + "; ".join(f"{c.label()} [{c.committer}]" for c in foreign)
            + f" is not registered to '{account.username}' (registered: "
            f"{', '.join(sorted(account.emails)) or 'none'}), and "
            f"{reason}. Gerrit would reject the push."
            + (f" Drop --no-amend to set the committer to {to}."
               if account.preferred_email else ""),
        )
    return rewrite


def _add_current_patchsets(
    client: Any, plan: list[dict[str, Any]], named: Target | None
) -> None:
    """For a dry run: the patchset each update would follow."""
    for entry in plan:
        if entry["action"] != "update":
            continue
        number = entry["change_number"]
        if named and named.change_number == number:
            entry["current_patchset"] = named.current_patchset
            continue
        detail = _rest(
            f"reading change {number}", client.get_change, number,
            ["CURRENT_REVISION"],
        )
        current = detail.get("current_revision")
        entry["current_patchset"] = (
            (detail.get("revisions") or {}).get(current) or {}
        ).get("_number")


def _push_failure(
    stderr: str,
    status: tuple[str, str] | None,
    remote: list[str],
    url: str,
    username: str,
    amended: list[dict[str, Any]],
    sha: str,
) -> UploadError:
    """The error for a push git or Gerrit refused."""
    if status is None:
        error = _transport_error(stderr, url, username)
        error.details["committer_amended"] = amended
        return error
    errors = [
        line for line in remote
        if line.lower().startswith(("error", "fatal"))
    ]
    return UploadError(
        ErrorCode.PUSH_REJECTED,
        f"Gerrit rejected the push: {status[1]}"
        + (f" -- {' '.join(errors)}" if errors else "")
        + (f". HEAD had already been rewritten to {sha[:12]} to fix the "
           "committer; see details.committer_amended."
           if amended else ""),
        details={
            "rejection": status[1],
            "remote_messages": remote,
            "committer_amended": amended,
            "sha": sha,
        },
    )


def upload(
    client: Any,
    repo: str = ".",
    change: str | None = None,
    branch: str | None = None,
    project: str | None = None,
    topic: str | None = None,
    dry_run: bool = False,
    amend: bool = True,
    series: bool = False,
) -> dict[str, Any]:
    """Push HEAD to refs/for/<branch> as the client's account.

    Without ``series``, HEAD must be the only commit Gerrit does not
    have yet.  With it, every such commit between the branch and HEAD is
    uploaded, each to the change its own Change-Id names.

    Raises:
        UploadError: on every refusal and failure.  Nothing has been
            pushed when it is raised, and commits have been rewritten
            only if its details say so.
    """
    if not getattr(client, "authenticated", False):
        raise UploadError(
            ErrorCode.AUTH_MISSING,
            "Uploading needs GERRIT_USER and GERRIT_PASS; select the "
            "account with --user.",
            exit_code=ExitCode.AUTH_ERROR,
        )
    if topic is not None and (not topic or re.search(r"[\s,%]", topic)):
        raise UploadError(
            ErrorCode.INVALID_INPUT,
            f"Topic '{topic}' cannot be passed as a push option (no "
            "whitespace, ',' or '%'). Upload without --topic and set it "
            "with: gerrit set-topic <change> <topic>",
            exit_code=ExitCode.INVALID_INPUT,
        )

    repo = _toplevel(repo)
    head_sha = _out(repo, ["rev-parse", "--verify", "HEAD^{commit}"])
    head = _read_commits(repo, ["-1", head_sha])[0]

    project, branch, named = _resolve_destination(
        client, repo, head, change, project, branch, series
    )
    if not series and head.change_id is None:
        raise UploadError(
            ErrorCode.NO_CHANGE_ID,
            f"HEAD {head.sha[:12]} ('{head.subject}') has no Change-Id "
            "trailer; Gerrit refuses it. Add one (the commit-msg hook "
            "generates it).",
        )

    account = _account(client)
    url = push_url(client.url, project, client.username)
    _check_url_rewrites(repo, url)

    warnings = []
    if _out(repo, ["status", "--porcelain", "--untracked-files=no"]):
        warnings.append(
            "The work tree has uncommitted changes to tracked files; they "
            "are not in any commit and are not uploaded."
        )

    with _Askpass(client.username, client.password) as askpass:
        tip = _branch_tip(repo, url, branch, askpass)
        commits = _read_commits(repo, [f"{tip}..{head.sha}"])
        if not commits:
            raise UploadError(
                ErrorCode.NO_NEW_CHANGES,
                f"HEAD {head.sha[:12]} is already on {branch}; there is "
                "nothing to upload.",
            )
        if len(commits) > MAX_COMMITS:
            raise UploadError(
                ErrorCode.TOO_MANY_COMMITS,
                f"HEAD is {len(commits)} commits ahead of {project} "
                f"{branch}; Gerrit would open a change for each. HEAD is "
                "probably based on another branch -- check --branch.",
            )

        known = _known_on_gerrit(client, [c.sha for c in commits])
        new = [c for c in commits if c.sha not in known]
        if not new:
            raise UploadError(
                ErrorCode.NO_NEW_CHANGES,
                f"Every commit between {branch} and HEAD {head.sha[:12]} "
                "is already on Gerrit; there is nothing new to upload.",
            )

        _check_range(commits, new, head, series, named, project, branch)

        plan = _plan(client, commits, known, project, branch)

        rewrite = _committer_rewrites(commits, known, account, amend)
        to = f"{account.name} <{account.preferred_email}>"
        amended = [
            {"old_sha": c.sha, "new_sha": None, "subject": c.subject,
             "from": c.committer, "to": to}
            for c in rewrite
        ]

        ref = f"refs/for/{branch}"
        if topic:
            ref += f"%topic={topic}"

        def push_args(sha: str) -> list[str]:
            return [*_GIT_AUTH_ARGS, "push", "--porcelain", url,
                    f"{sha}:{ref}"]

        head_entry = plan[-1]
        data: dict[str, Any] = {
            "dry_run": dry_run,
            "pushed": False,
            "series": series,
            "project": project,
            "branch": branch,
            "ref": ref,
            "topic": topic,
            "account": account.username,
            "sha": head.sha,
            "change_number": head_entry.get("change_number"),
            "change_id": head_entry["change_id"],
            "url": head_entry.get("url"),
            "new_change": head_entry["action"] == "create",
            "committer_amended": amended,
            "commits": plan,
            "warnings": warnings,
        }

        if dry_run:
            _add_current_patchsets(client, plan, named)
            data["current_patchset"] = head_entry.get("current_patchset")
            data["push"] = {
                "command": shlex.join(["git", *push_args(
                    "<HEAD after committer amend>" if rewrite else head.sha
                )]),
                "env": askpass.overrides(redact=True),
            }
            return data

        if rewrite:
            mapping = _rewrite_chain(
                repo, rewrite, head.sha, account.name,
                account.preferred_email or "",
            )
            for entry in amended:
                entry["new_sha"] = mapping[entry["old_sha"]]
            for entry in plan:
                if entry["sha"] in mapping:
                    entry["old_sha"] = entry["sha"]
                    entry["sha"] = mapping[entry["sha"]]
                    entry["committer"] = to
            data["sha"] = mapping[head.sha]

        result = _git(
            repo, push_args(data["sha"]), env=askpass.env(), check=False
        )

    stdout, stderr = _text(result.stdout), _text(result.stderr)
    remote = _remote_lines(stderr)
    status = _porcelain_status(stdout)
    if result.returncode != 0 or status is None or status[0] == "!":
        raise _push_failure(
            stderr, status, remote, url, client.username, amended, data["sha"]
        )

    for entry in plan:
        if entry["action"] == "none":
            continue
        number, patchset = _patchset_of(
            client, entry["change_id"], project, branch, entry["sha"],
            entry.get("change_number"),
        )
        entry["change_number"] = number
        entry["patchset"] = patchset
        if number:
            entry["url"] = f"{client.url.rstrip('/')}/c/{project}/+/{number}"

    data.update({
        "pushed": True,
        "change_number": head_entry.get("change_number"),
        "patchset": head_entry.get("patchset"),
        "url": head_entry.get("url"),
        "remote_messages": remote,
    })
    return data
