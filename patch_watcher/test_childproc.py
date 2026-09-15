"""A helper tool must not outlive the console that asked it a question."""

import os
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

from patch_watcher import childproc


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # pragma: no cover - reaped and recycled
        return True
    # A zombie is not alive; it is waiting to be reaped by an ancestor.
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except (OSError, IndexError):  # pragma: no cover - gone between calls
        return False
    return state != "Z"


def _wait_gone(pid, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _alive(pid):
            return True
        time.sleep(0.05)
    return False


class ChildProcTests(unittest.TestCase):
    """SIGKILL is the case that matters: it is what systemd sends when a
    stop times out, and no cleanup written in Python survives it.  Four
    maloo processes outlived the console that way, the oldest for two
    hours and forty minutes at 1.4 GB.
    """

    def _spawn_parent(self, source):
        with tempfile.TemporaryDirectory() as temp_dir:
            script = Path(temp_dir) / "parent.py"
            script.write_text(textwrap.dedent(source))
            root = str(Path(__file__).resolve().parent)
            env = dict(os.environ)
            env["PYTHONPATH"] = root + os.pathsep + env.get("PYTHONPATH", "")
            parent = subprocess.Popen(
                [sys.executable, str(script)],
                stdout=subprocess.PIPE, text=True, cwd=root, env=env,
            )
            child_pid = int(parent.stdout.readline().strip())
            self.addCleanup(self._reap, parent, child_pid)
            return parent, child_pid

    def _reap(self, parent, child_pid):
        for pid in (parent.pid, child_pid):
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        parent.wait(timeout=10)

    def test_a_tool_dies_when_the_console_is_killed(self):
        parent, child_pid = self._spawn_parent("""
            import sys, threading
            from patch_watcher import childproc
            # Spawned from a worker thread, as every poll tick is: PDEATHSIG
            # follows the spawning thread, and this one waits like they do.
            def work():
                child = childproc.popen([sys.executable, "-c", "import time; time.sleep(600)"])
                print(child.pid, flush=True)
                child.wait()
            thread = threading.Thread(target=work)
            thread.start()
            thread.join()
        """)
        self.assertTrue(_alive(child_pid))
        os.kill(parent.pid, signal.SIGKILL)
        self.assertTrue(
            _wait_gone(child_pid),
            "the tool outlived the process that spawned it",
        )

    def test_a_detached_child_is_left_alone(self):
        """start_new_session marks a child that is MEANT to survive: the
        agent hosts are spawned that way so no signal to the console reaches
        them, and a restart must not take a run's work with it."""
        parent, child_pid = self._spawn_parent("""
            import sys
            from patch_watcher import childproc
            child = childproc.popen(
                [sys.executable, "-c", "import time; time.sleep(600)"],
                start_new_session=True,
            )
            print(child.pid, flush=True)
            child.wait()
        """)
        os.kill(parent.pid, signal.SIGKILL)
        parent.wait(timeout=10)
        time.sleep(1.0)
        self.assertTrue(_alive(child_pid), "a detached run host was killed")

    def test_a_caller_supplied_hook_is_not_displaced(self):
        calls = []
        childproc.run(
            [sys.executable, "-c", "pass"],
            preexec_fn=lambda: calls.append(1),
        )
        self.assertEqual(childproc._bind_to_parent({"preexec_fn": print})["preexec_fn"], print)

    def test_the_tool_still_runs_normally(self):
        done = childproc.run(
            [sys.executable, "-c", "print('hello')"],
            capture_output=True, text=True,
        )
        self.assertEqual(done.stdout.strip(), "hello")
        self.assertEqual(done.returncode, 0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
