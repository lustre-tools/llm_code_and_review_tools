# Gerrit CLI Tool

CLI for Gerrit code review: extract/reply to comments, manage reviewers,
triage Maloo test results, review patch series, and manage changes.

## Installation

```bash
cd /shared/llm_code_and_review_tools/gerrit_cli && ./install.sh
# or: pip install -e .
```

## Agent Skill Description

**Gerrit CLI Tool** - Extract unresolved comments from Gerrit code reviews and reply to them. Supports extracting comment threads with surrounding code context, replying to comments with custom messages, and marking comments as "Done" or "Acknowledged" to resolve threads.

### Capabilities

1. **Extract Comments**: Get all unresolved review comments from a Gerrit change URL
   - Returns structured data with comment threads, authors, patch sets, and line numbers
   - Includes surrounding code context for each comment
   - Outputs JSON for easy programmatic use

2. **Reply to Comments**: Post replies to comment threads
   - Reply with custom message
   - Mark as "Done" to resolve the thread
   - Mark as "Acknowledged" to resolve without changes
   - Batch reply to multiple comments

3. **Code Review**: Review code changes and post review comments
   - Fetch diffs and file changes from a Gerrit change
   - View changed files with full diff context
   - Post review comments on specific lines
   - Set Code-Review vote (+1, -1, +2, -2)

4. **Find Patch Series**: Find all patches in a linear series
   - Given any patch URL, finds the complete series from base to tip
   - Traces parent commits to build the linear chain
   - Returns structured data with change numbers, subjects, and URLs
   - Note: Follows one linear path only; branches are not included

5. **Series Graph**: Interactive DAG visualizer for patch series
   - Shows the full topology of all related changes as an interactive HTML graph
   - Resolves stale patchset dependencies to show which patches need rebasing
   - Edge labels show patchset numbers; stale edges highlighted in orange
   - Click-to-re-anchor navigation, dark/light mode, filter controls

6. **Series Comments**: Get all unresolved comments from an entire patch series
   - Collects comments across all patches in a series
   - Groups comments by patch with metadata (change number, subject, patchset)
   - Includes author information and code context
   - Useful for reviewing feedback on a multi-patch series at once

## CLI Usage

### Extract Comments

```bash
# Extract unresolved comments with code context
gc extract https://review.whamcloud.com/c/fs/lustre-release/+/62796

# Output as JSON
gc extract --json https://review.whamcloud.com/c/fs/lustre-release/+/62796

# Include resolved comments too
gc extract --all https://review.whamcloud.com/c/fs/lustre-release/+/62796
```

### Reply to Comments

```bash
# Reply to thread 0 with a message
gc reply https://review.whamcloud.com/c/fs/lustre-release/+/62796 0 "Fixed in next patchset"

# Mark thread 0 as done
gc reply --done https://review.whamcloud.com/c/fs/lustre-release/+/62796 0

# Acknowledge thread 1
gc reply --ack https://review.whamcloud.com/c/fs/lustre-release/+/62796 1

# Reply and resolve
gc reply --resolve https://review.whamcloud.com/c/fs/lustre-release/+/62796 0 "Implemented as suggested"
```

### Batch Reply

Create a JSON file with replies:

```json
[
  {"thread_index": 0, "message": "Done", "mark_resolved": true},
  {"thread_index": 1, "message": "Will address in follow-up", "mark_resolved": false}
]
```

Then run:

```bash
gc batch https://review.whamcloud.com/c/fs/lustre-release/+/62796 replies.json
```

### Code Review

```bash
# Get code changes for review (human-readable format)
gc review https://review.whamcloud.com/c/fs/lustre-release/+/62796

# Get code changes as JSON (for programmatic use)
gc review --json https://review.whamcloud.com/c/fs/lustre-release/+/62796

# Show only added/deleted lines (not full diff context)
gc review --changes-only https://review.whamcloud.com/c/fs/lustre-release/+/62796

# Post a code review with comments from JSON file
gc review --post-comments review.json https://review.whamcloud.com/c/fs/lustre-release/+/62796
```

Review JSON file format:
```json
{
  "message": "Some suggestions for improvement",
  "vote": -1,
  "comments": [
    {"path": "file.c", "line": 42, "message": "Consider using const here"},
    {"path": "file.c", "line": 100, "message": "Missing error handling"}
  ]
}
```

### Find Patch Series

```bash
# Find all patches in a series (given any patch in the series)
gc series https://review.whamcloud.com/c/fs/lustre-release/+/61965

# Output as JSON
gc series --json https://review.whamcloud.com/c/fs/lustre-release/+/61965

# Output only URLs (one per line, useful for scripting)
gc series --urls-only https://review.whamcloud.com/c/fs/lustre-release/+/61965

# Output only change numbers (one per line)
gc series --numbers-only https://review.whamcloud.com/c/fs/lustre-release/+/61965

# Include abandoned patches in the series
gc series --include-abandoned https://review.whamcloud.com/c/fs/lustre-release/+/61965
```

### Series Comments

```bash
# Get all unresolved comments from all patches in a series
gc series-comments https://review.whamcloud.com/c/fs/lustre-release/+/61965

# Output as JSON
gc series-comments --json https://review.whamcloud.com/c/fs/lustre-release/+/61965

# Include resolved comments too
gc series-comments --all https://review.whamcloud.com/c/fs/lustre-release/+/61965

# Exclude code context (faster)
gc series-comments --no-context https://review.whamcloud.com/c/fs/lustre-release/+/61965
```

### Series Graph (DAG Visualizer)

Visualize the full DAG of all related changes for a patch series as an
interactive HTML graph. Unlike `series` which traces a single linear chain,
`graph` shows the complete topology — branches, abandoned forks, stale
patchsets, and which patches need rebasing.

```bash
# Generate and open interactive graph (default: opens in browser)
gc graph https://review.whamcloud.com/c/fs/lustre-release/+/61962

# Just a change number (uses GERRIT_URL)
gc graph 61962

# Save to a specific file
gc graph 61962 -o series.html

# Generate without opening browser
gc graph 61962 --no-open

# Include detailed inline comments (slower, fetches per-change)
gc graph 61962 --comments

# Skip CI link fetching for faster generation
gc graph 61962 --skip-ci-details
```

The generated HTML is self-contained (uses vis.js from CDN) and includes:

- **Vertical tree layout** growing upward from the anchor change
- **Review health coloring**: green (verified OK + 2 non-author CR +1s),
  red (any verified -1 or CR veto), blue (pending)
- **Review status line** on each node: verified voters (J:+1 M:-1) and
  code review summary (CR: 3x(+1))
- **Unresolved comment count** on nodes (from batch query, no extra calls)
- **Edge labels** showing which patchset each dependency goes through
  (e.g., `ps57`). Stale edges show `ps53->57` in orange with dashed lines
- **Click-to-re-anchor**: click any node to make it the new starting point;
  the tree re-layouts with that node as the root
- **Filters**: toggle abandoned/stale branches on/off
- **Side panel**: click a node to see full details including:
  - Review health badge, all verified voters with clickable Jenkins/Maloo links
  - Code review votes with reviewer names (author votes dimmed)
  - Unresolved comments list with clickable links to Gerrit (with `--comments`)
  - Chain of dependents/dependencies, and a "Re-anchor here" button
- **Dark/Light mode**: toggle with the "Light" button
- **Keyboard shortcuts**: `F` = fit to screen, `Z` = focus/zoom to selected
  node, `R` = reset to initial anchor

## Python API

### Extract Comments

```python
from gerrit_cli import extract_comments

# Extract unresolved comments
result = extract_comments(
    "https://review.whamcloud.com/c/fs/lustre-release/+/62796",
    include_resolved=False,  # Only unresolved
    include_code_context=True,  # Include surrounding code
    context_lines=3,  # Lines of context above/below
)

# Print summary
print(result.format_summary())

# Access structured data
print(f"Found {result.unresolved_count} unresolved threads")
for thread in result.threads:
    print(f"File: {thread.root_comment.file_path}")
    print(f"Line: {thread.root_comment.line}")
    print(f"Author: {thread.root_comment.author.name}")
    print(f"Message: {thread.root_comment.message}")

    # Code context
    if thread.root_comment.code_context:
        print("Context:")
        print(thread.root_comment.code_context.format())

# Export as JSON
import json
print(json.dumps(result.to_dict(), indent=2))
```

### Reply to Comments

```python
from gerrit_cli import CommentReplier, extract_comments

# Extract first
result = extract_comments("https://review.whamcloud.com/c/fs/lustre-release/+/62796")

# Reply to a thread
replier = CommentReplier()
reply_result = replier.reply_to_thread(
    change_number=62796,
    thread=result.threads[0],
    message="Fixed in patchset 5",
    mark_resolved=True,  # Mark as done
)

if reply_result.success:
    print("Reply posted!")
else:
    print(f"Error: {reply_result.error}")
```

### Mark as Done

```python
from gerrit_cli import CommentReplier, extract_comments

result = extract_comments("https://review.whamcloud.com/c/fs/lustre-release/+/62796")

replier = CommentReplier()
done_result = replier.mark_thread_done(
    change_number=62796,
    thread=result.threads[0],
    message="Done",  # Optional custom message
)
```

### Batch Reply

```python
from gerrit_cli import CommentReplier, extract_comments

result = extract_comments("https://review.whamcloud.com/c/fs/lustre-release/+/62796")

replier = CommentReplier()
results = replier.batch_reply(
    change_number=62796,
    replies=[
        {"comment": result.threads[0].root_comment, "message": "Done", "mark_resolved": True},
        {"comment": result.threads[1].root_comment, "message": "Will fix", "mark_resolved": False},
    ],
)

for r in results:
    print(f"Comment {r.comment_id}: {'✓' if r.success else '✗'}")
```

### Code Review

```python
from gerrit_cli import CodeReviewer, get_review_data, post_review

# Get review data for a change
review_data = get_review_data("https://review.whamcloud.com/c/fs/lustre-release/+/62796")

# Print formatted review information
print(review_data.format_for_review())

# Access structured data
print(f"Project: {review_data.change_info.project}")
print(f"Subject: {review_data.change_info.subject}")
print(f"Files changed: {len(review_data.files)}")

for file in review_data.files:
    print(f"  {file.path}: +{file.lines_added}/-{file.lines_deleted}")

    # Access individual hunks and lines
    for hunk in file.hunks:
        for line in hunk.lines:
            if line.type == "added":
                print(f"    +{line.line_number_new}: {line.content}")
            elif line.type == "deleted":
                print(f"    -{line.line_number_old}: {line.content}")

# Post a code review with comments
reviewer = CodeReviewer()
result = reviewer.post_review(
    change_number=62796,
    comments=[
        {"path": "lustre/utils/file.c", "line": 42, "message": "Consider using const here"},
        {"path": "lustre/utils/file.c", "line": 100, "message": "Missing error handling"},
    ],
    message="Some suggestions for improvement.",
    vote=-1,  # Code-Review vote: -2, -1, 0, +1, +2
)

if result.success:
    print(f"Posted {result.comments_posted} comments")
else:
    print(f"Error: {result.error}")

# Post a single inline comment
result = reviewer.post_comment(
    change_number=62796,
    path="lustre/utils/file.c",
    line=42,
    message="Consider using const here",
)
```

### Find Patch Series

```python
from gerrit_cli import find_series, SeriesFinder

# Find all patches in a series (given any patch URL)
series = find_series("https://review.whamcloud.com/c/fs/lustre-release/+/61965")

# Print summary
print(series.format_summary())

# Access structured data
print(f"Total patches: {len(series)}")
print(f"Target change {series.target_change} is at position {series.target_position}")
print(f"Base: {series.base_change}, Tip: {series.tip_change}")

# Iterate through patches (ordered base to tip)
for patch in series.patches:
    print(f"{patch.change_number}: {patch.subject}")
    print(f"  URL: {patch.url}")
    print(f"  Commit: {patch.commit} -> Parent: {patch.parent_commit}")

# Get just the change numbers or URLs
change_numbers = series.get_change_numbers()  # [62217, 62791, ...]
urls = series.get_urls()  # ["https://...", ...]

# Export as JSON
import json
print(json.dumps(series.to_dict(), indent=2))

# Use SeriesFinder directly for more control
finder = SeriesFinder()
series = finder.find_series_by_change(61965, include_abandoned=True)
```

### Series Comments

```python
from gerrit_cli import get_series_comments, SeriesFinder

# Get all unresolved comments from all patches in a series
result = get_series_comments(
    "https://review.whamcloud.com/c/fs/lustre-release/+/61965",
    include_resolved=False,  # Only unresolved
    include_code_context=True,  # Include surrounding code
    context_lines=3,  # Lines of context above/below
)

# Print summary
print(result.format_summary())
# Output: SERIES COMMENTS (130 unresolved across 17 patches)

# Access structured data
print(f"Total unresolved: {result.total_unresolved}")
print(f"Patches with comments: {result.patches_with_unresolved}")

# Iterate through patches with comments
for patch_comments in result.patches_with_comments:
    print(f"Change {patch_comments.change_number}: {patch_comments.subject}")
    print(f"  URL: {patch_comments.url}")
    print(f"  Patchset: {patch_comments.current_patchset}")
    print(f"  Unresolved: {patch_comments.unresolved_count}")

    for thread in patch_comments.threads:
        comment = thread.root_comment
        print(f"    {comment.file_path}:{comment.line or 'patchset'}")
        print(f"    Author: {comment.author.name}")
        print(f"    Message: {comment.message[:50]}...")
        if comment.code_context:
            print(f"    Context:\n{comment.code_context.format()}")

# Export as JSON
import json
print(json.dumps(result.to_dict(), indent=2))

# Use SeriesFinder directly for more control
finder = SeriesFinder()
result = finder.get_series_comments(
    url="https://review.whamcloud.com/c/fs/lustre-release/+/61965",
    include_resolved=True,
    include_code_context=False,
)
```

## Configuration

Configure via environment variables or `~/.config/gerrit-cli/.env`:
```bash
export GERRIT_URL="https://review.whamcloud.com"
export GERRIT_USER="your-username"
export GERRIT_PASS="your-http-password"
```

If `gerrit-cli` is not found after install, add `$HOME/.local/bin` to PATH.

## Output Format

All commands return JSON: `{"ok": true, "data": {...}, "meta": {"tool": "gerrit-cli", ...}}`

## Quick Start

```bash
# 1. Start reviewing - checks out first patch with comments
gc review-series <URL>

# 2. For each patch: fix issues, stage replies, finish
gc stage --done <index>        # Mark comment as done
gc stage <index> "message"     # Reply with message
git add <files> && git commit --amend --no-edit
gc finish-patch                # Auto-advances to next

# 3. When done
gc abort --keep-changes
git push origin HEAD:refs/for/master
```

## Commands

### Comments & Review
```bash
comments <URL>                   # Get unresolved comments
reply <thread_index> "msg"       # Reply to a comment
reply <thread_index> --done      # Mark as done
review <URL>                     # Get code changes for review
```

### Workflow
```bash
review-series <URL>              # Start review, checkout first patch
stage --done <index>             # Stage "Done" reply
stage <index> "message"          # Stage reply with message
finish-patch                     # Complete patch, auto-advance
abort                            # End session, restore original state
abort --keep-changes             # End session, keep current state
```

### Navigation
```bash
work-on-patch <URL> <change>     # Jump to specific patch
next-patch                       # Manually advance to next patch
status                           # Check session status (default if in session)
```

### Information
```bash
series-status <URL>              # Show status of all patches in series
series-comments <URL>            # Get comments for all patches in series
info <URL>                       # Show change info (reviewers, labels, etc.)
maloo <URL> [URL...]             # Triage Maloo CI results (batch mode)
watch <json-file>                # Check CI status on watched patches
```

### Reviewer Management
```bash
add-reviewer <URL> "Name"        # Add a reviewer to a change
reviewers <URL>                  # List reviewers on a change
```

### Change Management
```bash
checkout <URL>                   # Fetch and checkout a Gerrit change
abandon <URL>                    # Abandon a Gerrit change
message <URL> "text"             # Post a top-level message
```

### Interactive Mode
```bash
interactive <URL>                # Interactive mode for reviewing comments
i <URL>                          # Shorthand for interactive
```

### Staging Management
```bash
staged list                      # List all staged operations
staged show <change>             # Show staged for specific change
staged remove <change> <index>   # Remove one staged operation
staged clear [change]            # Clear staged (one change or all)
staged refresh <url>             # Refresh staged metadata
push <change>                    # Push staged operations for a change
```

### Reintegration (for stale patches)
```bash
continue-reintegration           # Continue after resolving conflicts
skip-reintegration               # Skip conflicting change
```

## Key Points

1. **Work earliest to latest** - finish-patch rebases all later patches
2. **Stage replies as you fix** - Don't forget to stage for each comment
3. **finish-patch auto-advances** - Finds the next patch with comments
4. **Conflicts?** - Fix them, run `git add`, then `finish-patch` again
5. **Stale patches?** - Auto-reintegrated (or prompts for conflict resolution)

## Error Handling

Exit codes: 0=success, 1=general, 2=auth, 3=not found, 4=invalid input, 5=network.

Error responses include: `code` (machine-readable), `message` (human-readable),
`http_status`, and `details`.

## Development

```bash
pip install -e ".[dev]"
pytest gerrit_cli/tests/
pytest gerrit_cli/tests/ --cov=gerrit_cli
ruff check gerrit_cli/
```
