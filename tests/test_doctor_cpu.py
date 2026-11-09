"""`arc doctor --deep` warns when a plugin's CPU demand per web worker, times the web workers the project is
deployed with, could take more than half of the machine's cores.

Found by a load test of a live site: a flood of logins for accounts that do not exist took 9-10 of 12 cores,
because 2 web workers x 2 hashes at once x 4 threads each is 16 threads. Nothing said so before it happened.
A plugin states its demand (`web_worker_cpu_demand()` on its capability); the kernel knows the workers and
the cores and does the arithmetic, so no plugin needs to know about either.
"""

from __future__ import annotations

from types import SimpleNamespace

from arc import doctor

HASHING = {
    "what": "password hashing (authn_argon2)",
    "threads": 8,
    "advice": "Lower parallelism or max_concurrent in authn_argon2.",
}


class TestTheWarning:
    def test_a_small_demand_is_not_worth_a_warning(self):
        assert (
            doctor.cpu_demand_warnings([{**HASHING, "threads": 2}], web_workers=2, cores=12) == []
        )  # 4 threads of 12

    def test_exactly_half_the_cores_is_still_fine(self):
        assert (
            doctor.cpu_demand_warnings([{**HASHING, "threads": 3}], web_workers=2, cores=12) == []
        )  # 6 of 12

    def test_more_than_half_warns_with_the_numbers_and_the_plugins_own_advice(self):
        (warning,) = doctor.cpu_demand_warnings(
            [HASHING], web_workers=2, cores=12
        )  # 16 threads of 12
        assert "password hashing (authn_argon2)" in warning
        assert "16 threads" in warning and "2 web workers" in warning and "12 cores" in warning
        assert "Lower parallelism or max_concurrent" in warning

    def test_each_plugin_gets_its_own_line(self):
        other = {"what": "image resizing", "threads": 9, "advice": "Lower resize_workers."}
        warnings = doctor.cpu_demand_warnings([HASHING, other], web_workers=2, cores=12)
        assert len(warnings) == 2 and "image resizing" in warnings[1]

    def test_no_web_workers_means_nothing_to_say(self):
        assert doctor.cpu_demand_warnings([HASHING], web_workers=0, cores=12) == []

    def test_a_single_core_machine_is_warned_by_anything(self):
        assert (
            len(doctor.cpu_demand_warnings([{**HASHING, "threads": 1}], web_workers=1, cores=1))
            == 1
        )


class TestGatheringTheDemand:
    def kernel(self, **capabilities):
        return SimpleNamespace(
            capabilities=lambda: {
                name: SimpleNamespace(instance=obj) for name, obj in capabilities.items()
            }
        )

    def test_it_asks_every_capability_that_has_the_hook(self):
        class WithHook:
            def web_worker_cpu_demand(self):
                return HASHING

        demands = doctor._cpu_demands(self.kernel(authn=WithHook(), pgdb=object()))
        assert demands == [HASHING]

    def test_a_capability_that_fails_to_answer_is_skipped_not_fatal(self):
        class Broken:
            def web_worker_cpu_demand(self):
                raise RuntimeError("not ready")

        class Fine:
            def web_worker_cpu_demand(self):
                return HASHING

        assert doctor._cpu_demands(self.kernel(broken=Broken(), fine=Fine())) == [HASHING]

    def test_no_answer_means_no_demand(self):
        class Quiet:
            def web_worker_cpu_demand(self):
                return None

        assert doctor._cpu_demands(self.kernel(q=Quiet())) == []


class TestForADeployedProject:
    """The pieces together: the web workers come from the deployment, the cores from the machine."""

    def kernel(self):
        class WithHook:
            def web_worker_cpu_demand(self):
                return HASHING

        return SimpleNamespace(
            project_root="/project",
            capabilities=lambda: {"authn": SimpleNamespace(instance=WithHook())},
        )

    def test_a_deployed_project_is_checked_against_its_web_workers_and_the_cores(self, monkeypatch):
        from arc import deploy, sizing, status

        monkeypatch.setattr(deploy, "read_deployments", lambda root: [object()])
        monkeypatch.setattr(
            status,
            "expected_processes",
            lambda deployment: {sizing.ROLE_GATEWAY: 2, sizing.ROLE_LINEUP_WORKER: 4},
        )
        monkeypatch.setattr(sizing, "detect_cpu_count", lambda: 12)
        (warning,) = doctor._cpu_warnings(self.kernel())
        assert "16 threads" in warning and "2 web workers" in warning and "12 cores" in warning

    def test_more_cores_make_the_same_deployment_fine(self, monkeypatch):
        from arc import deploy, sizing, status

        monkeypatch.setattr(deploy, "read_deployments", lambda root: [object()])
        monkeypatch.setattr(
            status, "expected_processes", lambda deployment: {sizing.ROLE_GATEWAY: 2}
        )
        monkeypatch.setattr(sizing, "detect_cpu_count", lambda: 64)
        assert doctor._cpu_warnings(self.kernel()) == []

    def test_a_project_that_was_not_deployed_is_not_checked(self, monkeypatch):
        from arc import deploy

        monkeypatch.setattr(deploy, "read_deployments", lambda root: [])
        assert doctor._cpu_warnings(self.kernel()) == []

    def test_the_warning_is_printed_with_the_others(self, capsys):
        doctor._print_deep(
            {
                "boot_ok": True,
                "health": {},
                "cpu_warnings": ["password hashing [authn_argon2] can use up to 16 threads"],
            }
        )
        assert (
            "can use up to 16 threads" in capsys.readouterr().err
        )  # including the square brackets in the text
