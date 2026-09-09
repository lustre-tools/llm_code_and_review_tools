"""Tests for host credential configuration.

Everything is driven through injected prompts against a temporary HOME; no
real credential file is read or written.
"""

import contextlib
import io
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from patch_watcher import pw_configure
from patch_watcher.pw_configure import Field, ToolConfig, configure_tool, read_env, write_env


class EnvFileTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "nested" / ".env"

    def test_write_creates_a_private_file_and_directory(self):
        write_env(self.path, {"A": "1"})
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.path.parent.stat().st_mode), 0o700)

    def test_secrets_never_pass_through_a_world_readable_moment(self):
        # Created 0600 by os.open, not chmod-ed afterwards.
        write_env(self.path, {"GERRIT_PASS": "secret"})
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertNotIn("secret", str(self.path))

    def test_round_trip_preserves_values(self):
        write_env(self.path, {"A": "1", "B": "two words"})
        self.assertEqual(read_env(self.path), {"A": "1", "B": "two words"})

    def test_comments_blanks_and_quotes_are_handled(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text(
            "# a comment\n\nA=1\nB='quoted'\nC=\"double\"\nnot a pair\n"
        )
        self.assertEqual(read_env(self.path), {"A": "1", "B": "quoted", "C": "double"})

    def test_missing_file_reads_as_empty(self):
        self.assertEqual(read_env(self.path), {})

    def test_unrelated_keys_are_preserved_on_rewrite(self):
        write_env(self.path, {"KEEP": "yes", "GERRIT_USER": "old"})
        tool = ToolConfig("t", self.path, (Field("GERRIT_USER", "user"),))
        configure_tool(
            tool, ask=lambda _: "new", ask_secret=lambda _: "",
            out=io.StringIO(), reconfigure=True,
        )
        values = read_env(self.path)
        self.assertEqual(values["GERRIT_USER"], "new")
        self.assertEqual(values["KEEP"], "yes")


class ConfigureToolTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / ".env"
        self.devnull = contextlib.nullcontext()
        self.output = io.StringIO()
        self.tool = ToolConfig("demo", self.path, (
            Field("URL", "URL", default="https://example.invalid"),
            Field("USER", "user"),
            Field("PASS", "password", secret=True),
        ))

    def run_configure(self, answers, secrets, **kwargs):
        answers, secrets = list(answers), list(secrets)
        return configure_tool(
            self.tool,
            ask=lambda _: answers.pop(0) if answers else "",
            ask_secret=lambda _: secrets.pop(0) if secrets else "",
            out=self.output,
            **kwargs,
        )

    def test_a_fully_answered_tool_is_written(self):
        self.assertTrue(self.run_configure(["", "alice"], ["hunter2"]))
        self.assertEqual(read_env(self.path), {
            "URL": "https://example.invalid", "USER": "alice", "PASS": "hunter2",
        })

    def test_an_already_configured_tool_is_left_alone(self):
        write_env(self.path, {"URL": "u", "USER": "alice", "PASS": "p"})
        # No prompts are consumed at all; a second run must be a no-op.
        self.assertFalse(self.run_configure([], []))
        self.assertEqual(read_env(self.path)["PASS"], "p")

    def test_reconfigure_prompts_but_an_empty_answer_keeps_the_old_value(self):
        write_env(self.path, {"URL": "u", "USER": "alice", "PASS": "p"})
        self.assertFalse(self.run_configure(["", ""], [""], reconfigure=True))
        self.assertEqual(read_env(self.path), {"URL": "u", "USER": "alice", "PASS": "p"})

    def test_a_missing_required_answer_writes_nothing(self):
        self.assertFalse(self.run_configure(["", ""], [""]))
        self.assertFalse(self.path.exists())


class ToolConfigsTests(unittest.TestCase):
    def test_paths_match_what_the_tools_actually_read(self):
        home = Path("/home/example")
        paths = {t.name: t.path for t in pw_configure.tool_configs(home)}
        self.assertEqual(paths["gerrit"], home / ".config/gerrit-cli/.env")
        self.assertEqual(paths["jira"], home / ".config/jira-tool/.env")
        self.assertEqual(paths["jenkins"], home / ".config/jenkins-tool/.env")
        self.assertEqual(paths["maloo"], home / ".config/maloo-tool/.env")
        self.assertEqual(paths["patch-watcher"], home / ".config/patch-watcher/config")

    def test_exactly_the_credential_shaped_fields_are_marked_secret(self):
        tools = pw_configure.tool_configs(Path("/home/example"))
        credential_shaped = sorted(
            (tool.name, field_spec.key)
            for tool in tools
            for field_spec in tool.fields
            if any(s in field_spec.key for s in ("PASS", "TOKEN", "SECRET"))
        )
        # Pin the population first.  Without this, an emptied table or a
        # renamed key leaves nothing to check and the test passes anyway.
        self.assertEqual(credential_shaped, [
            ("gerrit", "GERRIT_PASS"),
            ("jenkins", "JENKINS_TOKEN"),
            ("jira", "JIRA_TOKEN"),
            ("maloo", "MALOO_PASS"),
            ("patch-watcher", "GERRIT_PASS"),
        ])
        marked_secret = sorted(
            (tool.name, field_spec.key)
            for tool in tools
            for field_spec in tool.fields
            if field_spec.secret
        )
        # Equality both ways: every credential is prompted without echo, and
        # nothing else is hidden from the operator who is configuring it.
        self.assertEqual(marked_secret, credential_shaped)


if __name__ == "__main__":
    unittest.main()


class AtomicWriteTests(unittest.TestCase):
    """Credential writes must be all-or-nothing.

    `write_env` truncated in place, so a crash between the truncate and the
    write left the operator with an empty credential file. Every other durable
    writer in this codebase already used temp-file + rename.
    """

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "tool" / ".env"

    def test_a_failed_rename_leaves_the_previous_contents(self):
        write_env(self.path, {"GERRIT_PASS": "original"})

        def explode(source, destination):
            raise OSError("no space left on device")

        with unittest.mock.patch("os.replace", explode), self.assertRaises(OSError):
            write_env(self.path, {"GERRIT_PASS": "replacement"})

        self.assertEqual(read_env(self.path), {"GERRIT_PASS": "original"})

    def test_a_failed_rename_leaves_no_temp_file_behind(self):
        write_env(self.path, {"K": "V"})

        def explode(source, destination):
            raise OSError("boom")

        with unittest.mock.patch("os.replace", explode), self.assertRaises(OSError):
            write_env(self.path, {"K": "W"})

        leftovers = [p.name for p in self.path.parent.iterdir() if p.name != ".env"]
        self.assertEqual(leftovers, [])

    def test_a_value_containing_a_newline_is_refused_not_written(self):
        # A newline would silently corrupt the KEY=VALUE format and could
        # inject an unrelated key.
        for bad in ("line\nbreak", "carriage\rreturn"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    write_env(self.path, {"GERRIT_PASS": bad})
        self.assertFalse(self.path.exists())

    def test_the_temp_file_is_private_before_it_becomes_the_target(self):
        observed = {}
        real_replace = os.replace

        def record(source, destination):
            observed["mode"] = stat.S_IMODE(os.stat(source).st_mode)
            return real_replace(source, destination)

        with unittest.mock.patch("os.replace", record):
            write_env(self.path, {"GERRIT_PASS": "secret"})
        self.assertEqual(observed["mode"], 0o600,
                         "the secret was briefly readable by others")


class ConfigureToolNoteTests(unittest.TestCase):
    def test_a_tool_note_is_shown_when_prompting(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            tool = ToolConfig("demo", path, (Field("K", "key"),), note="get it from X")
            out = io.StringIO()
            configure_tool(tool, ask=lambda _: "v", ask_secret=lambda _: "", out=out)
            self.assertIn("get it from X", out.getvalue())
            self.assertIn(str(path), out.getvalue())


class KeyNameTests(unittest.TestCase):
    """The keys written must be the keys the CLIs read.

    pw-configure wrote JIRA_URL and JIRA_USER into ~/.config/jira-tool/.env.
    jira_tool/config.py reads JIRA_SERVER and JIRA_TOKEN out of that file and
    nothing else, so a host configured this way got a jira that answered every
    command with "Server URL not configured" -- while install.sh's own summary
    told the operator to set JIRA_SERVER. Two files, two names, one of them
    wrong.
    """

    def fields(self):
        return {
            tool.name: sorted(f.key for f in tool.fields)
            for tool in pw_configure.tool_configs(Path("/home/example"))
        }

    def test_each_tool_writes_exactly_the_keys_its_cli_reads(self):
        self.assertEqual(self.fields(), {
            # gerrit_cli/client.py
            "gerrit": ["GERRIT_PASS", "GERRIT_URL", "GERRIT_USER"],
            # patch_watcher/gerrit_status.py
            "patch-watcher": [
                "GERRIT_PASS", "GERRIT_URL", "GERRIT_USER",
                "REFRESH_INTERVAL_SECONDS",
            ],
            # jira_tool/config.py -- and no username: the token is the whole
            # authentication, so JIRA_USER was collected and read by nobody.
            "jira": ["JIRA_SERVER", "JIRA_TOKEN"],
            # jenkins_tool/config.py
            "jenkins": ["JENKINS_TOKEN", "JENKINS_URL", "JENKINS_USER"],
            # maloo_tool/config.py
            "maloo": ["MALOO_PASS", "MALOO_URL", "MALOO_USER"],
        })

    def test_the_keys_no_tool_reads_are_gone(self):
        self.assertNotIn("JIRA_URL", self.fields()["jira"])
        self.assertNotIn("JIRA_USER", self.fields()["jira"])


class UrlDefaultTests(unittest.TestCase):
    """Enter at a URL prompt must accept the default, not abandon the tool.

    JENKINS_URL and MALOO_URL were required with no default, so "Press Enter
    to keep the value shown in brackets" -- the instruction pw-configure
    prints -- landed on the required-and-empty branch, and configure_tool
    returned having written nothing at all. pw-doctor then reported the tool
    unconfigured, and running configure again did the same thing.
    """

    def configure(self, tool, answers):
        answers = list(answers)
        return configure_tool(
            tool,
            ask=lambda _: answers.pop(0) if answers else "",
            ask_secret=lambda _: "hunter2",
            out=io.StringIO(),
        )

    def test_pressing_enter_writes_the_cli_default(self):
        expected = {
            "jenkins": ("JENKINS_URL", "https://build.whamcloud.com"),
            "maloo": ("MALOO_URL", "https://testing.whamcloud.com"),
            "jira": ("JIRA_SERVER", "https://jira.whamcloud.com"),
            "gerrit": ("GERRIT_URL", "https://review.whamcloud.com"),
        }
        with tempfile.TemporaryDirectory() as directory:
            tools = {t.name: t for t in pw_configure.tool_configs(Path(directory))}
            for name, (key, default) in expected.items():
                with self.subTest(tool=name):
                    tool = tools[name]
                    # Enter at the URL, then a username for the tools that ask.
                    self.assertTrue(self.configure(tool, ["", "alice"]))
                    self.assertEqual(read_env(tool.path)[key], default)

    def test_no_required_field_is_left_with_nothing_to_accept(self):
        # A credential must be typed; that is the point of it. A URL the CLI
        # already defaults must not be, or Enter silently abandons the tool.
        for tool in pw_configure.tool_configs(Path("/home/example")):
            for field_spec in tool.fields:
                if field_spec.secret or not field_spec.key.endswith(("_URL", "_SERVER")):
                    continue
                with self.subTest(tool=tool.name, key=field_spec.key):
                    self.assertTrue(
                        field_spec.default,
                        f"{field_spec.key} is required with no default; "
                        "pressing Enter writes no file at all",
                    )


INSTALL_SH = Path(__file__).resolve().parent.parent / "install.sh"
# subprocess resolves the program against the env we hand it, and some of
# these tests hand it a deliberately minimal PATH.
BASH = shutil.which("bash") or "/bin/bash"


@unittest.skipUnless(INSTALL_SH.is_file(), "install.sh is not next to this checkout")
class InstallShEntryPointTests(unittest.TestCase):
    """`./install.sh --configure` is the documented way to reach this module.

    These live here because install.sh has no test module of its own, and the
    hook that would let it have one -- INSTALL_SH_NO_MAIN -- was placed above
    install_ltvm, run_configure and run_doctor. The only three functions it
    could not reach were the three that were broken, so every test below was
    unwritable until the guard moved.

    Each run gets a checkout with no <repo>/.venv, which is the state
    install.sh is in on a host that has not installed anything yet.
    """

    def setUp(self):
        self.workspace = tempfile.TemporaryDirectory()
        self.addCleanup(self.workspace.cleanup)
        self.root = Path(self.workspace.name)
        self.script = self.root / "install.sh"
        self.script.write_bytes(INSTALL_SH.read_bytes())
        self.script.chmod(0o755)
        # run_configure/run_doctor fall back to running the package out of the
        # checkout, so SCRIPT_DIR has to contain it.
        (self.root / "patch_watcher").symlink_to(Path(__file__).resolve().parent)
        self.home = self.root / "home"
        self.home.mkdir()
        self.stubs = self.root / "stubs"
        self.stubs.mkdir()

    def env(self, path=None):
        environment = dict(os.environ)
        # $CO would send pw_doctor's pool check at a real checkout root.
        environment.pop("CO", None)
        environment["HOME"] = str(self.home)
        environment["PATH"] = path or f"{self.stubs}:/usr/bin:/bin"
        return environment

    def stub(self, name, body):
        path = self.stubs / name
        path.write_text("#!/bin/bash\n" + body)
        path.chmod(0o755)
        return path

    def run_install_sh(self, *args, path=None):
        return subprocess.run(
            [BASH, str(self.script), *args],
            capture_output=True, text=True, timeout=180, env=self.env(path),
            check=False,
        )

    def source(self, snippet, path=None):
        return subprocess.run(
            [BASH, "-c", f'INSTALL_SH_NO_MAIN=1 source "{self.script}"\n{snippet}'],
            capture_output=True, text=True, timeout=180, env=self.env(path),
            check=False,
        )

    def test_the_test_hook_exposes_every_entry_point(self):
        declared = set(self.source("declare -F | awk '{print $3}'").stdout.split())
        self.assertLessEqual(
            {"install_tools", "uninstall_tools", "install_ltvm",
             "run_configure", "run_doctor", "require_runtime_python"},
            declared,
        )

    def test_configure_resolves_an_interpreter_without_a_prebuilt_venv(self):
        # $PYTHON is only assigned inside install_tools/uninstall_tools, so
        # this path ran `is_externally_managed ""` and then `"" -m
        # patch_watcher.pw_configure`: two "command not found" lines and 127.
        result = self.run_install_sh("--configure")
        self.assertNotIn("command not found", result.stderr)
        # pw_configure refuses a non-tty; seeing that message means it ran.
        self.assertIn("needs a terminal", result.stdout)
        self.assertEqual(result.returncode, 2)

    def test_doctor_resolves_an_interpreter_without_a_prebuilt_venv(self):
        result = self.run_install_sh("--doctor")
        self.assertNotIn("command not found", result.stderr)
        self.assertIn("checks passed", result.stdout)

    def test_neither_setup_command_demands_a_venv_on_a_pep_668_host(self):
        # resolve_python offers to build a venv, and without a tty it refuses
        # to continue. Neither command installs anything, so requiring one
        # would make both documented setup steps unusable on Homebrew/Debian
        # pythons -- which is most of them.
        for flag in ("--configure", "--doctor"):
            with self.subTest(flag=flag):
                result = self.run_install_sh(flag)
                self.assertNotIn("externally managed", result.stdout)

    def test_uninstall_removes_patch_watcher_itself(self):
        # It removed the ~/.local/bin symlinks and left the editable install
        # in place: invisible, still importable, and dangling the moment the
        # checkout it points at is deleted.
        log = self.root / "pip.log"
        for name in ("python3", "python3.11", "python3.12"):
            self.stub(name, f'''
if [ "$1" = "-c" ]; then echo "3.12"; exit 0; fi
echo "$*" >> "{log}"
''')
        result = self.source("uninstall_tools")
        self.assertEqual(result.returncode, 0, result.stderr)
        uninstalled = {
            line.split()[-1] for line in log.read_text().splitlines()
            if "pip uninstall" in line
        }
        self.assertIn("patch-watcher", uninstalled)

    # -- install_ltvm ------------------------------------------------------

    def ltvm_repo(self, *, with_binary=True):
        """A clone shaped like the real one: an `ltvm` script and no install.sh."""

        repo = self.home / "lustre-test-vms-v2"
        repo.mkdir()
        if with_binary:
            binary = repo / "ltvm"
            binary.write_text(
                "#!/bin/bash\n"
                f'echo "ltvm $*" >> "{self.root}/ltvm.log"\n'
                # what `ltvm install` does: put itself on PATH
                f'printf "#!/bin/bash\\ntrue\\n" > "{self.stubs}/ltvm"\n'
                f'chmod +x "{self.stubs}/ltvm"\n'
            )
            binary.chmod(0o755)
        return repo

    def test_with_ltvm_runs_the_installer_that_repo_actually_has(self):
        # It looked for $LTVM_REPO/install.sh, which has never existed, so
        # --with-ltvm was a no-op on every host. The real entry point is
        # `make install`, whose body is `sudo ./ltvm install`.
        repo = self.ltvm_repo()
        self.stub("sudo", f'echo "sudo $*" >> "{self.root}/ltvm.log"\nexec "$@"\n')
        result = self.source("install_ltvm", path=f"{self.stubs}:/usr/bin:/bin")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(
            (self.root / "ltvm.log").read_text().split("\n")[:2],
            ["sudo ./ltvm install", "ltvm install"],
        )
        self.assertFalse((repo / "install.sh").exists())

    def test_a_repo_without_the_ltvm_script_fails_loudly(self):
        self.ltvm_repo(with_binary=False)
        result = self.source('rc=0; install_ltvm || rc=$?; echo "rc=$rc"')
        self.assertIn("rc=1", result.stdout)
        self.assertIn("not executable", result.stdout)

    def test_an_absent_repo_still_names_what_to_do(self):
        result = self.source('rc=0; install_ltvm || rc=$?; echo "rc=$rc"')
        self.assertIn("rc=1", result.stdout)
        self.assertIn("LTVM_REPO", result.stdout)

    def test_it_says_so_when_it_cannot_elevate(self):
        # `ltvm install` writes /usr/local/bin and /etc. Silently running it
        # unprivileged would fail somewhere less obvious.
        self.ltvm_repo()
        minimal = self.root / "minimal-bin"
        minimal.mkdir()
        for name in ("id", "dirname"):
            (minimal / name).symlink_to(shutil.which(name))
        result = self.source(
            'rc=0; install_ltvm || rc=$?; echo "rc=$rc"', path=str(minimal)
        )
        self.assertIn("rc=1", result.stdout)
        self.assertIn("sudo is not available", result.stdout)
