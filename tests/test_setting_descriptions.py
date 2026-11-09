"""Descriptions live in the existing `setting` table (four added columns), next to the value they describe.

The admin Settings page shows what each setting or secret is for and lets an administrator rewrite that text.
The code declares the original (`declare(doc=...)`); the store keeps a copy per key, and an edit changes only
that copy, never the code. Most keys have never been set, so a declared key gets a row with no value: a row now
means "this key is known", and no longer "this key has a value".

What has to stay true, and is tested here: the table upgrades in place without losing a row; a value change
never touches a description and a description change never touches a value; clearing a value (`delete`, the way
back to the default) does not erase the description; a secret stays a secret; the copy at boot costs nothing
when nothing changed (seven processes start together) and never disturbs the settings' read caches.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from arc import store
from arc.settings import SettingsError, SettingsManager

OLD_SCHEMA = """
CREATE TABLE setting (
    key         TEXT PRIMARY KEY,
    value       TEXT,
    is_secret   INTEGER NOT NULL DEFAULT 0,
    updated_at  TEXT NOT NULL,
    updated_by  TEXT
);
CREATE TABLE secret (
    key         TEXT PRIMARY KEY REFERENCES setting(key),
    ciphertext  BLOB NOT NULL,
    updated_at  TEXT NOT NULL,
    updated_by  TEXT
);
CREATE TABLE secret_access_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    key         TEXT NOT NULL,
    accessed_at TEXT NOT NULL,
    accessed_by TEXT
);
"""

#: (name, type, not null, default, primary key) of every column, in order: what `PRAGMA table_info(setting)` gives
FINAL_COLUMNS = [
    ("key", "TEXT", 0, None, 1),
    ("value", "TEXT", 0, None, 0),
    ("is_secret", "INTEGER", 1, "0", 0),
    ("updated_at", "TEXT", 1, None, 0),
    ("updated_by", "TEXT", 0, None, 0),
    ("description", "TEXT", 0, None, 0),
    ("description_edited", "INTEGER", 1, "0", 0),
    ("description_by", "TEXT", 0, None, 0),
    ("description_at", "TEXT", 0, None, 0),
    ("type", "TEXT", 0, None, 0),
]


def columns(conn: sqlite3.Connection) -> list[tuple]:
    return [(r[1], r[2], r[3], r[4], r[5]) for r in conn.execute("PRAGMA table_info(setting)")]


def row(mgr: SettingsManager, key: str) -> sqlite3.Row | tuple | None:
    return mgr._conn.execute(
        "SELECT value, is_secret, description, description_edited, description_by, description_at, updated_by "
        "FROM setting WHERE key = ?",
        (key,),
    ).fetchone()


@pytest.fixture
def mgr(project: Path) -> SettingsManager:
    return SettingsManager(project / ".arc")


# --------------------------------------------------------------------------- the table
class TestTheTable:
    def test_a_new_store_has_exactly_the_final_columns(self, tmp_path: Path):
        conn = store.open_db(tmp_path / "fresh" / "arc.store.db")
        assert columns(conn) == FINAL_COLUMNS

    def test_an_old_store_is_upgraded_in_place_without_losing_anything(self, tmp_path: Path):
        path = tmp_path / "old" / "arc.store.db"
        path.parent.mkdir()
        old = sqlite3.connect(path)
        old.executescript(OLD_SCHEMA)
        old.execute(
            "INSERT INTO setting VALUES ('log_level', 'DEBUG', 0, '2026-08-01T00:00:00+00:00', 'cli')"
        )
        old.execute(
            "INSERT INTO setting VALUES ('pgdb_dsn', NULL, 1, '2026-08-01T00:00:00+00:00', 'cli')"
        )
        old.execute(
            "INSERT INTO secret VALUES ('pgdb_dsn', x'0102', '2026-08-01T00:00:00+00:00', 'cli')"
        )
        old.execute(
            "INSERT INTO secret_access_log(key, accessed_at, accessed_by) VALUES ('pgdb_dsn', 't', 'me')"
        )
        old.commit()
        old.close()

        conn = store.open_db(path)
        assert columns(conn) == FINAL_COLUMNS
        assert conn.execute(
            "SELECT key, value, is_secret, updated_by, description, description_edited FROM setting ORDER BY key"
        ).fetchall() == [
            ("log_level", "DEBUG", 0, "cli", None, 0),
            ("pgdb_dsn", None, 1, "cli", None, 0),
        ]
        assert conn.execute("SELECT count(*) FROM secret").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM secret_access_log").fetchone()[0] == 1

    def test_opening_it_again_changes_nothing(self, tmp_path: Path):
        path = tmp_path / "again" / "arc.store.db"
        conn = store.open_db(path)
        store._ensure_added_columns(conn)
        store._ensure_added_columns(conn)
        assert columns(conn) == FINAL_COLUMNS

    def test_a_second_process_adding_the_same_column_at_the_same_moment_is_harmless(
        self, tmp_path: Path
    ):
        path = tmp_path / "race" / "arc.store.db"
        path.parent.mkdir()
        old = sqlite3.connect(path)
        old.executescript(OLD_SCHEMA)
        old.execute(
            "ALTER TABLE setting ADD COLUMN description TEXT"
        )  # the other process got this far first
        old.commit()
        old.close()
        assert columns(store.open_db(path)) == FINAL_COLUMNS


class StaleConnection:
    """A connection whose reads of the `setting` table are out of date: what another process changed a moment
    ago is not in them. Writes and everything else go to the real connection, which is what makes the windows
    between a read and the write that follows it testable."""

    def __init__(self, real: sqlite3.Connection, stale_rows: dict[str, list[tuple]]):
        self._real, self._stale = real, stale_rows

    def execute(self, sql, *args):
        for marker, rows in self._stale.items():
            if marker in sql:
                return iter(rows)
        return self._real.execute(sql, *args)

    def __getattr__(self, name):
        return getattr(self._real, name)


class TestTheWindowsBetweenReadingAndWriting:
    def test_a_column_another_process_added_after_this_one_looked_is_not_an_error(
        self, tmp_path: Path
    ):
        conn = store.open_db(tmp_path / "window" / "arc.store.db")  # already has every column
        store._ensure_added_columns(
            StaleConnection(conn, {"PRAGMA table_info": []})
        )  # but this one saw none
        assert columns(conn) == FINAL_COLUMNS

    def test_an_edit_saved_between_the_copy_reading_and_writing_is_not_overwritten(
        self, mgr: SettingsManager
    ):
        mgr.declare("greeting", doc="old words")
        mgr.sync_descriptions()
        mgr.set_description(
            "greeting", "my words", by="a@example.com"
        )  # saved by an administrator just now
        stale = StaleConnection(
            mgr._conn,
            {"SELECT key, description, description_edited": [("greeting", "old words", 0)]},
        )
        assert (
            store.sync_descriptions(stale, {"greeting": "new words"}) == 1
        )  # it believed it had work to do
        assert row(mgr, "greeting")[2:4] == (
            "my words",
            1,
        )  # and the write itself refused to touch an edit


# --------------------------------------------------------------------------- the copy at boot
class TestCopyingTheCodesDescriptions:
    def test_a_declared_key_that_was_never_set_gets_a_row_with_its_description_and_no_value(
        self, mgr: SettingsManager
    ):
        mgr.declare("greeting", doc="Said when someone arrives.")
        assert mgr.sync_descriptions() == 1
        assert row(mgr, "greeting") == (
            None,
            0,
            "Said when someone arrives.",
            0,
            None,
            None,
            "declare",
        )

    def test_the_row_does_not_make_the_key_look_set(self, mgr: SettingsManager):
        mgr.declare("limit", type=int, default=5, doc="How many.")
        mgr.sync_descriptions()
        assert mgr.get("limit") == 5  # the declared default still applies
        assert mgr.delete("limit") is False  # nothing was ever set, so there is nothing to clear

    def test_a_second_copy_writes_nothing(self, mgr: SettingsManager):
        mgr.declare("greeting", doc="Said when someone arrives.")
        mgr.sync_descriptions()
        before = mgr._conn.total_changes
        assert mgr.sync_descriptions() == 0
        assert mgr._conn.total_changes == before

    def test_a_second_copy_runs_no_statement_that_could_write(self, mgr: SettingsManager):
        # total_changes cannot see an empty INSERT/UPDATE or a COMMIT, yet each of those takes the write lock
        # that every other process starting at the same moment is waiting for. Only the SQL itself shows them.
        mgr.declare("greeting", doc="Said when someone arrives.")
        mgr.sync_descriptions()
        statements: list[str] = []
        mgr._conn.set_trace_callback(statements.append)
        try:
            assert mgr.sync_descriptions() == 0
        finally:
            mgr._conn.set_trace_callback(None)
        assert statements, "the trace saw nothing, so this test would pass for any code"
        assert all(sql.lstrip().upper().startswith("SELECT") for sql in statements), statements

    def test_it_never_disturbs_the_read_caches(self, mgr: SettingsManager):
        mgr.declare("greeting", doc="Said when someone arrives.")
        epoch = store.write_epoch()
        mgr.sync_descriptions()
        mgr.set_description("greeting", "Changed by an administrator.", by="a@example.com")
        assert store.write_epoch() == epoch

    def test_a_better_description_in_the_code_reaches_a_key_nobody_edited(
        self, mgr: SettingsManager
    ):
        mgr.declare("greeting", doc="old words")
        mgr.sync_descriptions()
        mgr.declare("greeting", doc="new words")
        assert mgr.sync_descriptions() == 1
        assert row(mgr, "greeting")[2] == "new words"

    def test_but_not_one_an_administrator_has_edited(self, mgr: SettingsManager):
        mgr.declare("greeting", doc="old words")
        mgr.sync_descriptions()
        mgr.set_description("greeting", "my words", by="a@example.com")
        mgr.declare("greeting", doc="new words")
        assert mgr.sync_descriptions() == 0
        assert row(mgr, "greeting")[2] == "my words"

    def test_a_key_with_a_value_keeps_it(self, mgr: SettingsManager):
        mgr.set("greeting", "hello", updated_by="cli")
        mgr.declare("greeting", doc="Said when someone arrives.")
        mgr.sync_descriptions()
        assert mgr.get("greeting") == "hello"
        assert row(mgr, "greeting")[:3] == ("hello", 0, "Said when someone arrives.")

    def test_a_declared_secret_gets_its_description_too_and_stays_a_secret(
        self, mgr: SettingsManager
    ):
        mgr.declare("api_token", secret=True, doc="The token for the mail service.")
        mgr.sync_descriptions()
        value, is_secret, description, *_ = row(mgr, "api_token")
        assert (value, is_secret, description) == (None, 1, "The token for the mail service.")

    def test_a_key_declared_without_a_description_is_left_out(self, mgr: SettingsManager):
        mgr.declare("quiet")
        assert mgr.sync_descriptions() == 0
        assert row(mgr, "quiet") is None


# --------------------------------------------------------------------------- values and descriptions are separate
class TestValuesAndDescriptionsAreSeparate:
    def test_setting_or_changing_a_value_leaves_the_description_alone(self, mgr: SettingsManager):
        mgr.declare("greeting", doc="Said when someone arrives.")
        mgr.sync_descriptions()
        mgr.set_description("greeting", "My words.", by="a@example.com")
        mgr.set("greeting", "hello", updated_by="b@example.com")
        mgr.set("greeting", "hi", updated_by="b@example.com")
        assert row(mgr, "greeting")[:6] == (
            "hi",
            0,
            "My words.",
            1,
            "a@example.com",
            row(mgr, "greeting")[5],
        )

    def test_a_secret_value_change_leaves_the_description_alone(self, mgr: SettingsManager):
        mgr.declare("api_token", secret=True, doc="The token for the mail service.")
        mgr.sync_descriptions()
        mgr.set("api_token", "s3cret", secret=True, updated_by="a@example.com")
        assert row(mgr, "api_token")[2] == "The token for the mail service."

    def test_editing_a_description_leaves_who_set_the_value_alone(self, mgr: SettingsManager):
        mgr.declare("greeting", doc="Said when someone arrives.")
        mgr.set("greeting", "hello", updated_by="cli")
        mgr.sync_descriptions()
        mgr.set_description("greeting", "My words.", by="a@example.com")
        assert row(mgr, "greeting")[6] == "cli"
        assert mgr.get("greeting") == "hello"


# --------------------------------------------------------------------------- clearing a value
class TestClearingAValue:
    """`arc settings delete key` is how a value goes back to its default. It must not take the description with it."""

    def test_the_value_goes_the_description_stays(self, mgr: SettingsManager):
        mgr.declare("greeting", doc="Said when someone arrives.")
        mgr.sync_descriptions()
        mgr.set_description("greeting", "My words.", by="a@example.com")
        mgr.set("greeting", "hello")
        assert mgr.delete("greeting") is True
        assert mgr.get("greeting") is None
        assert row(mgr, "greeting")[:5] == (None, 0, "My words.", 1, "a@example.com")

    def test_clearing_it_twice_says_so(self, mgr: SettingsManager):
        mgr.declare("greeting", doc="Said when someone arrives.")
        mgr.sync_descriptions()
        mgr.set("greeting", "hello")
        assert (mgr.delete("greeting"), mgr.delete("greeting")) == (True, False)

    def test_a_secret_loses_its_value_but_stays_a_secret_with_its_description(
        self, mgr: SettingsManager
    ):
        mgr.declare("api_token", secret=True, doc="The token for the mail service.")
        mgr.sync_descriptions()
        mgr.set("api_token", "s3cret", secret=True)
        assert mgr.delete("api_token") is True
        assert (
            mgr._conn.execute("SELECT count(*) FROM secret WHERE key = 'api_token'").fetchone()[0]
            == 0
        )  # the ciphertext is gone
        value, is_secret, description, *_ = row(mgr, "api_token")
        assert (value, is_secret, description) == (None, 1, "The token for the mail service.")

    def test_a_key_with_no_description_is_removed_entirely_as_before(self, mgr: SettingsManager):
        mgr.set("adhoc", "x")
        assert mgr.delete("adhoc") is True
        assert row(mgr, "adhoc") is None

    def test_a_secret_with_no_description_is_removed_entirely_as_before(self, mgr: SettingsManager):
        mgr.set("adhoc_secret", "x", secret=True)
        assert mgr.delete("adhoc_secret") is True
        assert row(mgr, "adhoc_secret") is None
        assert (
            mgr._conn.execute("SELECT count(*) FROM secret WHERE key = 'adhoc_secret'").fetchone()[
                0
            ]
            == 0
        )

    def test_a_key_nobody_declared_keeps_a_description_an_administrator_wrote(
        self, mgr: SettingsManager
    ):
        mgr.set("adhoc", "x")
        mgr.set_description("adhoc", "Something we added by hand.", by="a@example.com")
        assert mgr.delete("adhoc") is True
        assert (
            row(mgr, "adhoc")[0] is None and row(mgr, "adhoc")[2] == "Something we added by hand."
        )


class TestResettingAKeyNobodyDeclared:
    """A hand-added key whose only reason to have a row is the description an administrator wrote: resetting that
    description is how it goes away. A key with a value, or any secret, keeps its row."""

    def test_the_empty_row_is_removed(self, mgr: SettingsManager):
        mgr.set("adhoc", "x")
        mgr.set_description("adhoc", "Something we added by hand.", by="a@example.com")
        mgr.delete("adhoc")
        mgr.reset_description("adhoc")
        assert row(mgr, "adhoc") is None
        assert "adhoc" not in mgr.list_all()

    def test_a_row_that_still_has_a_value_stays(self, mgr: SettingsManager):
        mgr.set("adhoc", "x")
        mgr.set_description("adhoc", "Something we added by hand.", by="a@example.com")
        mgr.reset_description("adhoc")
        assert row(mgr, "adhoc")[:4] == ("x", 0, None, 0)

    def test_a_secret_row_stays_so_that_it_is_not_left_unprotected(self, mgr: SettingsManager):
        mgr.set("adhoc_secret", "x", secret=True)
        mgr.set_description("adhoc_secret", "Something we added by hand.", by="a@example.com")
        mgr.delete("adhoc_secret")
        mgr.reset_description("adhoc_secret")
        assert row(mgr, "adhoc_secret")[:3] == (None, 1, None)


# --------------------------------------------------------------------------- a secret must stay a secret
class TestASecretStaysASecret:
    def test_a_key_that_was_a_plain_row_and_is_now_declared_secret_becomes_one(
        self, mgr: SettingsManager
    ):
        """An earlier release declared it plain and its description was copied into a row; a later release
        declares it a secret. Leaving the row plain would stop the key being redacted."""
        mgr.declare("api_token", doc="The token.")
        mgr.sync_descriptions()
        assert mgr.is_secret("api_token") is False
        mgr.declare("api_token", secret=True, doc="The token.")
        assert mgr.is_secret("api_token") is True

    def test_it_is_still_refused_when_a_plain_value_is_stored(self, mgr: SettingsManager):
        mgr.set("api_token", "visible")
        with pytest.raises(SettingsError):
            mgr.declare("api_token", secret=True)


# --------------------------------------------------------------------------- editing
class TestEditingADescription:
    def test_an_edit_is_kept_with_who_and_when(self, mgr: SettingsManager):
        mgr.declare("greeting", doc="Said when someone arrives.")
        mgr.sync_descriptions()
        mgr.set_description("greeting", "  My words.  ", by="a@example.com")
        description, edited, by, at = row(mgr, "greeting")[2:6]
        assert (description, edited, by) == ("My words.", 1, "a@example.com") and at

    def test_it_never_changes_the_code(self, mgr: SettingsManager):
        mgr.declare("greeting", doc="Said when someone arrives.")
        mgr.sync_descriptions()
        mgr.set_description("greeting", "My words.", by="a@example.com")
        assert mgr._declared_specs["greeting"].doc == "Said when someone arrives."
        assert mgr.list_all()["greeting"]["code_doc"] == "Said when someone arrives."

    def test_reset_brings_back_the_codes_text(self, mgr: SettingsManager):
        mgr.declare("greeting", doc="Said when someone arrives.")
        mgr.sync_descriptions()
        mgr.set_description("greeting", "My words.", by="a@example.com")
        mgr.reset_description("greeting")
        assert row(mgr, "greeting")[2:6] == ("Said when someone arrives.", 0, None, None)

    def test_a_secrets_description_can_be_edited_too(self, mgr: SettingsManager):
        mgr.declare("api_token", secret=True, doc="The token.")
        mgr.sync_descriptions()
        mgr.set_description("api_token", "Ask finance for it.", by="a@example.com")
        assert mgr.list_all()["api_token"]["doc"] == "Ask finance for it."
        assert mgr.is_secret("api_token") is True

    def test_a_key_that_is_declared_but_not_yet_copied_can_be_edited(self, mgr: SettingsManager):
        mgr.declare("greeting", doc="Said when someone arrives.")
        mgr.set_description("greeting", "My words.", by="a@example.com")
        assert mgr.list_all()["greeting"]["doc"] == "My words."

    @pytest.mark.parametrize("text", ["", "   ", "\n\t"])
    def test_an_empty_description_is_refused_with_a_hint_to_reset(
        self, mgr: SettingsManager, text: str
    ):
        mgr.declare("greeting", doc="Said when someone arrives.")
        with pytest.raises(SettingsError, match="reset"):
            mgr.set_description("greeting", text, by="a@example.com")

    def test_a_very_long_one_is_refused(self, mgr: SettingsManager):
        mgr.declare("greeting", doc="Said when someone arrives.")
        with pytest.raises(SettingsError, match="2000"):
            mgr.set_description("greeting", "x" * 2001, by="a@example.com")
        mgr.set_description("greeting", "x" * 2000, by="a@example.com")

    def test_a_key_that_does_not_exist_is_refused(self, mgr: SettingsManager):
        with pytest.raises(SettingsError, match="no setting or secret"):
            mgr.set_description("nope", "words", by="a@example.com")
        with pytest.raises(SettingsError, match="no setting or secret"):
            mgr.reset_description("nope")


# --------------------------------------------------------------------------- what the admin page reads
class TestListAll:
    def test_the_description_is_the_copy_in_the_store(self, mgr: SettingsManager):
        mgr.declare("greeting", doc="Said when someone arrives.")
        mgr.sync_descriptions()
        entry = mgr.list_all()["greeting"]
        assert (
            entry["doc"],
            entry["code_doc"],
            entry["doc_edited"],
            entry["doc_by"],
            entry["doc_at"],
        ) == (
            "Said when someone arrives.",
            "Said when someone arrives.",
            False,
            None,
            None,
        )

    def test_an_edit_shows_as_edited_with_who_and_when(self, mgr: SettingsManager):
        mgr.declare("greeting", doc="Said when someone arrives.")
        mgr.sync_descriptions()
        mgr.set_description("greeting", "My words.", by="a@example.com")
        entry = mgr.list_all()["greeting"]
        assert (entry["doc"], entry["code_doc"], entry["doc_edited"], entry["doc_by"]) == (
            "My words.",
            "Said when someone arrives.",
            True,
            "a@example.com",
        ) and entry["doc_at"]

    def test_before_the_first_copy_the_codes_text_is_shown(self, mgr: SettingsManager):
        mgr.declare("greeting", doc="Said when someone arrives.")
        assert mgr.list_all()["greeting"]["doc"] == "Said when someone arrives."

    def test_a_key_nobody_declared_and_nobody_described_has_an_empty_description(
        self, mgr: SettingsManager
    ):
        mgr.set("adhoc", "x")
        entry = mgr.list_all()["adhoc"]
        assert (entry["doc"], entry["code_doc"], entry["doc_edited"]) == ("", "", False)

    def test_the_value_of_a_secret_is_still_never_listed(self, mgr: SettingsManager):
        mgr.declare("api_token", secret=True, doc="The token.")
        mgr.sync_descriptions()
        mgr.set("api_token", "s3cret", secret=True)
        assert mgr.list_all()["api_token"]["value"] is None


# --------------------------------------------------------------------------- at boot
class TestAtBoot:
    def boot(self, project: Path):
        import arc

        from .conftest import FakeEntryPoint, write_lock

        write_lock(project, [])
        arc.boot(project_root=project, entry_points=[FakeEntryPoint("noop", lambda k: None)])
        return arc

    def test_every_declared_key_has_its_description_in_the_store_after_boot(self, project: Path):
        arc = self.boot(project)
        listed = arc.settings.list_all()
        conn = arc.settings._bound_manager()._conn
        stored = dict(conn.execute("SELECT key, description FROM setting"))
        assert listed and all(
            stored.get(key) == entry["doc"] for key, entry in listed.items() if entry["doc"]
        )
        assert stored["tracing_otlp_endpoint"]  # a kernel-declared key, as an example

    def test_a_second_boot_writes_nothing(self, project: Path):
        arc = self.boot(project)
        arc.shutdown()
        arc = self.boot(project)
        conn = arc.settings._bound_manager()._conn
        before = conn.total_changes
        assert arc.settings._bound_manager().sync_descriptions() == 0
        assert conn.total_changes == before

    def test_an_edit_survives_a_restart(self, project: Path):
        arc = self.boot(project)
        arc.settings._bound_manager().set_description(
            "tracing_otlp_endpoint", "Our collector lives at the office.", by="a@example.com"
        )
        arc.shutdown()
        arc = self.boot(project)
        entry = arc.settings.list_all()["tracing_otlp_endpoint"]
        assert (entry["doc"], entry["doc_edited"], entry["doc_by"]) == (
            "Our collector lives at the office.",
            True,
            "a@example.com",
        )
        assert entry["code_doc"] != entry["doc"]

    def test_a_store_that_cannot_be_written_does_not_stop_the_boot(
        self, project: Path, monkeypatch
    ):
        import arc
        from arc import store

        def refuse(conn, docs):
            raise sqlite3.OperationalError("attempt to write a readonly database")

        monkeypatch.setattr(store, "sync_descriptions", refuse)
        self.boot(project)  # must not raise
        assert arc.settings.list_all()["tracing_otlp_endpoint"][
            "doc"
        ]  # the code's own text is shown instead
