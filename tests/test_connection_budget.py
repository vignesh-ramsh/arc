"""The database connection budget (arc/sizing.py): how many connections each ARC process may
hold, given how many PostgreSQL allows.

Every ARC process owns its own pool, so a deployment opens (processes x pool size) connections.
Measured before this existed: 8 web workers + 8 job workers + a scheduler, each with a pool of 10,
is 170 possible connections against PostgreSQL's default 100 — and a burst of 600 jobs plus 64
clients drove it to 97, PostgreSQL refused 83 connection attempts, and 1,438 requests failed with
a 500. The numbers here are plain integers; reading the real server limits is pgdb's job.
"""

from __future__ import annotations

import itertools

import pytest

from arc import sizing
from arc.sizing import (
    ROLE_GATEWAY as GW,
    ROLE_LINEUP_SCHEDULER as SCHED,
    ROLE_LINEUP_WORKER as WORKER,
    ConnectionBudgetError,
    ConnectionPlan,
    fit_pool_to_server,
    plan_connection_pools,
)

DEV = {GW: 2, WORKER: 4, SCHED: 1}  # `arc deploy dev` on a four-queue project
PROD = {GW: 8, WORKER: 8, SCHED: 1}


class TestPlanning:
    def test_everyone_gets_what_they_want_when_it_all_fits(self):
        plan = plan_connection_pools(usable=100, reserve=10, processes=DEV)
        assert plan.pools == {GW: 10, WORKER: 5, SCHED: 2}
        assert plan.total == 2 * 10 + 4 * 5 + 2 == 42

    def test_the_old_defaults_would_not_have_fit_but_the_plan_does(self):
        """8+8+1 processes at the old pool of 10 each: 170 against a limit of 100."""
        assert (PROD[GW] + PROD[WORKER] + PROD[SCHED]) * 10 == 170
        plan = plan_connection_pools(usable=97, reserve=10, processes=PROD)
        assert plan.total <= plan.budget == 87

    def test_a_tight_server_scales_everything_down_together_to_exactly_the_budget(self):
        plan = plan_connection_pools(usable=40, reserve=10, processes=DEV)
        assert plan.total == 30 == plan.budget  # nothing wasted, nothing over
        assert plan.pools[GW] > plan.pools[WORKER] > 0  # the web tier keeps the lion's share

    def test_connections_left_over_by_rounding_go_back_to_the_gateway_first(self):
        plan = plan_connection_pools(usable=25, reserve=10, processes={GW: 2, WORKER: 3})
        assert plan.total <= 15
        assert plan.pools[GW] >= plan.pools[WORKER]

    def test_floors_are_respected_even_when_squeezed(self):
        plan = plan_connection_pools(usable=24, reserve=10, processes=DEV)  # floors need 13 of the 14
        assert plan.pools[GW] >= sizing.POOL_FLOORS[GW]
        assert plan.pools[WORKER] >= sizing.POOL_FLOORS[WORKER]
        assert plan.pools[SCHED] >= sizing.POOL_FLOORS[SCHED]
        assert plan.total <= 14

    def test_when_even_the_floors_do_not_fit_it_says_by_how_much_and_what_to_do(self):
        with pytest.raises(ConnectionBudgetError) as err:
            plan_connection_pools(usable=15, reserve=10, processes=DEV)
        text = str(err.value)
        assert "only 5 are available" in text and "15 allowed minus 10 reserved" in text
        assert "max_connections" in text  # and it names the ways out

    def test_a_server_with_nothing_left_after_the_reserve_is_an_error(self):
        with pytest.raises(ConnectionBudgetError, match="nothing is left"):
            plan_connection_pools(usable=10, reserve=10, processes=DEV)

    def test_a_role_with_no_processes_costs_nothing_and_is_left_out(self):
        plan = plan_connection_pools(usable=100, reserve=10, processes={GW: 2, WORKER: 0, SCHED: 0})
        assert plan.pools == {GW: 10} and plan.processes == {GW: 2}

    def test_gateway_only_deployment(self):
        plan = plan_connection_pools(usable=30, reserve=10, processes={GW: 4})
        assert plan.total <= 20 and plan.pools[GW] == 5

    def test_a_pool_the_operator_fixed_is_kept_and_paid_for_first(self):
        plan = plan_connection_pools(usable=50, reserve=10, processes=DEV, fixed={WORKER: 8})
        assert plan.pools[WORKER] == 8  # untouched
        assert plan.total <= 40
        assert plan.pools[GW] * 2 + 8 * 4 + plan.pools[SCHED] <= 40

    def test_fixed_pools_that_alone_exceed_the_budget_are_an_error_naming_them(self):
        with pytest.raises(ConnectionBudgetError, match="lineup-worker x 20"):
            plan_connection_pools(usable=50, reserve=10, processes=DEV, fixed={WORKER: 20})

    def test_desired_sizes_can_be_overridden(self):
        plan = plan_connection_pools(usable=200, reserve=10, processes=DEV, desired={GW: 25})
        assert plan.pools[GW] == 25

    def test_it_is_deterministic(self):
        a = plan_connection_pools(usable=47, reserve=10, processes=PROD)
        assert a == plan_connection_pools(usable=47, reserve=10, processes=PROD)

    def test_the_guarantees_hold_across_thousands_of_combinations(self):
        """total <= budget, floors <= pool <= desired, for every combination — or a clear error,
        never a plan that overshoots."""
        checked = 0
        for usable, gw, wk, sc in itertools.product(range(12, 140, 3), range(0, 9), range(0, 9), range(0, 3)):
            procs = {GW: gw, WORKER: wk, SCHED: sc}
            if not any(procs.values()):
                continue
            try:
                plan = plan_connection_pools(usable=usable, reserve=10, processes=procs)
            except ConnectionBudgetError:
                floor_cost = sum(n * sizing.POOL_FLOORS[r] for r, n in procs.items())
                assert floor_cost > usable - 10 or usable - 10 < 1  # it only refuses when it truly cannot fit
                continue
            assert plan.total <= plan.budget, (usable, procs, plan)
            for role, size in plan.pools.items():
                assert sizing.POOL_FLOORS[role] <= size <= sizing.POOL_DESIRED[role], (usable, procs, plan)
            checked += 1
        assert checked > 1500


class TestRoundTrip:
    def test_a_plan_survives_being_stored_as_a_setting(self):
        plan = plan_connection_pools(usable=97, reserve=10, processes=DEV)
        assert ConnectionPlan.from_json(plan.to_json()) == plan

    @pytest.mark.parametrize("junk", ["", "not json", "{}", '{"pools": 3}', '{"pools": {"a": "x"}, "processes": {}, "usable": 1, "reserve": 1}', "[1,2]", "null"])
    def test_anything_that_is_not_a_plan_is_none_never_a_crash(self, junk):
        """A hand-edited setting must never stop a process from starting."""
        assert ConnectionPlan.from_json(junk) is None


class TestFittingToWhatTheServerAllowsNow:
    """PostgreSQL's limit can be lowered after a deployment was planned (a restart of the
    database, a smaller managed instance). Each process applies the same formula to the same
    numbers, so together they stay inside the new limit."""

    def plan(self):
        return plan_connection_pools(usable=100, reserve=10, processes=PROD)

    def test_unchanged_when_the_plan_still_fits(self):
        plan = self.plan()
        for role in plan.pools:
            assert fit_pool_to_server(plan, role, usable_now=100) == plan.pools[role]
        assert fit_pool_to_server(plan, GW, usable_now=500) == plan.pools[GW]  # more room: no growth

    def test_every_role_shrinks_and_the_whole_deployment_fits_the_new_limit(self):
        plan = self.plan()
        shrunk = {r: fit_pool_to_server(plan, r, usable_now=50) for r in plan.pools}
        assert all(shrunk[r] <= plan.pools[r] for r in shrunk)
        assert sum(plan.processes[r] * shrunk[r] for r in shrunk) <= 50 - plan.reserve

    def test_floors_still_hold(self):
        plan = self.plan()
        assert fit_pool_to_server(plan, WORKER, usable_now=15) >= sizing.POOL_FLOORS[WORKER]

    def test_the_deployment_fits_the_new_limit_for_every_limit_down_to_the_floors(self):
        """The property the first version of this got wrong (rounding plus floors overshot by one)."""
        plan = self.plan()
        floor_cost = sum(plan.processes[r] * sizing.POOL_FLOORS[r] for r in plan.pools)
        for usable_now in range(floor_cost + plan.reserve, 120, 1):
            shrunk = {r: fit_pool_to_server(plan, r, usable_now) for r in plan.pools}
            assert sum(plan.processes[r] * shrunk[r] for r in shrunk) <= usable_now - plan.reserve, usable_now

    def test_below_the_floors_it_returns_the_floor_rather_than_failing(self):
        plan = self.plan()
        for role in plan.pools:
            assert fit_pool_to_server(plan, role, usable_now=12) == sizing.POOL_FLOORS[role]

    def test_a_role_not_in_the_plan_gets_a_safe_minimum(self):
        assert fit_pool_to_server(self.plan(), "something-else", usable_now=100) >= 1

    def test_the_formula_never_grows_a_pool(self):
        plan = self.plan()
        for usable_now in range(11, 200, 7):
            for role in plan.pools:
                assert fit_pool_to_server(plan, role, usable_now) <= plan.pools[role]
