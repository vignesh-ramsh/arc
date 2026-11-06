"""The connection budget as the operator meets it: `arc deploy` plans it and stores the plan,
`arc run` hands it to the processes it starts, `arc status` and `arc doctor --deep` compare it with
what PostgreSQL allows today (arc/cli.py, arc/status.py, arc/sizing.py).

The arithmetic itself is in test_connection_budget.py; here PostgreSQL is faked at the two
functions that talk to it, so nothing connects to a database.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest
from typer.testing import CliRunner

from arc import cli, deploy, sizing, status
from arc.deploy import Scope, SplitSpec
from arc.settings import SettingsManager

from .conftest import write_lock

budget = pytest.importorskip("pgdb.budget")  # the database plugin owns how the server is asked

PLAN_KEY = "pgdb_connection_plan"
REAL_INSTALL_SPLIT = deploy.install_split  # the `env` fixture replaces deploy.install_split


class FakeServer:
    """What PostgreSQL would answer; `down` makes it unreachable."""

    def __init__(self, max_connections=100, reserved=3, role_limit=-1, db_limit=-1, in_use=6):
        self.limits = budget.ServerLimits(max_connections, reserved, role_limit, db_limit)
        self.in_use = in_use
        self.down = False

    def _answer(self):
        if self.down:
            raise OSError("connection refused")
        return self.limits

    async def fetch_limits_for_dsn(self, dsn, *, timeout=5.0):
        return self._answer()

    async def inspect_dsn(self, dsn, *, timeout=5.0):
        return self._answer(), self.in_use


@pytest.fixture
def server(monkeypatch) -> FakeServer:
    fake = FakeServer()
    monkeypatch.setattr(budget, "fetch_limits_for_dsn", fake.fetch_limits_for_dsn)
    monkeypatch.setattr(budget, "inspect_dsn", fake.inspect_dsn)
    return fake


@pytest.fixture
def env(project, monkeypatch, server):
    """A project with gateway + lineup (queues default, mail) and a database DSN."""
    monkeypatch.chdir(project)
    write_lock(project, [{"name": "gateway", "capability": "gateway"}, {"name": "lineup", "capability": "lineup"}])
    mgr = SettingsManager(project / ".arc")
    mgr.set("lineup_queues", "default,mail")
    mgr.set("pgdb_dsn", "postgresql://u:p@127.0.0.1/db", secret=True)
    monkeypatch.setattr(cli.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(cli, "_find_arc_bin", lambda: "/x/arc")
    installs: list[dict] = []
    monkeypatch.setattr(deploy, "install_split", lambda **kw: installs.append(kw) or [])
    return mgr, installs


def deploy_dev():
    return CliRunner().invoke(cli.app, ["deploy", "dev"])


def stored_plan(mgr: SettingsManager) -> sizing.ConnectionPlan | None:
    return sizing.ConnectionPlan.from_json(mgr.get(PLAN_KEY) or "")


class TestDeployPlansTheConnections:
    def test_dev_stores_a_plan_for_exactly_the_processes_it_writes(self, env):
        mgr, installs = env
        result = deploy_dev()
        assert result.exit_code == 0, result.output
        plan = stored_plan(mgr)
        assert plan.processes == {"gateway-worker": 2, "lineup-worker": 2, "lineup-scheduler": 1}
        assert plan.pools == {"gateway-worker": 10, "lineup-worker": 5, "lineup-scheduler": 2}
        assert (plan.usable, plan.reserve) == (97, 10)  # 100 - 3 held for superusers
        assert len(installs) == 1

    def test_the_operator_sees_the_numbers(self, env):
        out = deploy_dev().output
        assert "Database connections:" in out and "97 usable" in out and "10 kept free" in out
        assert "32 of 87" in out  # what the deployment can use, of what is available

    def test_a_small_server_shrinks_the_pools_together_but_everything_still_fits(self, env, server):
        mgr, _ = env
        server.limits = budget.ServerLimits(30, 3, -1, -1)  # 27 usable, 17 for the application
        assert deploy_dev().exit_code == 0
        plan = stored_plan(mgr)
        assert plan.total <= 17
        assert all(plan.pools[r] >= sizing.POOL_FLOORS[r] for r in plan.pools)
        assert plan.pools["gateway-worker"] < 10

    def test_the_role_and_database_limits_count_not_just_max_connections(self, env, server):
        mgr, _ = env
        server.limits = budget.ServerLimits(500, 3, 25, -1)  # ALTER ROLE ... CONNECTION LIMIT 25
        assert deploy_dev().exit_code == 0
        assert stored_plan(mgr).usable == 25

    def test_an_explicit_budget_replaces_what_the_server_reports(self, env, server):
        mgr, _ = env
        mgr.set("pgdb_connection_budget", "40")  # e.g. behind PgBouncer
        assert deploy_dev().exit_code == 0
        assert stored_plan(mgr).usable == 40 and "pgdb_connection_budget = 40" in deploy_dev().output

    def test_the_reserve_is_configurable(self, env):
        mgr, _ = env
        mgr.set("pgdb_connection_reserve", "20")
        assert deploy_dev().exit_code == 0
        assert (stored_plan(mgr).reserve, stored_plan(mgr).budget) == (20, 77)

    def test_the_operators_pool_for_a_role_is_kept_and_counted_first(self, env):
        mgr, _ = env
        mgr.set("pgdb_pool_max_size_lineup_worker", "3")
        assert deploy_dev().exit_code == 0
        assert stored_plan(mgr).pools["lineup-worker"] == 3

    def test_changing_a_worker_count_and_deploying_again_replans(self, env):
        mgr, _ = env
        assert deploy_dev().exit_code == 0
        mgr.set("lineup_workers_per_queue", "3")
        assert deploy_dev().exit_code == 0
        assert stored_plan(mgr).processes["lineup-worker"] == 6

    def test_processes_that_cannot_fit_stop_the_command_before_anything_is_written(self, env, server):
        mgr, installs = env
        server.limits = budget.ServerLimits(15, 3, -1, -1)  # 12 usable - 10 reserved = 2 connections
        result = deploy_dev()
        assert result.exit_code == 1 and isinstance(result.exception, SystemExit)  # a clean stop, not a crash
        assert "Connection budget" in result.output and "max_connections" in result.output
        assert installs == [] and mgr.get(PLAN_KEY) is None

    def test_an_unreachable_server_is_reported_but_does_not_block_the_deployment(self, env, server):
        mgr, installs = env
        server.down = True
        result = deploy_dev()
        assert result.exit_code == 0, result.output
        assert "could not read PostgreSQL's connection limits" in result.output
        assert len(installs) == 1 and mgr.get(PLAN_KEY) is None

    def test_a_project_without_a_database_gets_no_plan_and_loses_a_stale_one(self, env):
        mgr, installs = env
        mgr.set(PLAN_KEY, '{"stale": true}')
        mgr.delete("pgdb_dsn")
        assert deploy_dev().exit_code == 0
        assert mgr.get(PLAN_KEY) is None and len(installs) == 1


class TestRunHandsEachChildItsShare:
    PLAN = sizing.ConnectionPlan(
        pools={"gateway-worker": 7, "lineup-worker": 3, "lineup-scheduler": 2},
        processes={"gateway-worker": 8, "lineup-worker": 8, "lineup-scheduler": 1},
        usable=97,
        reserve=10,
    )

    @pytest.mark.parametrize(
        "supervisor_role, kind",
        [
            ("gateway", "gateway-worker"),
            ("lineup-worker-1", "lineup-worker"),
            ("lineup-worker-12", "lineup-worker"),
            ("lineup-scheduler", "lineup-scheduler"),
        ],
    )
    def test_each_child_is_told_what_kind_of_process_it_is(self, supervisor_role, kind):
        env = cli._child_environment({"PATH": "/bin"}, supervisor_role, self.PLAN)
        assert env["ARC_PROCESS_ROLE"] == kind and env["PATH"] == "/bin"

    def test_every_child_gets_the_plan_this_run_made(self):
        env = cli._child_environment({}, "gateway", self.PLAN)
        assert sizing.ConnectionPlan.from_json(env["ARC_CONNECTION_PLAN"]) == self.PLAN

    def test_without_a_plan_nothing_about_connections_is_set(self):
        env = cli._child_environment({"A": "1"}, "lineup-worker-1", None)
        assert "ARC_CONNECTION_PLAN" not in env and env["ARC_PROCESS_ROLE"] == "lineup-worker"

    def test_a_process_of_an_unknown_kind_gets_no_role(self):
        assert "ARC_PROCESS_ROLE" not in cli._child_environment({}, "something-else", self.PLAN)

    def test_the_callers_environment_is_not_modified(self):
        original = {"A": "1"}
        cli._child_environment(original, "gateway", self.PLAN)
        assert original == {"A": "1"}


ROOT = Path("/srv/shop")
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
PLAN_2_3_1 = sizing.ConnectionPlan(
    pools={"gateway-worker": 10, "lineup-worker": 5, "lineup-scheduler": 2},
    processes={"gateway-worker": 2, "lineup-worker": 3, "lineup-scheduler": 1},
    usable=97,
    reserve=10,
)


@pytest.fixture
def deployment(tmp_path, monkeypatch) -> deploy.Deployment:
    scope = Scope("user", tmp_path / "units", ("systemctl", "--user"), "default.target")
    monkeypatch.setattr(deploy, "_ctl", lambda *a: None)
    monkeypatch.setattr(deploy, "_ctl_try", lambda *a: True)
    REAL_INSTALL_SPLIT(base="arc-shop", spec=SPEC, scope=scope, enable=False)
    (found,) = deploy.read_deployments(ROOT, scopes=[scope])
    return found


class TestStatusComparesPlanAndServer:
    def test_the_expected_processes_come_out_of_the_unit_files(self, deployment):
        assert status.expected_processes(deployment) == {
            "gateway-worker": 2,
            "lineup-worker": 3,
            "lineup-scheduler": 1,
        }

    def test_a_matching_plan_is_no_problem_and_reports_what_is_open(self, env, deployment, server):
        mgr, _ = env
        mgr.set(PLAN_KEY, PLAN_2_3_1.to_json())
        connections, problems = status.gather_connections(mgr, status.expected_processes(deployment))
        assert problems == []
        assert (connections.usable, connections.in_use, connections.reserve) == (97, 6, 10)
        assert connections.plan == PLAN_2_3_1

    def test_a_plan_made_for_other_counts_is_reported_with_the_fix(self, env, deployment):
        mgr, _ = env
        mgr.set(PLAN_KEY, PLAN_2_3_1.to_json())
        _, problems = status.gather_connections(
            mgr, {"gateway-worker": 2, "lineup-worker": 6, "lineup-scheduler": 1}, apply_command="arc deploy dev"
        )
        (problem,) = problems
        assert "made for" in problem and "6 lineup-worker" in problem and "`arc deploy dev`" in problem

    def test_a_lowered_server_limit_is_reported(self, env, deployment, server):
        mgr, _ = env
        mgr.set(PLAN_KEY, PLAN_2_3_1.to_json())
        server.limits = budget.ServerLimits(30, 3, -1, -1)
        _, problems = status.gather_connections(mgr, status.expected_processes(deployment))
        (problem,) = problems
        assert "now allows 27" in problem and "shrinks its pool" in problem

    def test_no_plan_with_more_pools_than_the_server_has_is_reported(self, env, deployment, server):
        mgr, _ = env
        server.limits = budget.ServerLimits(30, 3, -1, -1)
        _, problems = status.gather_connections(mgr, {"gateway-worker": 8, "lineup-worker": 8, "lineup-scheduler": 1})
        (problem,) = problems
        assert "no connection plan" in problem and "170" in problem

    def test_an_unreachable_server_is_a_problem_but_the_rest_is_still_reported(self, env, deployment, server):
        mgr, _ = env
        mgr.set(PLAN_KEY, PLAN_2_3_1.to_json())
        server.down = True
        connections, problems = status.gather_connections(mgr, status.expected_processes(deployment))
        assert connections.usable is None and connections.plan == PLAN_2_3_1
        assert any("could not ask PostgreSQL" in p for p in problems)

    def test_nothing_to_report_without_a_database(self, env, deployment):
        mgr, _ = env
        mgr.delete("pgdb_dsn")
        assert status.gather_connections(mgr, status.expected_processes(deployment)) == (None, [])

    def test_the_connections_appear_in_the_json_report(self, env, deployment):
        mgr, _ = env
        mgr.set(PLAN_KEY, PLAN_2_3_1.to_json())
        connections, problems = status.gather_connections(mgr, status.expected_processes(deployment))
        rep = status.build_report(
            deployment, {}, Counter(), desired=None, desired_has_queue_list=False, sources={},
            log_writer_answers=True, restart_command=None,
        )
        rep.connections = connections
        data = json.loads(json.dumps(rep.to_dict()))["connections"]
        assert data["usable"] == 97 and data["in_use"] == 6
        assert data["plan"]["planned_total"] == 2 * 10 + 3 * 5 + 2 and data["plan"]["budget"] == 87


class TestConnectionProblems:
    """The pure check both `arc status` and `arc doctor --deep` use."""

    def counts(self, g=2, w=3, s=1):
        return {"gateway-worker": g, "lineup-worker": w, "lineup-scheduler": s}

    def test_nothing_is_running_so_nothing_is_wrong(self):
        assert sizing.connection_problems(None, {}, 5) == []
        assert sizing.connection_problems(None, self.counts(0, 0, 0), 5) == []

    def test_a_plan_that_matches_and_fits_is_fine(self):
        assert sizing.connection_problems(PLAN_2_3_1, self.counts(), 97) == []

    def test_a_plan_that_still_fits_a_smaller_server_is_fine(self):
        assert sizing.connection_problems(PLAN_2_3_1, self.counts(), 60) == []  # needs 37 + 10

    def test_no_plan_and_a_server_that_cannot_be_asked_says_nothing_it_does_not_know(self):
        assert sizing.connection_problems(None, self.counts(8, 8, 1), None) == []

    def test_a_small_deployment_without_a_plan_fits_the_defaults(self):
        assert sizing.connection_problems(None, self.counts(1, 1, 1), 97) == []

    def test_the_fix_named_is_the_one_the_caller_gave(self):
        (problem,) = sizing.connection_problems(None, self.counts(8, 8, 1), 40, apply_command="arc deploy prod")
        assert "`arc deploy prod`" in problem
