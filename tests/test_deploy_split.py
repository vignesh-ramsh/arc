"""`arc deploy dev` / `arc deploy prod` — one systemd unit per process (arc/deploy.py).

Why: with a single `arc run` unit, a crash of ANY process meant a full restart of
everything (measured: kill -9 one lineup worker, site down for the whole cold start,
~5.5 s). With one unit each, systemd restarts exactly the process that died, in its own
cgroup, and a crash loop ends in a `failed` unit rather than spinning. These tests pin
the generated units and the install behaviour against a fake systemctl; the user-scope
path was exercised against a real user-systemd manager by hand, the system scope (which
needs root) only here and with `systemd-analyze verify`.
"""

from __future__ import annotations

import configparser
from pathlib import Path

import pytest

from arc import deploy
from arc.deploy import Scope, SplitSpec

SPEC = SplitSpec(
    project_root=Path("/srv/shop"),
    arc_bin="/srv/shop/.venv/bin/arc",
    host="127.0.0.1",
    port=8001,
    log_socket=Path("/srv/shop/.arc/runtime/log.sock"),
    gateway_workers=2,
    queue_workers={"default": 1, "mail": 2},
    scheduler=True,
)
BASE = "arc-shop"
USER = deploy.user_scope()
SYSTEM = deploy.system_scope("shopuser", "shopgroup")


def parse(text: str) -> configparser.ConfigParser:
    cp = configparser.ConfigParser(strict=False, interpolation=None)
    cp.optionxform = str  # keep directive case
    cp.read_string(text)
    return cp


def with_(**changes) -> SplitSpec:
    return SplitSpec(**{**SPEC.__dict__, **changes})


class TestGeneratedUnits:
    def test_one_unit_per_process_one_worker_set_per_queue_plus_a_target(self):
        assert deploy.split_unit_names(BASE, SPEC) == [
            "arc-shop-logs.service",
            "arc-shop-gateway.service",
            "arc-shop-worker-default-1.service",
            "arc-shop-worker-mail-1.service",
            "arc-shop-worker-mail-2.service",
            "arc-shop-scheduler.service",
            "arc-shop.target",
        ]

    def test_every_service_restarts_itself_and_stops_after_a_crash_loop(self):
        for unit, text in deploy.split_unit_texts(BASE, SPEC).items():
            if not unit.endswith(".service"):
                continue
            cp = parse(text)
            assert cp["Service"]["Restart"] == "always", unit
            assert cp["Service"]["RestartSec"] == "3", unit
            # 5 starts in 120 s, then the unit stays `failed` instead of spinning
            assert cp["Unit"]["StartLimitBurst"] == "5", unit
            assert cp["Unit"]["StartLimitIntervalSec"] == "120", unit

    def test_every_service_belongs_to_the_target_so_one_restart_covers_all(self):
        for unit, text in deploy.split_unit_texts(BASE, SPEC).items():
            if unit.endswith(".service"):
                assert parse(text)["Unit"]["PartOf"] == "arc-shop.target", unit

    def test_the_target_wants_every_service_and_is_what_gets_enabled(self):
        target = parse(deploy.split_unit_texts(BASE, SPEC)["arc-shop.target"])
        assert set(target["Unit"]["Wants"].split()) == {
            u for u in deploy.split_unit_names(BASE, SPEC) if u.endswith(".service")
        }
        assert target["Install"]["WantedBy"] == "default.target"

    def test_each_queue_gets_workers_that_consume_only_that_queue(self):
        texts = deploy.split_unit_texts(BASE, SPEC)
        exec_of = lambda unit: parse(texts[unit])["Service"]["ExecStart"]  # noqa: E731
        assert exec_of("arc-shop-worker-default-1.service") == "/srv/shop/.venv/bin/arc lineup worker --queues default"
        assert exec_of("arc-shop-worker-mail-1.service") == "/srv/shop/.venv/bin/arc lineup worker --queues mail"
        assert exec_of("arc-shop-worker-mail-2.service") == "/srv/shop/.venv/bin/arc lineup worker --queues mail"

    def test_the_gateway_and_log_writer_commands(self):
        texts = deploy.split_unit_texts(BASE, SPEC)
        assert parse(texts["arc-shop-logs.service"])["Service"]["ExecStart"] == "/srv/shop/.venv/bin/arc logs serve"
        assert parse(texts["arc-shop-gateway.service"])["Service"]["ExecStart"] == (
            "/srv/shop/.venv/bin/arc gateway serve --host 127.0.0.1 --port 8001 --workers 2"
        )

    def test_there_is_exactly_one_scheduler_and_it_polls_every_queue(self):
        names = deploy.split_unit_names(BASE, with_(queue_workers={"a": 5, "b": 5}))
        assert [n for n in names if "scheduler" in n] == ["arc-shop-scheduler.service"]
        texts = deploy.split_unit_texts(BASE, SPEC)
        assert parse(texts["arc-shop-scheduler.service"])["Service"]["ExecStart"].endswith(
            "lineup scheduler --queues default,mail"
        )

    def test_every_process_is_pointed_at_the_log_writer(self):
        # Raw lines, not configparser: systemd accumulates repeated Environment= lines, configparser keeps the last.
        for unit, text in deploy.split_unit_texts(BASE, SPEC).items():
            if unit.endswith(".service"):
                assert "\nEnvironment=ARC_LOG_SOCKET=/srv/shop/.arc/runtime/log.sock\n" in text, unit

    def test_each_process_is_told_which_kind_it_is_so_it_gets_its_share_of_the_connections(self):
        texts = deploy.split_unit_texts(BASE, SPEC)
        role_of = lambda unit: next(  # noqa: E731
            (line.split("=", 2)[2] for line in texts[unit].splitlines() if line.startswith("Environment=ARC_PROCESS_ROLE=")),
            None,
        )
        assert role_of("arc-shop-gateway.service") == "gateway-worker"
        assert role_of("arc-shop-worker-default-1.service") == "lineup-worker"
        assert role_of("arc-shop-worker-mail-2.service") == "lineup-worker"
        assert role_of("arc-shop-scheduler.service") == "lineup-scheduler"
        assert role_of("arc-shop-logs.service") is None  # the log writer never touches the database

    def test_the_role_names_are_the_ones_arc_ps_and_the_connection_plan_use(self):
        from arc import sizing

        assert {sizing.ROLE_GATEWAY, sizing.ROLE_LINEUP_WORKER, sizing.ROLE_LINEUP_SCHEDULER} == {
            "gateway-worker", "lineup-worker", "lineup-scheduler"
        }

    def test_the_log_writer_is_wanted_not_required_so_it_cannot_take_a_process_down(self):
        texts = deploy.split_unit_texts(BASE, SPEC)
        for unit, text in texts.items():
            if not unit.endswith(".service") or unit.endswith("-logs.service"):
                continue
            cp = parse(text)
            assert cp["Unit"]["Wants"] == "arc-shop-logs.service", unit
            assert "arc-shop-logs.service" in cp["Unit"]["After"], unit
        assert "Wants" not in parse(texts["arc-shop-logs.service"])["Unit"]

    def test_no_unit_requires_or_binds_to_a_sibling(self):
        # That is the isolation: no Requires=/BindsTo=/PropagatesStopTo= between services.
        for unit, text in deploy.split_unit_texts(BASE, SPEC).items():
            for forbidden in ("Requires=", "BindsTo=", "PropagatesStopTo="):
                assert forbidden not in text, (unit, forbidden)

    def test_no_gateway_unit_when_the_gateway_plugin_is_not_installed(self):
        assert "arc-shop-gateway.service" not in deploy.split_unit_names(BASE, with_(gateway_workers=None))

    def test_no_lineup_units_when_lineup_is_not_installed(self):
        names = deploy.split_unit_names(BASE, with_(queue_workers={}, scheduler=False))
        assert names == ["arc-shop-logs.service", "arc-shop-gateway.service", "arc-shop.target"]

    def test_queue_names_that_are_not_valid_in_a_unit_name_are_made_safe(self):
        names = deploy.split_unit_names(BASE, with_(queue_workers={"high/priority": 1}))
        assert "arc-shop-worker-high_priority-1.service" in names

    def test_two_queues_that_would_share_a_unit_name_are_refused(self):
        with pytest.raises(deploy.DeployError, match="share unit names"):
            deploy.split_unit_texts(BASE, with_(queue_workers={"a/b": 1, "a_b": 1}))

    def test_the_default_name_follows_the_project_directory(self):
        assert deploy.base_name(Path("/srv/shop")) == "arc-shop"
        assert deploy.base_name(Path("/srv/shop"), name="custom") == "custom"

    def test_every_unit_carries_the_ownership_marker(self):
        for text in deploy.split_unit_texts(BASE, SPEC).values():
            assert text.split("\n", 1)[0] == "# managed by `arc deploy` — base=arc-shop"


class TestScopes:
    def test_user_units_run_as_whoever_owns_the_user_manager(self):
        for unit, text in deploy.split_unit_texts(BASE, SPEC, USER).items():
            assert "User=" not in text, unit

    def test_system_units_run_as_the_project_owner_never_root(self):
        for unit, text in deploy.split_unit_texts(BASE, SPEC, SYSTEM).items():
            if unit.endswith(".service"):
                cp = parse(text)
                assert cp["Service"]["User"] == "shopuser" and cp["Service"]["Group"] == "shopgroup", unit

    def test_system_units_start_on_boot_not_on_login(self):
        target = parse(deploy.split_unit_texts(BASE, SPEC, SYSTEM)["arc-shop.target"])
        assert target["Install"]["WantedBy"] == "multi-user.target"

    def test_the_two_scopes_use_the_right_systemctl_and_directory(self):
        assert USER.ctl == ("systemctl", "--user") and ".config/systemd/user" in str(USER.unit_dir)
        assert SYSTEM.ctl == ("systemctl",) and SYSTEM.unit_dir == Path("/etc/systemd/system")


class FakeSystemd:
    """Redirects the unit directory into tmp_path and records every systemctl call."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.dir = tmp_path / "units"
        self.calls: list[tuple[str, ...]] = []
        self.scope = Scope("user", self.dir, ("systemctl", "--user"), "default.target")
        monkeypatch.setattr(deploy, "_ctl", lambda scope, *a: self.calls.append(a))
        monkeypatch.setattr(deploy, "_ctl_try", lambda scope, *a: self.calls.append(a) or True)

    def put(self, unit: str, text: str = "[Unit]\n") -> Path:
        self.dir.mkdir(parents=True, exist_ok=True)
        path = self.dir / unit
        path.write_text(text, encoding="utf-8")
        return path

    def files(self) -> set[str]:
        return {p.name for p in self.dir.glob("*")} if self.dir.exists() else set()


@pytest.fixture
def systemd(tmp_path, monkeypatch) -> FakeSystemd:
    return FakeSystemd(tmp_path, monkeypatch)


def install(systemd: FakeSystemd, spec: SplitSpec = SPEC, *, enable: bool) -> list[Path]:
    return deploy.install_split(base=BASE, spec=spec, scope=systemd.scope, enable=enable)


class TestInstall:
    def test_without_enable_it_only_writes_files_and_reloads(self, systemd):
        written = install(systemd, enable=False)
        assert {p.name for p in written} == set(deploy.split_unit_names(BASE, SPEC, systemd.scope))
        assert systemd.files() == set(deploy.split_unit_names(BASE, SPEC, systemd.scope))
        # nothing enabled, started, stopped or restarted: re-running can never take a live site down
        assert systemd.calls == [("daemon-reload",)]

    def test_with_enable_the_target_is_enabled_then_restarted(self, systemd):
        install(systemd, enable=True)
        assert systemd.calls[-2:] == [("enable", "arc-shop.target"), ("restart", "arc-shop.target")]

    def test_enabling_stops_the_old_single_arc_run_unit_first_and_keeps_its_file(self, systemd):
        systemd.put("arc-shop.service", "[Service]\nExecStart=/x/arc run --port 8001\n")  # no marker: the old shape
        install(systemd, enable=True)
        calls = systemd.calls
        assert ("stop", "arc-shop.service") in calls and ("disable", "arc-shop.service") in calls
        assert calls.index(("stop", "arc-shop.service")) < calls.index(("restart", "arc-shop.target"))
        assert "arc-shop.service" in systemd.files()  # only disabled: the way back, if ever wanted

    def test_without_enable_a_running_old_unit_is_left_alone(self, systemd):
        systemd.put("arc-shop.service")
        install(systemd, enable=False)
        assert not any(call[0] in ("stop", "disable") for call in systemd.calls)

    def test_a_marked_unit_of_the_same_name_is_not_mistaken_for_the_old_shape(self, systemd):
        install(systemd, enable=False)
        systemd.calls.clear()
        install(systemd, enable=True)
        assert ("stop", "arc-shop.service") not in systemd.calls

    def test_units_from_an_older_bigger_deployment_are_stopped_and_removed_on_enable(self, systemd):
        install(systemd, with_(queue_workers={"default": 1, "mail": 4}), enable=False)
        systemd.calls.clear()
        install(systemd, enable=True)  # mail 4 -> 2
        for stale in ("arc-shop-worker-mail-3.service", "arc-shop-worker-mail-4.service"):
            assert ("stop", stale) in systemd.calls
            assert stale not in systemd.files()
        assert systemd.files() == set(deploy.split_unit_names(BASE, SPEC, systemd.scope))

    def test_a_queue_that_no_longer_exists_loses_its_units_too(self, systemd):
        install(systemd, with_(queue_workers={"default": 1, "mail": 1, "old": 2}), enable=False)
        install(systemd, enable=True)
        assert not any("worker-old" in f for f in systemd.files())

    def test_stale_units_are_left_in_place_when_not_enabling(self, systemd):
        install(systemd, with_(queue_workers={"default": 1, "mail": 4}), enable=False)
        install(systemd, enable=False)
        assert "arc-shop-worker-mail-4.service" in systemd.files()

    def test_another_projects_units_are_never_touched_even_with_a_confusable_name(self, systemd):
        """`arc-shop-worker` is a different project; its gateway unit is
        `arc-shop-worker-gateway.service`. A queue called `gateway` here would produce
        a name that collides in shape. Ownership is the marker, not the file name."""
        other = SplitSpec(**{**SPEC.__dict__, "project_root": Path("/srv/other")})
        deploy.install_split(base="arc-shop-worker", spec=other, scope=systemd.scope, enable=False)
        theirs = {f for f in systemd.files() if f.startswith("arc-shop-worker")}
        systemd.put("arc-shop-extra.service")  # an unmarked file that merely shares our prefix
        install(systemd, enable=True)
        assert theirs <= systemd.files() and "arc-shop-extra.service" in systemd.files()
        assert not any(call[0] == "stop" and call[1] in theirs for call in systemd.calls)

    def test_two_projects_side_by_side_do_not_interfere(self, systemd):
        install(systemd, enable=False)
        other = SplitSpec(**{**SPEC.__dict__, "project_root": Path("/srv/blog"), "queue_workers": {"default": 3}})
        deploy.install_split(base="arc-blog", spec=other, scope=systemd.scope, enable=True)
        assert set(deploy.split_unit_names(BASE, SPEC, systemd.scope)) <= systemd.files()


class TestDeployedWorkerQueues:
    """What `arc doctor --deep` compares the plugins' queues against: a plugin that adds a
    queue after the units were written leaves it with no worker, and jobs for it just wait."""

    def test_none_when_the_project_was_not_deployed_this_way(self, systemd):
        assert deploy.deployed_worker_queues(Path("/srv/shop"), scopes=[systemd.scope]) is None

    def test_the_queues_the_installed_worker_units_consume(self, systemd):
        install(systemd, enable=False)
        assert deploy.deployed_worker_queues(Path("/srv/shop"), scopes=[systemd.scope]) == {"default", "mail"}

    def test_another_projects_units_do_not_count(self, systemd):
        other = SplitSpec(**{**SPEC.__dict__, "project_root": Path("/srv/blog"), "queue_workers": {"billing": 1}})
        deploy.install_split(base="arc-blog", spec=other, scope=systemd.scope, enable=False)
        assert deploy.deployed_worker_queues(Path("/srv/shop"), scopes=[systemd.scope]) is None
        assert deploy.deployed_worker_queues(Path("/srv/blog"), scopes=[systemd.scope]) == {"billing"}

    def test_hand_written_units_without_our_marker_are_ignored(self, systemd):
        systemd.put(
            "mine.service",
            "[Service]\nWorkingDirectory=/srv/shop\nExecStart=/x/arc lineup worker --queues secret\n",
        )
        assert deploy.deployed_worker_queues(Path("/srv/shop"), scopes=[systemd.scope]) is None

    def test_the_scheduler_is_not_a_worker(self, systemd):
        install(systemd, with_(queue_workers={"only": 1}), enable=False)
        assert deploy.deployed_worker_queues(Path("/srv/shop"), scopes=[systemd.scope]) == {"only"}


class TestDoctorQueueCoverage:
    def kernel(self, queues, has_lineup=True):
        import types

        lineup = types.SimpleNamespace(queues=lambda: queues)
        return types.SimpleNamespace(
            has=lambda name: has_lineup and name == "lineup",
            get=lambda name: lineup,
            project_root=Path("/srv/shop"),
        )

    def test_a_queue_with_no_worker_unit_is_reported_missing(self, monkeypatch):
        from arc import doctor

        monkeypatch.setattr(deploy, "deployed_worker_queues", lambda root: {"default"})
        coverage = doctor._queue_coverage(self.kernel(["default", "mail"]))
        assert coverage == {"declared": ["default", "mail"], "covered": ["default"], "missing": ["mail"]}

    def test_full_coverage_has_nothing_missing(self, monkeypatch):
        from arc import doctor

        monkeypatch.setattr(deploy, "deployed_worker_queues", lambda root: {"default", "mail", "extra"})
        assert doctor._queue_coverage(self.kernel(["default", "mail"]))["missing"] == []

    def test_a_project_not_deployed_with_arc_deploy_is_not_judged(self, monkeypatch):
        from arc import doctor

        monkeypatch.setattr(deploy, "deployed_worker_queues", lambda root: None)
        assert doctor._queue_coverage(self.kernel(["default"])) is None

    def test_without_lineup_there_is_nothing_to_check(self):
        from arc import doctor

        assert doctor._queue_coverage(self.kernel([], has_lineup=False)) is None
