"""
arc._state
-----------------
Process-wide runtime state: the single active Kernel for this process.

Deliberately tiny and dependency-free. It exists so that `arc/__init__.py`
(PEP 562 attribute lookup), `arc.runtime` (boot/shutdown) and `arc.settings`
(module-level runtime API) can all reach the active kernel without importing
each other in a circle.

Access goes through get_kernel()/set_kernel() — never `from ... import`
the variable itself, which would snapshot a stale binding.
"""

from __future__ import annotations

import typing

if typing.TYPE_CHECKING:
    from .kernel import Kernel

_active_kernel: "Kernel | None" = None


def get_kernel() -> "Kernel | None":
    return _active_kernel


def set_kernel(kernel: "Kernel | None") -> None:
    global _active_kernel
    _active_kernel = kernel