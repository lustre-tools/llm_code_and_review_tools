"""Opt-in browser check: drive the real dashboard in Chrome.

Excluded from the unit suite because it needs credentials, network, and a
browser.  It exercises what unit tests cannot: that the page actually renders,
that its controls are reachable, that GET never mutates, and that POST without
a CSRF token is refused.

    PATCH_WATCHER_TEST_CONFIG=~/.config/patch-watcher/config \\
        ~/llm_code_and_review_tools/.venv/bin/python browser_check.py

Read-only against Gerrit: it watches dormant changes and clicks only
non-mutating controls.  It never starts an agent or posts to Gerrit.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

DEFAULT_CHANGES = (44206, 44386, 45276)
CHANGE_URL = "https://review.whamcloud.com/c/fs/lustre-release/+/{}"


def main(argv: list[str] | None = None) -> int:
    raw_config = os.environ.get("PATCH_WATCHER_TEST_CONFIG")
    if not raw_config or not Path(raw_config).expanduser().is_file():
        print("set PATCH_WATCHER_TEST_CONFIG to a patch-watcher config file")
        return 2
    config_path = Path(raw_config).expanduser()

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("playwright is not installed in this interpreter")
        return 2

    workspace = Path(tempfile.mkdtemp(prefix="pw-browser-"))
    from patch_watcher import gerrit_status

    original_load = gerrit_status.GerritConfig.load
    gerrit_status.GerritConfig.load = classmethod(
        lambda cls, path=None: original_load.__func__(cls, config_path)
    )
    from patch_watcher import app

    app.ACTIVE_WATCH_FILE = workspace / "patches.txt"
    app.initialize_session_store(workspace / "sessions.sqlite3")
    app.initialize_automation_store(workspace / "automation.sqlite3")
    app.initialize_standing_policy_store(workspace / "standing.json")
    app.initialize_run_controller(runs_directory=workspace / "runs", start=False)
    for number in DEFAULT_CHANGES:
        patch, error = app.add_patch(CHANGE_URL.format(number))
        if patch is not None and not error:
            app.refresh_watched_patch(patch)
            app.sync_automation_patch(patch)

    server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    failures: list[str] = []

    def check(condition, message):
        print(("  ok   " if condition else "  FAIL ") + message)
        if not condition:
            failures.append(message)

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel="chrome", headless=True)
            console: list[str] = []
            page = browser.new_page()
            page.on("console", lambda m: console.append(f"{m.type}: {m.text}"))
            page.on("pageerror", lambda e: console.append(f"pageerror: {e}"))
            page.goto(base, wait_until="networkidle")

            print("dashboard:")
            body = page.inner_text("body")
            for number in DEFAULT_CHANGES:
                check(str(number) in body, f"change {number} is on the page")
            # Assert the user-facing label, not the internal state name: CSS
            # capitalisation once turned "ci-failed" into "Ci Failed".
            check("CI failed" in body, "watch state renders as 'CI failed'")
            check("Ci Failed" not in body, "no mangled initialism in the label")
            check("Jenkins" in body and "Maloo" in body, "both CI services shown")
            errors = [line for line in console if not line.startswith("log:")]
            check(not errors, f"no console errors ({errors[:2]})")

            # Colour must never be the only signal.
            chips = page.locator(".status-chip")
            count = chips.count()
            check(count > 0, f"status chips present ({count})")
            empty = [
                i for i in range(count) if not chips.nth(i).inner_text().strip()
            ]
            check(not empty, f"every chip carries text, not just colour ({empty[:3]})")

            print("safety:")
            forms = page.locator("form[method='post'], form:not([method])")
            missing = []
            for i in range(forms.count()):
                if forms.nth(i).locator("input[name='csrf_token']").count() == 0:
                    missing.append(forms.nth(i).get_attribute("action") or f"form{i}")
            check(not missing, f"every POST form carries a CSRF token ({missing[:3]})")

            # A GET must never mutate. Snapshot, walk every in-page link, compare.
            before = len(app.PATCHES), page.content()
            links = page.eval_on_selector_all(
                "a[href]",
                "els => els.map(e => e.getAttribute('href')).filter(h => h && "
                "!h.startsWith('http') && !h.startsWith('#'))",
            )
            visited = 0
            for href in dict.fromkeys(links):
                response = page.goto(base + href, wait_until="domcontentloaded")
                visited += 1
                if response is not None and response.status >= 500:
                    failures.append(f"GET {href} returned {response.status}")
                    print(f"  FAIL GET {href} -> {response.status}")
            check(
                len(app.PATCHES) == before[0],
                f"visiting {visited} links changed no state",
            )

            page.goto(base, wait_until="networkidle")

            print("controls:")
            # Each patch has one compact Actions disclosure. Open them all and
            # confirm the controls actually render rather than erroring.
            details = page.locator("details.patch-actions")
            opened = details.count()
            for i in range(opened):
                # Nested disclosures exist inside each panel; click only the
                # outermost summary of this details element.
                details.nth(i).locator("> summary").click()
            check(opened > 0, f"per-patch Actions disclosures present ({opened})")
            if opened:
                selects = page.locator("details.patch-actions select").count()
                check(selects > 0, f"standing-policy controls render ({selects} selects)")

            # Every form control a human operates needs an accessible name,
            # otherwise the page is unusable with a screen reader.
            unlabelled = page.eval_on_selector_all(
                "select, textarea, input:not([type=hidden]):not([type=submit])",
                """els => els.filter(e => {
                    if (e.getAttribute('aria-label') || e.getAttribute('title')) return false;
                    if (e.id && document.querySelector(`label[for="${e.id}"]`)) return false;
                    if (e.closest('label')) return false;
                    if (e.getAttribute('placeholder')) return false;
                    return true;
                }).map(e => e.name || e.tagName)""",
            )
            check(not unlabelled, f"every visible control has a name ({unlabelled[:4]})")

            # A confirmation page must be display-only: reaching it by GET must
            # not enable anything.
            before_gate = page.goto(
                base + "/automation/global/confirm-enable", wait_until="domcontentloaded"
            )
            check(
                before_gate is not None and before_gate.status == 200,
                "global-automation confirm page renders",
            )
            gate_body = page.inner_text("body")
            check("?" in gate_body, "confirm page asks rather than asserts")
            check(
                page.locator("form[method='post'] input[name='csrf_token']").count() > 0,
                "confirm page's action is a tokened POST, not a link",
            )
            from patch_watcher import app as _app
            check(
                not _app.AUTOMATION_STORE.get_global_automation().enabled,
                "visiting the confirm page did NOT enable automation",
            )

            page.goto(base, wait_until="networkidle")

            # POST without a CSRF token must be refused.
            refused = page.evaluate(
                """async (base) => {
                    const r = await fetch(base + '/add', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/x-www-form-urlencoded'},
                        body: 'url=https://review.whamcloud.com/c/fs/lustre-release/+/1',
                    });
                    return r.status;
                }""",
                base,
            )
            check(refused >= 400, f"CSRF-less POST refused (status {refused})")
            check(len(app.PATCHES) == before[0], "CSRF-less POST added no patch")

            browser.close()
    finally:
        server.shutdown()
        gerrit_status.GerritConfig.load = original_load
        shutil.rmtree(workspace, ignore_errors=True)

    print()
    if failures:
        print(f"BROWSER CHECK FAILED ({len(failures)})")
        for failure in failures:
            print("  -", failure)
        return 1
    print("BROWSER CHECK OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
