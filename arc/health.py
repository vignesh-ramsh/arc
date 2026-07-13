"""
arc.health
-----------------
Aggregated health across every currently-registered capability — the
Kernel-level home for what used to be a gateway-only "working preview"
(its own docstring called it that). Any process that has called
arc.boot() can ask this now, not just one serving HTTP — a background
worker or a bare CLI command gets the same answer without booting Gateway
just to get it.

Mechanism (duck-typed, domain-blind per §3.1): a capability MAY expose an
`async def health() -> dict`. check() calls whichever ones do and collects
the results; anything without one is silently skipped — the Kernel never
needs to know what "psqldb" or "redix" even are, only that the attribute
might exist.

    import arc
    arc.boot()
    results = await arc.health.check()
    # {"psqldb": {"ok": True, "version": "..."}, "redix": {"ok": True}}
    arc.health.all_ok(results)   -> bool
"""

from __future__ import annotations

from . import _state
from .kernel import KernelError


class HealthError(KernelError):
    """arc.health.check() was called before arc.boot() — no active kernel,
    so no capabilities exist yet to check."""


async def check() -> dict[str, dict]:
    kernel = _state.get_kernel()
    if kernel is None:
        raise HealthError(
            "arc.health.check() requires arc.boot() first — there is no "
            "active kernel, so no capabilities to check."
        )
    results: dict[str, dict] = {}
    for name, cap in kernel.capabilities().items():
        health_fn = getattr(cap.instance, "health", None)
        if not callable(health_fn):
            continue
        try:
            results[name] = await health_fn()
        except Exception as exc:
            results[name] = {"ok": False, "error": f"{exc.__class__.__name__}: {exc}"}
    return results


def all_ok(results: dict[str, dict]) -> bool:
    return all(r.get("ok", True) for r in results.values())
