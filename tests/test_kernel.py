"""arc.kernel — the in-process capability registry, tested via direct
Kernel() construction (no boot() / no project directory needed at all)."""

from __future__ import annotations

import warnings

import pytest

from arc.kernel import (
    ArcAdvisory,
    Capability,
    ExportError,
    Kernel,
    KernelError,
    capability_name_problem,
)


class TestCapabilityNameProblem:
    def test_valid_name_has_no_problem(self):
        assert capability_name_problem("psqldb") is None

    def test_non_identifier_is_a_problem(self):
        assert capability_name_problem("not-an-identifier") is not None

    def test_empty_string_is_a_problem(self):
        assert capability_name_problem("") is not None

    def test_non_string_is_a_problem(self):
        assert capability_name_problem(123) is not None

    def test_leading_underscore_is_a_problem(self):
        assert capability_name_problem("_private") is not None

    @pytest.mark.parametrize("name", ["boot", "kernel", "settings", "Kernel", "__version__"])
    def test_reserved_names_are_a_problem(self, name):
        assert capability_name_problem(name) is not None


class TestExport:
    def test_export_then_get_returns_the_same_instance(self):
        kernel = Kernel()
        sentinel = object()
        kernel.export("widget", sentinel)
        assert kernel.get("widget") is sentinel
        assert kernel.has("widget") is True

    def test_has_is_false_for_unknown_capability(self):
        kernel = Kernel()
        assert kernel.has("nope") is False

    def test_get_unknown_capability_raises(self):
        kernel = Kernel()
        with pytest.raises(KernelError):
            kernel.get("nope")

    def test_invalid_capability_name_raises_export_error(self):
        kernel = Kernel()
        with pytest.raises(ExportError):
            kernel.export("not-an-identifier", object())

    def test_duplicate_capability_name_raises_export_error(self):
        kernel = Kernel()
        kernel.export("widget", object())
        with pytest.raises(ExportError):
            kernel.export("widget", object())

    def test_missing_hard_requirement_raises_export_error(self):
        kernel = Kernel()
        with pytest.raises(ExportError):
            kernel.export("dependent", object(), requires=["missing"])

    def test_hard_requirement_already_registered_succeeds(self):
        kernel = Kernel()
        kernel.export("base", object())
        kernel.export("dependent", object(), requires=["base"])
        assert kernel.has("dependent")

    def test_optional_requirement_absent_is_fine(self):
        kernel = Kernel()
        kernel.export("solo", object(), optional_requires=["absent"])
        assert kernel.has("solo")

    def test_capabilities_view_is_read_only(self):
        kernel = Kernel()
        kernel.export("widget", object())
        caps = kernel.capabilities()
        assert isinstance(caps["widget"], Capability)
        with pytest.raises(TypeError):
            caps["widget"] = None  # type: ignore[index]

    def test_current_plugin_is_none_outside_register(self):
        kernel = Kernel()
        assert kernel.current_plugin() is None


class TestAdvise:
    def test_advise_records_and_warns(self):
        kernel = Kernel()
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            kernel.advise("something worth knowing")
        assert "something worth knowing" in kernel.advisories
        assert any(issubclass(w.category, ArcAdvisory) for w in caught)
