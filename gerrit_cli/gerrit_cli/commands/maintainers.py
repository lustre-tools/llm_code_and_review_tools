"""Who Lustre's MAINTAINERS file names for a change's files.

get_maintainer.pl lists the R: (and M:) people of every section whose F:
patterns match a file; runs checking it by hand found a maintainer the
git-history ranking missed (fb-019d54d2436c, change 69015).  The file is
stale (Patrick, 2026-11-12), so reviewer-candidates reports these people
beside the ranking and never ranks, adds or un-holds anyone by them.

Matching follows get_maintainer.pl: in an F: or X: pattern "." is
literal, "*" is ".*" and "?" is "."; a pattern ending in "/" matches
everything below it, and any other must match from the start with as
many "/" as the file has.  A section is skipped for a file one of its X:
patterns matches.  N: is a regex searched anywhere in the path.
"""

import re
import subprocess



def person_keys(names, emails):
    """Keys one person is known by: each email, each normalized name."""
    from .reviewer_candidates import normalize_name
    keys = {"e:" + e.strip().lower() for e in emails if e and "@" in e}
    keys |= {"n:" + normalize_name(n) for n in names if normalize_name(n)}
    return sorted(keys)

_TAG = re.compile(r"^([A-Z]):\s*(.*)$")
_PERSON = re.compile(r"^\s*(.*?)\s*<([^<>\s]+)>")


def read_maintainers(repo, ref):
    """MAINTAINERS at ref in repo, or None if there is none."""
    try:
        proc = subprocess.run(
            ["git", "-C", repo, "show", f"{ref}:MAINTAINERS"],
            capture_output=True, text=True, errors="replace")
    except OSError:
        return None
    return proc.stdout if proc.returncode == 0 else None


def parse_sections(text):
    """[{name, people: [(role, name, email)], F, X, N}] in file order."""
    sections, header, section = [], "", None
    for line in text.splitlines():
        match = _TAG.match(line)
        if match:
            kind, value = match.group(1), match.group(2).strip()
            if section is None:
                section = {"name": header, "people": [], "F": [], "X": [],
                           "N": []}
                sections.append(section)
            if kind in ("F", "X", "N") and value:
                section[kind].append(value)
            elif kind in ("R", "M"):
                person = _PERSON.match(value)
                if person:
                    section["people"].append(
                        (kind, person.group(1), person.group(2)))
        elif not line.strip() or line.lstrip().startswith("#"):
            continue
        else:
            header, section = line.strip(), None
    return [s for s in sections if s["people"]]


def tree_dirs(repo, ref):
    """Every directory in the tree at ref, or None if git cannot say."""
    try:
        proc = subprocess.run(
            ["git", "-C", repo, "-c", "core.quotePath=false", "ls-tree",
             "-r", "-d", "--name-only", ref],
            capture_output=True, text=True, errors="replace")
    except OSError:
        return None
    return set(proc.stdout.splitlines()) if proc.returncode == 0 else None


def _pattern(raw, path, dirs):
    value = raw.replace(".", r"\.").replace("*", ".*").replace("?", ".")
    # get_maintainer.pl adds the slash when the pattern is a directory in
    # the tree.  Without the tree, a changed file below it shows it is one.
    if not value.endswith("/"):
        if dirs is not None:
            is_dir = raw in dirs
        else:
            is_dir = (not any(c in raw for c in "*?[")
                      and path.startswith(raw + "/"))
        if is_dir:
            value += "/"
    return value


def file_match(path, raw, dirs=None):
    """(matches, depth): get_maintainer.pl's file_match_pattern, and how
    specific the pattern is (its pattern depth).  dirs: the tree's
    directories, as get_maintainer.pl's -d test sees them."""
    value = _pattern(raw, path, dirs)
    try:
        if not re.match(value, path):
            return False, 0
    except re.error:
        return False, 0
    if not value.endswith("/") and path.count("/") != value.count("/"):
        return False, 0
    depth = value.count("/") + (0 if value.endswith("/") else 1)
    return True, (-1 if value.startswith(".*") else depth)


def maintainers_for(sections, paths, dirs=None):
    """People the sections name for paths: [{name, email, role, sections,
    files}], the most specific match first."""
    people, order = {}, []
    for path in paths:
        for section in sections:
            if any(file_match(path, x, dirs)[0] for x in section["X"]):
                continue
            depth = None
            for raw in section["F"]:
                hit, d = file_match(path, raw, dirs)
                if hit:
                    depth = d if depth is None else max(depth, d)
            if depth is None:
                for raw in section["N"]:
                    try:
                        if re.search(raw, path, re.X):
                            depth = 0
                            break
                    except re.error:
                        continue
            if depth is None:
                continue
            for role, name, email in section["people"]:
                key = email.lower()
                if key not in people:
                    people[key] = {"name": name, "email": email, "role": role,
                                   "sections": [], "files": [], "_depth": depth}
                    order.append(key)
                entry = people[key]
                if role == "M":
                    entry["role"] = "M"
                if section["name"] not in entry["sections"]:
                    entry["sections"].append(section["name"])
                if path not in entry["files"]:
                    entry["files"].append(path)
                entry["_depth"] = max(entry["_depth"], depth)
    out = sorted((people[k] for k in order),
                 key=lambda e: (-e["_depth"], -len(e["files"])))
    for entry in out:
        del entry["_depth"]
    return out


def change_maintainers(repo, ref, paths):
    """(source, people): maintainers_for the MAINTAINERS at ref, with
    source "REF:MAINTAINERS"; (None, []) when it is missing or cannot be
    read."""
    try:
        text = read_maintainers(repo, ref)
        if not text:
            return None, []
        return f"{ref}:MAINTAINERS", maintainers_for(
            parse_sections(text), paths, tree_dirs(repo, ref))
    except Exception:  # stale or odd file: extra information only
        return None, []


def mark_maintainers(maintainers, candidates, excluded=(), on_change=(),
                     held_omitted=()):
    """Say where each maintainer stands in the ranking (rank in the
    listed candidates, on_change, excluded, hold) and mark the candidate
    rows that are maintainers.  Changes no ranking, eligibility or hold."""
    def index(people):
        found = {}
        for i, person in enumerate(people):
            aliases = person.get("aliases") or {}
            names = [person.get("name")] + list(aliases.get("names") or [])
            emails = [person.get("email")] + list(aliases.get("emails") or [])
            for key in person_keys(names, emails):
                found.setdefault(key, i)
        return found

    ranked = index(candidates)
    out = index(excluded)
    held = index(held_omitted)
    roles = index([{"name": n, "email": e} for n, e, _r in on_change])
    for m in maintainers:
        keys = person_keys([m["name"]], [m["email"]])
        rank = next((ranked[k] for k in keys if k in ranked), None)
        m["rank"] = None if rank is None else rank + 1
        m["on_change"] = None
        if rank is not None:
            cand = candidates[rank]
            cand.setdefault("maintainer", [])
            for name in m["sections"]:
                if name not in cand["maintainer"]:
                    cand["maintainer"].append(name)
            m["on_change"] = cand.get("on_change")
        role = next((roles[k] for k in keys if k in roles), None)
        if role is not None:
            m["on_change"] = list(on_change)[role][2]
        gone = next((out[k] for k in keys if k in out), None)
        if gone is not None:
            m["excluded"] = excluded[gone].get("reason")
        hold = next((held[k] for k in keys if k in held), None)
        if hold is not None:
            m["hold"] = held_omitted[hold].get("hold")
    return maintainers
