"""
ARC kernel — capability-based Python platform.

`import arc` is the only import application code ever needs (Architecture
§3.2). After `arc.boot()`, every enabled plugin's capability is reachable as
`arc.<capability>` via module-level `__getattr__` (PEP 562) — never
`import arc.psqldb`.

    import arc

    arc.boot()
    user = await arc.psqldb.fetch_one(...)
"""

from __future__ import annotations

from typing import Any

from . import codec, health  # noqa: F401 - attaches arc.codec / arc.health as real submodules
from .kernel import (  # noqa: F401
    ArcAdvisory,
    Capability,
    ExportError,
    Kernel,
    KernelError,
)
from .resolver import BootPlan, PluginSpec, ResolutionError  # noqa: F401
from .runtime import BootError, boot, find_project_root, shutdown  # noqa: F401

__version__ = "0.1.0"


def __getattr__(name: str) -> Any:
    """PEP 562: resolve arc.<capability> against the booted kernel (§3.2).

    Only consulted for names not found in the module dict — which is exactly
    why capability names may never collide with the kernel's own namespace
    (enforced at resolution and export time; see arc.kernel).
    """
    from . import _state

    kernel = _state.get_kernel()
    if kernel is not None and kernel.has(name):
        return kernel.get(name)
    if kernel is None:
        raise AttributeError(
            f"module 'arc' has no attribute {name!r}. If {name!r} is a plugin "
            f"capability, call arc.boot() first — capabilities are only "
            f"attached to `arc` after boot."
        )
    raise AttributeError(
        f"module 'arc' has no attribute {name!r}, and no enabled plugin "
        f"exports a capability with that name "
        f"(booted capabilities: {sorted(kernel.capabilities()) or 'none'})."
    )


def __dir__() -> list[str]:
    from . import _state

    names = set(globals())
    kernel = _state.get_kernel()
    if kernel is not None:
        names.update(kernel.capabilities())
    return sorted(names)