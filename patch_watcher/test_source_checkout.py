import subprocess
import tempfile
import unittest
from pathlib import Path

from source_checkout import CheckoutError, GerritRevision, prepare_revision_checkout


SHA = "d" * 40


def revision(**updates):
    values = {
        "change_number": 61965,
        "project": "fs/lustre-release",
        "patchset": 4,
        "revision_sha": SHA,
        "revision_ref": "refs/changes/65/61965/4",
    }
    values.update(updates)
    return GerritRevision(**values)


class SourceCheckoutTests(unittest.TestCase):
    def test_revision_rejects_unsafe_or_inconsistent_identifiers(self):
        bad = (
            {"project": "../../private"},
            {"project": "https://evil.invalid/repo"},
            {"revision_sha": "abc"},
            {"revision_ref": "refs/heads/main"},
            {"revision_ref": "refs/changes/65/61965/3"},
        )
        for update in bad:
            with self.subTest(update=update), self.assertRaises(ValueError):
                revision(**update)

    def test_checkout_uses_fixed_host_exact_ref_and_no_shell(self):
        calls = []

        def runner(command, **kwargs):
            calls.append((command, kwargs))
            stdout = b""
            if command[-2:] == ["rev-parse", "HEAD"]:
                stdout = (SHA + "\n").encode()
            return subprocess.CompletedProcess(command, 0, stdout, b"")

        with tempfile.TemporaryDirectory() as directory:
            result = prepare_revision_checkout(Path(directory), revision(), runner=runner)
        self.assertEqual(result, Path(directory).resolve())
        flattened = [item for command, _ in calls for item in command]
        self.assertIn("https://review.whamcloud.com/fs/lustre-release", flattened)
        self.assertIn("refs/changes/65/61965/4", flattened)
        self.assertIn(SHA, flattened)
        for _command, kwargs in calls:
            self.assertNotIn("shell", kwargs)
            self.assertIs(kwargs["stdin"], subprocess.DEVNULL)

    def test_checkout_requires_empty_precreated_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            (target / "existing").write_text("x")
            with self.assertRaisesRegex(CheckoutError, "empty"):
                prepare_revision_checkout(target, revision())
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(CheckoutError, "pre-created"):
                prepare_revision_checkout(Path(directory) / "missing", revision())

    def test_checkout_reports_stage_without_leaking_stderr(self):
        def runner(command, **_kwargs):
            return subprocess.CompletedProcess(
                command, 1, b"", b"https://user:secret@review.whamcloud.com"
            )

        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(CheckoutError) as raised:
                prepare_revision_checkout(Path(directory), revision(), runner=runner)
        self.assertNotIn("secret", str(raised.exception))

    def test_checkout_rejects_wrong_resolved_head(self):
        def runner(command, **_kwargs):
            stdout = ("e" * 40 + "\n").encode() if command[-2:] == ["rev-parse", "HEAD"] else b""
            return subprocess.CompletedProcess(command, 0, stdout, b"")

        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(CheckoutError, "pinned revision"):
                prepare_revision_checkout(Path(directory), revision(), runner=runner)


if __name__ == "__main__":
    unittest.main()
