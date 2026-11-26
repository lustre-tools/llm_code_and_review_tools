"""patchmap: which agent session owns which patch, branch and worktree.

The map is ~/.claude/patchmap.yaml (PATCHMAP_FILE overrides): a YAML
list with one flow mapping per line, one line per patch.  Every string is
written JSON-quoted (valid YAML), so it parses without PyYAML and survives
colons and '#' in descriptions.  Write it only through this script, which
takes a lock so concurrent sessions do not clobber each other.

  patchmap set --dir ~/src/x.wt/LU-123-foo --ticket LU-123 --desc "foo fix" \\
               --state "local, unpushed"
  patchmap set --patch 71234 --state "PS2 in CI"   # row found by patch...
  patchmap set --dir ~/src/x.wt/LU-123-foo --patch 71234   # ...or by dir
  patchmap show [--mine | --session ID | --stale] [--json]
  patchmap get KEY          # KEY: patch number, dir, or branch
  patchmap rm KEY
  patchmap title            # this session's name, as show prints it

A patch is a change number on the default tracker, <tracker>:N on another
one, owner/repo#N for a GitHub PR, or "new" before it has one.
~/.config/patchmap.toml (PATCHMAP_CONFIG moves it) gives each tracker and
ticket prefix a URL template, which show prints:

  [trackers]
  default = "https://review.whamcloud.com/{n}"
  gerrithub = "https://review.gerrithub.io/{n}"
  github = "https://github.com/{repo}/pull/{n}"
  [tickets]
  "LU-" = "https://jira.whamcloud.com/browse/{id}"

One host keeps the map.  A session there working on another host's
worktree passes --host.  Opt-in, for a host trusted to log in to the map's
host: set PATCHMAP_REMOTE to the map's host, and patchmap fills in this
host's name and this session's id and title itself and runs the command
there over ssh.
"""

import argparse
import datetime
import fcntl
import glob
import json
import os
import re
import shlex
import socket
import subprocess
import sys
import tomllib

MAP = os.path.expanduser(os.environ.get("PATCHMAP_FILE", "~/.claude/patchmap.yaml"))
FIELDS = ["patch", "ticket", "desc", "branch", "dir", "host", "session", "app", "title", "state", "updated"]
HEADER = "# patchmap.yaml: one line per patch; write it with `patchmap set`, read with `patchmap show`.\n"
HOME = os.path.expanduser("~")
# The map's name before patchmap moved here; the first run renames it.
OLD_MAP = os.path.expanduser("~/.claude/patch-sessions.yaml")


def tilde(path):
    path = os.path.abspath(os.path.expanduser(path))
    if path == HOME or path.startswith(HOME + "/"):
        return "~" + path[len(HOME) :]
    return path


def short_host():
    return socket.gethostname().split(".")[0]


def scalar(value):
    if value is None:
        return "null"
    if isinstance(value, int):
        return str(value)
    return json.dumps(value, ensure_ascii=False)


def dump_row(row):
    parts = [f"{k}: {scalar(row[k])}" for k in FIELDS if row.get(k) is not None]
    parts += [f"{k}: {scalar(v)}" for k, v in row.items() if k not in FIELDS and v is not None]
    return "- {" + ", ".join(parts) + "}\n"


def parse_row(line):
    """Parse '- {k: v, ...}' as written by dump_row (JSON scalars)."""
    body = line.strip()
    if not (body.startswith("- {") and body.endswith("}")):
        raise ValueError(f"not a patchmap row: {line.strip()!r}")
    text, out, i = body[3:-1], [], 0
    # Quote the bare keys, leaving JSON strings untouched, then json.loads.
    key_start = True
    while i < len(text):
        c = text[i]
        if c == '"':
            j = i + 1
            while text[j] != '"':
                j += 2 if text[j] == "\\" else 1
            out.append(text[i : j + 1])
            i = j + 1
            key_start = False
        elif key_start and not c.isspace():
            j = text.index(":", i)
            out.append(json.dumps(text[i:j].strip()) + ":")
            i = j + 1
            key_start = False
        else:
            if c == ",":
                key_start = True
            out.append(c)
            i += 1
    return json.loads("{" + "".join(out) + "}")


def load(path=None):
    path = path or MAP
    rows = []
    if os.path.exists(path):
        with open(path) as f:
            for n, line in enumerate(f, 1):
                if line.strip() and not line.lstrip().startswith("#"):
                    try:
                        row = parse_row(line)
                    except (ValueError, json.JSONDecodeError) as e:
                        sys.exit(f"patchmap: {path}:{n}: {e}")
                    # "lu" was the ticket's field before tickets were any tracker's
                    if "lu" in row:
                        row.setdefault("ticket", row.pop("lu"))
                    rows.append(row)
    return rows


def save(rows, path=None):
    path = path or MAP
    rows.sort(key=lambda r: (not isinstance(r.get("patch"), int), str(r.get("patch")), r.get("dir") or ""))
    tmp = f"{path}.tmp.{os.getpid()}"
    with open(tmp, "w") as f:
        f.write(HEADER)
        f.writelines(dump_row(r) for r in rows)
    os.replace(tmp, path)


class Locked:
    def __enter__(self):
        os.makedirs(os.path.dirname(MAP), exist_ok=True)
        self.fd = open(MAP + ".lock", "a")
        fcntl.flock(self.fd, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        self.fd.close()


def migrate():
    """Rename the map from its old name, under the old name's lock.

    The old name stays as a symlink to the new one for a while, for
    sessions that read the file by its old name.  Remove it once none do.
    """
    if os.environ.get("PATCHMAP_FILE") or os.path.islink(OLD_MAP) or not os.path.exists(OLD_MAP):
        return
    if os.path.exists(MAP):
        return
    with open(OLD_MAP + ".lock", "a") as fd:
        fcntl.flock(fd, fcntl.LOCK_EX)
        if os.path.islink(OLD_MAP) or not os.path.exists(OLD_MAP) or os.path.exists(MAP):
            return
        os.rename(OLD_MAP, MAP)
        os.symlink(os.path.basename(MAP), OLD_MAP)
        print(f"patchmap: moved {tilde(OLD_MAP)} to {tilde(MAP)}", file=sys.stderr)


def as_patch(value):
    if value is None:
        return None
    return int(value) if str(value).isdigit() else str(value)


def matches(row, key):
    # "new" is the placeholder of every unpushed row, never a key
    if key is None or key == "new":
        return False
    if str(key).isdigit():
        return row.get("patch") == int(key)
    return key in (row.get("patch"), row.get("branch"), row.get("dir")) or (
        key.startswith(("~", "/", ".")) and row.get("dir") == tilde(key)
    )


def find(rows, *keys):
    for key in keys:
        for row in rows:
            if matches(row, key):
                return row
    return None


def git_branch(path):
    try:
        out = subprocess.run(
            ["git", "-C", os.path.expanduser(path), "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    branch = out.stdout.strip()
    return branch if out.returncode == 0 and branch != "HEAD" else None


def label(row):
    patch = row.get("patch") or "new"
    inner = " ".join(x for x in (row.get("ticket"), row.get("desc")) if x)
    return f'{patch} ("{inner}")' if inner else str(patch)


# ---- links ----


def config_path():
    xdg = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.expanduser(os.environ.get("PATCHMAP_CONFIG") or os.path.join(xdg, "patchmap.toml"))


def load_config():
    path = config_path()
    try:
        with open(path, "rb") as f:
            conf = tomllib.load(f)
    except FileNotFoundError:
        return {}
    except (OSError, tomllib.TOMLDecodeError) as e:
        print(f"patchmap: ignoring {tilde(path)}: {e}", file=sys.stderr)
        return {}
    return {k: v for k, v in conf.items() if isinstance(v, dict)}


def _fill(template, **fields):
    try:
        return template.format(**fields)
    except (KeyError, IndexError, ValueError, AttributeError):
        return None


def patch_url(patch, conf):
    """The URL of a patch from the [trackers] templates, or None."""
    trackers = conf.get("trackers", {})
    if isinstance(patch, int):
        tracker, fields = "default", {"n": patch}
    elif isinstance(patch, str) and re.fullmatch(r"[\w.-]+/[\w.-]+#\d+", patch):
        repo, n = patch.split("#")
        tracker, fields = "github", {"repo": repo, "n": n}
    elif isinstance(patch, str) and re.fullmatch(r"[\w.-]+:\d+", patch):
        tracker, n = patch.split(":")
        fields = {"n": n}
    else:
        return None
    template = trackers.get(tracker)
    return _fill(template, **fields) if isinstance(template, str) else None


def ticket_url(ticket, conf):
    """The URL of a ticket from the longest matching [tickets] prefix, or None."""
    if not ticket:
        return None
    prefixes = [p for p, t in conf.get("tickets", {}).items() if isinstance(t, str) and ticket.startswith(p)]
    if not prefixes:
        return None
    return _fill(conf["tickets"][max(prefixes, key=len)], id=ticket)


# ---- session names ----
#
# A session's name comes from, in order:
#  1. the Claude desktop app's record of it (macOS), which holds the
#     sidebar title, its local_ id and the CLI session ids it has run under;
#  2. its Claude Code transcript, on any host: the last "custom-title"
#     record (a /rename, or the app's title), else the last "ai-title";
#  3. the title `set` stored in the row, for a session whose host is not
#     this one.

SESSIONS_GLOB = os.environ.get(
    "PATCHMAP_SESSIONS_GLOB", "~/Library/Application Support/Claude/claude-code-sessions/*/*/local_*.json"
)
# The tail is read first: the CLI writes the title records again and again,
# so the last few MB of even a long transcript hold one.
TAIL_BYTES = 4 << 20


def claude_dir():
    return os.path.expanduser(os.environ.get("CLAUDE_CONFIG_DIR", "~/.claude"))


def app_names():
    """Map CLI session uuid and local_ id to (title, archived)."""
    names = {}
    for path in glob.glob(os.path.expanduser(SESSIONS_GLOB)):
        try:
            with open(path) as f:
                rec = json.load(f)
        except (OSError, ValueError):
            continue
        entry = (rec.get("title") or rec.get("sessionId"), bool(rec.get("isArchived")))
        for sid in [rec.get("sessionId"), rec.get("cliSessionId"), *(rec.get("priorCliSessionIds") or [])]:
            if isinstance(sid, str):
                names[sid] = entry
    return names


def _titles_in(lines):
    custom = ai = None
    for line in lines:
        if b'"custom-title"' not in line and b'"ai-title"' not in line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue  # the tail's first line is usually cut
        if rec.get("type") == "custom-title" and rec.get("customTitle"):
            custom = rec["customTitle"]
        elif rec.get("type") == "ai-title" and rec.get("aiTitle"):
            ai = rec["aiTitle"]
    return custom, ai


def transcript_title(path):
    """The name a transcript gives its session, or None."""
    try:
        with open(path, "rb") as f:
            size = f.seek(0, os.SEEK_END)
            f.seek(max(0, size - TAIL_BYTES))
            custom, ai = _titles_in(f.read().splitlines())
            if custom is None and size > TAIL_BYTES:
                f.seek(0)
                custom, ai_head = _titles_in(f)
                ai = ai or ai_head
    except OSError:
        return None
    return custom or ai


def transcript_path(sid):
    paths = glob.glob(os.path.join(glob.escape(claude_dir()), "projects", "*", glob.escape(sid) + ".jsonl"))
    return max(paths, key=os.path.getmtime) if paths else None


class SessionNames:
    """Session names on this host, looked up as rows ask for them."""

    def __init__(self):
        self.app = app_names()
        self.cache = {}

    def lookup(self, sid, app=None):
        """(title, archived) for a session, or (None, False)."""
        hit = self.app.get(sid) or self.app.get(app or "")
        if hit:
            return hit
        if not sid:
            return None, False
        if sid not in self.cache:
            path = transcript_path(sid)
            self.cache[sid] = transcript_title(path) if path else None
        return self.cache[sid], False

    def label(self, row):
        sid = str(row.get("session") or "")
        title, archived = self.lookup(sid, row.get("app"))
        title = title or row.get("title")
        if title is None:
            return sid[:8] or "-"
        return f'"{title}"' + (" [archived]" if archived else "") + (f" ({sid[:8]})" if sid else "")


def own_session():
    """(session id, app id, title) of the session this runs in."""
    sid = os.environ.get("CLAUDE_CODE_SESSION_ID")
    app = os.environ.get("CLAUDE_CODE_HOST_SESSION_ID")
    title = SessionNames().lookup(sid, app)[0] if sid or app else None
    return sid, app, title


# ---- commands ----


def cmd_set(args):
    with Locked():
        rows = load()
        dir_ = tilde(args.dir) if args.dir else None
        row = find(rows, args.patch, dir_, args.branch)
        if row is None:
            if not (args.dir or (args.patch and args.patch != "new")):
                sys.exit("patchmap: a new row needs --dir or --patch NNNNN")
            row = {}
            rows.append(row)
        if args.dir and not args.branch and "branch" not in row:
            args.branch = git_branch(args.dir)
        if args.session is None:
            sid, app, title = own_session()
            if sid:
                args.session = sid
                args.app = args.app or app
                args.title = args.title or title
        updates = {
            "patch": as_patch(args.patch),
            "ticket": args.ticket,
            "desc": args.desc,
            "branch": args.branch,
            "dir": dir_,
            "host": args.host,
            "session": args.session,
            "app": args.app,
            "title": args.title,
            "state": args.state,
        }
        if args.session and args.session != row.get("session"):
            # another session's app id and title do not carry over
            row.pop("app", None)
            row.pop("title", None)
        row.update({k: v for k, v in updates.items() if v is not None})
        row.setdefault("patch", "new")
        row.setdefault("host", short_host())
        row["updated"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        for k in args.clear or []:
            row.pop(k, None)
        save(rows)
        print(dump_row(row), end="")


def stale(row):
    d = row.get("dir")
    return bool(d) and row.get("host") == short_host() and not os.path.isdir(os.path.expanduser(d))


def cmd_show(args):
    rows = load()
    names = SessionNames()
    if args.mine:
        args.session = os.environ.get("CLAUDE_CODE_SESSION_ID")
        if not args.session:
            sys.exit("patchmap: --mine needs $CLAUDE_CODE_SESSION_ID")
    if args.session:
        key = args.session.lower()
        rows = [r for r in rows if str(r.get("session", "")).startswith(args.session) or key in names.label(r).lower()]
    if args.stale:
        rows = [r for r in rows if stale(r)]
    conf = load_config()
    if args.json:
        for r in rows:
            r["url"] = patch_url(r.get("patch"), conf)
            r["ticket_url"] = ticket_url(r.get("ticket"), conf)
            r["label"] = label(r)
            r["session_label"] = names.label(r)
            r["dir_gone"] = stale(r)
        json.dump(rows, sys.stdout)
        print()
        return
    out = []
    for r in rows:
        flag = "  [dir gone]" if stale(r) else ""
        links = "  ".join(u for u in (patch_url(r.get("patch"), conf), ticket_url(r.get("ticket"), conf)) if u)
        out.append(
            f"{label(r)}  [{names.label(r)}]\n"
            f"    {r.get('state', '?')}{flag}\n"
            f"    {r.get('host', '?')}:{r.get('dir') or '-'}"
            f"  branch {r.get('branch') or '-'}"
            f"  updated {r.get('updated', '?')}" + (f"\n    {links}" if links else "")
        )
    page("".join(line + "\n" for line in out), args.no_pager)


def page(text, no_pager=False):
    """Print text, through a pager when stdout is a terminal, as git does.

    PATCHMAP_PAGER, then PAGER, names it ("" or "cat" for none); less gets
    LESS=FRX unless LESS is set, so output that fits on one screen just
    prints.
    """
    pager = os.environ.get("PATCHMAP_PAGER", os.environ.get("PAGER", "less"))
    if no_pager or not sys.stdout.isatty() or pager.strip() in ("", "cat"):
        sys.stdout.write(text)
        return
    env = dict(os.environ)
    env.setdefault("LESS", "FRX")
    try:
        proc = subprocess.Popen(pager, shell=True, stdin=subprocess.PIPE, env=env, text=True)
    except OSError:
        sys.stdout.write(text)
        return
    try:
        proc.stdin.write(text)
        proc.stdin.close()
    except BrokenPipeError:
        pass  # the pager quit before reading it all
    proc.wait()


def cmd_get(args):
    for r in load():
        if matches(r, args.key):
            print(dump_row(r), end="")


def cmd_rm(args):
    with Locked():
        rows = load()
        if args.key == "new":
            sys.exit("patchmap: 'new' matches every unpushed row; remove one by --dir, branch or patch number")
        gone = [r for r in rows if matches(r, args.key)]
        if not gone:
            sys.exit(f"patchmap: no row matches {args.key}")
        if len(gone) > 1 and not args.all:
            sys.exit(
                f"patchmap: {args.key} matches {len(gone)} rows; use --all to remove them all:\n"
                + "".join(dump_row(r) for r in gone)
            )
        save([r for r in rows if r not in gone])
        # print what went, so a mistake can be put back with `set`
        print(f"removed {len(gone)} row(s):")
        for r in gone:
            print(dump_row(r), end="")


def cmd_title(args):
    sid, app, title = own_session()
    if not (sid or app):
        sys.exit("patchmap: not in a Claude Code session ($CLAUDE_CODE_SESSION_ID unset)")
    print(title or sid)


def remote_argv(remote, argv, args):
    """The ssh command that runs a set, show, get or rm on the map's host.

    A set gets this host and this session filled in first: the far side
    has neither this session's environment nor its transcript.  A
    relative --dir is made absolute here, where it means something.
    """
    argv = list(argv)
    if args.cmd == "set":
        if args.dir:
            i = argv.index("--dir") if "--dir" in argv else None
            if i is not None:
                argv[i + 1] = tilde(args.dir)
            else:
                argv = [a if not a.startswith("--dir=") else "--dir=" + tilde(args.dir) for a in argv]
            if not args.branch:
                branch = git_branch(args.dir)
                if branch:
                    argv += ["--branch", branch]
        if not args.host:
            argv += ["--host", short_host()]
        if args.session is None:
            sid, app, title = own_session()
            if sid:
                argv += ["--session", sid]
                if app and not args.app:
                    argv += ["--app", app]
                if title and not args.title:
                    argv += ["--title", title]
    elif args.cmd == "show" and args.mine:
        sid = os.environ.get("CLAUDE_CODE_SESSION_ID")
        if not sid:
            sys.exit("patchmap: --mine needs $CLAUDE_CODE_SESSION_ID")
        argv = [a for a in argv if a != "--mine"] + ["--session", sid]
    cmd = os.environ.get("PATCHMAP_REMOTE_CMD", "patchmap")
    return ["ssh", "-o", "BatchMode=yes", remote, shlex.join([*shlex.split(cmd), *argv])]


def build_parser():
    p = argparse.ArgumentParser(
        prog="patchmap", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("set", help="add or update a row (found by patch, dir or branch)")
    s.add_argument("--patch", help="change number, TRACKER:N, PR (owner/repo#N), or new")
    s.add_argument("--ticket", "--lu", help="the work item: LU-nnnnn, or any [tickets] key")
    for f in ("desc", "branch", "dir", "host", "session", "app", "title", "state"):
        s.add_argument("--" + f)
    s.add_argument("--clear", action="append", choices=FIELDS, help="drop a field from the row")
    s.set_defaults(func=cmd_set)
    s = sub.add_parser("show", help='print rows as NNNNN ("TICKET desc"), with their links')
    s.add_argument("--mine", action="store_true", help="only this session's rows ($CLAUDE_CODE_SESSION_ID)")
    s.add_argument("--session", help="only rows of a session (id prefix or title text)")
    s.add_argument("--stale", action="store_true", help="only rows whose worktree on this host is gone")
    s.add_argument("--json", action="store_true", help="the rows as a JSON list, with their labels")
    s.add_argument("--no-pager", action="store_true", help="never page, even on a terminal")
    s.set_defaults(func=cmd_show)
    s = sub.add_parser("get", help="print matching rows as YAML")
    s.add_argument("key")
    s.set_defaults(func=cmd_get)
    s = sub.add_parser("rm", help="remove a row by patch, dir or branch")
    s.add_argument("key")
    s.add_argument("--all", action="store_true", help="remove every matching row, not only one")
    s.set_defaults(func=cmd_rm)
    s = sub.add_parser("title", help="print this session's name")
    s.set_defaults(func=cmd_title)
    return p


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    args = build_parser().parse_args(argv)
    remote = os.environ.get("PATCHMAP_REMOTE")
    if remote and args.cmd != "title" and remote not in (short_host(), socket.gethostname()):
        sys.exit(subprocess.run(remote_argv(remote, argv, args)).returncode)
    migrate()
    args.func(args)


if __name__ == "__main__":
    main()
