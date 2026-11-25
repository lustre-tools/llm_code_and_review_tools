"""VM validation of review findings (--validate).

After a review, one validator agent per change tries to show each
selected finding on an ltvm VM: it builds the newest revision of the
change, writes a reproducer and a QA report, runs the reproducer, and
gives each finding a verdict with evidence (validate-prompt.md).  The
verdicts go into the manifest; `post` holds refuted findings back,
annotates confirmed ones, and replies in the thread of a finding that
is on Gerrit already.

The findings come from lreview's own review JSON, or, for a change
with no local review, from the unresolved AI-review threads on Gerrit.

Design: docs/validate-design.md.
"""

import json
import os
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

VALIDATE_PROMPT_PATH = Path(__file__).resolve().parent / "validate-prompt.md"

# Severity markers from review-prompts kernel/gerrit-review.md.
MARKERS = ("defect", "minor", "style", "typo", "suggestion", "nit")
DEFAULT_CLASSES = ("defect",)
# A comment that carries no marker counts as this one.
UNMARKED = "minor"

VERDICTS = ("confirmed", "refuted", "obsolete", "inconclusive",
            "not-testable")
METHODS = ("unmodified", "fail_loc", "kernel-debug", "instrumented", "build")
# Verdicts that reach Gerrit.  The others stay in the results.
POSTED = ("confirmed", "refuted")
# Verdicts that count only with a file under validation/ behind them.
NEEDS_PROOF = ("confirmed", "refuted", "obsolete")
# Verdicts that count only with a QA report (F<n>/report.md).
NEEDS_REPORT = ("confirmed", "refuted")
# A second validation of the same revision tests these again; it
# keeps every other verdict.
RETEST = ("inconclusive",)

VALIDATION_JSON_NAME = "validation.json"
VALIDATION_DIR_NAME = "validation"
REPORT_NAME = "report.md"
# A verdict text longer than this gets cut in the Gerrit comment.
REPLY_LIMIT = 4000

DEFAULT_VALIDATE_MODEL = "opus"
DEFAULT_VALIDATE_EFFORT = "high"
DEFAULT_VALIDATE_TARGET = "rocky9"
DEFAULT_VALIDATE_TIMEOUT = 5400
# The prefix of posted verdicts; <model> is the validator's model.
VALIDATION_PREFIX = "[AI validation - <model>]"
# The Bash tool's own limit on one command; the default 10 minutes
# stops a Lustre build part-way.
BASH_MAX_TIMEOUT_MS = 3_600_000
# A foreground ltvm build or deploy writes nothing to the event log
# until it returns.  The stall limit must be longer than the longest
# command the Bash tool allows.
DEFAULT_VALIDATE_STALL = BASH_MAX_TIMEOUT_MS // 1000 + 300

_MARKER_RE = re.compile(r"^\s*\(([A-Za-z-]+)\)")
# The bot prefix line that a posted review comment starts with:
# "**[AI review - opus]**", or a plain "[Marc Bot]".
_PREFIX_LINE_RE = re.compile(r"^\s*(\*\*)?\[[^\]\n]*\](\*\*)?[ \t]*\n+")


def parse_classes(text: Optional[str]) -> tuple:
    """The markers named by --validate-classes ("defect,minor", "all")."""
    if not text:
        return DEFAULT_CLASSES
    names = [n.strip().lower().strip("()") for n in text.split(",")]
    names = [n for n in names if n]
    if "all" in names:
        return MARKERS
    bad = [n for n in names if n not in MARKERS]
    if bad:
        raise ValueError(
            f"unknown finding class(es): {', '.join(bad)}; "
            f"use {', '.join(MARKERS)} or all")
    if not names:
        return DEFAULT_CLASSES
    return tuple(dict.fromkeys(names))


def strip_prefix(message: str) -> str:
    """A posted comment's text without the bot prefix line."""
    return _PREFIX_LINE_RE.sub("", message or "", count=1)


def marker(message: str) -> str:
    """The severity marker a comment starts with, or UNMARKED."""
    match = _MARKER_RE.match(strip_prefix(message))
    if match and match.group(1).lower() in MARKERS:
        return match.group(1).lower()
    return UNMARKED


@dataclass
class Revision:
    """A revision of a change: what a validator tests, or what a
    finding was reviewed on."""
    sha: str
    patchset: Optional[int] = None
    ref: str = ""
    merged: bool = False
    branch: Optional[str] = None

    def label(self) -> str:
        return (f"patchset {self.patchset}" if self.patchset
                else f"commit {self.sha[:12]}")


def tested_revision(change) -> Revision:
    return Revision(sha=change.sha, patchset=change.patchset,
                    ref=getattr(change, "ref", "") or "",
                    merged=getattr(change, "status", None) == "MERGED",
                    branch=getattr(change, "branch", None))


@dataclass
class Finding:
    id: str
    file: str
    line: Optional[int]
    index: Optional[int]  # position in spec["comments"][file]
    cls: str
    message: str
    # Stable across runs: the Gerrit comment ID, else file and text.
    key: str = ""
    gerrit_id: Optional[str] = None
    reviewed_sha: Optional[str] = None
    reviewed_patchset: Optional[int] = None

    def as_dict(self) -> dict:
        return {"id": self.id, "file": self.file, "line": self.line,
                "comment_index": self.index, "class": self.cls,
                "message": self.message,
                "reviewed_commit": self.reviewed_sha,
                "reviewed_patchset": self.reviewed_patchset}


def _text_key(path: str, line, message: str) -> str:
    """The key of a finding from lreview's own review: a re-review of
    the same commit writes new findings, which must not inherit the
    verdicts of the old ones."""
    import hashlib
    digest = hashlib.sha1(strip_prefix(message).strip().encode()).hexdigest()
    return f"{path}:{line}#{digest[:12]}"


def select_findings(spec: Optional[dict], classes,
                    reviewed: Optional[Revision] = None,
                    ids: Optional[dict] = None) -> list[Finding]:
    """The review comments to validate, in ID order.

    Each finding gets an ID F<n>.  `ids` maps the keys of an earlier
    validation to their IDs: a finding keeps its ID, and a new one gets
    the next free number, so that F<n> and its evidence directory mean
    the same finding in every run.  Without `ids`, the IDs follow file
    order, then comment order.  Notes (unresolved: false) are never
    validated.  A comment that carries its own revision (`commit_id`,
    `patch_set`: from Gerrit) overrides `reviewed`."""
    comments = (spec or {}).get("comments")
    if not isinstance(comments, dict):
        return []
    ids = dict(ids or {})
    taken = {int(v[1:]) for v in ids.values()
             if isinstance(v, str) and v[1:].isdigit()}
    out: list[Finding] = []
    for path in sorted(comments):
        entries = comments[path]
        if not isinstance(entries, list):
            continue
        for index, entry in enumerate(entries):
            if not isinstance(entry, dict):
                continue
            if not entry.get("unresolved", True):
                continue
            cls = marker(entry.get("message", ""))
            if cls not in classes:
                continue
            line = entry.get("line")
            if line is None and isinstance(entry.get("range"), dict):
                line = entry["range"].get("end_line")
            gerrit_id = entry.get("gerrit_id")
            key = gerrit_id or _text_key(path, line,
                                         entry.get("message", ""))
            fid = ids.get(key)
            if fid is None:
                n = max(taken | {0}) + 1
                taken.add(n)
                fid = ids[key] = f"F{n}"
            out.append(Finding(
                fid, path, line, index, cls,
                strip_prefix(entry.get("message", "")), key=key,
                gerrit_id=gerrit_id,
                reviewed_sha=(entry.get("commit_id")
                              or (reviewed.sha if reviewed else None)),
                reviewed_patchset=(entry.get("patch_set")
                                   or (reviewed.patchset if reviewed
                                       else None))))
    return sorted(out, key=lambda f: int(f.id[1:]))


# --- findings from Gerrit -------------------------------------------

def gerrit_findings_spec(gerrit_comments: dict, tag: str,
                         revisions: Optional[dict] = None) -> dict:
    """A review JSON built from the AI-review threads on Gerrit.

    Each unresolved thread whose root comment carries `tag` becomes one
    comment, from any account.  A thread is unresolved when its newest
    comment is.  The comment keeps the root's Gerrit ID, patchset and
    commit, so that a verdict can reply in its thread.  `revisions`
    maps a patchset number to its commit, for a Gerrit that does not
    send commit_id."""
    by_id = {}
    for path, items in (gerrit_comments or {}).items():
        for c in items or []:
            if isinstance(c, dict) and c.get("id"):
                by_id[c["id"]] = {**c, "_path": path}

    def root_of(c):
        seen = set()
        while c.get("in_reply_to") and c["id"] not in seen:
            seen.add(c["id"])
            parent = by_id.get(c["in_reply_to"])
            if parent is None:
                return None
            c = parent
        return None if c.get("in_reply_to") else c

    threads: dict = {}
    for c in by_id.values():
        root = root_of(c)
        if root is not None:
            threads.setdefault(root["id"], []).append(c)
    comments: dict = {}
    for root_id, members in threads.items():
        root = by_id[root_id]
        path = root["_path"]
        if root.get("tag") != tag or path.startswith("/PATCHSET"):
            continue
        last = max(members, key=lambda c: c.get("updated") or "")
        if not last.get("unresolved"):
            continue
        commit = root.get("commit_id") or (revisions or {}).get(
            root.get("patch_set"))
        entry = {"message": strip_prefix(root.get("message", "")),
                 "unresolved": True, "gerrit_id": root_id,
                 "patch_set": root.get("patch_set"), "commit_id": commit}
        for k in ("line", "range", "side"):
            if root.get(k) is not None:
                entry[k] = root[k]
        comments.setdefault(path, []).append(entry)
    for entries in comments.values():
        entries.sort(key=lambda e: (e.get("line") or 0, e["gerrit_id"]))
    return {"comments": comments, "source": "gerrit"}


@dataclass
class ValidateConfig:
    classes: tuple = DEFAULT_CLASSES
    jobs: int = 1
    model: Optional[str] = DEFAULT_VALIDATE_MODEL
    effort: Optional[str] = DEFAULT_VALIDATE_EFFORT
    timeout: int = DEFAULT_VALIDATE_TIMEOUT
    stall_timeout: float = DEFAULT_VALIDATE_STALL
    target: str = DEFAULT_VALIDATE_TARGET
    baseline: bool = False
    keep_vms: bool = False
    # Test every finding again, also those with a kept verdict.
    force: bool = False
    # One per batch: how many validations hold VMs at the same time.
    gate: threading.BoundedSemaphore = field(init=False, repr=False)
    # Set on Ctrl-C: a validation that gets the gate after it starts
    # no validator.
    cancel: threading.Event = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if self.jobs < 1:
            raise ValueError(f"validate jobs must be >= 1, got {self.jobs}")
        self.gate = threading.BoundedSemaphore(self.jobs)
        self.cancel = threading.Event()


def owner_id(change) -> str:
    """The LTVM_OWNER_ID of one validation: lreview finds and destroys
    the validator's VMs by it."""
    return f"lreview:{os.getpid()}:{change.slug}"


def vm_role(change) -> str:
    """The role part of the VM names the validator uses."""
    tag = str(change.number) if change.number else change.sha[:7]
    return f"lrv-{tag}"


def ltvm_skill_path() -> Optional[Path]:
    """The ltvm skill, which the validator reads for ltvm usage."""
    candidates = [os.environ.get("LTVM_SKILL"),
                  str(Path.home() / ".claude" / "skills" / "ltvm" /
                      "SKILL.md")]
    for c in candidates:
        if c and Path(c).expanduser().is_file():
            return Path(c).expanduser().resolve()
    return None


def validator_prompt(vcfg: ValidateConfig, change, findings: list[Finding],
                     owner: str) -> str:
    """The validator's prompt: the bundled protocol, then this run."""
    skill = ltvm_skill_path()
    skill_line = (f"Read the ltvm guide first: {skill}" if skill else
                  "No ltvm guide was found on this host. Use "
                  "`ltvm --help` and `ltvm <command> --help`.")
    tested = tested_revision(change)
    older = sorted({(f.reviewed_patchset, f.reviewed_sha) for f in findings
                    if f.reviewed_sha and f.reviewed_sha != tested.sha},
                   key=lambda x: (x[0] or 0, x[1]))
    if older:
        revs = ", ".join(f"patchset {ps} = {sha}" if ps else sha
                         for ps, sha in older)
        reviewed_line = (
            f"Some findings were reviewed on an older revision ({revs}). "
            "The commits are in the repository. For each such finding, "
            "first compare the code (`git diff <reviewed commit> HEAD -- "
            "<file>`). Give `obsolete` when the code that the finding "
            "names is gone or no longer does what the finding claims.")
    else:
        reviewed_line = ("Every finding was reviewed on the tested "
                         "revision. Do not give `obsolete`.")
    findings_json = json.dumps([f.as_dict() for f in findings], indent=2)
    per_finding = max(10, vcfg.timeout // 60 // max(1, len(findings)))
    run = f"""
## This run

- Change: {_change_label(change)}
- Tested revision: {tested.sha} ({tested.label()}), checked out in the
  current directory{" -- the change is MERGED" if tested.merged else ""}
- {reviewed_line}
- Default ltvm target: {vcfg.target}
- VM role for names: `{vm_role(change)}` (name VMs `<hh><N>-{vm_role(change)}`
  as the ltvm guide says; add a suffix such as `-srv` for cluster nodes)
- `LTVM_OWNER_ID` is already set to `{owner}`. Do not change it. lreview
  destroys every VM with this owner when you exit.
- Baseline run on the parent commit: {"ON" if vcfg.baseline else "OFF"}
- Time limit for the whole validation: {vcfg.timeout // 60} minutes.
  Give each finding at most {per_finding} minutes, then move on.
- {skill_line}

### Findings to validate

Use these IDs for the directories and in `validation.json`.

```json
{findings_json}
```
"""
    return VALIDATE_PROMPT_PATH.read_text() + run


def _change_label(change) -> str:
    if change.number:
        return (f"Gerrit change {change.number} ps{change.patchset}: "
                f"{change.subject}")
    return f"local commit {change.sha[:12]}: {change.subject}"


def _file_under(root: Optional[Path], name) -> Optional[Path]:
    """root/name when that is a file inside root, else None."""
    if root is None or not isinstance(name, str) or not name:
        return None
    path = (root / name).resolve()
    if path.is_relative_to(root.resolve()) and path.is_file():
        return path
    return None


def _has_files(item: dict, root: Optional[Path]) -> bool:
    """Whether the reproducer or an evidence file of a verdict exists
    under the collected evidence directory."""
    names = [item.get("reproducer"), *(item.get("evidence") or [])]
    return any(_file_under(root, n) for n in names)


def _text(value) -> str:
    if isinstance(value, list):
        return "\n".join(str(v) for v in value).strip()
    return str(value or "").strip()


def normalize(spec: Optional[dict], findings: list[Finding],
              error: Optional[str] = None,
              evidence_root: Optional[Path] = None,
              tested: Optional[Revision] = None) -> dict:
    """The validator's output, checked and completed: exactly one
    verdict per finding, in the order of `findings`.  A finding the
    validator gave no valid verdict for is inconclusive.  So is:

    - a confirmed, refuted or obsolete verdict with no reproducer or
      evidence file under evidence_root;
    - an obsolete verdict for a finding that was reviewed on the
      tested revision.

    A confirmed or refuted verdict with no QA report keeps its verdict,
    with a note: the Gerrit reply does not use the report.

    An entry's "note" is lreview's text, not the validator's."""
    given = {}
    for item in (spec or {}).get("findings") or []:
        if isinstance(item, dict) and item.get("id"):
            given.setdefault(str(item["id"]), item)
    out = []
    for f in findings:
        item = given.get(f.id)
        verdict = (item or {}).get("verdict")
        base = {"id": f.id, "key": f.key, "file": f.file, "line": f.line,
                "comment_index": f.index, "class": f.cls,
                "gerrit_id": f.gerrit_id,
                "reviewed_commit": f.reviewed_sha,
                "reviewed_patchset": f.reviewed_patchset,
                "posted": False}
        if verdict not in VERDICTS:
            reason = ("validator gave no verdict" if item is None else
                      f"validator gave an unknown verdict {verdict!r}")
            if error:
                reason += f" ({error})"
            out.append({**base, "verdict": "inconclusive",
                        "method": None, "summary": "", "note": reason,
                        "reproducer": None, "evidence": [], "report": None,
                        "steps": "", "actual": ""})
            continue
        method = item.get("method")
        evidence = item.get("evidence") or []
        if isinstance(evidence, str):
            evidence = [evidence]
        item = {**item, "evidence": evidence}
        report = item.get("report") or f"{f.id}/{REPORT_NAME}"
        has_report = _file_under(evidence_root, report) is not None
        note = None
        if verdict in NEEDS_PROOF and not _has_files(item, evidence_root):
            note = (f"validator said {verdict}, but left no reproducer "
                    "or evidence file")
        elif (verdict == "obsolete" and tested is not None
              and f.reviewed_sha in (None, tested.sha)):
            note = ("validator said obsolete, but the finding was "
                    "reviewed on the tested revision")
        if note:
            verdict = "inconclusive"
        elif verdict in NEEDS_REPORT and not has_report:
            note = "the validator wrote no QA report"
        out.append({
            **base, "verdict": verdict, "note": note,
            "method": method if method in METHODS else None,
            "baseline": item.get("baseline"),
            "target": item.get("target"),
            "summary": _text(item.get("summary")),
            "steps": _text(item.get("steps")),
            "actual": _text(item.get("actual")),
            "reproducer": item.get("reproducer"),
            "instrumentation": item.get("instrumentation"),
            "evidence": [str(e) for e in evidence],
            "report": report if has_report else None,
        })
    return {"target": (spec or {}).get("target"),
            "vms": (spec or {}).get("vms") or [],
            "findings": out}


def counts(validation: dict) -> dict:
    out = {v: 0 for v in VERDICTS}
    for item in validation.get("findings") or []:
        out[item["verdict"]] = out.get(item["verdict"], 0) + 1
    return out


def counts_text(c: dict) -> str:
    parts = [f"{c[v]} {v}" for v in VERDICTS if c.get(v)]
    return ", ".join(parts) or "nothing validated"


# --- VMs -------------------------------------------------------------

def _ltvm(*args, owner: Optional[str] = None,
          timeout=600) -> subprocess.CompletedProcess:
    """Run ltvm.  With owner, ltvm acts as that owner: it destroys a VM
    that the validator claimed (a claim of another live owner refuses
    a destroy, --force or not)."""
    env = {**os.environ, "LTVM_NO_UPDATE": "1"}
    if owner:
        env.update(LTVM_OWNER_ID=owner, LTVM_OWNER_PID=str(os.getpid()))
    return subprocess.run(["ltvm", "--no-update", *args],
                          capture_output=True, text=True, timeout=timeout,
                          env=env)


def owned_vms(owner: str) -> tuple[list[str], list[str]]:
    """(clusters, standalone VMs) that carry this owner ID."""
    try:
        proc = _ltvm("list", "--json", timeout=60)
        data = json.loads(proc.stdout)
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        return [], []
    clusters = [c["name"] for c in data.get("clusters") or []
                if c.get("owner_id") == owner]
    members = {m for c in data.get("clusters") or []
               if c.get("name") in clusters for m in c.get("members") or []}
    vms = [v["name"] for v in data.get("vms") or []
           if v.get("owner_id") == owner and v["name"] not in members]
    return clusters, vms


def destroy_owned_vms(owner: str) -> list[str]:
    """Destroy every cluster and VM with this owner; returns the names."""
    clusters, vms = owned_vms(owner)
    gone = []
    for name in clusters:
        try:
            if _ltvm("cluster", "destroy", name, "--force",
                     owner=owner).returncode == 0:
                gone.append(name)
        except (OSError, subprocess.SubprocessError):
            pass
    if vms:
        try:
            if _ltvm("destroy", "--force", *vms,
                     owner=owner).returncode == 0:
                gone.extend(vms)
        except (OSError, subprocess.SubprocessError):
            pass
    return gone


# --- running a validation -------------------------------------------

def artifact_names(slug: str, mode: str) -> tuple[str, str]:
    """(validation JSON, evidence directory) of a manifest entry."""
    from .runner import artifact_tag
    stem = f"validation-{slug}{artifact_tag(mode)}"
    return f"{stem}.json", stem


def index_name(json_name: str) -> str:
    return json_name[:-len(".json")] + ".md"


def refuted_name(json_name: str) -> str:
    return "refuted-" + json_name[len("validation-"):]


def previous_validation(results_dir: Path, json_name: str,
                        tested: Revision) -> Optional[dict]:
    """The earlier validation of this tested revision, or None.
    Verdicts on another revision say nothing about this one."""
    try:
        old = json.loads((results_dir / json_name).read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(old, dict):
        return None
    if (old.get("tested") or {}).get("sha") != tested.sha:
        return None
    return old


def plan_findings(spec: Optional[dict], vcfg: ValidateConfig,
                  reviewed: Optional[Revision], previous: Optional[dict]
                  ) -> tuple[list[Finding], list[Finding], dict]:
    """(all selected findings, those to test now, kept verdicts by ID).

    A verdict of an earlier validation of the same tested revision
    stays, unless it is inconclusive or --force is on.  A kept verdict
    keeps its posted mark.  So does the verdict of a finding outside
    this run's classes: a later run with more classes must not post
    it twice."""
    old_items = {i["key"]: i for i in (previous or {}).get("findings") or []
                 if i.get("key")}
    ids = {k: i["id"] for k, i in old_items.items()}
    findings = select_findings(spec, vcfg.classes, reviewed, ids)
    kept = {}
    if not vcfg.force:
        for f in findings:
            old = old_items.get(f.key)
            if old and old.get("verdict") not in RETEST:
                kept[f.id] = old
    todo = [f for f in findings if f.id not in kept]
    selected = {f.key for f in findings}
    for key, old in old_items.items():
        if key not in selected:
            kept[old["id"]] = old
    return findings, todo, kept


def _collect(src: Path, dest: Path, ids: list[str]) -> None:
    """Copy the validator's validation/ directory into the results.
    A finding tested again loses its old evidence first; the evidence
    of a kept verdict stays."""
    dest.mkdir(parents=True, exist_ok=True)
    for fid in ids:
        old = dest / fid
        if old.is_dir():
            shutil.rmtree(old, ignore_errors=True)
    if src.is_dir():
        shutil.copytree(src, dest, symlinks=True, dirs_exist_ok=True)


def run_validation(config, vcfg: ValidateConfig, change, worktree: Path,
                   findings: list[Finding], mode: str = "full", *,
                   slug: Optional[str] = None, kept: Optional[dict] = None,
                   all_findings: Optional[list] = None,
                   review_spec: Optional[dict] = None,
                   on_gerrit: bool = False) -> dict:
    """Run the validator for one change in its worktree and collect the
    verdicts.  `change` is the tested revision.  `findings` are the
    ones to test, `kept` the verdicts of the others (by ID), `slug`
    names the artifacts (default: the change's).  Returns the
    manifest's "validation" record.  The VMs the validator made are
    destroyed before this returns, also on error."""
    from .agents import get_agent
    from .runner import (_log, _run_agent, ReviewStalled, detect_model,
                         live_token_count, parse_final_usage)
    from .ui import console

    slug = slug or change.slug
    kept = kept or {}
    all_findings = all_findings or findings
    tested = tested_revision(change)
    json_name, dir_name = artifact_names(slug, mode)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    log_path = config.results_dir / f"{dir_name}-{stamp}.{os.getpid()}.log"
    owner = owner_id(change)
    record = {"classes": list(vcfg.classes), "selected": len(all_findings),
              "tested": len(findings), "kept": len(kept),
              "model": vcfg.model, "effort": vcfg.effort,
              "target": vcfg.target, "baseline": vcfg.baseline,
              "revision": tested.sha, "patchset": tested.patchset,
              "merged": tested.merged,
              "log": log_path.name, "json": None, "dir": None,
              "index": None, "refuted": None,
              "status": "failed", "counts": None, "error": None,
              "vms_destroyed": [], "posted": False}
    for stale in (worktree / VALIDATION_JSON_NAME,
                  worktree / VALIDATION_DIR_NAME):
        if stale.is_dir():
            shutil.rmtree(stale, ignore_errors=True)
        elif stale.exists():
            stale.unlink()

    agent = get_agent("claude")
    cmd = agent.build_cmd(
        vcfg.model, vcfg.effort, [],
        validator_prompt(vcfg, change, findings, owner),
        lean=config.lean)
    env = {**agent.env(config.lean),
           "LTVM_OWNER_ID": owner,
           "LTVM_OWNER_PID": str(os.getpid()),
           "LTVM_NO_UPDATE": "1",
           "BASH_MAX_TIMEOUT_MS": str(BASH_MAX_TIMEOUT_MS),
           **config.env}

    with vcfg.gate:
        if vcfg.cancel.is_set():
            record.update(status="cancelled", error="interrupted")
            return record
        _log(f"[{slug}] {console.color('cyan', 'validation started')}"
             f": {len(findings)} finding(s) "
             f"({', '.join(vcfg.classes)}) on {tested.label()}, "
             f"{vcfg.target}"
             + (f"; {len(kept)} verdict(s) kept" if kept else ""))
        start = time.monotonic()
        error = None
        try:
            returncode = _run_agent(cmd, worktree, log_path, vcfg.timeout,
                                    env, None, vcfg.stall_timeout)
            record["status"] = "done"
            if returncode != 0:
                error = f"validator exited {returncode}"
        except subprocess.TimeoutExpired:
            record["status"] = "timeout"
            error = f"timed out after {vcfg.timeout}s"
        except ReviewStalled as exc:
            record["status"] = "stalled"
            error = f"no output for {int(exc.silent_s)}s"
        except OSError as exc:
            error = str(exc)
        finally:
            if vcfg.keep_vms:
                kept_vms = owned_vms(owner)
                if kept_vms[0] or kept_vms[1]:
                    _log(f"[{slug}] keeping VMs "
                         f"{', '.join(kept_vms[0] + kept_vms[1])} "
                         "(--keep-vms)")
            else:
                record["vms_destroyed"] = destroy_owned_vms(owner)
                left = owned_vms(owner)
                if left[0] or left[1]:
                    _log(f"[{slug}] warning: could not destroy "
                         f"{', '.join(left[0] + left[1])}; remove them "
                         "with ltvm destroy")
        record["duration_s"] = round(time.monotonic() - start)

    tokens, cost = parse_final_usage(log_path)
    record["tokens"] = tokens if tokens is not None else live_token_count(
        log_path)
    record["cost_usd"] = cost
    record["model"] = detect_model(log_path, vcfg.model)

    spec = None
    out_json = worktree / VALIDATION_JSON_NAME
    if out_json.is_file():
        try:
            spec = json.loads(out_json.read_text())
            if not isinstance(spec, dict):
                raise ValueError("expected a JSON object")
        except (OSError, ValueError) as exc:
            spec = None
            error = error or f"{VALIDATION_JSON_NAME}: {exc}"
    elif error is None:
        error = f"validator wrote no {VALIDATION_JSON_NAME}"
    dest = config.results_dir / dir_name
    _collect(worktree / VALIDATION_DIR_NAME, dest, [f.id for f in findings])
    validation = normalize(spec, findings, error, dest, tested)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    for item in validation["findings"]:
        item["validated_at"] = stamp
    validation["findings"] = sorted(
        validation["findings"] + list(kept.values()),
        key=lambda i: int(i["id"][1:]))
    validation["evidence_dir"] = dir_name
    validation["tested"] = {"sha": tested.sha, "patchset": tested.patchset,
                            "merged": tested.merged,
                            "branch": tested.branch}
    validation["model"] = record["model"]
    record["dir"] = dir_name
    record.update(finish(config.results_dir, json_name, validation,
                         on_gerrit, review_spec))
    record["error"] = error
    color = "red" if error else "green"
    _log(f"[{slug}] {console.color(color, 'validation ' + record['status'])}"
         f": {counts_text(record['counts'])}"
         + (f" ({error})" if error else "") + f" -> {json_name}")
    return record


def finish(results_dir: Path, json_name: str, validation: dict,
           on_gerrit: bool, review_spec: Optional[dict]) -> dict:
    """Write the validation JSON, its index and, for a review not yet on
    Gerrit, the file of refuted findings.  Returns the record fields."""
    (results_dir / json_name).write_text(
        json.dumps(validation, indent=2) + "\n")
    index = index_name(json_name)
    (results_dir / index).write_text(index_text(validation))
    refuted = None
    if not on_gerrit:
        refuted = write_refuted(results_dir, json_name, validation,
                                review_spec)
    return {"json": json_name, "index": index, "refuted": refuted,
            "counts": counts(validation),
            "posted": all_posted(validation)}


def all_posted(validation: dict) -> bool:
    return all(i.get("posted") for i in validation.get("findings") or []
               if i["verdict"] in POSTED)


def validate_change(config, vcfg: ValidateConfig, change,
                    worktree: Path, spec: Optional[dict],
                    mode: str = "full", *,
                    reviewed: Optional[Revision] = None,
                    slug: Optional[str] = None,
                    on_gerrit: bool = False) -> Optional[dict]:
    """Select and validate the findings of one change, or None when no
    comment is in the selected classes.  `change` is the revision to
    test (the newest); `reviewed` is the revision the review saw
    (default: the same).  `on_gerrit`: the findings are on Gerrit
    already, so none is held back.  A review that ran in place (in the
    user's checkout) is validated in a worktree of its own."""
    from .runner import _log
    slug = slug or change.slug
    json_name, _ = artifact_names(slug, mode)
    tested = tested_revision(change)
    previous = previous_validation(config.results_dir, json_name, tested)
    findings, todo, kept = plan_findings(
        spec, vcfg, reviewed or tested, previous)
    if not findings:
        return None
    if not todo:
        _log(f"[{slug}] validation: every finding has a verdict on "
             f"{tested.label()}; nothing to test (--force tests again)")
        validation = {**previous, "findings": sorted(
            kept.values(), key=lambda i: int(i["id"][1:]))}
        record = {"classes": list(vcfg.classes), "selected": len(findings),
                  "tested": 0, "kept": len(kept), "status": "kept",
                  "model": previous.get("model"), "revision": tested.sha,
                  "patchset": tested.patchset, "merged": tested.merged,
                  "dir": previous.get("evidence_dir"), "error": None}
        record.update(finish(config.results_dir, json_name, validation,
                             on_gerrit, spec))
        return record
    from . import worktree as wt
    from .runner import prepare_worktree
    own = None
    if Path(worktree).resolve() == Path(config.repo).resolve():
        own = prepare_worktree(config, change)
        worktree = own
    try:
        return run_validation(config, vcfg, change, worktree, todo, mode,
                              slug=slug, kept=kept, all_findings=findings,
                              review_spec=spec, on_gerrit=on_gerrit)
    finally:
        if own is not None and not config.keep_worktrees:
            wt.remove_worktree(config.repo, own)


# --- reports ---------------------------------------------------------

def _loc(item: dict) -> str:
    return (f"{item['file']}:{item['line']}" if item.get("line")
            else item["file"])


def evidence_path(results_dir: Path, validation: dict,
                  name: Optional[str]) -> Optional[Path]:
    if not name or not validation.get("evidence_dir"):
        return None
    return results_dir / validation["evidence_dir"] / name


def index_text(validation: dict) -> str:
    """validation-<change>.md: one line for each finding, with a link to
    its QA report."""
    tested = validation.get("tested") or {}
    rev = (f"patchset {tested['patchset']}" if tested.get("patchset")
           else f"commit {str(tested.get('sha') or '')[:12]}")
    lines = [f"# VM validation, {rev}", "",
             f"Tested commit: `{tested.get('sha')}`"
             + (" (merged)" if tested.get("merged") else ""),
             f"Verdicts: {counts_text(counts(validation))}", "",
             "| ID | Verdict | Class | Location | Report | Summary |",
             "|----|---------|-------|----------|--------|---------|"]
    for item in validation.get("findings") or []:
        report = item.get("report")
        link = (f"[{report}]({validation.get('evidence_dir')}/{report})"
                if report else "-")
        summary = (item.get("summary") or item.get("note") or "")
        summary = summary.replace("|", "\\|").replace("\n", " ")
        lines.append(f"| {item['id']} | {item['verdict']} | {item['class']} "
                     f"| `{_loc(item)}` | {link} | {summary} |")
    return "\n".join(lines) + "\n"


def load_record_validation(results_dir: Path,
                           record: Optional[dict]) -> Optional[dict]:
    if not record or not record.get("json"):
        return None
    try:
        return json.loads((results_dir / record["json"]).read_text())
    except (OSError, json.JSONDecodeError):
        return None


def report_lines(results_dir: Path, record: Optional[dict]) -> list[str]:
    """The end-of-run lines of one change: each finding with its verdict
    and its report (else its reproducer), then the index, the refuted
    findings file, and for a merged change the ticket reminder."""
    validation = load_record_validation(results_dir, record)
    if not validation:
        return []
    out = []
    for item in validation.get("findings") or []:
        path = (evidence_path(results_dir, validation, item.get("report"))
                or evidence_path(results_dir, validation,
                                 item.get("reproducer")))
        text = f"{item['id']} {item['verdict']:<12} {_loc(item)}"
        if path:
            text += f"  {path}"
        if item.get("note"):
            text += f"  ({item['note']})"
        out.append(text)
    if record.get("index"):
        out.append(f"index: {results_dir / record['index']}")
    if record.get("refuted"):
        out.append("refuted findings, to post by hand: "
                   f"{results_dir / record['refuted']}")
    confirmed = counts(validation).get("confirmed", 0)
    tested = validation.get("tested") or {}
    if tested.get("merged") and confirmed:
        branch = tested.get("branch") or "its branch"
        out.append(f"MERGED: {confirmed} confirmed bug(s) are in {branch}. "
                   "Each one needs a ticket.")
    return out


def reports_text(results_dir: Path, record: Optional[dict]) -> str:
    """The QA reports of a validation, for the text dump of a local
    review."""
    validation = load_record_validation(results_dir, record)
    if not validation:
        return ""
    chunks = []
    for item in validation.get("findings") or []:
        head = f"--- {item['id']} {item['verdict']}: {_loc(item)}"
        path = evidence_path(results_dir, validation, item.get("report"))
        try:
            body = path.read_text().strip() if path else ""
        except OSError:
            body = ""
        body = body or item.get("summary") or item.get("note") or ""
        chunks.append(f"{head}\n{body}")
    return "\n\n".join(chunks)


def write_refuted(results_dir: Path, json_name: str, validation: dict,
                  review_spec: Optional[dict]) -> Optional[str]:
    """refuted-<change>.json: the refuted findings in the format of
    `gc review --post-comments`, for a person who wants to post one
    anyway.  None (and no file) when nothing is refuted."""
    path = results_dir / refuted_name(json_name)
    comments: dict = {}
    for item in validation.get("findings") or []:
        if item["verdict"] != "refuted":
            continue
        try:
            original = dict(review_spec["comments"][item["file"]]
                            [item["comment_index"]])
        except (KeyError, IndexError, TypeError):
            original = {"line": item.get("line")}
        evidence = evidence_path(results_dir, validation,
                                 item.get("report") or item.get("reproducer"))
        note = ("VM validation refuted this finding on "
                f"{_rev_text(validation)}")
        note += f": {item['summary']}" if item.get("summary") else "."
        if evidence:
            note += f"\nEvidence: {evidence}"
        original["message"] = (
            f"{(original.get('message') or '').rstrip()}\n\n{note}")
        original["unresolved"] = True
        comments.setdefault(item["file"], []).append(original)
    if not comments:
        if path.exists():
            path.unlink()
        return None
    path.write_text(json.dumps({"tag": "autogenerated:ai-review",
                                "comments": comments}, indent=2) + "\n")
    return path.name


# --- posting ----------------------------------------------------------

_METHOD_TEXT = {
    "unmodified": "unmodified code",
    "fail_loc": "fail_loc",
    "kernel-debug": "kernel debug options",
    "instrumented": "instrumented code",
    "build": "build",
}


def load_validation(results_dir: Path, entry: dict) -> Optional[dict]:
    return load_record_validation(results_dir, entry.get("validation"))


def save_validation(results_dir: Path, entry: dict,
                    validation: dict) -> None:
    """Write back a validation after a post marked verdicts posted."""
    record = entry["validation"]
    (results_dir / record["json"]).write_text(
        json.dumps(validation, indent=2) + "\n")
    record["posted"] = all_posted(validation)


def validation_prefix(validation: dict, record: Optional[dict]) -> str:
    model = (validation.get("model") or (record or {}).get("model")
             or "AI")
    return VALIDATION_PREFIX.replace("<model>", model)


def tested_sha(validation: Optional[dict]) -> Optional[str]:
    return ((validation or {}).get("tested") or {}).get("sha")


def _rev_text(validation: dict) -> str:
    tested = validation.get("tested") or {}
    if tested.get("patchset"):
        return f"patchset {tested['patchset']}"
    return "the tested commit"


def _cap(text: str) -> str:
    if len(text) <= REPLY_LIMIT:
        return text
    return (text[:REPLY_LIMIT].rstrip()
            + "\n\n[cut: the full QA report is with the person who ran "
            "the validation]")


def reply_text(item: dict, validation: Optional[dict] = None) -> str:
    """The text of a confirmed or refuted verdict, for a reply in the
    finding's thread or a note under the finding in a new review.  A
    confirmed one carries the steps to reproduce and the actual
    result."""
    how = _METHOD_TEXT.get(item.get("method") or "", "")
    where = ", ".join(x for x in (how, item.get("target")) if x)
    word = "Confirmed" if item["verdict"] == "confirmed" else "Refuted"
    head = f"{word} on a VM, {_rev_text(validation or {})}"
    if where:
        head += f" ({where})"
    text = f"{head}: {item['summary']}" if item.get("summary") else head
    if item["verdict"] == "confirmed":
        if item.get("steps"):
            text += f"\n\nSteps to reproduce:\n{item['steps']}"
        if item.get("actual"):
            text += f"\n\nActual result:\n{item['actual']}"
    return _cap(text)


def apply_verdicts(spec: dict, validation: dict) -> tuple[dict, list]:
    """The review JSON to post: refuted and obsolete findings removed,
    confirmed ones annotated.  Returns (new spec, the held findings)."""
    by_pos = {(i["file"], i["comment_index"]): i
              for i in validation.get("findings") or []}
    comments = spec.get("comments")
    if not isinstance(comments, dict):
        return spec, []
    held = []
    out = {}
    for path, entries in comments.items():
        kept = []
        for index, entry in enumerate(entries or []):
            item = by_pos.get((path, index))
            if item and item["verdict"] in ("refuted", "obsolete"):
                held.append(item)
                continue
            if item and item["verdict"] == "confirmed":
                entry = {**entry, "message":
                         f"{entry.get('message', '').rstrip()}\n\n"
                         f"{reply_text(item, validation)}"}
            kept.append(entry)
        if kept:
            out[path] = kept
    return {**spec, "comments": out}, held


def decisive(validation: Optional[dict]) -> bool:
    """Whether a validation has a verdict worth posting."""
    return any(i["verdict"] in POSTED
               for i in (validation or {}).get("findings") or [])


def unposted(validation: Optional[dict]) -> list[dict]:
    """The confirmed and refuted verdicts not yet on Gerrit."""
    return [i for i in (validation or {}).get("findings") or []
            if i["verdict"] in POSTED and not i.get("posted")]


def cover_line(validation: dict) -> Optional[str]:
    """The line under the cover message of a review posted with its
    verdicts applied, or None when no verdict is decisive."""
    held = ("refuted", "obsolete")
    if not any(i["verdict"] in POSTED + held
               for i in validation.get("findings") or []):
        return None
    c = counts(validation)
    parts = [f"{c[v]} {v}" + (" (not posted)" if v in held else "")
             for v in VERDICTS if c.get(v)]
    line = "VM validation: " + ", ".join(parts) + "."
    defects = [i for i in validation.get("findings") or []
               if i["class"] == "defect"]
    if defects and all(i["verdict"] == "refuted" for i in defects):
        line += (" The VM runs refuted every (defect) finding, so none "
                 "of them is posted.")
    return line


def verdict_message(validation: dict,
                    unmatched: Optional[list] = None) -> Optional[str]:
    """The patchset-level message that goes with verdict replies, or
    None when nothing is left to post.  It gives the counts and lists
    each verdict in `unmatched`: those whose thread lreview could not
    find."""
    if not unposted(validation):
        return None
    c = counts(validation)
    lines = [f"VM validation of the findings on {_rev_text(validation)}: "
             + ", ".join(f"{c[v]} {v}" for v in VERDICTS if c.get(v))
             + "."]
    rest = [f"- ({i['class']}) {_loc(i)}: {reply_text(i, validation)}"
            for i in unmatched or []]
    if rest:
        lines += [""] + rest
    return "\n".join(lines)


def find_posted(gerrit_comments: dict, spec: dict, item: dict,
                sha: str, tag: str) -> Optional[dict]:
    """The published Gerrit comment that a finding went out as, or None.

    A finding read from Gerrit carries its comment ID.  For one from
    lreview's own review, Gerrit does not return comment IDs when a
    review is posted, so lreview finds its comment by file, line, tag,
    revision and text.  The posted text carries the review prefix in
    front, so it ends with the finding's message."""
    if item.get("gerrit_id"):
        for c in gerrit_comments.get(item["file"]) or []:
            if c.get("id") == item["gerrit_id"]:
                return c
        return {"id": item["gerrit_id"], "line": item.get("line"),
                "patch_set": item.get("reviewed_patchset"),
                "commit_id": item.get("reviewed_commit")}
    try:
        original = spec["comments"][item["file"]][item["comment_index"]]
    except (KeyError, IndexError, TypeError):
        return None
    text = (original.get("message") or "").strip()
    line = original.get("line")
    if line is None and isinstance(original.get("range"), dict):
        line = original["range"].get("end_line")
    if not text:
        return None
    matches = [
        c for c in gerrit_comments.get(item["file"]) or []
        if c.get("tag") == tag and not c.get("in_reply_to")
        and c.get("line") == line
        and c.get("commit_id", sha) == sha
        and (c.get("message") or "").strip().endswith(text)]
    if not matches:
        return None
    return max(matches, key=lambda c: c.get("updated") or "")


def plan_replies(gerrit_comments: dict, spec: dict, validation: dict,
                 sha: str, tag: str) -> tuple[dict, list]:
    """({revision: {file: [reply]}}, unmatched items) for the verdicts
    not yet posted.  A reply goes on the revision of the comment it
    answers, so the replies go in one review per revision.  Each reply
    carries "_id", the finding ID, which the poster removes."""
    groups: dict = {}
    unmatched = []
    for item in unposted(validation):
        parent = find_posted(gerrit_comments, spec, item, sha, tag)
        if parent is None:
            unmatched.append(item)
            continue
        revision = str(parent.get("commit_id") or parent.get("patch_set")
                       or item.get("reviewed_commit") or sha)
        reply = {"in_reply_to": parent["id"],
                 "message": reply_text(item, validation),
                 "unresolved": True, "_id": item["id"]}
        if parent.get("line") is not None:
            reply["line"] = parent["line"]
        groups.setdefault(revision, {}).setdefault(
            item["file"], []).append(reply)
    return groups, unmatched


# --- QA reports to the ticket -----------------------------------------

TICKET_RE = re.compile(r"^\s*([A-Z][A-Z0-9]+-\d+)\b")


def ticket_of(subject: Optional[str]) -> Optional[str]:
    """The Jira key at the start of a commit subject ("LU-21098 lnet:
    ..."), or None."""
    m = TICKET_RE.match(subject or "")
    return m.group(1) if m else None


def report_attachment_name(entry: dict, validation: dict, item: dict,
                           path: Optional[Path] = None) -> str:
    """A name unique to one QA report: the change, the tested patchset
    and commit, the finding ID and the time of the verdict.  A verdict
    from before lreview recorded that time uses the time of the report
    file."""
    tested = validation.get("tested") or {}
    sha = (tested.get("sha") or entry.get("sha") or "")[:8]
    ps = tested.get("patchset") or entry.get("patchset")
    stamp = item.get("validated_at")
    if not stamp and path is not None and path.exists():
        stamp = time.strftime("%Y%m%dT%H%M%SZ",
                              time.gmtime(path.stat().st_mtime))
    stamp = stamp or "undated"
    return (f"lreview-{entry['number']}-ps{ps}-{sha}-{item['id']}-"
            f"{stamp}.md")


def change_url(entry: dict) -> str:
    base = (entry.get("base_url") or "").rstrip("/")
    return f"{base}/c/{entry.get('repository')}/+/{entry['number']}"


def comment_url(entry: dict, comment_id: str) -> str:
    return f"{change_url(entry)}/comment/{comment_id}/"


def finding_links(results_dir: Path, entry: dict, validation: dict,
                  items: list, comments=None) -> dict:
    """{finding ID: URL of the Gerrit comment of the finding}.  A
    finding read from Gerrit carries its comment ID.  For a finding
    from lreview's own posted review, lreview reads the published
    comments (`comments`, a callable that returns them) and finds the
    comment as the Gerrit replies do.  A finding with no comment found
    gets no link."""
    links = {i["id"]: comment_url(entry, i["gerrit_id"])
             for i in items if i.get("gerrit_id")}
    rest = [i for i in items if i["id"] not in links]
    if not rest or not entry.get("posted"):
        return links
    try:
        published = comments() if comments else _published(entry)
        spec = json.loads((results_dir / entry["json"]).read_text())
    except Exception:  # noqa: BLE001 - fall back to file and line
        return links
    from .poster import DEFAULT_TAG
    for item in rest:
        parent = find_posted(published or {}, spec, item, entry["sha"],
                             DEFAULT_TAG)
        if parent and parent.get("id"):
            links[item["id"]] = comment_url(entry, parent["id"])
    return links


def _published(entry: dict) -> dict:
    from gerrit_cli.client import GerritCommentsClient
    client = (GerritCommentsClient(url=entry["base_url"])
              if entry.get("base_url") else GerritCommentsClient())
    return client.get_comments(entry["number"])


def report_comment(entry: dict, validation: dict,
                   attached: list[tuple[dict, str]],
                   links: Optional[dict] = None) -> str:
    """The short Jira comment that goes with the attached reports.  Each
    line names the Gerrit comment of the finding, else its file and
    line."""
    links = links or {}
    tested = validation.get("tested") or {}
    sha = (tested.get("sha") or entry.get("sha") or "")[:10]
    ps = tested.get("patchset") or entry.get("patchset")
    lines = [f"{validation_prefix(validation, entry.get('validation'))} "
             f"A test on a VM confirmed {len(attached)} review finding(s) "
             f"in {change_url(entry)} patchset {ps} (commit {sha}).",
             "The QA report of each finding is attached:"]
    lines += [f"- {links.get(item['id']) or _loc(item)}: {name}"
              for item, name in attached]
    return "\n".join(lines)


def _jira(args: list, run=subprocess.run) -> tuple[bool, str]:
    try:
        proc = run(["jira", *args], capture_output=True, text=True,
                   timeout=300)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    if proc.returncode != 0:
        text = (proc.stderr or proc.stdout or "").strip()
        return False, text.splitlines()[-1] if text else \
            f"jira exited {proc.returncode}"
    return True, ""


def post_reports(results_dir: Path, entry: dict, *, dry_run: bool = False,
                 run=subprocess.run, comments=None) -> tuple[str, str]:
    """Attach the QA report of each confirmed finding to the ticket that
    the commit subject names, with one short comment.  A report that
    went to the ticket before is not attached again.  Returns (status,
    detail): status is "posted", "would post", "skipped" or "error"."""
    if entry.get("number") is None or entry.get("local"):
        return "skipped", "a local review has no change to link"
    validation = load_validation(results_dir, entry)
    if not validation:
        return "skipped", "no validation"
    ticket = ticket_of(entry.get("subject"))
    if ticket is None:
        return "skipped", "the commit subject names no ticket"
    todo = []
    for item in validation.get("findings") or []:
        if item["verdict"] != "confirmed" or item.get("report_posted"):
            continue
        path = evidence_path(results_dir, validation, item.get("report"))
        if path is None or not path.is_file():
            continue
        todo.append((item, path,
                     report_attachment_name(entry, validation, item,
                                            path)))
    if not todo:
        return "skipped", f"no new QA report of a confirmed finding " \
                          f"for {ticket}"
    if dry_run:
        return "would post", (f"{len(todo)} report(s) to {ticket}: "
                              + ", ".join(n for _, _, n in todo))
    attached, errors = [], []
    for item, path, name in todo:
        ok, error = _jira(["attachment", "upload", ticket, str(path),
                           "--filename", name], run)
        if not ok:
            errors.append(f"{item['id']}: {error}")
            continue
        item["report_posted"] = {"ticket": ticket, "attachment": name}
        attached.append((item, name))
    if attached:
        # Record the uploads before the comment, so that a failed
        # comment does not attach the reports a second time.
        save_validation(results_dir, entry, validation)
        links = finding_links(results_dir, entry, validation,
                              [i for i, _ in attached], comments)
        ok, error = _jira(["comment", ticket, "--body",
                           report_comment(entry, validation, attached,
                                          links)], run)
        if not ok:
            errors.append(f"comment: {error}")
    detail = f"{len(attached)} report(s) attached to {ticket}"
    if errors:
        return "error", detail + "; " + "; ".join(errors)
    return "posted", detail
