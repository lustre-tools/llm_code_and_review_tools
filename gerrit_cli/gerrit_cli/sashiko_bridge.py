"""Bridge between Gerrit and Sashiko automated code review.

Fetches patches from Gerrit, submits them to a running Sashiko instance
for review, and posts the structured findings back as Gerrit inline comments.

Usage:
    python -m gerrit_cli.sashiko_bridge review 64591
    python -m gerrit_cli.sashiko_bridge review 64591 --vote
    python -m gerrit_cli.sashiko_bridge review 64591 --dry-run
    python -m gerrit_cli.sashiko_bridge search "owner:self status:open -age:7d"
"""

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Optional

import requests

from .client import GerritCommentsClient
from .reviewer import CodeReviewer, ReviewResult


DEFAULT_SASHIKO_URL = "http://127.0.0.1:8080"
POLL_INTERVAL_SECONDS = 30
MAX_POLL_MINUTES = 60

# Severity -> Code-Review vote mapping
SEVERITY_VOTES = {
    "Critical": -2,
    "High": -1,
    "Medium": 0,
    "Low": 0,
}


def get_change_ref(client: GerritCommentsClient, change_number: int) -> dict[str, Any]:
    """Get the change detail including the current revision's fetch ref."""
    detail = client.get_change_detail(change_number)
    return detail


def fetch_change_into_repo(
    repo_path: str,
    gerrit_url: str,
    project: str,
    fetch_ref: str,
) -> str:
    """Fetch a Gerrit change ref into the local repository and return the FETCH_HEAD SHA."""
    fetch_url = f"{gerrit_url}/{project}"

    print(f"  Fetching {fetch_ref} from {fetch_url}...")
    result = subprocess.run(
        ["git", "fetch", fetch_url, fetch_ref],
        cwd=repo_path,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git fetch failed: {result.stderr}")

    # Get the SHA of FETCH_HEAD
    result = subprocess.run(
        ["git", "rev-parse", "FETCH_HEAD"],
        cwd=repo_path,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git rev-parse FETCH_HEAD failed: {result.stderr}")

    sha = result.stdout.strip()
    print(f"  Fetched commit: {sha[:12]}")
    return sha


def submit_to_sashiko(
    sashiko_url: str,
    sha: str,
    repo_path: Optional[str] = None,
) -> str:
    """Submit a commit SHA to Sashiko for review. Returns the submission ID."""
    url = f"{sashiko_url}/api/submit"
    payload: dict[str, Any] = {
        "type": "remote",
        "sha": sha,
    }
    if repo_path:
        payload["repo"] = repo_path

    try:
        resp = requests.post(url, json=payload, timeout=30)
        resp.raise_for_status()
    except requests.ConnectionError:
        raise RuntimeError(
            f"Cannot connect to Sashiko server at {sashiko_url}. "
            "Is the server running? Start it with: cd ~/sashiko/sashiko-gerrit-review && ./target/release/sashiko"
        )
    except requests.RequestException as e:
        raise RuntimeError(f"Sashiko server error: {e}")
    data = resp.json()
    print(f"  Submitted to Sashiko: id={data.get('id', '?')}, status={data.get('status', '?')}")
    return data.get("id", sha)


def poll_for_review(
    sashiko_url: str,
    patchset_id: str,
    max_minutes: int = MAX_POLL_MINUTES,
) -> Optional[dict[str, Any]]:
    """Poll Sashiko until the review is complete or timeout."""
    start = time.time()
    deadline = start + max_minutes * 60

    while time.time() < deadline:
        # First find the patchset by its submission ID
        try:
            resp = requests.get(
                f"{sashiko_url}/api/patch",
                params={"id": patchset_id},
                timeout=15,
            )
            if resp.status_code == 404:
                # Might not be ingested yet
                elapsed = int(time.time() - start)
                print(f"  Waiting for ingestion... ({elapsed}s)")
                time.sleep(POLL_INTERVAL_SECONDS)
                continue

            resp.raise_for_status()
            patchset = resp.json()
            status = patchset.get("status", "Unknown")

            if status in ("Reviewed", "Failed", "Failed To Apply"):
                ps_id = patchset.get("id")
                if ps_id is None:
                    return patchset

                # Fetch the review
                review_resp = requests.get(
                    f"{sashiko_url}/api/review",
                    params={"patchset_id": ps_id},
                    timeout=15,
                )
                if review_resp.status_code == 200:
                    review = review_resp.json()
                    return {
                        "patchset": patchset,
                        "review": review,
                    }
                else:
                    return {"patchset": patchset, "review": None}

            elif status in ("Pending", "In Review", "Applying", "Incomplete"):
                elapsed = int(time.time() - start)
                # Sashiko runs 9 review stages; estimate ~2-4 min per stage
                stage_hint = ""
                if status == "In Review":
                    # Check logs for stage progress
                    ps_id = patchset.get("id")
                    if ps_id:
                        try:
                            rev_resp = requests.get(
                                f"{sashiko_url}/api/review",
                                params={"patchset_id": ps_id},
                                timeout=5,
                            )
                            if rev_resp.status_code == 200:
                                rev = rev_resp.json()
                                logs = rev.get("logs") or ""
                                # Count completed stages from logs
                                import re
                                stages = re.findall(r"Stage (\d+)", logs)
                                if stages:
                                    last_stage = max(int(s) for s in stages)
                                    stage_hint = f", stage {last_stage}/9"
                                    remaining_stages = 9 - last_stage
                                    est_remaining = remaining_stages * 120  # ~2min/stage
                                    if est_remaining > 0:
                                        stage_hint += f", ~{est_remaining // 60}m remaining"
                        except Exception:
                            pass
                print(f"  Review in progress (status: {status}{stage_hint}, {elapsed}s elapsed)...")
                time.sleep(POLL_INTERVAL_SECONDS)
            else:
                print(f"  Unexpected status: {status}")
                time.sleep(POLL_INTERVAL_SECONDS)

        except requests.RequestException as e:
            print(f"  Connection error: {e}")
            time.sleep(POLL_INTERVAL_SECONDS)

    print(f"  Timeout after {max_minutes} minutes")
    return None


def findings_to_gerrit_comments(
    findings: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], str]:
    """Convert Sashiko findings to Gerrit inline comment format.

    Returns:
        Tuple of (inline_comments, summary_message)
    """
    inline_comments = []
    unlocated_findings = []

    for f in findings:
        severity = f.get("severity", "Low")
        problem = f.get("problem", "")
        explanation = f.get("severity_explanation", "")
        file_path = f.get("file_path")
        line_number = f.get("line_number")

        # Build the comment message
        msg_parts = [f"[{severity}] {problem}"]
        if explanation:
            msg_parts.append(f"\nReasoning: {explanation}")

        message = "\n".join(msg_parts)

        if file_path and line_number:
            inline_comments.append({
                "path": file_path,
                "line": line_number,
                "message": message,
                "unresolved": severity in ("High", "Critical"),
            })
        else:
            unlocated_findings.append(f"- [{severity}] {problem}")

    # Build summary
    severity_counts: dict[str, int] = {}
    for f in findings:
        sev = f.get("severity", "Low")
        severity_counts[sev] = severity_counts.get(sev, 0) + 1

    summary_parts = ["Sashiko Automated Review"]
    summary_parts.append("=" * 25)

    if not findings:
        summary_parts.append("No issues found. LGTM!")
    else:
        count_str = ", ".join(
            f"{count} {sev}" for sev, count in sorted(severity_counts.items())
        )
        summary_parts.append(f"Found {len(findings)} issue(s): {count_str}")

    if unlocated_findings:
        summary_parts.append("")
        summary_parts.append("General findings (no specific file location):")
        summary_parts.extend(unlocated_findings)

    summary = "\n".join(summary_parts)
    return inline_comments, summary


def compute_vote(findings: list[dict[str, Any]]) -> int:
    """Compute a Code-Review vote based on findings severity."""
    if not findings:
        return 0  # No findings = no vote (don't auto-approve)

    worst_severity = "Low"
    severity_rank = {"Low": 0, "Medium": 1, "High": 2, "Critical": 3}

    for f in findings:
        sev = f.get("severity", "Low")
        if severity_rank.get(sev, 0) > severity_rank.get(worst_severity, 0):
            worst_severity = sev

    return SEVERITY_VOTES.get(worst_severity, 0)


def do_review(
    change_number: int,
    sashiko_url: str = DEFAULT_SASHIKO_URL,
    repo_path: Optional[str] = None,
    dry_run: bool = False,
    vote: bool = False,
    max_minutes: int = MAX_POLL_MINUTES,
) -> dict[str, Any]:
    """Main review workflow: fetch from Gerrit, review with Sashiko, post back.

    Args:
        change_number: Gerrit change number
        sashiko_url: URL of running Sashiko instance
        repo_path: Path to the local kernel/lustre git repository
        dry_run: If True, print what would be posted but don't post
        vote: If True, include a Code-Review vote based on severity
        max_minutes: Maximum minutes to wait for review completion

    Returns:
        Dict with review results
    """
    print(f"\n{'='*60}")
    print(f"Sashiko Review for Gerrit Change #{change_number}")
    print(f"{'='*60}")

    # 1. Get change details from Gerrit
    print("\n[1/5] Fetching change details from Gerrit...")
    client = GerritCommentsClient()
    detail = client.get_change_detail(change_number)

    project = detail.get("project", "")
    subject = detail.get("subject", "")
    revisions = detail.get("revisions", {})

    # Find the current revision
    current_rev = detail.get("current_revision", "")
    rev_data = revisions.get(current_rev, {})
    patchset_number = rev_data.get("_number", "?")
    fetch_info = rev_data.get("fetch", {})

    # Get the fetch ref - try anonymous http first, then ssh
    fetch_ref = None
    gerrit_fetch_url = None
    for protocol in ("anonymous http", "http", "ssh"):
        if protocol in fetch_info:
            fetch_ref = fetch_info[protocol].get("ref")
            gerrit_fetch_url = fetch_info[protocol].get("url")
            break

    if not fetch_ref:
        # Construct it manually
        change_num_str = str(change_number)
        suffix = change_num_str[-2:] if len(change_num_str) >= 2 else change_num_str
        fetch_ref = f"refs/changes/{suffix}/{change_number}/{patchset_number}"

    gerrit_url = client.rest.url.rstrip("/")
    if not gerrit_fetch_url:
        gerrit_fetch_url = f"{gerrit_url}/{project}"

    print(f"  Project: {project}")
    print(f"  Subject: {subject}")
    print(f"  Patchset: {patchset_number}")
    print(f"  Ref: {fetch_ref}")

    # 2. Fetch into local repo
    print("\n[2/5] Fetching change into local repository...")
    if not repo_path:
        # Try to find a lustre checkout
        candidates = [
            "/mnt/additional_storage/lustre-release",
            Path.home() / "lustre-release",
            Path.cwd(),
        ]
        for c in candidates:
            if Path(c).exists() and (Path(c) / ".git").exists():
                repo_path = str(c)
                break

        if not repo_path:
            raise RuntimeError(
                "Cannot find Lustre git repository. Use --repo to specify the path."
            )

    print(f"  Using repo: {repo_path}")
    sha = fetch_change_into_repo(repo_path, gerrit_fetch_url, "", fetch_ref)

    # 3. Submit to Sashiko
    print("\n[3/5] Submitting to Sashiko for review...")
    submission_id = submit_to_sashiko(sashiko_url, sha, repo_path)

    # 4. Wait for review
    print("\n[4/5] Waiting for Sashiko review...")
    result = poll_for_review(sashiko_url, submission_id, max_minutes)

    if result is None:
        return {"success": False, "error": "Review timed out"}

    review = result.get("review")
    patchset_data = result.get("patchset", {})

    if review is None:
        status = patchset_data.get("status", "Unknown")
        reason = patchset_data.get("failed_reason", "No review data available")
        print(f"\n  Review status: {status}")
        if reason:
            print(f"  Reason: {reason}")
        return {"success": False, "error": f"Review {status}: {reason}"}

    # Extract findings
    findings = review.get("findings", [])
    summary_text = review.get("summary", "")

    # Token usage
    tokens_in = review.get("tokens_in") or 0
    tokens_out = review.get("tokens_out") or 0
    tokens_cached = review.get("tokens_cached") or 0
    tokens_total = tokens_in + tokens_out

    print(f"\n  Review complete!")
    print(f"  Model: {review.get('model', '?')}")
    print(f"  Summary: {summary_text}")
    print(f"  Findings: {len(findings)}")
    for f in findings:
        loc = ""
        if f.get("file_path"):
            loc = f" ({f['file_path']}:{f.get('line_number', '?')})"
        print(f"    [{f.get('severity', '?')}]{loc} {f.get('problem', '')[:80]}")

    # Token spend report
    print(f"\n  Token usage:")
    print(f"    Input:  {tokens_in:,} ({tokens_cached:,} cached)")
    print(f"    Output: {tokens_out:,}")
    print(f"    Total:  {tokens_total:,}")
    # Rough cost estimate for Sonnet 4.6: $3/MTok in, $15/MTok out
    cost_in = (tokens_in - tokens_cached) * 3.0 / 1_000_000
    cost_cached = tokens_cached * 0.30 / 1_000_000  # cached is 90% cheaper
    cost_out = tokens_out * 15.0 / 1_000_000
    cost_total = cost_in + cost_cached + cost_out
    print(f"    Est. cost: ${cost_total:.3f}")

    # 5. Post back to Gerrit
    print("\n[5/5] Posting review to Gerrit...")
    inline_comments, cover_message = findings_to_gerrit_comments(findings)

    review_vote = compute_vote(findings) if vote else None

    if dry_run:
        print("\n  DRY RUN - would post:")
        print(f"  Cover message: {cover_message[:200]}...")
        print(f"  Inline comments: {len(inline_comments)}")
        for c in inline_comments:
            print(f"    {c['path']}:{c['line']} - {c['message'][:60]}...")
        if review_vote is not None:
            print(f"  Vote: Code-Review {review_vote:+d}")
        return {
            "success": True,
            "dry_run": True,
            "findings": findings,
            "comments": inline_comments,
            "vote": review_vote,
        }

    reviewer = CodeReviewer()
    post_result = reviewer.post_review(
        change_number=change_number,
        comments=inline_comments if inline_comments else None,
        message=cover_message,
        vote=review_vote,
    )

    if post_result.success:
        print(f"  Posted {post_result.comments_posted} inline comment(s) to Gerrit")
        if review_vote is not None:
            print(f"  Vote: Code-Review {review_vote:+d}")
    else:
        print(f"  Failed to post review: {post_result.error}")

    return {
        "success": post_result.success,
        "findings": findings,
        "comments_posted": post_result.comments_posted,
        "vote": review_vote,
        "error": post_result.error,
    }


def main():
    parser = argparse.ArgumentParser(
        description="Bridge between Gerrit and Sashiko code review",
    )
    parser.add_argument(
        "--sashiko-url",
        default=DEFAULT_SASHIKO_URL,
        help=f"Sashiko server URL (default: {DEFAULT_SASHIKO_URL})",
    )
    parser.add_argument(
        "--repo",
        help="Path to local git repository (auto-detected if not specified)",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    # review subcommand
    review_parser = subparsers.add_parser(
        "review",
        help="Submit a Gerrit change for Sashiko review",
    )
    review_parser.add_argument(
        "change",
        help="Gerrit change number or URL",
    )
    review_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be posted without actually posting",
    )
    review_parser.add_argument(
        "--vote",
        action="store_true",
        help="Include a Code-Review vote based on findings severity",
    )
    review_parser.add_argument(
        "--timeout",
        type=int,
        default=MAX_POLL_MINUTES,
        help=f"Max minutes to wait for review (default: {MAX_POLL_MINUTES})",
    )

    args = parser.parse_args()

    if args.command == "review":
        change_input = args.change
        _, change_number = GerritCommentsClient.parse_gerrit_url(change_input)

        result = do_review(
            change_number=change_number,
            sashiko_url=args.sashiko_url,
            repo_path=args.repo,
            dry_run=args.dry_run,
            vote=args.vote,
            max_minutes=args.timeout,
        )

        if not result.get("success"):
            sys.exit(1)


if __name__ == "__main__":
    main()
