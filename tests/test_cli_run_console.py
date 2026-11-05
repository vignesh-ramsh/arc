"""`arc run`'s child-process console relay (arc/cli.py's _format_child_line /
_status_style / _REQUEST_LINE_RE) — pure functions, tested directly with no
subprocess/event-loop/boot() needed.

The relay used to push every child line through Rich's console.print()
(escape + markup parse + emoji pass + render + flush) on ONE thread shared by
all workers; at a few thousand request lines per second that thread saturated
a core, the children's stdout pipe filled, and the workers blocked in write()
(see gateway's gateway_access_log setting). It now writes plain text / direct
ANSI. These tests pin that the output stays what a person saw before: a dimmed
"[role]" prefix, request status recolored by class, no line text ever
misread as Rich markup or an emoji shortcode ("...:100:..." once printed 💯)."""

from __future__ import annotations

import io
import time

import pytest
from rich.console import Console
from rich.markup import escape

from arc.cli import _REQUEST_LINE_RE, _format_child_line, _status_style

GREEN, CYAN, YELLOW, BOLD_RED = "\x1b[32m", "\x1b[36m", "\x1b[33m", "\x1b[1;31m"
DIM, RESET = "\x1b[2m", "\x1b[0m"


class TestPlainOutput:
    def test_a_plain_line_gets_a_role_prefix_and_a_newline(self):
        assert _format_child_line("gateway", "plain log line", color=False) == "[gateway] plain log line\n"

    def test_an_emoji_shortcode_lookalike_is_left_literally_alone(self):
        # Rich would otherwise turn ":100:" into the 💯 emoji.
        out = _format_child_line("gateway", "[14:23:05] arc: line :100: of the file", color=False)
        assert ":100:" in out and "💯" not in out

    def test_literal_square_brackets_are_not_treated_as_style(self):
        out = _format_child_line("gateway", "[14:23:05] arc: unresolved [safe] style-like text", color=True)
        assert "[safe]" in out and "\\[safe]" not in out

    def test_a_request_line_is_not_colored_when_color_is_off(self):
        out = _format_child_line("gateway", "[14:23:05] gateway.access: GET /health -> 200 (3ms)", color=False)
        assert "\x1b" not in out
        assert out.endswith("GET /health -> 200 (3ms)\n")

    def test_control_characters_that_could_move_the_cursor_are_stripped(self):
        out = _format_child_line("gateway", "ok\rOVERWRITE\x07\x08\x0b\x0c", color=False)
        assert out == "[gateway] okOVERWRITE\n"

    def test_ansi_color_codes_from_the_child_pass_through(self):
        child = "\x1b[31mred from uvicorn\x1b[0m"
        assert child in _format_child_line("gateway", child, color=False)


class TestColoredOutput:
    def test_the_role_prefix_is_dimmed(self):
        out = _format_child_line("lineup", "booted", color=True)
        assert out == f"{DIM}[lineup]{RESET} booted\n"

    @pytest.mark.parametrize(
        "line,ansi,status",
        [
            ("[14:23:05] gateway.access: GET /health -> 200 (3ms)", GREEN, "200"),
            ("[14:23:05] gateway.access: GET /old -> 301 (0ms)", CYAN, "301"),
            ("[14:23:05] gateway.access: GET /missing -> 404 (1ms)", YELLOW, "404"),
            ("[14:23:05] gateway.access: POST /boom -> 500 (9ms)", BOLD_RED, "500"),
        ],
    )
    def test_request_status_is_recolored_by_class(self, line, ansi, status):
        out = _format_child_line("gateway", line, color=True)
        assert f"{ansi}{status}{RESET}" in out

    def test_the_rest_of_a_request_line_is_untouched_by_recoloring(self):
        out = _format_child_line("gateway", "[14:23:05] gateway.access: GET /path/:100: -> 200 (3ms)", color=True)
        assert f"{GREEN}200{RESET}" in out
        assert "/path/:100:" in out and " (3ms)\n" in out

    def test_a_line_that_merely_contains_an_arrow_is_not_mistaken_for_a_request(self):
        out = _format_child_line("lineup", "job a -> b finished", color=True)
        assert out == f"{DIM}[lineup]{RESET} job a -> b finished\n"


class TestMatchesWhatRichUsedToPrint:
    """Differential: render the old way (Rich markup, escape(), console.print)
    and compare byte for byte with the new direct-ANSI output."""

    @staticmethod
    def _rich(role: str, text: str) -> str:
        buf = io.StringIO()
        console = Console(file=buf, force_terminal=True, color_system="standard", width=500)
        match = _REQUEST_LINE_RE.match(text)
        if match is None:
            body = escape(text)
        else:
            style = _status_style(int(match.group("status")))
            body = (
                f"{escape(match.group('prefix'))}[{style}]{match.group('status')}[/{style}]"
                f"{escape(match.group('suffix'))}"
            )
        console.print(f"[dim]\\[{role}][/dim] {body}", emoji=False, highlight=False)
        return buf.getvalue()

    @pytest.mark.parametrize(
        "text",
        [
            "booted worker 3",
            "[14:23:05] arc: unresolved [safe] style-like text",
            "[14:23:05] gateway.access: GET /health -> 200 (3ms)",
            "[14:23:05] gateway.access: GET /missing -> 404 (1ms)",
            "[14:23:05] gateway.access: POST /boom -> 500 (9ms)",
        ],
    )
    def test_identical_bytes(self, text):
        assert _format_child_line("gateway", text, color=True) == self._rich("gateway", text)


class TestThroughput:
    def test_formatting_and_writing_tens_of_thousands_of_lines_per_second(self):
        """The reason this exists: Rich managed ~4k lines/s on one thread. A
        generous floor (10k/s; the real figure is >100k/s) so a slow CI box
        can't flake it, while still failing if per-line markup parsing or a
        render pipeline ever creeps back in."""
        sink = io.StringIO()
        line = "[14:23:05] gateway.access: GET /api/v1/benchplug.get_item -> 200 (3ms)"
        n = 20_000
        start = time.perf_counter()
        for _ in range(n):
            sink.write(_format_child_line("gateway", line, color=True))
        elapsed = time.perf_counter() - start
        assert n / elapsed > 10_000, f"{n / elapsed:,.0f} lines/s"


class TestStatusStyle:
    @pytest.mark.parametrize(
        "status,expected",
        [
            (200, "green"),
            (204, "green"),
            (301, "cyan"),
            (399, "cyan"),
            (404, "yellow"),
            (499, "yellow"),
            (500, "bold red"),
            (503, "bold red"),
        ],
    )
    def test_status_maps_to_expected_style(self, status, expected):
        assert _status_style(status) == expected
