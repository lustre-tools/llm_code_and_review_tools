"""Gerrit Janitor HTTP client.

The Janitor has no REST API -- results are static HTML pages and YAML
files served from https://testing.whamcloud.com/gerrit-janitor/<build>/
(redirects to /lustre-reports/<build>/).

This client scrapes the results.html page and fetches per-test YAML
and log files.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from typing import Any

import requests
import yaml

from .config import JanitorConfig


# ---------------------------------------------------------------------------
# HTML parser for results.html
# ---------------------------------------------------------------------------

class _ResultsParser(HTMLParser):
    """Parse the Janitor results.html table into structured data."""

    def __init__(self) -> None:
        super().__init__()
        self.build_number: int | None = None
        self.change_number: int | None = None
        self.patchset: int | None = None
        self.subject: str = ""
        self.build_status: str = ""
        self.distros: list[dict[str, str]] = []
        self.sections: list[dict[str, Any]] = []

        # Parser state
        self._in_title = False
        self._title_text = ""
        self._in_h3 = False
        self._h3_text = ""
        self._current_section: dict[str, Any] | None = None
        self._in_row = False
        self._cells: list[str] = []
        self._cell_idx = -1
        self._in_cell = False
        self._cell_text = ""
        self._cell_href = ""
        self._cell_bgcolor = ""
        self._in_build_table = False
        self._build_row_cells: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        adict = dict(attrs)
        if tag == "title":
            self._in_title = True
            self._title_text = ""
        elif tag == "h3":
            self._in_h3 = True
            self._h3_text = ""
        elif tag == "table":
            # First table after build status is distros table
            if self.build_status and not self.distros and not self._current_section:
                self._in_build_table = True
        elif tag == "tr":
            self._in_row = True
            self._cells = []
            self._build_row_cells = []
        elif tag == "td":
            self._in_cell = True
            self._cell_text = ""
            self._cell_href = ""
            self._cell_bgcolor = adict.get("bgcolor", "")
            self._cell_idx = len(self._cells)
        elif tag == "a" and self._in_cell:
            href = adict.get("href", "")
            if href:
                self._cell_href = href

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self._title_text += data
        if self._in_h3:
            self._h3_text += data
        if self._in_cell:
            self._cell_text += data

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
            self._parse_title(self._title_text)
        elif tag == "h3":
            self._in_h3 = False
            text = self._h3_text.strip()
            if text.startswith("Overall build status:"):
                self.build_status = text.split(":", 1)[1].strip()
            elif "testing:" in text.lower():
                # "Initial testing: Failure" or "Comprehensive testing: Not started"
                parts = text.split(":", 1)
                phase = parts[0].strip()
                status = parts[1].strip() if len(parts) > 1 else ""
                self._current_section = {
                    "phase": phase,
                    "status": status,
                    "tests": [],
                }
                self.sections.append(self._current_section)
        elif tag == "td":
            self._in_cell = False
            text = self._cell_text.strip()
            if self._in_build_table:
                self._build_row_cells.append(text)
            else:
                self._cells.append(text)
        elif tag == "tr":
            self._in_row = False
            if self._in_build_table and len(self._build_row_cells) >= 2:
                distro = self._build_row_cells[0]
                status = self._build_row_cells[1]
                if distro.lower() != "distro":
                    self.distros.append({"distro": distro, "status": status})
            elif self._current_section and len(self._cells) >= 2:
                test_name = self._cells[0]
                status_text = self._cells[1] if len(self._cells) > 1 else ""
                extra = self._cells[2] if len(self._cells) > 2 else ""
                if test_name.lower() in ("test", ""):
                    return
                self._current_section["tests"].append(
                    self._parse_test_row(test_name, status_text, extra)
                )
        elif tag == "table":
            self._in_build_table = False

    def _parse_title(self, text: str) -> None:
        # "Results for build #61009 64440 rev 10: LU-19956 ..."
        m = re.search(r"build #(\d+)", text)
        if m:
            self.build_number = int(m.group(1))
        m = re.search(r"(\d{4,6})\s+rev\s+(\d+)", text)
        if m:
            self.change_number = int(m.group(1))
            self.patchset = int(m.group(2))
        # Subject is after the colon
        m = re.search(r"rev \d+:\s*(.+)", text)
        if m:
            self.subject = m.group(1).strip()

    @staticmethod
    def _parse_test_row(
        test_name: str, status_text: str, extra: str
    ) -> dict[str, Any]:
        """Parse a single test result row."""
        result: dict[str, Any] = {"test": test_name}

        if not status_text:
            result["status"] = "NOT_RUN"
            return result

        # Parse status and duration from text like "Success(1023s)"
        # or "Timeout(11332s)" or "Client crashed(5340s)"
        m = re.match(
            r"(Success|Timeout|Client crashed|Failure|"
            r"Server crashed|LBUG|Error)\s*\((\d+)s\)",
            status_text,
        )
        if m:
            raw_status = m.group(1)
            result["duration_s"] = int(m.group(2))
        else:
            raw_status = status_text
            result["duration_s"] = None

        status_map = {
            "Success": "PASS",
            "Timeout": "TIMEOUT",
            "Client crashed": "CRASH",
            "Server crashed": "CRASH",
            "LBUG": "CRASH",
            "Failure": "FAIL",
            "Error": "FAIL",
        }
        result["status"] = status_map.get(raw_status, raw_status)
        result["status_detail"] = raw_status

        # Extract extra info (blocking state, crash processing errors, etc.)
        if extra:
            result["extra"] = extra.strip()

        # Extract sub-messages (like "Server: Blocking in !RUNNING state")
        inner = re.findall(
            r"\((?:Server|Client):\s*(.+?)\)", status_text
        )
        if inner:
            result["notes"] = inner

        return result


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class JanitorClient:
    """Client for Gerrit Janitor test results."""

    def __init__(self, config: JanitorConfig) -> None:
        self.config = config
        self.session = requests.Session()

    def _build_url(self, build: int, path: str = "") -> str:
        return f"{self.config.base_url}/{build}/{path}"

    # -- Build lookup --

    def resolve_change(self, change: int) -> int | None:
        """Find the latest Janitor build for a Gerrit change number.

        Walks backwards from recent builds checking the REF file.
        Returns the build number or None.
        """
        # Try to find by fetching recent builds.  The Janitor
        # results page title contains the change number, and the
        # REF file has the exact ref.  We'll try a range of recent
        # build numbers.
        #
        # First, try to guess from the results page index.
        # The simplest heuristic: fetch the parent directory listing
        # and find builds that match.
        try:
            resp = self.session.get(
                f"{self.config.base_url}/",
                timeout=15,
            )
            resp.raise_for_status()
        except Exception:
            return None

        # Parse build numbers from directory listing
        builds = [
            int(m.group(1))
            for m in re.finditer(r'href="(\d+)/"', resp.text)
        ]
        builds.sort(reverse=True)

        # Check last 200 builds for our change
        for build in builds[:200]:
            try:
                ref_resp = self.session.get(
                    self._build_url(build, "REF"),
                    timeout=5,
                )
                if ref_resp.status_code != 200:
                    continue
                ref = ref_resp.text.strip()
                # REF is like "refs/changes/40/64440/10"
                m = re.search(r"/(\d+)/\d+$", ref)
                if m and int(m.group(1)) == change:
                    return build
            except Exception:
                continue
        return None

    def get_ref(self, build: int) -> dict[str, Any] | None:
        """Get the REF info for a build."""
        try:
            resp = self.session.get(
                self._build_url(build, "REF"), timeout=10,
            )
            if resp.status_code != 200:
                return None
            ref = resp.text.strip()
            # "refs/changes/40/64440/10"
            m = re.match(r"refs/changes/\d+/(\d+)/(\d+)", ref)
            if m:
                return {
                    "ref": ref,
                    "change": int(m.group(1)),
                    "patchset": int(m.group(2)),
                }
            return {"ref": ref}
        except Exception:
            return None

    # -- Results page --

    def get_results(self, build: int) -> dict[str, Any] | None:
        """Parse the results.html page for a build."""
        try:
            resp = self.session.get(
                self._build_url(build, "results.html"),
                timeout=15,
            )
            if resp.status_code != 200:
                return None
        except Exception:
            return None

        parser = _ResultsParser()
        parser.feed(resp.text)

        return {
            "build": parser.build_number or build,
            "change": parser.change_number,
            "patchset": parser.patchset,
            "subject": parser.subject,
            "build_status": parser.build_status,
            "distros": parser.distros,
            "sections": parser.sections,
            "url": self._build_url(build, "results.html"),
        }

    # -- Test detail (per-test results.yml) --

    def list_test_files(
        self, build: int, test_dir: str
    ) -> list[dict[str, str]]:
        """List files in a test result directory."""
        url = self._build_url(build, f"testresults/{test_dir}/")
        try:
            resp = self.session.get(url, timeout=15)
            if resp.status_code != 200:
                return []
        except Exception:
            return []

        files = []
        for m in re.finditer(
            r'<a href="([^"]+)">([^<]+)</a>\s*</td>'
            r'<td[^>]*>[^<]*</td>'
            r'<td[^>]*>\s*([^\s<]+)',
            resp.text,
        ):
            name = m.group(2).strip()
            size = m.group(3).strip()
            if name in ("Parent Directory", "Name"):
                continue
            files.append({
                "name": name,
                "href": m.group(1),
                "size": size,
            })
        return files

    def get_test_yaml(
        self, build: int, test_dir: str
    ) -> dict[str, Any] | None:
        """Fetch and parse results.yml for a specific test."""
        url = self._build_url(
            build, f"testresults/{test_dir}/results.yml"
        )
        try:
            resp = self.session.get(url, timeout=15)
            if resp.status_code != 200:
                return None
            return yaml.safe_load(resp.text)
        except Exception:
            return None

    def fetch_log(
        self, build: int, test_dir: str, filename: str,
        max_bytes: int = 500_000,
    ) -> str | None:
        """Fetch a log file from a test result directory.

        Returns the text content, truncated to max_bytes.
        """
        url = self._build_url(
            build, f"testresults/{test_dir}/{filename}"
        )
        try:
            resp = self.session.get(
                url, timeout=30, stream=True,
            )
            if resp.status_code != 200:
                return None
            # Read up to max_bytes
            chunks = []
            total = 0
            for chunk in resp.iter_content(chunk_size=65536):
                chunks.append(chunk)
                total += len(chunk)
                if total >= max_bytes:
                    break
            data = b"".join(chunks)[:max_bytes]
            return data.decode("utf-8", errors="replace")
        except Exception:
            return None

    def find_test_dir(
        self, build: int, test_name: str
    ) -> str | None:
        """Find the testresults directory for a test name.

        test_name is like "sanity2@ldiskfs+DNE". We need to map
        it to the directory name like
        "sanity2-ldiskfs-DNE-rocky8.10_x86_64-rocky8.10_x86_64".

        Falls back to listing the testresults/ dir and matching.
        """
        url = self._build_url(build, "testresults/")
        try:
            resp = self.session.get(url, timeout=15)
            if resp.status_code != 200:
                return None
        except Exception:
            return None

        # Normalize test name for matching:
        # "sanity2@ldiskfs+DNE" -> prefix "sanity2-ldiskfs-DNE"
        # "sanity2@zfs" -> prefix "sanity2-zfs"
        normalized = test_name.replace("@", "-").replace("+", "-")

        dirs = re.findall(r'href="([^"]+/)"', resp.text)
        # Find best match (longest prefix match)
        matches = [
            d.rstrip("/") for d in dirs
            if d.rstrip("/").startswith(normalized)
        ]
        if not matches:
            # Try looser match
            parts = normalized.split("-")
            matches = [
                d.rstrip("/") for d in dirs
                if all(p in d for p in parts)
            ]
        if not matches:
            return None
        # Prefer non-retry directories, then latest (retry1 > base)
        matches.sort(key=lambda d: ("retry" in d, d))
        # Return the latest (retry if exists)
        return matches[-1] if matches else None
