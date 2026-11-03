"""reviewer-candidates: who knows the files a change touches.

Reviewer-finding runs worked this out by hand for every change: per
touched file, a git log for Reviewed-by trailers and another for authors
over the last year, tallied and ranked.  This does the tally from a local
checkout's history; picking who to add stays with the caller.
"""

import re
import subprocess
import sys
import unicodedata
from collections import Counter

from ..ci_parse import is_bot_participant
from ..errors import ErrorCode, ExitCode
from ._helpers import _cli, error_code_for, output_error, output_success
from .reviewers import commit_author

# A reviewer is worth more than an author: authors of a file are often
# not the people who review it (get_maintainer.pl's weakness).
REVIEW_WEIGHT = 2
AUTHOR_WEIGHT = 1

_TRAILER = re.compile(r"^\s*Reviewed-by:\s*(.*?)\s*<([^<>\s]+)>\s*$",
                      re.IGNORECASE | re.MULTILINE)
_RS, _US = "\x1e", "\x1f"

_CHANGE_OPTIONS = ["CURRENT_REVISION", "CURRENT_COMMIT", "CURRENT_FILES",
                   "DETAILED_LABELS", "DETAILED_ACCOUNTS"]


def normalize_name(name):
    """Lowercase, accents and punctuation dropped, spaces collapsed."""
    text = unicodedata.normalize("NFKD", name or "")
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = re.sub(r"[^\w\s]", " ", text.lower())
    return " ".join(text.split())


def _keys(name, email):
    keys = []
    if email and "@" in email:
        keys.append("e:" + email.strip().lower())
    norm = normalize_name(name)
    if norm:
        keys.append("n:" + norm)
    return keys


class Identities:
    """People as merged identities: one email under two names, or one
    name under two emails, are the same person (union-find on both)."""

    def __init__(self):
        self.parent = {}

    def _find(self, key):
        self.parent.setdefault(key, key)
        while self.parent[key] != key:
            self.parent[key] = self.parent[self.parent[key]]
            key = self.parent[key]
        return key

    def add(self, name, email):
        keys = _keys(name, email)
        for key in keys[1:]:
            self.parent[self._find(key)] = self._find(keys[0])
        return self._find(keys[0]) if keys else None

    def lookup(self, name, email):
        """The person an account is, if any history knows them."""
        for key in _keys(name, email):
            if key in self.parent:
                return self._find(key)
        return None


def git_history(repo, ref, paths, months):
    """[{hash, name, email, date, reviewers: [(name, email)], files}] for
    the non-merge commits on ref within the window touching paths."""
    cmd = [
        "git", "-C", repo, "-c", "core.quotePath=false",
        "--literal-pathspecs", "log", "--no-merges", "--name-only",
        f"--since={months} months ago",
        f"--format={_RS}%H{_US}%an{_US}%ae{_US}%cs{_US}%B{_US}",
        ref, "--", *paths,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True,
                          errors="replace")
    if proc.returncode != 0:
        raise ValueError(f"git log failed in {repo}: {proc.stderr.strip()}")
    commits = []
    for record in proc.stdout.split(_RS)[1:]:
        parts = record.split(_US)
        if len(parts) < 6:
            continue
        sha, name, email, date, body, rest = parts[:6]
        commits.append({
            "hash": sha, "name": name, "email": email, "date": date,
            "reviewers": _TRAILER.findall(body),
            "files": [line for line in rest.splitlines() if line.strip()],
        })
    return commits


def _bot(name, email):
    return is_bot_participant({"name": name, "username": (email or "").split("@")[0]})


def rank_candidates(commits, exclude=(), on_change=None):
    """Tally reviews and authorship per person and file, then rank.

    exclude: [(name, email, reason)] people left out (owner, author).
    on_change: [(name, email, role)] people already on the change, who
    are marked, not dropped.
    Returns (candidates, excluded), each ranked by score.
    """
    ids = Identities()
    events = []  # (name, email, kind, file, date)
    for commit in commits:
        people = [(commit["name"], commit["email"], "authored")]
        people += [(n, e, "reviewed") for n, e in commit["reviewers"]]
        for name, email, kind in people:
            ids.add(name, email)
            for path in commit["files"]:
                events.append((name, email, kind, path, commit["date"]))

    people = {}
    for name, email, kind, path, date in events:
        root = ids.lookup(name, email)
        person = people.setdefault(root, {
            "names": Counter(), "emails": Counter(), "files": {},
            "last_reviewed": "", "last_authored": "", "bot": False,
            "count_reviewed": 0, "count_authored": 0,
        })
        person["names"][name] += 1
        if email:
            person["emails"][email.lower()] += 1
        person["bot"] = person["bot"] or _bot(name, email)
        per_file = person["files"].setdefault(path, {"reviewed": 0, "authored": 0})
        per_file[kind] += 1
        last = "last_" + kind
        person[last] = max(person[last], date)

    # A commit counts once per person however many files it touches.
    for commit in commits:
        seen = set()
        for name, email, kind in [(commit["name"], commit["email"], "authored")] + [
                (n, e, "reviewed") for n, e in commit["reviewers"]]:
            root = ids.lookup(name, email)
            if (root, kind) not in seen:
                seen.add((root, kind))
                people[root]["count_" + kind] += 1

    excluded_roots = {}
    for name, email, reason in exclude:
        root = ids.lookup(name, email)
        if root is not None:
            excluded_roots.setdefault(root, reason)
    roles = {}
    for name, email, role in on_change or ():
        root = ids.lookup(name, email)
        if root is not None:
            roles.setdefault(root, role)

    candidates, excluded = [], []
    for root, person in people.items():
        reviewed = person["count_reviewed"]
        authored = person["count_authored"]
        files = sorted(
            ({"path": path, **counts} for path, counts in person["files"].items()),
            key=lambda f: (-(f["reviewed"] * REVIEW_WEIGHT
                             + f["authored"] * AUTHOR_WEIGHT), f["path"]))
        entry = {
            # The commonest spelling; on a tie, the one written with capitals.
            "name": max(person["names"].items(),
                        key=lambda item: (item[1], sum(map(str.isupper, item[0]))))[0],
            "email": (person["emails"].most_common(1) or [("", 0)])[0][0],
            "score": reviewed * REVIEW_WEIGHT + authored * AUTHOR_WEIGHT,
            "reviewed": reviewed,
            "authored": authored,
            # Near 1 for whoever lands every commit (the gatekeeper's
            # Reviewed-by), which says little about knowing these files.
            "review_share": round(reviewed / len(commits), 2) if commits else 0,
            "last_reviewed": person["last_reviewed"] or None,
            "last_authored": person["last_authored"] or None,
            "file_count": len(files),
            "files": files,
            "on_change": roles.get(root),
        }
        names = sorted(person["names"])
        emails = sorted(person["emails"])
        if len(names) > 1 or len(emails) > 1:
            entry["aliases"] = {"names": names, "emails": emails}
        reason = "bot" if person["bot"] else excluded_roots.get(root)
        if reason:
            excluded.append(dict(entry, reason=reason))
        else:
            candidates.append(entry)

    def order(entry):
        latest = max(entry["last_reviewed"] or "", entry["last_authored"] or "")
        return (-entry["score"], -entry["file_count"],
                -int(latest.replace("-", "") or 0), entry["name"])

    return sorted(candidates, key=order), sorted(excluded, key=order)


def change_context(client, change_number):
    """(files, exclude, on_change, summary) from the change on Gerrit."""
    change = client.get_change(change_number, options=_CHANGE_OPTIONS)
    revision = (change.get("revisions") or {}).get(
        change.get("current_revision") or "") or {}
    files = []
    for path, info in (revision.get("files") or {}).items():
        if path.startswith("/"):  # /COMMIT_MSG, /MERGE_LIST
            continue
        files.append(path)
        old = (info or {}).get("old_path")
        if old and old not in files:
            files.append(old)
    owner = change.get("owner") or {}
    author = commit_author(change)
    exclude = [(owner.get("name", ""), owner.get("email", ""), "owner")]
    if author:
        exclude.append((author["name"], author["email"], "commit_author"))
    on_change = []
    owner_id = owner.get("_account_id")
    for role in ("REVIEWER", "CC"):
        for account in (change.get("reviewers") or {}).get(role) or []:
            if account.get("_account_id") == owner_id:
                continue
            on_change.append((account.get("name", ""),
                              account.get("email", ""), role))
    summary = {
        "change_number": change_number,
        "project": change.get("project"),
        "branch": change.get("branch"),
        "subject": change.get("subject"),
        "owner": {"name": owner.get("name", ""), "email": owner.get("email", "")},
        "commit_author": author,
    }
    return files, exclude, on_change, summary


def cmd_reviewer_candidates(args):
    """Rank reviewer candidates for a change's files from git history."""
    command = "reviewer-candidates"
    pretty = getattr(args, "pretty", False)
    try:
        paths = list(getattr(args, "paths", None) or [])
        if not args.url and not paths:
            raise ValueError("Give a change, or files with --path")
        if args.months <= 0:
            raise ValueError("--months must be positive")
        repo = args.repo or "."
        check = subprocess.run(["git", "-C", repo, "rev-parse", "--verify",
                                "--quiet", f"{args.ref}^{{commit}}"],
                               capture_output=True, text=True)
        if check.returncode != 0:
            raise ValueError(f"{repo} is not a git checkout with ref "
                             f"{args.ref}; point --repo at a lustre-release "
                             "checkout")

        data = {}
        exclude = [(e, e, "excluded") for e in (args.exclude or [])]
        on_change = []
        client = None
        if args.url:
            cli = _cli()
            _base, change_number = cli.GerritCommentsClient.parse_gerrit_url(args.url)
            client = cli.GerritCommentsClient()
            files, more_exclude, on_change, summary = change_context(
                client, change_number)
            data.update(summary)
            exclude += more_exclude
            if not paths:
                paths = files
        if not paths:
            raise ValueError("The change touches no files")

        commits = git_history(repo, args.ref, paths, args.months)
        candidates, excluded = rank_candidates(commits, exclude, on_change)
        known = {f for c in commits for f in c["files"]}
        total = len(candidates)
        if args.limit > 0:
            candidates = candidates[:args.limit]
        load = getattr(args, "load", False)
        asked_path = getattr(args, "asked", None)
        order = "score"
        if load or asked_path:
            from . import reviewer_load
            asked = reviewer_load.read_asked(asked_path) if asked_path else None
            if load:
                if client is None:
                    client = _cli().GerritCommentsClient()
                reviewer_load.add_gerrit_load(client, candidates)
            candidates = reviewer_load.balance(candidates, asked)
            order = "balanced"
            if asked is not None:
                data["asked_by"] = asked.get("by")
                data["asked_generated_at"] = asked.get("generated_at")
        if args.brief:
            for entry in candidates + excluded:
                entry["files"] = [f["path"] for f in entry["files"]]
        data.update({
            "repo": repo,
            "ref": args.ref,
            "window_months": args.months,
            "commits_examined": len(commits),
            "paths": paths,
            "paths_without_history": [p for p in paths if p not in known
                                      and not any(k.startswith(p.rstrip("/") + "/")
                                                  for k in known)],
            "order": order,
            "candidates": candidates,
            "count": len(candidates),
            "total_candidates": total,
            "excluded": excluded,
        })
        output_success(data, command, pretty)
        sys.exit(ExitCode.SUCCESS)
    except ValueError as e:
        sys.exit(output_error(ErrorCode.INVALID_INPUT, str(e), command, pretty))
    except Exception as e:
        sys.exit(output_error(error_code_for(e), str(e), command, pretty))
