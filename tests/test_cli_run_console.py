"""`arc run`'s child-process console rendering (arc/cli.py's
_render_child_line/_status_style/_REQUEST_LINE_RE) — the pure-function
pieces of the orchestrator's console rework, tested directly with no
subprocess/event-loop/boot() needed. Added because a raw, unescaped
console.print() of child stdout was corrupting output (Rich interprets
both its own markup syntax AND emoji shortcodes in plain text — a literal
"...:100:..." line rendered as 💯), and because request log lines should
now get their HTTP status recolored by class."""

from __future__ import annotations

import pytest

from arc.cli import _render_child_line, _status_style


class TestRenderChildLineEscapesPlainText:
    def test_a_plain_line_with_no_markup_is_returned_unchanged(self):
        assert _render_child_line("plain log line") == "plain log line"

    def test_an_emoji_shortcode_lookalike_is_escaped_not_interpreted(self):
        # Rich would otherwise turn ":100:" into the 💯 emoji.
        rendered = _render_child_line("[14:23:05] arc: line :100: of the file")
        assert ":100:" in rendered
        assert "\\:100:" in rendered or "[:100:]" not in rendered

    def test_literal_square_brackets_are_escaped_not_treated_as_style(self):
        rendered = _render_child_line("[14:23:05] arc: unresolved [safe] style-like text")
        assert "[safe]" in rendered
        assert "\\[safe]" in rendered


class TestRenderChildLineRecolorsRequestLines:
    def test_a_2xx_request_line_is_colored_green(self):
        line = "[14:23:05] gateway.middleware: GET /health -> 200 (3ms)"
        rendered = _render_child_line(line)
        assert "[green]200[/green]" in rendered

    def test_a_4xx_request_line_is_colored_yellow(self):
        line = "[14:23:05] gateway.middleware: GET /missing -> 404 (1ms)"
        rendered = _render_child_line(line)
        assert "[yellow]404[/yellow]" in rendered

    def test_a_5xx_request_line_is_colored_bold_red(self):
        line = "[14:23:05] gateway.middleware: POST /boom -> 500 (9ms)"
        rendered = _render_child_line(line)
        assert "[bold red]500[/bold red]" in rendered

    def test_a_3xx_request_line_is_colored_cyan(self):
        line = "[14:23:05] gateway.middleware: GET /old -> 301 (0ms)"
        rendered = _render_child_line(line)
        assert "[cyan]301[/cyan]" in rendered

    def test_the_rest_of_a_request_line_is_left_untouched_by_recoloring(self):
        line = "[14:23:05] gateway.middleware: GET /path/:100: -> 200 (3ms)"
        rendered = _render_child_line(line)
        assert "[green]200[/green]" in rendered
        assert "/path/:100:" in rendered


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
