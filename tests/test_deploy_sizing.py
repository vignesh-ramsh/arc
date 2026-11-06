"""How many processes `arc deploy` runs (arc/sizing.py plan_workers, arc/cli.py _deployment_spec).

dev:  2 web workers, 1 worker per queue, 1 scheduler — small and predictable.
prod: sized from the machine — that many web workers, and the same number of job workers
      in total, shared out over the queues (at least one each).
Either way an explicit setting wins and is honoured as given.
"""

from __future__ import annotations

import pytest
import typer
from typer.testing import CliRunner

from arc import cli, deploy, sizing
from arc.settings import SettingsManager

from .conftest import write_lock

QUEUES = ["default", "mail", "critical", "low"]


class TestPlanWorkers:
    def test_dev_is_two_web_workers_and_one_worker_per_queue(self):
        plan = sizing.plan_workers(mode="dev", queues=QUEUES)
        assert plan.gateway == 2
        assert plan.per_queue == {"default": 1, "mail": 1, "critical": 1, "low": 1}

    def test_dev_ignores_the_size_of_the_machine(self, monkeypatch):
        monkeypatch.setattr(sizing, "detect_cpu_count", lambda: 64)
        monkeypatch.setattr(sizing, "detect_memory_gb", lambda: 256.0)
        assert sizing.plan_workers(mode="dev", queues=QUEUES).gateway == 2

    @pytest.fixture
    def machine_of_8(self, monkeypatch):
        monkeypatch.setattr(sizing, "calculate_worker_count", lambda **_: 8)

    def test_prod_web_workers_follow_the_machine(self, machine_of_8):
        assert sizing.plan_workers(mode="prod", queues=QUEUES).gateway == 8

    def test_prod_job_workers_are_shared_out_evenly_over_the_queues(self, machine_of_8):
        assert sizing.plan_workers(mode="prod", queues=QUEUES).per_queue == dict.fromkeys(QUEUES, 2)
        assert sizing.plan_workers(mode="prod", queues=["default", "mail"]).per_queue == {"default": 4, "mail": 4}

    def test_prod_gives_every_queue_at_least_one_worker(self, machine_of_8):
        many = [f"q{i}" for i in range(20)]
        assert set(sizing.plan_workers(mode="prod", queues=many).per_queue.values()) == {1}

    def test_prod_on_a_small_machine_is_still_valid(self, monkeypatch):
        monkeypatch.setattr(sizing, "detect_cpu_count", lambda: 1)
        monkeypatch.setattr(sizing, "detect_memory_gb", lambda: 1.0)
        plan = sizing.plan_workers(mode="prod", queues=QUEUES)
        assert plan.gateway >= 1 and all(n >= 1 for n in plan.per_queue.values())

    @pytest.mark.parametrize("mode", ["dev", "prod"])
    def test_an_explicit_web_worker_count_wins_and_has_no_ceiling(self, mode):
        assert sizing.plan_workers(mode=mode, queues=QUEUES, gateway=64).gateway == 64

    @pytest.mark.parametrize("mode", ["dev", "prod"])
    def test_an_explicit_per_queue_count_replaces_the_default_for_every_queue(self, mode):
        assert sizing.plan_workers(mode=mode, queues=QUEUES, per_queue=3).per_queue == dict.fromkeys(QUEUES, 3)

    @pytest.mark.parametrize("mode", ["dev", "prod"])
    def test_a_per_queue_override_beats_the_per_queue_default(self, mode):
        plan = sizing.plan_workers(mode=mode, queues=QUEUES, per_queue=3, queue_overrides={"mail": 9})
        assert plan.per_queue == {"default": 3, "mail": 9, "critical": 3, "low": 3}

    def test_an_override_for_a_queue_that_does_not_exist_is_ignored(self):
        plan = sizing.plan_workers(mode="dev", queues=["default"], queue_overrides={"ghost": 5})
        assert plan.per_queue == {"default": 1}

    def test_an_unknown_mode_is_an_error(self):
        with pytest.raises(ValueError):
            sizing.plan_workers(mode="staging", queues=QUEUES)


class TestDeploymentSpec:
    def lock(self, project, *, gateway=True, lineup=True):
        specs = []
        if gateway:
            specs.append({"name": "gateway", "capability": "gateway"})
        if lineup:
            specs.append({"name": "lineup", "capability": "lineup"})
        write_lock(project, specs)

    def spec(self, project, mode="dev"):
        return cli._deployment_spec(project, "/x/arc", "127.0.0.1", 8001, mode)

    def test_dev_from_a_configured_queue_list(self, project):
        self.lock(project)
        SettingsManager(project / ".arc").set("lineup_queues", "default,mail,critical,low")
        spec = self.spec(project)
        assert spec.gateway_workers == 2
        assert spec.queue_workers == dict.fromkeys(QUEUES, 1)
        assert spec.scheduler is True

    def test_the_queue_list_is_cleaned_up(self, project):
        self.lock(project)
        SettingsManager(project / ".arc").set("lineup_queues", " default , mail,,default ")
        assert list(self.spec(project).queue_workers) == ["default", "mail"]

    def test_prod_sizes_from_the_machine(self, project, monkeypatch):
        monkeypatch.setattr(sizing, "calculate_worker_count", lambda **_: 6)
        self.lock(project)
        SettingsManager(project / ".arc").set("lineup_queues", "default,mail,low")
        spec = self.spec(project, "prod")
        assert spec.gateway_workers == 6
        assert spec.queue_workers == dict.fromkeys(["default", "mail", "low"], 2)

    def test_the_operators_settings_win_in_either_mode(self, project):
        self.lock(project)
        mgr = SettingsManager(project / ".arc")
        mgr.set("lineup_queues", "default,mail")
        mgr.set("gateway_workers", "5")
        mgr.set("lineup_workers_per_queue", "3")
        mgr.set("lineup_workers_mail", "7")
        for mode in ("dev", "prod"):
            spec = self.spec(project, mode)
            assert spec.gateway_workers == 5
            assert spec.queue_workers == {"default": 3, "mail": 7}

    def test_without_the_gateway_plugin_there_is_no_gateway(self, project):
        self.lock(project, gateway=False)
        SettingsManager(project / ".arc").set("lineup_queues", "default")
        assert self.spec(project).gateway_workers is None

    def test_a_disabled_lineup_plugin_means_no_workers_and_no_scheduler(self, project):
        write_lock(project, [{"name": "gateway", "capability": "gateway"}, {"name": "lineup", "capability": "lineup", "enabled": False}])
        spec = self.spec(project)
        assert spec.queue_workers == {} and spec.scheduler is False

    @pytest.mark.parametrize("bad", ["0", "-2", "many", "1.5"])
    def test_a_nonsense_worker_count_stops_the_command(self, project, bad):
        self.lock(project)
        mgr = SettingsManager(project / ".arc")
        mgr.set("lineup_queues", "default")
        mgr.set("gateway_workers", bad)
        with pytest.raises(typer.Exit):
            self.spec(project)

    def test_queues_come_from_the_plugins_when_the_setting_is_unset(self, project, monkeypatch):
        import types

        import arc

        self.lock(project)
        lineup = types.SimpleNamespace(queues=lambda: ["default", "mail"])
        kernel = types.SimpleNamespace(get=lambda name: lineup)
        monkeypatch.setattr(arc, "boot", lambda **_: kernel)
        assert list(self.spec(project).queue_workers) == ["default", "mail"]

    def test_if_the_queues_cannot_be_worked_out_it_stops_instead_of_guessing(self, project, monkeypatch, capsys):
        """A queue missing from the units is a queue nobody consumes: silent data loss."""
        import arc

        self.lock(project)

        def broken(**_):
            raise arc.BootError("pgdb_dsn is not set")

        monkeypatch.setattr(arc, "boot", broken)
        with pytest.raises(typer.Exit):
            self.spec(project)
        assert "lineup_queues" in capsys.readouterr().err  # tells them how to name the queues themselves


class TestCommands:
    """`arc deploy dev` / `arc deploy prod` end to end, with systemd and nginx faked."""

    @pytest.fixture
    def env(self, project, monkeypatch):
        monkeypatch.chdir(project)
        write_lock(project, [{"name": "gateway", "capability": "gateway"}, {"name": "lineup", "capability": "lineup"}])
        SettingsManager(project / ".arc").set("lineup_queues", "default,mail")
        monkeypatch.setattr(cli.shutil, "which", lambda name: f"/usr/bin/{name}")
        monkeypatch.setattr(cli, "_find_arc_bin", lambda: "/x/arc")
        calls: list[tuple] = []
        monkeypatch.setattr(
            deploy, "install_split", lambda **kw: calls.append(("install", kw)) or []
        )
        monkeypatch.setattr(deploy, "enable_linger", lambda user: calls.append(("linger", user)))
        return project, calls

    def test_the_split_flag_is_gone(self, env):
        result = CliRunner().invoke(cli.app, ["deploy", "dev", "--split"])
        assert result.exit_code == 2 and "No such option" in result.output
        assert CliRunner().invoke(cli.app, ["deploy", "dev", "--single"]).exit_code == 2

    def test_dev_installs_per_process_user_units_without_enabling(self, env):
        project, calls = env
        result = CliRunner().invoke(cli.app, ["deploy", "dev"])
        assert result.exit_code == 0, result.output
        (_, kw), = calls
        assert kw["scope"].name == "user" and kw["enable"] is False
        assert kw["spec"].gateway_workers == 2 and kw["spec"].queue_workers == {"default": 1, "mail": 1}
        assert SettingsManager(project / ".arc").get("restart_command") == f"systemctl --user restart arc-{project.name}.target"

    def test_dev_enable_also_turns_on_lingering(self, env):
        _, calls = env
        assert CliRunner().invoke(cli.app, ["deploy", "dev", "--enable"]).exit_code == 0
        assert [c[0] for c in calls] == ["install", "linger"] and calls[0][1]["enable"] is True

    def test_dev_prints_what_it_decided_so_nobody_has_to_guess(self, env):
        out = CliRunner().invoke(cli.app, ["deploy", "dev"]).output
        assert "2 web worker" in out and "queue mail" in out and "scheduler" in out
        assert "lineup_workers_per_queue" in out and "run this command again" in out

    def test_prod_installs_system_units_last_as_the_project_owner_sized_from_the_machine(self, env, monkeypatch):
        project, calls = env
        order: list[str] = []
        monkeypatch.setattr(sizing, "calculate_worker_count", lambda **_: 8)
        monkeypatch.setattr(deploy, "require_root", lambda: None)
        monkeypatch.setattr(deploy, "project_owner", lambda root: ("shopuser", "shopgroup"))
        monkeypatch.setattr(deploy, "port_blocked_by_other", lambda p: False)
        monkeypatch.setattr(deploy, "resolve_domain", lambda d: "1.2.3.4")
        monkeypatch.setattr(deploy, "public_ip", lambda: "1.2.3.4")
        monkeypatch.setattr(deploy, "domain_already_configured_elsewhere", lambda d, own_site: None)
        monkeypatch.setattr(deploy, "ensure_nginx_and_certbot_installed", lambda need_certbot: None)
        monkeypatch.setattr(deploy, "install_nginx_site", lambda site, text: order.append("nginx") or "/etc/nginx/x")
        monkeypatch.setattr(deploy, "install_split", lambda **kw: order.append("units") or calls.append(("install", kw)) or [])

        result = CliRunner().invoke(cli.app, ["deploy", "prod", "--domain", "shop.example", "--no-ssl", "--yes"])

        assert result.exit_code == 0, result.output
        assert order == ["nginx", "units"]  # the app starts last, with the final settings
        (_, kw), = calls
        assert kw["scope"].name == "system" and kw["scope"].run_as == ("shopuser", "shopgroup")
        assert kw["enable"] is True  # production is always-on
        assert kw["spec"].gateway_workers == 8 and kw["spec"].queue_workers == {"default": 4, "mail": 4}
        assert SettingsManager(project / ".arc").get("restart_command") == f"sudo systemctl restart arc-{project.name}.target"
        assert SettingsManager(project / ".arc").get("gateway_trusted_proxies") == "127.0.0.1"

    def prod_env(self, monkeypatch, calls):
        """Everything `deploy prod` talks to outside this process, faked; returns the nginx texts written."""
        written: list[str] = []
        monkeypatch.setattr(deploy, "require_root", lambda: None)
        monkeypatch.setattr(deploy, "project_owner", lambda root: ("shopuser", "shopgroup"))
        monkeypatch.setattr(deploy, "port_blocked_by_other", lambda p: False)
        monkeypatch.setattr(deploy, "resolve_domain", lambda d: "1.2.3.4")
        monkeypatch.setattr(deploy, "public_ip", lambda: "1.2.3.4")
        monkeypatch.setattr(deploy, "domain_already_configured_elsewhere", lambda d, own_site: None)
        monkeypatch.setattr(deploy, "ensure_nginx_and_certbot_installed", lambda need_certbot: None)
        monkeypatch.setattr(deploy, "install_nginx_site", lambda site, text: written.append(text) or "/etc/nginx/x")
        monkeypatch.setattr(deploy, "install_split", lambda **kw: calls.append(("install", kw)) or [])
        return written

    def test_prod_writes_an_nginx_site_that_keeps_metrics_off_the_internet(self, env, monkeypatch):
        _, calls = env
        written = self.prod_env(monkeypatch, calls)
        result = CliRunner().invoke(cli.app, ["deploy", "prod", "--domain", "shop.example", "--no-ssl", "--yes"])
        assert result.exit_code == 0, result.output
        (text,) = written
        assert "location ~ ^/metrics(/|$)" in text and "allow 127.0.0.1;" in text and "deny all;" in text
        assert text.count("allow ") == 2  # this machine only, until the operator says otherwise

    def test_prod_lets_the_operator_name_a_scraper(self, env, monkeypatch):
        project, calls = env
        SettingsManager(project / ".arc").set("deploy_metrics_allow", "203.0.113.7, 10.0.0.0/24")
        written = self.prod_env(monkeypatch, calls)
        assert CliRunner().invoke(cli.app, ["deploy", "prod", "--domain", "shop.example", "--no-ssl", "--yes"]).exit_code == 0
        assert "allow 203.0.113.7/32;" in written[0] and "allow 10.0.0.0/24;" in written[0]

    def test_prod_with_a_bad_allow_list_stops_before_installing_anything(self, env, monkeypatch):
        project, calls = env
        SettingsManager(project / ".arc").set("deploy_metrics_allow", "1.2.3.4; allow all")
        written = self.prod_env(monkeypatch, calls)
        result = CliRunner().invoke(cli.app, ["deploy", "prod", "--domain", "shop.example", "--no-ssl", "--yes"])
        assert result.exit_code == 1 and "deploy_metrics_allow" in result.output
        assert written == [] and calls == []  # no nginx site, no units

    def test_prod_refuses_to_run_without_root(self, env, monkeypatch):
        _, calls = env
        def not_root():
            raise deploy.DeployError("must run as root")
        monkeypatch.setattr(deploy, "require_root", not_root)
        result = CliRunner().invoke(cli.app, ["deploy", "prod", "--domain", "shop.example", "--no-ssl", "--yes"])
        assert result.exit_code == 1 and calls == []
