"""arc.settings.SettingsManager — plain/secret get/set/delete, and the
typed declare()/get()/validate_declared()/list_all() surface added for the
Improvement Doc's §1 P0 ("typed settings declaration"). Exercised directly
against a temp .arc/ dir — no boot() needed for any of this."""

from __future__ import annotations

from pathlib import Path

import pytest

from arc.settings import REDACTED, SettingsError, SettingsManager


@pytest.fixture
def mgr(project: Path) -> SettingsManager:
    return SettingsManager(project / ".arc")


class TestPlainGetSetDelete:
    def test_unset_key_is_none(self, mgr: SettingsManager):
        assert mgr.get("nope") is None

    def test_set_then_get_round_trips_as_a_string(self, mgr: SettingsManager):
        mgr.set("greeting", "hello")
        assert mgr.get("greeting") == "hello"

    def test_delete_existing_key_returns_true(self, mgr: SettingsManager):
        mgr.set("greeting", "hello")
        assert mgr.delete("greeting") is True
        assert mgr.get("greeting") is None

    def test_delete_missing_key_returns_false(self, mgr: SettingsManager):
        assert mgr.delete("nope") is False


class TestSecrets:
    def test_secret_get_without_reveal_is_redacted(self, mgr: SettingsManager):
        mgr.set("token", "super-secret-value", secret=True)
        assert mgr.get("token") == REDACTED

    def test_secret_get_with_reveal_returns_the_real_value(self, mgr: SettingsManager):
        mgr.set("token", "super-secret-value", secret=True)
        assert mgr.get("token", reveal=True) == "super-secret-value"

    def test_setting_a_declared_secret_key_as_plain_raises(self, mgr: SettingsManager):
        mgr.set("token", "value", secret=True)
        with pytest.raises(SettingsError):
            mgr.set("token", "value2", secret=False)

    def test_declaring_secret_over_an_existing_plain_key_raises(self, mgr: SettingsManager):
        mgr.set("token", "plain-value")
        with pytest.raises(SettingsError):
            mgr.declare("token", secret=True)


class TestTypedDeclareAndGet:
    def test_unset_typed_key_returns_its_declared_default(self, mgr: SettingsManager):
        mgr.declare("pool_size", type=int, default=10, doc="pool size")
        assert mgr.get("pool_size") == 10

    def test_set_typed_int_value_is_coerced(self, mgr: SettingsManager):
        mgr.declare("pool_size", type=int, default=10)
        mgr.set("pool_size", "25")
        value = mgr.get("pool_size")
        assert value == 25
        assert isinstance(value, int)

    def test_bad_int_value_raises_naming_the_key(self, mgr: SettingsManager):
        mgr.declare("pool_size", type=int, default=10)
        mgr.set("pool_size", "not-a-number")
        with pytest.raises(SettingsError, match="pool_size"):
            mgr.get("pool_size")

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("true", True),
            ("false", False),
            ("1", True),
            ("0", False),
            ("yes", True),
            ("no", False),
            ("on", True),
            ("off", False),
            ("TRUE", True),
            ("False", False),
        ],
    )
    def test_bool_coercion(self, mgr: SettingsManager, raw, expected):
        mgr.declare("flag", type=bool, default=False)
        mgr.set("flag", raw)
        assert mgr.get("flag") is expected

    def test_bad_bool_value_raises(self, mgr: SettingsManager):
        mgr.declare("flag", type=bool, default=False)
        mgr.set("flag", "maybe")
        with pytest.raises(SettingsError):
            mgr.get("flag")

    def test_float_coercion(self, mgr: SettingsManager):
        mgr.declare("ratio", type=float, default=1.0)
        mgr.set("ratio", "0.75")
        assert mgr.get("ratio") == 0.75

    def test_unsupported_type_raises_at_declare_time(self, mgr: SettingsManager):
        with pytest.raises(SettingsError):
            mgr.declare("bad", type=list)

    def test_redeclaring_same_type_is_idempotent(self, mgr: SettingsManager):
        mgr.declare("pool_size", type=int, default=10)
        mgr.declare("pool_size", type=int, default=10)  # must not raise
        assert mgr.get("pool_size") == 10

    def test_redeclaring_with_a_different_type_raises(self, mgr: SettingsManager):
        mgr.declare("pool_size", type=int, default=10)
        with pytest.raises(SettingsError):
            mgr.declare("pool_size", type=str, default="10")

    def test_secret_typed_value_is_coerced_only_when_revealed(self, mgr: SettingsManager):
        mgr.declare("api_timeout", secret=True, type=int, default=30)
        mgr.set("api_timeout", "60", secret=True)
        assert mgr.get("api_timeout") == REDACTED  # never coerces the placeholder
        assert mgr.get("api_timeout", reveal=True) == 60


class TestValidateDeclared:
    def test_all_valid_values_pass_silently(self, mgr: SettingsManager):
        mgr.declare("pool_size", type=int, default=10)
        mgr.set("pool_size", "42")
        mgr.validate_declared()  # must not raise

    def test_one_bad_value_raises_naming_it(self, mgr: SettingsManager):
        mgr.declare("pool_size", type=int, default=10)
        mgr.set("pool_size", "garbage")
        with pytest.raises(SettingsError, match="pool_size"):
            mgr.validate_declared()

    def test_multiple_bad_values_are_all_named_in_one_error(self, mgr: SettingsManager):
        mgr.declare("pool_size", type=int, default=10)
        mgr.declare("ratio", type=float, default=1.0)
        mgr.set("pool_size", "garbage")
        mgr.set("ratio", "also-garbage")
        with pytest.raises(SettingsError) as exc_info:
            mgr.validate_declared()
        assert "pool_size" in str(exc_info.value)
        assert "ratio" in str(exc_info.value)

    def test_untyped_declared_keys_are_never_validated(self, mgr: SettingsManager):
        mgr.declare("url")  # no type= — plain declare, unchanged old behavior
        mgr.set("url", "not-checked-against-anything")
        mgr.validate_declared()  # must not raise


class TestListAll:
    def test_declared_but_unset_key_shows_its_default(self, mgr: SettingsManager):
        mgr.declare("pool_size", type=int, default=10, doc="the pool size")
        data = mgr.list_all()
        assert data["pool_size"]["value"] is None
        assert data["pool_size"]["default"] == 10
        assert data["pool_size"]["type"] == "int"
        assert data["pool_size"]["doc"] == "the pool size"
        assert data["pool_size"]["kind"] == "setting"

    def test_set_key_shows_its_raw_string_value(self, mgr: SettingsManager):
        mgr.declare("pool_size", type=int, default=10)
        mgr.set("pool_size", "42")
        assert mgr.list_all()["pool_size"]["value"] == "42"

    def test_secret_never_shows_a_value(self, mgr: SettingsManager):
        mgr.set("token", "real-secret", secret=True)
        entry = mgr.list_all()["token"]
        assert entry["value"] is None
        assert entry["kind"] == "secret"

    def test_undeclared_plain_key_has_no_type_metadata(self, mgr: SettingsManager):
        mgr.set("random_key", "some-value")
        entry = mgr.list_all()["random_key"]
        assert entry["value"] == "some-value"
        assert entry["type"] is None
        assert entry["default"] is None
        assert entry["doc"] == ""
