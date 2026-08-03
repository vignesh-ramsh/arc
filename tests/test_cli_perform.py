"""`arc perform`'s pure argument-parsing/target-resolution pieces
(arc/cli.py's _parse_perform_json_args/_resolve_perform_target) — tested
directly, with no boot()/subprocess needed, same convention
test_cli_run_console.py already uses for arc run's own pure helpers.

The full command (booting, opening every capability, importing/calling
the resolved target, closing everything again) was verified live against
a real project instead — it needs a real Postgres-backed boot to mean
anything, the same reason arc run's own orchestration isn't unit-tested
here either.
"""

from __future__ import annotations

import pytest

from arc.cli import _parse_perform_json_args, _resolve_perform_target


class TestParsePerformJsonArgs:
    def test_defaults_are_empty_list_and_dict(self):
        args, kwargs = _parse_perform_json_args("[]", "{}")
        assert args == []
        assert kwargs == {}

    def test_a_real_args_array_and_kwargs_object_parse_correctly(self):
        args, kwargs = _parse_perform_json_args('[1, "x"]', '{"employee_id": "E001"}')
        assert args == [1, "x"]
        assert kwargs == {"employee_id": "E001"}

    def test_malformed_args_json_raises_with_a_clear_message(self):
        with pytest.raises(ValueError, match="--args is not valid JSON"):
            _parse_perform_json_args("{not json}", "{}")

    def test_malformed_kwargs_json_raises_with_a_clear_message(self):
        with pytest.raises(ValueError, match="--kwargs is not valid JSON"):
            _parse_perform_json_args("[]", "{not json}")

    def test_args_that_parses_but_isnt_a_list_is_rejected(self):
        with pytest.raises(ValueError, match="--args must be a JSON array"):
            _parse_perform_json_args('{"oops": 1}', "{}")

    def test_kwargs_that_parses_but_isnt_an_object_is_rejected(self):
        with pytest.raises(ValueError, match="--kwargs must be a JSON object"):
            _parse_perform_json_args("[]", "[1, 2]")


class TestResolvePerformTarget:
    def test_no_colon_means_a_whitelisted_function_name(self):
        assert _resolve_perform_target("hrms.get_employee") is None

    def test_colon_splits_into_module_path_and_attr_path(self):
        assert _resolve_perform_target("hrms.tasks.reports:nightly_headcount_report") == (
            "hrms.tasks.reports",
            "nightly_headcount_report",
        )

    def test_a_dotted_attr_path_after_the_colon_is_preserved_whole(self):
        # e.g. a nested class/namespace on the imported module — the
        # command splits attr_path on "." itself, one getattr() per part.
        module_path, attr_path = _resolve_perform_target("pkg.mod:Namespace.func")
        assert module_path == "pkg.mod"
        assert attr_path == "Namespace.func"

    def test_missing_module_side_is_rejected(self):
        with pytest.raises(ValueError, match="module.path:function_name"):
            _resolve_perform_target(":func")

    def test_missing_attr_side_is_rejected(self):
        with pytest.raises(ValueError, match="module.path:function_name"):
            _resolve_perform_target("pkg.mod:")
