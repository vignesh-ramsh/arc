"""`arc status` — expected (the unit files) vs running (systemd + the process registry) vs
what the settings would produce today (arc/status.py).

The point of the command: after `arc deploy`, one place that says how many web workers,
which queues and how many scheduler are running, and loudly where reality differs — a unit
that failed, a worker count that doesn't match, a setting changed but never applied, a
leftover `arc run` running workers a second time. Facts are injected, so every case is
tested without systemd; the live run against a real deployment is in the PR notes.
"""

from __future__ import annotations

import json
import subprocess
import types
from collections import Counter
from pathlib import Path

import pytest
from typer.testing import CliRunner

from arc import cli, deploy, events, sizing, status
from arc.deploy import Scope, SplitSpec
from arc.settings import SettingsManager
from arc.status import UnitState

from .conftest import write_lock

ROOT = Path("/srv/shop")
BASE = "arc-shop"
SPEC = SplitSpec(
    project_root=ROOT,
    arc_bin="/srv/shop/.venv/bin/arc",
    host="127.0.0.1",
    port=8001,
    log_socket=ROOT / ".arc/runtime/log.sock",
    gateway_workers=2,
    queue_workers={"default": 1, "mail": 2},
    scheduler=True,
)


@pytest.fixture
def user_deployment(tmp_path, monkeypatch) -> deploy.Deployment:
    """A real deployment: the units install_split writes, read back from disk."""
    scope = Scope("user", tmp_path / "units", ("systemctl", "--user"), "default.target")
    monkeypatch.setattr(deploy, "_ctl", lambda *a: None)
    monkeypatch.setattr(deploy, "_ctl_try", lambda *a: True)
    deploy.install_split(base=BASE, spec=SPEC, scope=scope, enable=False)
    (deployment,) = deploy.read_deployments(ROOT, scopes=[scope])
    return deployment


def healthy_states(deployment: deploy.Deployment, **overrides: UnitState) -> dict[str, UnitState]:
    states = {u.name: UnitState(u.name, "active", "running", 0, 1000 + i) for i, u in enumerate(deployment.units)}
    states[deployment.target] = UnitState(deployment.target, "active", "active")
    states.update(overrides)
    return states


HEALTHY_REGISTRY = Counter({"gateway-worker": 2, "lineup-worker": 3, "lineup-scheduler": 1})


def report(deployment, states=None, registry=None, *, desired=None, has_list=False, sources=None, logs_ok=True, restart="systemctl --user restart arc-shop.target"):
    return status.build_report(
        deployment,
        states if states is not None else healthy_states(deployment),
        registry if registry is not None else HEALTHY_REGISTRY,
        desired=desired,
        desired_has_queue_list=has_list,
        sources=sources or {},
        log_writer_answers=logs_ok,
        restart_command=restart,
    )


def row(rep, name):
    return next(r for r in rep.rows if r.name == name)


class TestReadingTheDeployment:
    def test_roles_queues_and_counts_come_back_out_of_the_unit_files(self, user_deployment):
        by_role = {}
        for u in user_deployment.units:
            by_role.setdefault(u.role, []).append(u)
        (gateway,) = by_role["gateway"]
        assert (gateway.workers, gateway.host, gateway.port) == (2, "127.0.0.1", 8001)
        assert sorted((u.queue for u in by_role["worker"])) == ["default", "mail", "mail"]
        (scheduler,) = by_role["scheduler"]
        assert scheduler.queues == ("default", "mail")
        assert len(by_role["logs"]) == 1
        assert user_deployment.base == BASE and user_deployment.target_installed

    def test_another_projects_units_are_not_ours(self, user_deployment, tmp_path, monkeypatch):
        other = SplitSpec(**{**SPEC.__dict__, "project_root": Path("/srv/blog")})
        deploy.install_split(base="arc-blog", spec=other, scope=user_deployment.scope, enable=False)
        (only,) = deploy.read_deployments(ROOT, scopes=[user_deployment.scope])
        assert only.base == BASE

    def test_nothing_deployed_gives_an_empty_list(self, tmp_path):
        scope = Scope("user", tmp_path / "none", ("systemctl", "--user"), "default.target")
        assert deploy.read_deployments(ROOT, scopes=[scope]) == []


class TestCounts:
    def test_a_healthy_deployment_matches_everywhere(self, user_deployment):
        rep = report(user_deployment)
        assert rep.ok and rep.problems == []
        assert [(r.name, r.expected, r.running) for r in rep.rows] == [
            ("gateway (web workers)", 2, 2),
            ("queue default", 1, 1),
            ("queue mail", 2, 2),
            ("scheduler", 1, 1),
            ("log writer", 1, 1),
        ]

    def test_fewer_web_workers_running_than_deployed(self, user_deployment):
        rep = report(user_deployment, registry=Counter({**HEALTHY_REGISTRY, "gateway-worker": 1}))
        assert not rep.ok and not row(rep, "gateway (web workers)").ok

    def test_more_web_workers_running_than_deployed_is_also_wrong(self, user_deployment):
        rep = report(user_deployment, registry=Counter({**HEALTHY_REGISTRY, "gateway-worker": 8}))
        assert not row(rep, "gateway (web workers)").ok

    def test_a_queue_worker_that_is_not_running_is_counted_per_queue(self, user_deployment):
        mail_unit = next(u.name for u in user_deployment.units if u.queue == "mail")
        rep = report(
            user_deployment,
            states=healthy_states(user_deployment, **{mail_unit: UnitState(mail_unit, "inactive", "dead")}),
            registry=Counter({**HEALTHY_REGISTRY, "lineup-worker": 2}),
        )
        assert (row(rep, "queue mail").expected, row(rep, "queue mail").running) == (2, 1)
        assert row(rep, "queue default").ok

    def test_an_active_unit_with_no_process_is_not_counted_as_running(self, user_deployment):
        """systemd can call a unit `active` (e.g. `active (exited)`) while no process exists."""
        worker = next(u.name for u in user_deployment.units if u.queue == "default")
        rep = report(
            user_deployment,
            states=healthy_states(user_deployment, **{worker: UnitState(worker, "active", "exited", 0, 0)}),
            registry=Counter({**HEALTHY_REGISTRY, "lineup-worker": 2}),
        )
        assert row(rep, "queue default").running == 0 and not rep.ok

    def test_a_missing_scheduler_is_flagged(self, user_deployment):
        rep = report(user_deployment, registry=Counter({**HEALTHY_REGISTRY, "lineup-scheduler": 0}))
        assert not row(rep, "scheduler").ok

    def test_a_log_writer_whose_socket_does_not_answer_is_not_running(self, user_deployment):
        rep = report(user_deployment, logs_ok=False)
        assert row(rep, "log writer").running == 0 and not rep.ok

    def test_restarts_are_summed_per_row_and_in_total(self, user_deployment):
        names = [u.name for u in user_deployment.units if u.queue == "mail"]
        states = healthy_states(user_deployment, **{n: UnitState(n, "active", "running", 3, 1) for n in names})
        rep = report(user_deployment, states=states)
        assert row(rep, "queue mail").restarts == 6
        assert rep.restarts_total == 6 and rep.ok  # restarts alone are information, not a fault


class TestSystemdProblems:
    def test_a_failed_unit_says_how_to_recover_in_the_right_scope(self, user_deployment):
        worker = next(u.name for u in user_deployment.units if u.queue == "default")
        rep = report(user_deployment, states=healthy_states(user_deployment, **{worker: UnitState(worker, "failed", "failed", 5, 0, "start-limit-hit")}))
        assert rep.units_failed == 1 and not rep.ok
        (problem,) = [p for p in rep.problems if "FAILED" in p]
        assert "start-limit-hit" in problem
        assert f"systemctl --user reset-failed {worker} && systemctl --user start {worker}" in problem

    def test_system_scope_recovery_commands_have_no_user_flag(self, user_deployment):
        system = deploy.Deployment(BASE, Scope("system", Path("/etc/systemd/system"), ("systemctl",), "multi-user.target"), user_deployment.units, True)
        worker = system.units[2].name
        rep = report(system, states=healthy_states(system, **{worker: UnitState(worker, "failed", "failed", 0, 0, "signal")}))
        assert "systemctl reset-failed" in rep.problems[0] and "--user" not in rep.problems[0]
        assert rep.mode == "prod" and rep.scope == "system"

    def test_a_unit_stuck_restarting_is_reported(self, user_deployment):
        worker = user_deployment.units[1].name
        rep = report(user_deployment, states=healthy_states(user_deployment, **{worker: UnitState(worker, "activating", "auto-restart")}))
        assert any("is restarting" in p for p in rep.problems)

    def test_an_inactive_target_means_nothing_starts_on_boot(self, user_deployment):
        target = user_deployment.target
        rep = report(user_deployment, states=healthy_states(user_deployment, **{target: UnitState(target, "inactive", "dead")}))
        assert any("is inactive" in p for p in rep.problems) and not rep.target_active

    def test_a_leftover_arc_run_running_workers_a_second_time(self, user_deployment):
        rep = report(user_deployment, registry=Counter({**HEALTHY_REGISTRY, "lineup-worker": 8}))
        (problem,) = rep.problems
        assert "8 lineup worker process" in problem and "3 worker unit" in problem and "arc run" in problem


class TestDriftBetweenSettingsAndUnits:
    def plan(self, **kw):
        return sizing.WorkerPlan(gateway=kw.get("gateway", 2), per_queue=kw.get("per_queue", {"default": 1, "mail": 2}))

    def test_no_drift_when_the_settings_describe_what_is_deployed(self, user_deployment):
        assert report(user_deployment, desired=self.plan()).ok

    def test_a_changed_web_worker_setting_that_was_never_applied(self, user_deployment):
        rep = report(user_deployment, desired=self.plan(gateway=4))
        assert any("settings give 4 web worker(s) but the units run 2" in p and "arc deploy dev" in p for p in rep.problems)

    def test_the_hint_names_prod_for_a_system_deployment(self, user_deployment):
        system = deploy.Deployment(BASE, Scope("system", Path("/etc/systemd/system"), ("systemctl",), "multi-user.target"), user_deployment.units, True)
        rep = report(system, desired=self.plan(gateway=4))
        assert "arc deploy prod" in rep.problems[0]

    def test_a_changed_per_queue_count(self, user_deployment):
        rep = report(user_deployment, desired=self.plan(per_queue={"default": 3, "mail": 2}))
        assert any("queue default: settings give 3 worker(s) but the units run 1" in p for p in rep.problems)

    def test_a_configured_queue_with_no_worker_unit(self, user_deployment):
        rep = report(user_deployment, desired=self.plan(per_queue={"default": 1, "mail": 2, "billing": 1}))
        assert any("queue billing is configured but has no worker unit" in p for p in rep.problems)

    def test_units_for_a_queue_no_longer_configured_are_only_flagged_when_the_list_is_explicit(self, user_deployment):
        desired = self.plan(per_queue={"default": 1})
        # No lineup_queues setting: the deployed queues ARE the queue list, so nothing is "removed".
        assert report(user_deployment, desired=desired, has_list=False).ok
        # With an explicit list, a queue that was dropped from it but still has units is drift.
        explicit = report(user_deployment, desired=desired, has_list=True)
        assert any("queue mail, which is no longer in lineup_queues" in p for p in explicit.problems)

    def test_queue_rows_follow_the_configured_order(self, user_deployment):
        rep = report(user_deployment, desired=self.plan(per_queue={"mail": 2, "default": 1}))
        assert [r.name for r in rep.rows if r.name.startswith("queue ")] == ["queue mail", "queue default"]


class TestSources:
    def mgr(self, project, **settings):
        m = SettingsManager(project / ".arc")
        for k, v in settings.items():
            m.set(k, v)
        return m

    def test_dev_defaults_are_labelled_as_such(self, project):
        plan, sources = status.desired_plan(self.mgr(project), "dev", ["default", "mail"])
        assert plan.gateway == 2 and sources["gateway"] == "dev default" and sources["queue mail"] == "dev default"

    def test_prod_defaults_say_they_come_from_the_machine(self, project, monkeypatch):
        monkeypatch.setattr(sizing, "calculate_worker_count", lambda **_: 8)
        plan, sources = status.desired_plan(self.mgr(project), "prod", ["default", "mail"])
        assert plan.gateway == 8 and sources["gateway"] == "sized from this machine"

    def test_explicit_settings_are_named(self, project):
        mgr = self.mgr(project, gateway_workers="5", lineup_workers_per_queue="3", lineup_workers_mail="9")
        plan, sources = status.desired_plan(mgr, "dev", ["default", "mail"])
        assert plan.gateway == 5 and plan.per_queue == {"default": 3, "mail": 9}
        assert sources == {"gateway": "setting gateway_workers", "queue default": "setting lineup_workers_per_queue", "queue mail": "setting lineup_workers_mail"}

    def test_the_configured_queue_list_wins_over_the_deployed_one(self, project):
        plan, _ = status.desired_plan(self.mgr(project, lineup_queues="a,b"), "dev", ["old"])
        assert list(plan.per_queue) == ["a", "b"]

    def test_a_nonsense_setting_is_ignored_here_not_a_crash(self, project):
        plan, sources = status.desired_plan(self.mgr(project, gateway_workers="lots"), "dev", ["default"])
        assert plan.gateway == 2 and sources["gateway"] == "dev default"


class TestReadingSystemd:
    SHOW = (
        "Id=arc-shop-gateway.service\nActiveState=active\nSubState=running\nNRestarts=2\nMainPID=4242\nResult=success\n\n"
        "Id=arc-shop-worker-mail-1.service\nActiveState=failed\nSubState=failed\nNRestarts=5\nMainPID=0\nResult=start-limit-hit\n"
    )

    def run_with(self, monkeypatch, *, stdout="", returncode=0, error=None):
        def fake(cmd, **kw):
            if error:
                raise error
            return subprocess.CompletedProcess(cmd, returncode, stdout, "")
        monkeypatch.setattr(subprocess, "run", fake)

    def test_blocks_are_parsed_into_states(self, monkeypatch):
        self.run_with(monkeypatch, stdout=self.SHOW)
        states = status.read_unit_states(deploy.user_scope(), ["a", "b"])
        gw = states["arc-shop-gateway.service"]
        assert (gw.active_state, gw.restarts, gw.main_pid, gw.running) == ("active", 2, 4242, True)
        mail = states["arc-shop-worker-mail-1.service"]
        assert (mail.active_state, mail.result, mail.running) == ("failed", "start-limit-hit", False)

    @pytest.mark.parametrize("kw", [{"returncode": 1}, {"error": FileNotFoundError()}, {"error": subprocess.TimeoutExpired("systemctl", 10)}])
    def test_if_systemctl_cannot_be_asked_the_answer_is_none_not_a_crash(self, monkeypatch, kw):
        self.run_with(monkeypatch, **kw)
        assert status.read_unit_states(deploy.user_scope(), ["a"]) is None


class TestTheCommand:
    @pytest.fixture
    def env(self, project, monkeypatch, user_deployment):
        monkeypatch.chdir(project)
        write_lock(project, [{"name": "gateway", "capability": "gateway"}, {"name": "lineup", "capability": "lineup"}])
        mgr = SettingsManager(project / ".arc")
        mgr.set("lineup_queues", "default,mail")
        mgr.set("lineup_workers_mail", "2")  # the fixture deploys mail with 2 workers; the settings must say so too
        mgr.set("restart_command", "systemctl --user restart arc-shop.target")
        state = types.SimpleNamespace(states=healthy_states(user_deployment), registry=HEALTHY_REGISTRY, logs=True)
        monkeypatch.setattr(deploy, "read_deployments", lambda root, scopes=None: [user_deployment])
        monkeypatch.setattr(status, "read_unit_states", lambda scope, names: state.states)
        monkeypatch.setattr(events, "list_processes", lambda root: [{"role": r, "pid": i} for r, n in state.registry.items() for i in range(n)])
        monkeypatch.setattr(status, "_log_writer_answers", lambda root: state.logs)
        return state

    def invoke(self, *args):
        return CliRunner().invoke(cli.app, ["status", *args])

    def test_healthy_exits_zero_and_says_so(self, env):
        result = self.invoke()
        assert result.exit_code == 0, result.output
        for text in ("gateway (web workers)", "queue mail", "scheduler", "log writer", "Everything matches."):
            assert text in result.output

    def test_a_problem_exits_one_so_monitoring_can_use_it(self, env):
        env.registry = Counter({**HEALTHY_REGISTRY, "gateway-worker": 1})
        result = self.invoke()
        assert result.exit_code == 1 and "Everything matches." not in result.output

    def test_json_is_valid_and_carries_the_numbers(self, env):
        result = self.invoke("--json")
        data = json.loads(result.output)
        assert result.exit_code == 0 and data["ok"] is True
        gateway = next(p for p in data["processes"] if p["name"].startswith("gateway"))
        assert (gateway["expected"], gateway["running"]) == (2, 2)
        assert data["deployment"]["base"] == BASE and data["units"]["failed"] == 0

    def test_json_exit_code_also_reflects_problems(self, env):
        env.registry = Counter()
        result = self.invoke("--json")
        assert result.exit_code == 1 and json.loads(result.output)["ok"] is False

    def test_a_setting_changed_but_never_applied_is_caught_end_to_end(self, env, project):
        SettingsManager(project / ".arc").set("gateway_workers", "4")
        result = self.invoke()
        assert result.exit_code == 1
        assert "settings give 4 web worker(s) but the units run 2" in result.output
        assert "gateway_workers" in result.output  # and the table says what configures it (the cell may wrap)

    def test_systemd_being_unreachable_is_reported_and_fails(self, env, monkeypatch):
        monkeypatch.setattr(status, "read_unit_states", lambda scope, names: None)
        result = self.invoke()
        assert result.exit_code == 1 and "could not ask systemd" in result.output

    def test_a_project_not_deployed_this_way_is_not_an_error(self, env, monkeypatch):
        monkeypatch.setattr(deploy, "read_deployments", lambda root, scopes=None: [])
        result = self.invoke()
        assert result.exit_code == 0
        assert "isn't deployed with `arc deploy`" in result.output and "gateway-worker × 2" in result.output

    def test_nothing_deployed_and_nothing_running(self, env, monkeypatch):
        monkeypatch.setattr(deploy, "read_deployments", lambda root, scopes=None: [])
        env.registry = Counter()
        assert "Nothing is running" in self.invoke().output

    def test_outside_a_project_it_says_so(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("ARC_PROJECT_ROOT", raising=False)
        result = self.invoke()
        assert result.exit_code == 1 and "Not an ARC project" in result.output
