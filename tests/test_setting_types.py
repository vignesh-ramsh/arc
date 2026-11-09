"""A key added by hand from the admin page is created with a type and a description.

The code declares a type for most keys (`declare(type=int)`); a key an administrator adds has none anywhere, so the
`setting` table keeps one (`str`, `int`, `float` or `bool`). It is given when the key is created and never changed:
what the code declares always wins, a stored type decides what `get()` returns and what `set()` accepts, and a call
that is refused writes nothing at all.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from arc import store
from arc.settings import SettingsError, SettingsManager


@pytest.fixture
def mgr(project: Path) -> SettingsManager:
    return SettingsManager(project / ".arc")


def stored(mgr: SettingsManager, key: str) -> tuple | None:
    return mgr._conn.execute(
        "SELECT value, is_secret, type, description, description_edited, description_by FROM setting WHERE key = ?",
        (key,),
    ).fetchone()


class TestCreatingAKeyWithATypeAndADescription:
    def test_both_are_stored_with_the_value_and_who_wrote_the_description(
        self, mgr: SettingsManager
    ):
        mgr.set(
            "retry_limit",
            "5",
            updated_by="a@example.com",
            type="int",
            description="How many times to retry.",
        )
        assert stored(mgr, "retry_limit") == (
            "5",
            0,
            "int",
            "How many times to retry.",
            1,
            "a@example.com",
        )

    def test_the_description_is_trimmed(self, mgr: SettingsManager):
        mgr.set("greeting", "hi", description="  Said on arrival.  \n")
        assert stored(mgr, "greeting")[3] == "Said on arrival."

    def test_a_key_may_still_be_created_with_neither(self, mgr: SettingsManager):
        mgr.set("plain_old", "x")
        assert stored(mgr, "plain_old") == ("x", 0, None, None, 0, None)

    def test_a_secret_gets_them_too_and_its_value_is_not_in_the_setting_row(
        self, mgr: SettingsManager
    ):
        mgr.set(
            "api_pin",
            "1234",
            secret=True,
            type="int",
            description="The pin.",
            updated_by="a@example.com",
        )
        assert stored(mgr, "api_pin") == (None, 1, "int", "The pin.", 1, "a@example.com")
        assert mgr.get("api_pin", reveal=True) == 1234

    def test_list_all_shows_the_type_and_the_description(self, mgr: SettingsManager):
        mgr.set("retry_limit", "5", type="int", description="How many times to retry.")
        entry = mgr.list_all()["retry_limit"]
        assert (entry["type"], entry["doc"], entry["doc_edited"]) == (
            "int",
            "How many times to retry.",
            True,
        )
        assert entry["code_doc"] == ""  # nothing in the code: this one belongs to the administrator

    def test_an_untyped_key_shows_no_type(self, mgr: SettingsManager):
        mgr.set("plain_old", "x")
        assert mgr.list_all()["plain_old"]["type"] is None


class TestTheTypeDecidesWhatIsReadAndWhatIsAccepted:
    @pytest.mark.parametrize(
        ("type_", "raw", "expected"),
        [
            ("int", "42", 42),
            ("float", "1.5", 1.5),
            ("bool", "yes", True),
            ("bool", "off", False),
            ("str", "12", "12"),
        ],
    )
    def test_get_returns_the_value_as_that_type(self, mgr: SettingsManager, type_, raw, expected):
        mgr.set("thing", raw, type=type_, description="A thing.")
        value = mgr.get("thing")
        assert value == expected and type(value) is type(expected)

    def test_a_later_value_must_still_be_that_type(self, mgr: SettingsManager):
        mgr.set("retry_limit", "5", type="int", description="How many times to retry.")
        with pytest.raises(SettingsError, match="int"):
            mgr.set("retry_limit", "five")
        assert mgr.get("retry_limit") == 5  # the refused value was not written

    def test_a_valid_later_value_is_accepted_and_read_back_fresh(self, mgr: SettingsManager):
        mgr.set("retry_limit", "5", type="int", description="How many times to retry.")
        assert mgr.get("retry_limit") == 5  # now cached
        mgr.set("retry_limit", "9")
        assert mgr.get("retry_limit") == 9

    @pytest.mark.parametrize(
        ("type_", "raw"), [("int", "4.5"), ("int", ""), ("float", "abc"), ("bool", "maybe")]
    )
    def test_a_value_that_is_not_of_the_type_is_refused_when_the_key_is_created(
        self, mgr: SettingsManager, type_, raw
    ):
        with pytest.raises(SettingsError, match=type_):
            mgr.set("thing", raw, type=type_, description="A thing.")

    def test_the_message_for_a_bool_lists_what_is_accepted(self, mgr: SettingsManager):
        with pytest.raises(SettingsError, match="true/false"):
            mgr.set("flag", "maybe", type="bool", description="A flag.")

    def test_a_secret_value_must_be_of_its_type_too(self, mgr: SettingsManager):
        with pytest.raises(SettingsError, match="int"):
            mgr.set("api_pin", "abcd", secret=True, type="int", description="The pin.")

    def test_a_refused_call_leaves_nothing_behind(self, mgr: SettingsManager):
        with pytest.raises(SettingsError):
            mgr.set("thing", "abc", type="int", description="A thing.")
        with pytest.raises(SettingsError):
            mgr.set("other", "1", type="int", description="   ")
        with pytest.raises(SettingsError):
            mgr.set("third", "1", type="decimal", description="A thing.")
        assert not any(store.key_exists(mgr._conn, k) for k in ("thing", "other", "third"))

    def test_the_type_does_not_leak_into_a_key_with_a_declared_type(self, mgr: SettingsManager):
        mgr.declare("pool_size", type=int, default=5, doc="Connections.")
        mgr.set("pool_size", "7")
        assert mgr.get("pool_size") == 7
        assert mgr.list_all()["pool_size"]["type"] == "int"


class TestRefusingWhatCannotBeGivenLater:
    def test_a_type_cannot_be_given_to_a_key_that_exists(self, mgr: SettingsManager):
        mgr.set("retry_limit", "5", type="int", description="How many times to retry.")
        with pytest.raises(SettingsError, match="already exists"):
            mgr.set("retry_limit", "6", type="str")
        assert stored(mgr, "retry_limit")[2] == "int"

    def test_a_description_cannot_be_given_to_a_key_that_exists(self, mgr: SettingsManager):
        mgr.set("retry_limit", "5", type="int", description="How many times to retry.")
        with pytest.raises(SettingsError, match="already exists"):
            mgr.set("retry_limit", "6", description="Something else.")
        assert stored(mgr, "retry_limit")[3] == "How many times to retry."

    def test_a_declared_key_counts_as_existing_even_before_it_has_a_value(
        self, mgr: SettingsManager
    ):
        mgr.declare("restart_command", doc="The command.")
        with pytest.raises(SettingsError, match="already exists"):
            mgr.set("restart_command", "x", type="str", description="Mine.")

    def test_a_key_kept_only_for_its_description_counts_as_existing(self, mgr: SettingsManager):
        mgr.set("retry_limit", "5", type="int", description="How many times to retry.")
        mgr.delete("retry_limit")  # the value goes, the row (and with it the type) stays
        assert stored(mgr, "retry_limit")[:3] == (None, 0, "int")
        with pytest.raises(SettingsError, match="already exists"):
            mgr.set("retry_limit", "1", type="int", description="Again.")
        mgr.set("retry_limit", "1")  # an ordinary value write still works, and is still checked
        assert mgr.get("retry_limit") == 1

    def test_a_type_that_is_not_one_of_the_four_is_refused(self, mgr: SettingsManager):
        with pytest.raises(SettingsError, match="not a type"):
            mgr.set("thing", "1", type="decimal", description="A thing.")

    def test_a_description_over_the_limit_is_refused_before_anything_is_written(
        self, mgr: SettingsManager
    ):
        with pytest.raises(SettingsError, match=str(store.MAX_DESCRIPTION_CHARS)):
            mgr.set("thing", "1", type="int", description="x" * (store.MAX_DESCRIPTION_CHARS + 1))
        assert not store.key_exists(mgr._conn, "thing")


class TestTheColumn:
    def test_a_store_made_before_the_column_gets_it_without_losing_a_row(self, tmp_path: Path):
        path = tmp_path / "old" / "arc.store.db"
        path.parent.mkdir()
        old = sqlite3.connect(path)
        old.executescript(
            "CREATE TABLE setting (key TEXT PRIMARY KEY, value TEXT, is_secret INTEGER NOT NULL DEFAULT 0, "
            "updated_at TEXT NOT NULL, updated_by TEXT, description TEXT, description_edited INTEGER NOT NULL "
            "DEFAULT 0, description_by TEXT, description_at TEXT);"
            "INSERT INTO setting(key, value, updated_at) VALUES ('kept', 'v', 'then');"
        )
        old.commit()
        old.close()
        conn = store.open_db(path)
        assert "type" in {r[1] for r in conn.execute("PRAGMA table_info(setting)")}
        assert conn.execute("SELECT key, value, type FROM setting").fetchall() == [
            ("kept", "v", None)
        ]

    def test_a_key_set_the_old_way_has_no_type_and_reads_as_text(self, mgr: SettingsManager):
        mgr.set("legacy", "007")
        assert mgr.get("legacy") == "007"
