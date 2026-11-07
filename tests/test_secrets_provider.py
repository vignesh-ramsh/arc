"""Where secrets live: `[secrets].provider` in .arc/arc.toml (arc/settings.py).

* An unknown provider used to be ignored: the secrets stayed in the local file while the operator
  believed they were somewhere else. It is refused now - at boot, in `arc doctor`, and by every
  command that touches a secret.
* The `env` provider reads each secret from `ARC_SECRET_<KEY>` and stores nothing: the 12-factor
  shape, for platforms that inject secrets (containers, systemd EnvironmentFile, CI).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import tomlkit
from typer.testing import CliRunner

import arc
from arc import cli, runtime
from arc.settings import REDACTED, SettingsError, SettingsManager, env_secret_name, read_secrets_provider


def set_provider(project: Path, name: str | None) -> None:
    path = project / ".arc" / "arc.toml"
    doc = tomlkit.parse(path.read_text())
    table = doc.setdefault("secrets", tomlkit.table())
    if name is None:
        table.pop("provider", None)
    else:
        table["provider"] = name
    path.write_text(tomlkit.dumps(doc))


@pytest.fixture
def mgr(project) -> SettingsManager:
    return SettingsManager(project / ".arc")


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch):
    import os

    for name in [n for n in os.environ if n.startswith("ARC_SECRET_")]:
        monkeypatch.delenv(name)


class TestKnownProviders:
    def test_local_file_is_the_default(self, project, mgr):
        set_provider(project, None)
        assert mgr.secrets_provider() == "local_file" and read_secrets_provider(project / ".arc" / "arc.toml") == "local_file"

    @pytest.mark.parametrize("name", ["local_file", "env"])
    def test_the_providers_arc_has(self, project, mgr, name):
        set_provider(project, name)
        assert mgr.secrets_provider() == name

    @pytest.mark.parametrize("name", ["keyring", "aws_kms", "vault", "LOCAL_FILE", "", "env "])
    def test_anything_else_is_refused_and_says_what_exists(self, project, mgr, name):
        set_provider(project, name)
        with pytest.raises(SettingsError) as caught:
            mgr.secrets_provider()
        text = str(caught.value)
        assert repr(name) in text and "local_file, env" in text and "arc.toml" in text
        with pytest.raises(SettingsError):
            read_secrets_provider(project / ".arc" / "arc.toml")

    def test_a_secret_cannot_be_stored_or_read_under_an_unknown_provider(self, project, mgr):
        mgr.declare("api_token", secret=True)
        set_provider(project, "keyring")
        with pytest.raises(SettingsError):
            mgr.set("api_token", "x", secret=True)
        with pytest.raises(SettingsError):
            mgr.get("api_token", reveal=True)
        assert not mgr._conn.execute("SELECT 1 FROM secret").fetchone()  # nothing slipped into the local file

    def test_plain_settings_do_not_care_which_provider_is_configured(self, project, mgr):
        set_provider(project, "keyring")
        mgr.set("some_plain_key", "v")
        assert mgr.get("some_plain_key") == "v"

    def test_boot_refuses_to_start(self, project):
        set_provider(project, "keyring")
        with pytest.raises(runtime.BootError, match="keyring"):
            arc.boot(project_root=project, entry_points=())

    def test_boot_still_starts_with_each_real_provider(self, project):
        for name in ("local_file", "env"):
            set_provider(project, name)
            arc.boot(project_root=project, entry_points=(), force=True)
            arc.runtime.shutdown()


class TestDoctorChecksIt:
    def doctor(self, *args):
        return CliRunner().invoke(cli.app, ["doctor", *args])

    def test_a_dry_run_that_says_ok_must_not_hide_a_boot_failure(self, project, monkeypatch):
        monkeypatch.chdir(project)
        set_provider(project, "keyring")
        result = self.doctor()
        assert result.exit_code == 1
        assert "Boot would FAIL" in result.output and "keyring" in " ".join(result.output.split())
        assert "Boot resolution OK" not in result.output

    def test_json_says_so_too(self, project, monkeypatch):
        monkeypatch.chdir(project)
        set_provider(project, "keyring")
        result = self.doctor("--json")
        assert result.exit_code == 1 and json.loads(result.output)["ok"] is False

    def test_it_opens_no_store_to_find_out(self, tmp_path, project, monkeypatch):
        monkeypatch.chdir(project)
        for leftover in (project / ".arc").glob("arc.store.db*"):
            leftover.unlink()
        set_provider(project, "keyring")
        self.doctor()
        assert not (project / ".arc" / "arc.store.db").exists()

    def test_the_real_providers_pass(self, project, monkeypatch):
        monkeypatch.chdir(project)
        for name in ("local_file", "env"):
            set_provider(project, name)
            assert self.doctor().exit_code == 0


class TestEnvironmentProvider:
    @pytest.fixture
    def env_mgr(self, project, mgr):
        set_provider(project, "env")
        mgr.declare("api_token", secret=True, doc="a token")
        return mgr

    def test_the_variable_name_is_derived_from_the_key(self):
        assert env_secret_name("pgdb_dsn") == "ARC_SECRET_PGDB_DSN"
        assert env_secret_name("my-odd.key_1") == "ARC_SECRET_MY_ODD_KEY_1"

    def test_a_secret_is_read_from_its_variable(self, env_mgr, monkeypatch):
        monkeypatch.setenv("ARC_SECRET_API_TOKEN", "s3cret-from-env")
        assert env_mgr.get("api_token", reveal=True) == "s3cret-from-env"

    def test_a_masked_read_shows_that_it_is_set_but_not_what(self, env_mgr, monkeypatch):
        monkeypatch.setenv("ARC_SECRET_API_TOKEN", "s3cret-from-env")
        assert env_mgr.get("api_token") == REDACTED

    def test_an_unset_secret_is_none_or_its_declared_default(self, project, mgr, monkeypatch):
        set_provider(project, "env")
        mgr.declare("no_default", secret=True)
        mgr.declare("with_default", secret=True, default="dev-placeholder")
        assert mgr.get("no_default", reveal=True) is None and mgr.get("no_default") is None
        assert mgr.get("with_default", reveal=True) == "dev-placeholder"

    def test_an_empty_variable_counts_as_unset(self, env_mgr, monkeypatch):
        monkeypatch.setenv("ARC_SECRET_API_TOKEN", "")
        assert env_mgr.get("api_token", reveal=True) is None and env_mgr.get("api_token") is None

    def test_typed_secrets_are_coerced_like_any_setting(self, project, mgr, monkeypatch):
        set_provider(project, "env")
        mgr.declare("port_secret", secret=True, type=int, default=1)
        monkeypatch.setenv("ARC_SECRET_PORT_SECRET", "8443")
        assert mgr.get("port_secret", reveal=True) == 8443
        monkeypatch.setenv("ARC_SECRET_PORT_SECRET", "not-a-number")
        with pytest.raises(SettingsError, match="port_secret"):
            mgr.get("port_secret", reveal=True)

    def test_it_cannot_store_a_secret_and_says_which_variable_to_set(self, env_mgr):
        with pytest.raises(SettingsError) as caught:
            env_mgr.set("api_token", "x", secret=True)
        assert "ARC_SECRET_API_TOKEN" in str(caught.value) and "'env'" in str(caught.value)
        assert env_mgr._conn.execute("SELECT 1 FROM secret").fetchone() is None

    def test_the_command_line_reports_it_instead_of_crashing(self, env_mgr, project, monkeypatch):
        monkeypatch.chdir(project)
        result = CliRunner().invoke(cli.app, ["settings", "set", "api_token", "x", "--secret"])
        assert result.exit_code == 1 and "ARC_SECRET_API_TOKEN" in " ".join(result.output.split())

    def test_plain_settings_are_unaffected(self, env_mgr):
        env_mgr.set("plain_key", "plain value")
        assert env_mgr.get("plain_key") == "plain value"

    def test_a_value_in_the_local_file_is_ignored_until_the_provider_is_switched_back(self, project, mgr, monkeypatch):
        mgr.declare("api_token", secret=True)
        mgr.set("api_token", "stored-locally", secret=True)
        assert mgr.get("api_token", reveal=True) == "stored-locally"
        set_provider(project, "env")
        assert mgr.get("api_token", reveal=True) is None  # not silently mixed in
        monkeypatch.setenv("ARC_SECRET_API_TOKEN", "from-env")
        assert mgr.get("api_token", reveal=True) == "from-env"
        set_provider(project, "local_file")
        monkeypatch.delenv("ARC_SECRET_API_TOKEN")
        assert mgr.get("api_token", reveal=True) == "stored-locally"

    def test_secrets_left_on_a_placeholder_are_reported_from_the_environment_too(self, project, mgr, monkeypatch):
        set_provider(project, "env")
        mgr.declare("with_default", secret=True, default="dev-placeholder")
        assert mgr.secrets_on_default() == ["with_default"]
        monkeypatch.setenv("ARC_SECRET_WITH_DEFAULT", "real")
        assert mgr.secrets_on_default() == []

    def test_the_values_to_scrub_from_error_text_include_the_environment_ones(self, env_mgr, monkeypatch):
        monkeypatch.setenv("ARC_SECRET_API_TOKEN", "tok-123")
        monkeypatch.setenv("ARC_SECRET_OTHER", "other-456")
        monkeypatch.setenv("ARC_SECRET_EMPTY", "")
        monkeypatch.setenv("NOT_A_SECRET", "visible")
        values = env_mgr.secret_values_for_redaction()
        assert "tok-123" in values and "other-456" in values
        assert "" not in values and "visible" not in values

    def test_the_values_stored_locally_are_still_scrubbed_after_switching(self, project, mgr):
        mgr.declare("api_token", secret=True)
        mgr.set("api_token", "old-local-value", secret=True)
        set_provider(project, "env")
        assert "old-local-value" in mgr.secret_values_for_redaction()

    def test_showing_an_environment_secret_to_a_person_can_be_recorded(self, env_mgr, monkeypatch):
        with pytest.raises(SettingsError, match="no stored secret value"):
            env_mgr.record_secret_access("api_token", "admin@example.com")
        monkeypatch.setenv("ARC_SECRET_API_TOKEN", "tok")
        env_mgr.record_secret_access("api_token", "admin@example.com")
        assert env_mgr.access_log("api_token")[0]["accessed_by"] == "admin@example.com"

    def test_the_listing_still_calls_it_a_secret_and_never_shows_a_value(self, env_mgr, monkeypatch):
        monkeypatch.setenv("ARC_SECRET_API_TOKEN", "tok-123")
        entry = env_mgr.list_all()["api_token"]
        assert entry["kind"] == "secret" and entry["value"] is None and "tok-123" not in str(entry)

    def test_a_missing_environment_secret_does_not_stop_boot_by_itself(self, project, monkeypatch):
        set_provider(project, "env")
        arc.boot(project_root=project, entry_points=(), force=True)  # whoever needs it complains when it reads it
        arc.runtime.shutdown()
