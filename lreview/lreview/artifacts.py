"""Provider-neutral review-result.json validation."""
import subprocess

REVIEW_RESULT_NAME = "review-result.json"

def validate_review_result(spec, repo, base_sha, head_sha):
    if not isinstance(spec, dict) or set(spec) - {"version", "message", "findings"}:
        raise ValueError("review-result.json has unknown fields or is not an object")
    if spec.get("version") != 1 or not isinstance(spec.get("message", ""), str):
        raise ValueError("review-result.json requires version 1 and string message")
    findings = spec.get("findings", [])
    if not isinstance(findings, list): raise ValueError("findings must be a list")
    changed = _changed_new_lines(repo, base_sha, head_sha)
    for finding in findings:
        if not isinstance(finding, dict) or set(finding) - {"path", "line", "side", "message", "location_kind", "unresolved"}:
            raise ValueError("finding has unknown fields")
        if not isinstance(finding.get("message"), str): raise ValueError("finding message must be a string")
        kind = finding.get("location_kind", "inline")
        path, line = finding.get("path"), finding.get("line")
        if kind == "inline":
            if not isinstance(path, str) or path.startswith("/") or ".." in path.split("/") or not isinstance(line, int) or line < 1:
                raise ValueError("inline findings require a safe path and positive line")
            if line not in changed.get(path, set()): raise ValueError(f"{path}:{line} is not an added PR line")
        elif kind not in ("summary", "commit_message") or path is not None or line is not None:
            raise ValueError("summary/commit_message findings require null path and line")
    return spec

def _changed_new_lines(repo, base, head):
    output = subprocess.check_output(["git", "-C", str(repo), "diff", "--unified=0", f"{base}...{head}"], text=True)
    found, path, line = {}, None, None
    for text in output.splitlines():
        if text.startswith("+++ b/"): path = text[6:]
        elif text.startswith("@@"):
            import re
            m = re.search(r"\+(\d+)(?:,(\d+))?", text); line = int(m.group(1)) if m else None
            count = int(m.group(2) or 1) if m else 0
            if path and line is not None: found.setdefault(path, set()).update(range(line, line + count))
    return found
