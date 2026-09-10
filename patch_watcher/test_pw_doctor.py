"""Tests for the host readiness check.

`pw-doctor` is a shipped console entry point and had zero coverage. It is also
the thing an operator runs to find out why agents will not start, so a check
that reports the wrong answer is worse than no check.
"""

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from patch_watcher import pw_doctor
from patch_watcher.pw_doctor import (
    Check,
    check_binaries,
    check_bypass_disclaimer,
    check_checkout_pool,
    check_tool_configs,
    main,
    render,
)
from patch_watcher.workspace import CheckoutPool, CheckoutPoolError


def completed(stdout="", returncode=0, stderr=""):
    return subprocess.CompletedProcess(
        ["claude"], returncode, stdout.encode(), stderr.encode()
    )


class CheckSeverityTests(unittest.TestCase):
    """Advisory findings must not be reported as blocking.

    Reporting a degraded-but-working host as a hard failure trains the operator
    to ignore the doctor, which is worse than not checking at all.
    """

    def test_only_a_failed_non_advisory_check_blocks(self):
        self.assertTrue(Check("a", False, "d").blocking)
        self.assertFalse(Check("a", False, "d", advisory=True).blocking)
        self.assertFalse(Check("a", True, "d").blocking)

    def test_render_marks_the_three_states_distinctly(self):
        text = render([
            Check("ok-one", True, "fine"),
            Check("warn-one", False, "meh", fix="do x", advisory=True),
            Check("fail-one", False, "bad", fix="do y"),
        ])
        self.assertIn("[ok  ] ok-one", text)
        self.assertIn("[warn] warn-one", text)
        self.assertIn("[FAIL] fail-one", text)
        self.assertIn("1 blocking", text)
        self.assertIn("1 advisory", text)

    def test_a_host_with_only_advisories_is_reported_as_usable(self):
        text = render([Check("a", True, "d"), Check("b", False, "d", advisory=True)])
        self.assertIn("can run agents", text)

    def test_serialization_carries_the_fix_and_advisory_flag(self):
        value = Check("n", False, "d", fix="f", advisory=True).to_dict()
        self.assertEqual(value["fix"], "f")
        self.assertTrue(value["advisory"])
        self.assertNotIn("advisory", Check("n", True, "d").to_dict())


class BinaryCheckTests(unittest.TestCase):
    def test_every_required_binary_is_checked(self):
        checks = check_binaries(which=lambda name: f"/usr/bin/{name}")
        self.assertEqual(
            {c.name for c in checks},
            {f"binary:{name}" for name in pw_doctor.REQUIRED_BINARIES},
        )
        self.assertTrue(all(c.ok for c in checks))

    def test_a_missing_binary_names_how_to_install_it(self):
        checks = {c.name: c for c in check_binaries(which=lambda name: None)}
        self.assertFalse(checks["binary:ltvm"].ok)
        self.assertIn("lustre-test-vms", checks["binary:ltvm"].fix)
        self.assertIn("install.sh", checks["binary:gerrit"].fix)


class ToolConfigTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "tool" / ".env"
        self.path.parent.mkdir()

    def configs(self):
        return {"demo": self.path}

    def test_a_missing_config_is_reported_with_its_path(self):
        check = check_tool_configs(self.configs())[0]
        self.assertFalse(check.ok)
        self.assertIn(str(self.path), check.detail)

    def test_a_private_config_passes(self):
        self.path.write_text("K=V\n")
        self.path.chmod(0o600)
        self.assertTrue(check_tool_configs(self.configs())[0].ok)

    def test_a_stricter_mode_is_still_acceptable(self):
        # 0400 is more private than 0600; demanding equality flagged it.
        self.path.write_text("K=V\n")
        self.path.chmod(0o400)
        self.assertTrue(check_tool_configs(self.configs())[0].ok)

    def test_group_or_other_access_is_refused(self):
        self.path.write_text("K=V\n")
        self.path.chmod(0o644)
        check = check_tool_configs(self.configs())[0]
        self.assertFalse(check.ok)
        self.assertIn("chmod 600", check.fix)


class AgentInstructionDiscoveryTests(unittest.TestCase):
    """Agents know this environment only because CLAUDE.md is above them.

    The run prompt is ~450 words and carries no ltvm syntax, no build
    gotchas and no co<N>- naming rule. All of that reaches the agent through
    Claude Code's ancestor-directory discovery of CLAUDE.md, starting from
    its working directory -- the checkout. Nothing in the tool arranges that;
    it works because the pool happens to live under $HOME. $CO accepts any
    directory, so pointing it elsewhere silently strips every agent of the
    environment contract its prompt still promises.
    """

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.home = self.root / "home"
        self.home.mkdir()

    def _pool(self, root, name):
        for index in (1, 2):
            (root / str(index)).mkdir(parents=True, exist_ok=True)
        return CheckoutPool(root, (1, 2), database=self.root / f"{name}.sqlite3")

    def test_a_pool_under_an_instructed_directory_passes(self):
        (self.home / "CLAUDE.md").write_text("# instructions\n", encoding="utf-8")
        pool = self._pool(self.home / "co", "good")
        checks = pw_doctor.check_agent_instructions(pool, home=self.home)
        self.assertEqual([check.ok for check in checks], [True])

    def test_a_pool_with_no_discoverable_instructions_is_reported(self):
        pool = self._pool(self.root / "mnt" / "checkouts", "bad")
        checks = pw_doctor.check_agent_instructions(pool, home=self.home)
        self.assertEqual(len(checks), 1)
        self.assertFalse(checks[0].ok)
        # Advisory: the tool still runs, the agents are just ignorant.
        self.assertTrue(checks[0].advisory)
        self.assertIn("ltvm syntax", checks[0].detail)
        self.assertTrue(checks[0].fix)

    def test_an_instruction_file_in_the_checkout_itself_counts(self):
        root = self.root / "mnt" / "checkouts"
        pool = self._pool(root, "inline")
        for index in (1, 2):
            (root / str(index) / "AGENTS.md").write_text("x\n", encoding="utf-8")
        checks = pw_doctor.check_agent_instructions(pool, home=self.home)
        self.assertEqual([check.ok for check in checks], [True])

    def test_an_undeclared_pool_is_not_reported_twice(self):
        pool = CheckoutPool(
            self.root / "empty", (), database=self.root / "empty.sqlite3"
        )
        self.assertEqual(pw_doctor.check_agent_instructions(pool, home=self.home), [])


class BypassDisclaimerTests(unittest.TestCase):
    """The default check must never launch anything.

    The obvious probe -- running claude with a bogus resume id -- starts a
    real bypassPermissions session once the disclaimer IS accepted, and
    leaves it running under a fixed id that later runs collide on. A health
    check must not spawn agents.

    The probe itself uses --print, the mode the host runs claude in; --bg has
    a stricter rule of its own, and probing it reported a refusal that never
    applied to a real run.
    """

    def test_the_default_check_runs_no_subprocess(self):
        calls = []

        def runner(*args, **kwargs):
            calls.append(args)
            return completed()

        check = check_bypass_disclaimer(runner=runner)
        self.assertEqual(calls, [])
        self.assertTrue(check.advisory)
        self.assertIn("not verified", check.detail)

    def test_the_probe_reports_the_disclaimer_refusal(self):
        check = check_bypass_disclaimer(
            probe=True,
            runner=lambda *a, **k: completed(
                stderr="--print with bypassPermissions requires accepting the disclaimer first"
            ),
        )
        self.assertFalse(check.ok)
        self.assertIn("disclaimer", check.fix)

    def test_the_probe_does_not_call_an_unrelated_failure_a_success(self):
        check = check_bypass_disclaimer(
            probe=True,
            runner=lambda *a, **k: completed(
                returncode=1, stderr="Invalid API key. Please run /login"
            ),
        )
        self.assertFalse(check.ok, "a broken claude was reported as ready")
        self.assertIn("Invalid API key", check.detail)

    def test_the_probe_reports_success_only_on_a_clean_exit(self):
        check = check_bypass_disclaimer(probe=True, runner=lambda *a, **k: completed())
        self.assertTrue(check.ok)

    def test_the_probe_uses_print_mode_and_a_rejected_bogus_id_is_a_pass(self):
        """What a real, accepted host produces: exit 1, "No conversation
        found" -- rejected past the permission check, before any model call."""
        calls = []

        def runner(command, **kwargs):
            calls.append(command)
            return completed(
                returncode=1,
                stderr="No conversation found with session ID: 00000000-0000-0000-0000-000000000000",
            )

        check = check_bypass_disclaimer(probe=True, runner=runner)
        self.assertTrue(check.ok, check.detail)
        self.assertIn("--print", calls[0])
        self.assertNotIn("--bg", calls[0])
        self.assertEqual(calls[0][calls[0].index("--permission-mode") + 1], "bypassPermissions")

    def test_an_unrunnable_claude_is_reported_not_raised(self):
        def explode(*args, **kwargs):
            raise OSError("no such executable")

        check = check_bypass_disclaimer(probe=True, runner=explode)
        self.assertFalse(check.ok)
        self.assertIn("could not run claude", check.detail)


class CheckoutPoolCheckTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.root = self.base / "co"
        for index in (1, 2):
            (self.root / str(index)).mkdir(parents=True)

    def pool(self, indices):
        return CheckoutPool(self.root, indices, database=self.base / "pool.sqlite3")

    def test_an_undeclared_pool_is_advisory_not_blocking(self):
        # Agents still start; they clone per run instead. Calling that a
        # failure made pw-doctor exit non-zero on a working host.
        check = check_checkout_pool(self.pool(()))[0]
        self.assertFalse(check.ok)
        self.assertTrue(check.advisory)
        self.assertIn("clone a private tree per run", check.detail)

    def test_a_declared_pool_reports_its_root_and_availability(self):
        checks = {c.name: c for c in check_checkout_pool(self.pool((1, 2)))}
        self.assertTrue(checks["pool:config"].ok)
        self.assertIn(str(self.root), checks["pool:config"].detail)
        self.assertTrue(checks["pool:directories"].ok)
        self.assertIn("2 of 2 free", checks["pool:availability"].detail)

    def test_a_declared_but_missing_checkout_is_named(self):
        checks = {c.name: c for c in check_checkout_pool(self.pool((1, 9)))}
        self.assertFalse(checks["pool:directories"].ok)
        self.assertIn("9", checks["pool:directories"].detail)

    def test_an_exhausted_pool_is_advisory_and_says_what_to_do(self):
        pool = self.pool((1, 2))
        pool.allocate("run-1")
        pool.allocate("run-2")
        checks = {c.name: c for c in check_checkout_pool(pool)}
        self.assertFalse(checks["pool:availability"].ok)
        self.assertTrue(checks["pool:availability"].advisory)
        self.assertIn("Cancel the listed runs", checks["pool:availability"].fix)
        # The holders must be named; "cancel a run" is useless when the holder
        # is a run that no longer exists.
        self.assertIn("run-1", checks["pool:availability"].detail)

    def test_an_unusable_pool_config_is_advisory_not_blocking(self):
        # The README says an undeclared pool is not fatal, and the app does
        # fall back to per-run clones -- but this path exited non-zero, so the
        # first pw-doctor on a cold machine contradicted the docs.
        class Unusable:
            def __init__(self):
                raise CheckoutPoolError("no checkout root found")

        import unittest.mock

        with unittest.mock.patch.object(
            CheckoutPool, "from_config", side_effect=CheckoutPoolError("no root")
        ):
            checks = check_checkout_pool()
        self.assertEqual(len(checks), 1)
        self.assertFalse(checks[0].ok)
        self.assertTrue(checks[0].advisory, "a cold machine must not fail the doctor")
        self.assertFalse(checks[0].blocking)



class MainTests(unittest.TestCase):
    def test_json_output_is_machine_readable(self):
        import contextlib
        import io
        import unittest.mock

        buffer = io.StringIO()
        # Without this, main() constructs the real CheckoutPool and so creates
        # ~/.local/state/patch-watcher/checkout-pool.sqlite3 in whoever's HOME
        # is running the suite (see check_checkout_pool). A unit test does not
        # get to leave files in someone's home directory.
        with unittest.mock.patch.object(
            CheckoutPool, "from_config",
            side_effect=CheckoutPoolError("no pool during tests"),
        ), contextlib.redirect_stdout(buffer):
            main(["--json"])
        payload = json.loads(buffer.getvalue())
        self.assertTrue(all("name" in item and "ok" in item for item in payload))

    def test_exit_status_reflects_blocking_checks_only(self):
        import contextlib
        import io

        advisory_only = [Check("a", True, "d"), Check("b", False, "d", advisory=True)]
        blocking = [Check("a", False, "d")]
        for checks, expected in ((advisory_only, 0), (blocking, 1)):
            with self.subTest(expected=expected):
                original = pw_doctor.run_checks
                # Bind the loop variable explicitly; a bare closure over
                # `checks` would see whatever the last iteration left.
                pw_doctor.run_checks = (
                    lambda _checks=checks, **kwargs: _checks
                )
                try:
                    with contextlib.redirect_stdout(io.StringIO()):
                        self.assertEqual(main([]), expected)
                finally:
                    pw_doctor.run_checks = original


if __name__ == "__main__":
    unittest.main()


class OwnConfigCheckTests(unittest.TestCase):
    """The doctor must check Patch Watcher's OWN config.

    It checked gerrit-cli, jira-tool, jenkins-tool and maloo-tool but omitted
    ~/.config/patch-watcher/config -- the one file the dashboard needs for
    every Gerrit read. On a host with the other four present it reported
    "This host can run agents" and exited 0, while every patch row said
    "Gerrit is not configured".
    """

    def test_patch_watchers_own_config_is_among_the_checked_files(self):
        self.assertIn("patch-watcher", pw_doctor.TOOL_CONFIGS)
        self.assertEqual(
            pw_doctor.TOOL_CONFIGS["patch-watcher"].name, "config",
        )

    def test_a_config_present_but_empty_is_not_reported_as_configured(self):
        # Existence-and-mode alone is a permissions check dressed as a
        # configuration check: a file containing "X=y" used to pass.
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config"
            path.write_text("X=y\n")
            path.chmod(0o600)
            check = check_tool_configs({"patch-watcher": path})[0]
            self.assertFalse(check.ok)
            self.assertIn("GERRIT_URL", check.detail)

    def test_a_fully_populated_config_passes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config"
            path.write_text(
                "GERRIT_URL=https://review.whamcloud.com\n"
                "GERRIT_USER=someone\nGERRIT_PASS=secret\n"
            )
            path.chmod(0o600)
            self.assertTrue(check_tool_configs({"patch-watcher": path})[0].ok)

    def test_a_key_present_but_blank_counts_as_missing(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config"
            path.write_text("GERRIT_URL=\nGERRIT_USER=someone\nGERRIT_PASS=secret\n")
            path.chmod(0o600)
            check = check_tool_configs({"patch-watcher": path})[0]
            self.assertFalse(check.ok)
            self.assertIn("GERRIT_URL", check.detail)

    def test_a_tool_with_no_required_keys_is_unaffected(self):
        # "maloo" used to be such a tool, which was the bug; a name that is
        # genuinely not in REQUIRED_CONFIG_KEYS still falls back to the
        # existence-and-mode check.
        self.assertNotIn("unlisted", pw_doctor.REQUIRED_CONFIG_KEYS)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text("ANYTHING=here\n")
            path.chmod(0o600)
            self.assertTrue(check_tool_configs({"unlisted": path})[0].ok)


class PoolHolderVisibilityTests(unittest.TestCase):
    """An exhausted pool must name who holds it.

    "finish or cancel a run" is useless advice when the holder is a run that no
    longer exists, and nothing else in the tool shows checkout ownership -- the
    only recovery was hand-editing checkout-pool.sqlite3, which is documented
    nowhere.
    """

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.root = self.base / "co"
        for index in (1, 2):
            (self.root / str(index)).mkdir(parents=True)
        self.pool = CheckoutPool(
            self.root, (1, 2), database=self.base / "pool.sqlite3"
        )

    def availability(self):
        return {c.name: c for c in check_checkout_pool(self.pool)}["pool:availability"]

    def test_holders_are_named_when_the_pool_is_exhausted(self):
        self.pool.allocate("pw-engineer-ghost-1")
        self.pool.allocate("pw-engineer-ghost-2")
        check = self.availability()
        self.assertFalse(check.ok)
        self.assertIn("pw-engineer-ghost-1", check.detail)
        self.assertIn("pw-engineer-ghost-2", check.detail)

    def test_the_fix_names_the_database_and_the_recovery_command(self):
        self.pool.allocate("pw-engineer-ghost-1")
        self.pool.allocate("pw-engineer-ghost-2")
        fix = self.availability().fix
        self.assertIn(str(self.pool.database), fix)
        self.assertIn("DELETE FROM pw_checkout_allocation", fix)

    def test_a_partially_used_pool_still_lists_its_holder(self):
        self.pool.allocate("pw-engineer-live")
        check = self.availability()
        self.assertTrue(check.ok, "one free checkout means agents can still start")
        self.assertIn("pw-engineer-live", check.detail)

    def test_an_unreadable_allocation_table_does_not_break_the_check(self):
        class Broken:
            root = self.root
            indices = (1, 2)
            database = self.base / "pool.sqlite3"

            def free(self):
                return ()

            def allocations(self):
                raise OSError("database is locked")

        check = {c.name: c for c in check_checkout_pool(Broken())}["pool:availability"]
        self.assertIn("0 of 2 free", check.detail)


class EveryToolIsActuallyCheckedTests(unittest.TestCase):
    """An empty credential file is not a configured tool -- for any of them.

    `_missing_required_keys` existed, and its own docstring described this
    exact failure, but REQUIRED_CONFIG_KEYS covered only patch-watcher and
    gerrit. For jira, jenkins and maloo the doctor checked that the file
    existed with mode 0600, so a completely EMPTY ~/.config/jira-tool/.env
    reported `[ok]` and the operator went looking elsewhere for why `jira`
    would not run.
    """

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)

    def private(self, name, body):
        path = self.base / name
        path.write_text(body)
        path.chmod(0o600)
        return path

    def test_an_empty_config_fails_for_every_tool(self):
        for tool in pw_doctor.TOOL_CONFIGS:
            with self.subTest(tool=tool):
                check = check_tool_configs({tool: self.private(tool, "")})[0]
                self.assertFalse(check.ok, f"an empty {tool} config reported ok")
                self.assertIn("is missing:", check.detail)

    def test_the_missing_keys_are_named(self):
        check = check_tool_configs({"jira": self.private("jira", "JIRA_TOKEN=t\n")})[0]
        self.assertFalse(check.ok)
        self.assertIn("JIRA_SERVER", check.detail)
        self.assertNotIn("JIRA_TOKEN", check.detail)

    def test_the_old_wrong_key_name_does_not_satisfy_the_check(self):
        # This is what pw-configure used to write. `jira` cannot run on it.
        check = check_tool_configs({"jira": self.private(
            "jira", "JIRA_URL=https://jira.whamcloud.com\nJIRA_USER=u\nJIRA_TOKEN=t\n"
        )})[0]
        self.assertFalse(check.ok)
        self.assertIn("JIRA_SERVER", check.detail)

    def test_a_url_the_cli_defaults_is_not_demanded(self):
        # jenkins_tool and maloo_tool default their URLs to the production
        # servers, so credentials alone are a working configuration.
        # Demanding the URL would fail a host that works.
        for tool, body in (
            ("jenkins", "JENKINS_USER=u\nJENKINS_TOKEN=t\n"),
            ("maloo", "MALOO_USER=u\nMALOO_PASS=p\n"),
        ):
            with self.subTest(tool=tool):
                check = check_tool_configs({tool: self.private(tool, body)})[0]
                self.assertTrue(check.ok, check.detail)


class ConfigureAndDoctorAgreeTests(unittest.TestCase):
    """The configurator and the doctor must name the same files and keys.

    They did not: pw_configure wrote JIRA_URL and JIRA_USER while the jira CLI
    -- and, once it learned to look, the doctor -- read JIRA_SERVER and
    JIRA_TOKEN. Running `pw-configure` and then `pw-doctor` reported a green
    host with a jira that could not answer a single query. Binding the two
    tables together here means a rename in one file fails in the other.
    """

    def setUp(self):
        from patch_watcher import pw_configure

        self.home = Path("/home/example")
        self.tools = {t.name: t for t in pw_configure.tool_configs(self.home)}

    def test_both_modules_name_the_same_config_files(self):
        self.assertEqual(set(pw_doctor.TOOL_CONFIGS), set(self.tools))
        for tool, path in pw_doctor.TOOL_CONFIGS.items():
            with self.subTest(tool=tool):
                self.assertEqual(
                    Path(path).relative_to(Path.home()),
                    self.tools[tool].path.relative_to(self.home),
                )

    def test_every_key_the_doctor_requires_is_one_configure_writes(self):
        for tool, required in pw_doctor.REQUIRED_CONFIG_KEYS.items():
            with self.subTest(tool=tool):
                written = {f.key for f in self.tools[tool].fields}
                self.assertLessEqual(set(required), written)

    def test_every_key_only_the_operator_can_supply_is_one_the_doctor_checks(self):
        # A required field with no default cannot be filled in by anything but
        # the operator, so if the doctor does not look for it a half-written
        # file passes.
        for name, tool in self.tools.items():
            with self.subTest(tool=name):
                mandatory = {
                    f.key for f in tool.fields if f.required and not f.default
                }
                self.assertLessEqual(
                    mandatory, set(pw_doctor.REQUIRED_CONFIG_KEYS[name])
                )


class FixHintTests(unittest.TestCase):
    """A fix hint has to work for the person reading it.

    The config hints said `./install.sh --configure`, which does nothing
    unless the reader happens to be standing in the checkout (and which was
    itself broken), and the binary hints named
    ~/llm_code_and_review_tools/install.sh, a path that exists only on the
    machine this was written on. Meanwhile the package installs a
    `pw-configure` console script that neither hint mentioned.
    """

    def all_fixes(self):
        with tempfile.TemporaryDirectory() as directory:
            absent = Path(directory) / "absent"
            configs = dict.fromkeys(pw_doctor.TOOL_CONFIGS, absent)
            checks = check_binaries(which=lambda name: None)
            checks += check_tool_configs(configs)
        return checks

    def test_config_hints_name_the_installed_console_script(self):
        with tempfile.TemporaryDirectory() as directory:
            absent = Path(directory) / "absent"
            for tool in pw_doctor.TOOL_CONFIGS:
                with self.subTest(tool=tool):
                    check = check_tool_configs({tool: absent})[0]
                    self.assertEqual(check.fix, f"run: pw-configure --only {tool}")

    def test_the_tool_names_in_the_hints_are_ones_pw_configure_accepts(self):
        # `pw-configure --only <name>` matches on ToolConfig.name; a hint
        # naming anything else sends the operator to "no such tool".
        from patch_watcher import pw_configure

        known = {t.name for t in pw_configure.tool_configs(Path("/home/example"))}
        self.assertLessEqual(set(pw_doctor.TOOL_CONFIGS), known)

    def test_no_hint_hardcodes_one_developers_home_directory(self):
        for check in self.all_fixes():
            with self.subTest(check=check.name):
                self.assertNotIn("~/llm_code_and_review_tools", check.fix)

    def test_no_hint_needs_the_reader_to_be_in_a_particular_directory(self):
        for check in self.all_fixes():
            with self.subTest(check=check.name):
                self.assertNotIn("./install.sh", check.fix)


class PoolCheckWriteTests(unittest.TestCase):
    """The pool check is not read-only, and the module now says so.

    `check_checkout_pool` has to construct a CheckoutPool to learn what is
    declared and who holds what, and that constructor creates
    ~/.local/state/patch-watcher/ and checkout-pool.sqlite3. The module
    docstring claimed every check was read-only, so `pw-doctor` on a cold
    machine silently created state the operator had not asked for. Pinning
    the real behavior here keeps code and docstring from drifting apart
    again.
    """

    def test_the_pool_check_creates_the_state_database(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            (base / "co" / "1").mkdir(parents=True)
            config = base / "checkout-pool.json"
            config.write_text(
                json.dumps({"root": str(base / "co"), "checkouts": [1]})
            )
            database = base / "state" / "checkout-pool.sqlite3"
            self.assertFalse(database.exists())

            pool = CheckoutPool.from_config(config, database=database)
            checks = {c.name: c for c in check_checkout_pool(pool)}

            self.assertTrue(checks["pool:config"].ok)
            self.assertTrue(
                database.exists(),
                "documented as the doctor's one write; it did not happen",
            )

    def test_the_module_docstring_names_the_state_it_writes(self):
        self.assertNotIn("Every check is read-only", pw_doctor.__doc__)
        self.assertIn("checkout-pool.sqlite3", pw_doctor.__doc__)
