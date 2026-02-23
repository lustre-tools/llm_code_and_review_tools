"""Jenkins REST API client."""

from typing import Any

import requests

from .config import JenkinsConfig


class JenkinsClient:
    """Client for the Jenkins JSON API."""

    def __init__(self, config: JenkinsConfig, timeout: int = 30) -> None:
        self.config = config
        self.timeout = timeout
        self.session = requests.Session()
        self.session.auth = (config.user, config.token)

    def _get_json(
        self, path: str, params: dict[str, Any] | None = None
    ) -> Any:
        """Make a GET request to a Jenkins JSON API endpoint."""
        url = f"{self.config.base_url}/{path.lstrip('/')}"
        if not url.endswith("/api/json") and "/api/json" not in url:
            url = url.rstrip("/") + "/api/json"
        resp = self.session.get(url, params=params, timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

    def _get_text(self, path: str) -> str:
        """Make a GET request and return plain text."""
        url = f"{self.config.base_url}/{path.lstrip('/')}"
        resp = self.session.get(url, timeout=self.timeout)
        resp.raise_for_status()
        return resp.text

    # -- Jobs --

    def get_jobs(self) -> list[dict[str, Any]]:
        """Get all jobs with basic info."""
        data = self._get_json(
            "/api/json",
            params={"tree": "jobs[name,url,color,"
                    "healthReport[description,score]]"},
        )
        return data.get("jobs", [])

    # -- Builds --

    def get_builds(
        self, job_name: str, limit: int = 10
    ) -> list[dict[str, Any]]:
        """Get recent builds for a job."""
        data = self._get_json(
            f"/job/{job_name}/api/json",
            params={"tree": f"builds[number,url,result,timestamp,"
                    f"duration,building]{{0,{limit}}}"},
        )
        return data.get("builds", [])

    def get_build(
        self, job_name: str, build_number: int | str = "lastBuild"
    ) -> dict[str, Any]:
        """Get detailed info for a specific build."""
        return self._get_json(
            f"/job/{job_name}/{build_number}/api/json",
            params={"tree": "number,url,result,timestamp,duration,building,"
                    "estimatedDuration,displayName,description,fullDisplayName,"
                    "actions[parameters[name,value],"
                    "causes[shortDescription,userName,upstreamBuild,"
                    "upstreamProject,upstreamUrl],"
                    "buildsByBranchName[*[buildNumber,revision[SHA1]]],"
                    "lastBuiltRevision[SHA1,branch[name]]],"
                    "runs[number,url,result,building,duration,fullDisplayName,builtOn],"
                    "changeSet[items[commitId,msg,author[fullName]]]"},
        )

    def get_run_console_text(self, run_url: str) -> str:
        """Get console output for a matrix run by its full URL."""
        url = run_url.rstrip("/") + "/consoleText"
        resp = self.session.get(url, timeout=self.timeout)
        resp.raise_for_status()
        return resp.text

    # -- Console output --

    def get_console_text(
        self, job_name: str, build_number: int | str = "lastBuild"
    ) -> str:
        """Get plain-text console output for a build."""
        return self._get_text(
            f"/job/{job_name}/{build_number}/consoleText"
        )

    # -- Views --

    def get_view(self, view_name: str) -> dict[str, Any]:
        """Get jobs within a specific view."""
        return self._get_json(
            f"/view/{view_name}/api/json",
            params={"tree": "name,url,description,"
                    "jobs[name,url,color,"
                    "healthReport[description,score]]"},
        )

    # -- Write helpers --

    def get_crumb(self) -> dict[str, str]:
        """Get a CSRF crumb for POST requests."""
        data = self._get_json("/crumbIssuer/api/json")
        return {
            data["crumbRequestField"]: data["crumb"],
        }

    def _post(self, path: str, data: dict[str, str] | None = None) -> int:
        """POST with CSRF crumb, return HTTP status code."""
        crumb = self.get_crumb()
        url = f"{self.config.base_url}/{path.lstrip('/')}"
        resp = self.session.post(
            url,
            headers=crumb,
            data=data,
            timeout=self.timeout,
            allow_redirects=False,
        )
        # 302 redirect is the normal success response for stop/kill/rebuild
        if resp.status_code in (200, 302):
            return resp.status_code
        resp.raise_for_status()
        return resp.status_code

    # -- Abort --

    def abort_build(
        self, job_name: str, build_number: int | str
    ) -> int:
        """Abort a build (graceful stop).

        Posts to the /stop endpoint. For matrix builds this stops
        the parent and all running sub-builds.
        """
        return self._post(f"/job/{job_name}/{build_number}/stop")

    def kill_build(
        self, job_name: str, build_number: int | str
    ) -> int:
        """Hard-kill a build.

        Posts to the /kill endpoint. Use when /stop doesn't work.
        """
        return self._post(f"/job/{job_name}/{build_number}/kill")

    # -- Retrigger --

    def retrigger_build(
        self, job_name: str, build_number: int | str
    ) -> str:
        """Retrigger a Gerrit-triggered build.

        POSTs to the gerrit-trigger-retrigger-this endpoint which
        re-runs the build with the same Gerrit event parameters.

        Returns the redirect location or HTTP status.
        """
        crumb = self.get_crumb()
        url = (
            f"{self.config.base_url}/job/{job_name}"
            f"/{build_number}/gerrit-trigger-retrigger-this/"
        )
        resp = self.session.post(
            url,
            headers=crumb,
            timeout=self.timeout,
            allow_redirects=False,
        )
        if resp.status_code in (200, 302):
            location = resp.headers.get("Location", "")
            return location or f"HTTP {resp.status_code}"
        resp.raise_for_status()
        return f"HTTP {resp.status_code}"

    # -- Search helpers --

    def find_builds_by_gerrit_change(
        self,
        job_name: str,
        change_number: int,
        max_builds: int = 20,
    ) -> list[dict[str, Any]]:
        """Find builds triggered by a specific Gerrit change number.

        Searches recent builds of a job for ones whose parameters
        contain the given Gerrit change number.
        """
        builds = self.get_builds(job_name, limit=max_builds)
        matching = []
        for b in builds:
            detail = self.get_build(job_name, b["number"])
            for action in detail.get("actions", []):
                params = action.get("parameters", [])
                for p in params:
                    if (
                        p.get("name") == "GERRIT_CHANGE_NUMBER"
                        and str(p.get("value")) == str(change_number)
                    ):
                        matching.append(detail)
                        break
                else:
                    continue
                break
        return matching

    def find_review_builds(
        self,
        change_number: int,
        max_builds: int = 30,
    ) -> list[dict[str, Any]]:
        """Find builds for a Gerrit review across review jobs.

        Searches lustre-reviews and similar *-reviews jobs.
        """
        review_jobs = []
        for job in self.get_jobs():
            name = job.get("name", "")
            if "reviews" in name.lower() and job.get("color") not in (
                "disabled", "notbuilt"
            ):
                review_jobs.append(name)

        all_matches: list[dict[str, Any]] = []
        for job_name in review_jobs:
            matches = self.find_builds_by_gerrit_change(
                job_name, change_number, max_builds=max_builds
            )
            for m in matches:
                m["_job_name"] = job_name
            all_matches.extend(matches)

        all_matches.sort(
            key=lambda x: x.get("timestamp", 0), reverse=True
        )
        return all_matches
