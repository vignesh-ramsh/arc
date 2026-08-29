"""arc.resolver's PEP 440 version-specifier support on requires/
optional_requires — "pgdb>=3.0" alongside the plain "pgdb" form that
existed before this. Exercised directly against resolve()'s pure,
side-effect-free planning (no boot() needed for any of this) — see
resolver.py's own module docstring."""

from __future__ import annotations

import pytest
import tomlkit

from arc.registry import PluginManifest, parse_requirement, validate_requires, version_satisfies
from arc.resolver import ResolutionError, resolve

from .conftest import FakeEntryPoint


def _manifest(name: str, version: str = "0.1.0", requires: list[str] | None = None) -> PluginManifest:
    return PluginManifest(name=name, version=version, capability=name, requires=requires or [])


def _lock_doc(specs: list[dict]) -> tomlkit.TOMLDocument:
    doc = tomlkit.document()
    plugins_table = tomlkit.table()
    for spec in specs:
        entry = tomlkit.table()
        entry["version"] = spec.get("version", "0.1.0")
        entry["capability"] = spec.get("capability", spec["name"])
        entry["requires"] = list(spec.get("requires", []))
        entry["optional_requires"] = list(spec.get("optional_requires", []))
        entry["enabled"] = spec.get("enabled", True)
        plugins_table[spec["name"]] = entry
    doc["plugins"] = plugins_table
    return doc


def _eps(*names: str) -> list[FakeEntryPoint]:
    return [FakeEntryPoint(n, lambda kernel: None) for n in names]


class TestParseRequirement:
    def test_plain_name_has_no_version_specifier(self):
        assert parse_requirement("pgdb") == ("pgdb", None)

    def test_name_with_specifier_splits_cleanly(self):
        assert parse_requirement("pgdb>=3.0") == ("pgdb", ">=3.0")

    def test_compound_specifier_is_kept_as_one_string(self):
        assert parse_requirement("pgdb>=3.0,<4.0") == ("pgdb", ">=3.0,<4.0")

    def test_whitespace_between_name_and_specifier_is_tolerated(self):
        assert parse_requirement("pgdb >= 3.0") == ("pgdb", ">= 3.0")

    def test_empty_string_raises(self):
        with pytest.raises(ValueError):
            parse_requirement("")

    def test_specifier_with_no_name_raises(self):
        with pytest.raises(ValueError):
            parse_requirement(">=3.0")


class TestVersionSatisfies:
    def test_floor_only(self):
        assert version_satisfies("3.2.0", ">=3.0") is True
        assert version_satisfies("2.9.0", ">=3.0") is False

    def test_floor_and_ceiling(self):
        assert version_satisfies("3.5.0", ">=3.0,<4.0") is True
        assert version_satisfies("4.0.0", ">=3.0,<4.0") is False

    def test_invalid_specifier_raises(self):
        with pytest.raises(ValueError):
            version_satisfies("1.0.0", "not-a-specifier")

    def test_backwards_operator_gets_an_actionable_hint(self):
        with pytest.raises(ValueError, match="did you mean '<=2.0.0'"):
            version_satisfies("1.0.0", "=<2.0.0")

    def test_invalid_version_raises(self):
        with pytest.raises(ValueError):
            version_satisfies("not-a-version", ">=1.0")


class TestResolveUnversionedRequiresStillWorksExactlyAsBefore:
    def test_plain_requires_boots_regardless_of_provider_version(self):
        specs = [
            {"name": "pgdb", "version": "0.0.1"},
            {"name": "authn", "requires": ["pgdb"]},
        ]
        plan = resolve(lock_doc=_lock_doc(specs), entry_points=_eps("pgdb", "authn"))
        names_in_order = [s.name for s in plan.load_order]
        assert names_in_order.index("pgdb") < names_in_order.index("authn")


class TestResolveHardRequiresVersionChecking:
    def test_satisfied_floor_boots_cleanly(self):
        specs = [
            {"name": "pgdb", "version": "3.2.0"},
            {"name": "authn", "requires": ["pgdb>=3.0"]},
        ]
        plan = resolve(lock_doc=_lock_doc(specs), entry_points=_eps("pgdb", "authn"))
        names = [s.name for s in plan.load_order]
        assert names.index("pgdb") < names.index("authn")

    def test_unsatisfied_floor_raises_naming_both_plugins_and_versions(self):
        specs = [
            {"name": "pgdb", "version": "2.1.0"},
            {"name": "authn", "requires": ["pgdb>=3.0"]},
        ]
        with pytest.raises(ResolutionError, match="pgdb>=3.0") as exc_info:
            resolve(lock_doc=_lock_doc(specs), entry_points=_eps("pgdb", "authn"))
        assert "2.1.0" in str(exc_info.value)

    def test_unsatisfied_floor_names_both_remediation_paths(self):
        specs = [
            {"name": "pgdb", "version": "2.1.0"},
            {"name": "authn", "requires": ["pgdb>=3.0"]},
        ]
        with pytest.raises(ResolutionError) as exc_info:
            resolve(lock_doc=_lock_doc(specs), entry_points=_eps("pgdb", "authn"))
        message = str(exc_info.value)
        assert "Upgrade 'pgdb'" in message
        assert "arc plugin disable authn" in message

    def test_ceiling_also_enforced(self):
        specs = [
            {"name": "pgdb", "version": "4.0.0"},
            {"name": "authn", "requires": ["pgdb>=3.0,<4.0"]},
        ]
        with pytest.raises(ResolutionError):
            resolve(lock_doc=_lock_doc(specs), entry_points=_eps("pgdb", "authn"))

    def test_invalid_specifier_syntax_raises_a_clear_resolution_error(self):
        specs = [
            {"name": "pgdb", "version": "3.0.0"},
            {"name": "authn", "requires": ["pgdb>=3.x"]},
        ]
        with pytest.raises(ResolutionError, match="authn"):
            resolve(lock_doc=_lock_doc(specs), entry_points=_eps("pgdb", "authn"))


class TestValidateRequiresSubsetChecking:
    """validate_requires(manifests, universe=...) — the piece behind `arc
    build -p <plugin>` only failing over THAT plugin's own requires, never
    an unrelated plugin's (arc/arc/cli.py's build command)."""

    def test_default_universe_is_the_manifests_themselves(self):
        pgdb = _manifest("pgdb", version="0.1.0")
        authn = _manifest("authn", requires=["pgdb>=2.0.0"])
        errors = validate_requires([pgdb, authn])
        assert len(errors) == 1
        assert "authn" in errors[0] and "pgdb>=2.0.0" in errors[0]

    def test_checking_a_subset_still_resolves_against_the_wider_universe(self):
        pgdb = _manifest("pgdb", version="3.0.0")
        authn = _manifest("authn", requires=["pgdb>=2.0.0"])
        # Checking ONLY authn, but pgdb (outside the checked subset) is what
        # its requirement resolves against — must NOT report pgdb as "not
        # among the plugins being built" just because it wasn't in `manifests`.
        errors = validate_requires([authn], universe=[pgdb, authn])
        assert errors == []

    def test_scoped_check_ignores_an_unrelated_broken_plugin(self):
        pgdb = _manifest("pgdb", version="0.1.0")
        authn = _manifest("authn", requires=["pgdb>=2.0.0"])  # broken
        filer = _manifest("filer", requires=[])  # fine, no requires of its own
        # Scoping to just `filer` must be silent, even though authn
        # (elsewhere in the universe) is broken.
        errors = validate_requires([filer], universe=[pgdb, authn, filer])
        assert errors == []

    def test_scoped_check_still_reports_its_own_broken_requirement(self):
        pgdb = _manifest("pgdb", version="0.1.0")
        authn = _manifest("authn", requires=["pgdb>=2.0.0"])
        errors = validate_requires([authn], universe=[pgdb, authn])
        assert len(errors) == 1


class TestResolveOptionalRequiresVersionChecking:
    def test_satisfied_optional_influences_load_order(self):
        specs = [
            {"name": "redix", "version": "1.5.0"},
            {"name": "authn", "optional_requires": ["redix>=1.0"]},
        ]
        plan = resolve(lock_doc=_lock_doc(specs), entry_points=_eps("redix", "authn"))
        names = [s.name for s in plan.load_order]
        assert names.index("redix") < names.index("authn")

    def test_unsatisfied_optional_does_not_fail_boot_just_warns_and_skips_ordering(self):
        specs = [
            {"name": "redix", "version": "0.5.0"},
            {"name": "authn", "optional_requires": ["redix>=1.0"]},
        ]
        plan = resolve(lock_doc=_lock_doc(specs), entry_points=_eps("redix", "authn"))
        # both still boot — no ResolutionError — but the warning names the mismatch
        assert any("redix>=1.0" in w for w in plan.warnings)
        assert any("0.5.0" in w for w in plan.warnings)
