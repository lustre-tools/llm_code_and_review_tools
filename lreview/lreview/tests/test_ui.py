"""Tests for the terminal output console."""

import io

from lreview.ui import Console


class FakeTty(io.StringIO):
    def isatty(self):
        return True


def _console_with(monkeypatch, stream):
    console = Console()
    monkeypatch.setattr("lreview.ui.sys.stdout", stream)
    return console


class TestNonTty:
    """Piped output: plain appended lines, no escape codes."""

    def test_status_appends_plain_lines(self, monkeypatch):
        out = io.StringIO()
        console = _console_with(monkeypatch, out)
        console.status("running: x 1m")
        console.event("done x")
        assert out.getvalue() == "running: x 1m\ndone x\n"
        assert "\x1b" not in out.getvalue()
        assert "\r" not in out.getvalue()

    def test_colors_disabled(self, monkeypatch):
        console = _console_with(monkeypatch, io.StringIO())
        assert console.color("red", "boom") == "boom"


class TestTty:
    """Interactive output: in-place status line, colors on."""

    def test_status_redrawn_in_place(self, monkeypatch):
        out = FakeTty()
        console = _console_with(monkeypatch, out)
        console.status("running: x 1m")
        console.status("running: x 2m")
        text = out.getvalue()
        # Second status clears and overwrites, no newline between
        assert text == ("\r\x1b[2Krunning: x 1m"
                        "\r\x1b[2Krunning: x 2m")

    def test_event_reprints_status_below(self, monkeypatch):
        out = FakeTty()
        console = _console_with(monkeypatch, out)
        console.status("running: x 1m")
        console.event("[x] clean")
        text = out.getvalue()
        # Event clears the status, prints its line, redraws the status
        assert text.endswith("\r\x1b[2K[x] clean\nrunning: x 1m")

    def test_clear_status(self, monkeypatch):
        out = FakeTty()
        console = _console_with(monkeypatch, out)
        console.status("running: x 1m")
        console.clear_status()
        console.event("done")
        assert out.getvalue().endswith("\r\x1b[2Kdone\n")

    def test_colors_enabled(self, monkeypatch):
        out = FakeTty()
        console = _console_with(monkeypatch, out)
        monkeypatch.delenv("NO_COLOR", raising=False)
        assert console.color("green", "ok") == "\x1b[32mok\x1b[0m"

    def test_no_color_env_respected(self, monkeypatch):
        out = FakeTty()
        console = _console_with(monkeypatch, out)
        monkeypatch.setenv("NO_COLOR", "1")
        assert console.color("green", "ok") == "ok"
