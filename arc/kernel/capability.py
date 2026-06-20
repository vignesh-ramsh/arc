"""
arc.kernel.capability
=====================
The capability container — Arc's dependency-injection mechanism.

A *capability* is a named, versioned service one plugin offers and another
consumes. The db plugin ``provide``s ``"db.session"``; the api plugin
``require``s it. The kernel itself provides nothing domain-specific — it only
hosts the container.

Capabilities can be registered as a ready instance or as a lazy factory
(built on first ``require``). Factories let a plugin defer expensive
construction (engines, app objects) until everything has been wired.

Hard vs optional consumption
----------------------------
``require(name)``  — raises ``CapabilityError`` if absent. Use for hard deps.
``get(name)``      — returns ``None`` if absent. Use for *optional* deps
                     (declared via ``Plugin.requires_optional``), where the
                     consumer has a fallback when no plugin provides it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from arc.kernel.exceptions import CapabilityError


@dataclass
class _Provider:
    name: str
    version: str
    source: str  # plugin name that provided it
    instance: Any = None
    factory: Callable[[], Any] | None = None
    _building: bool = False

    def resolve(self) -> Any:
        if self.instance is not None:
            return self.instance
        if self.factory is None:
            raise CapabilityError(
                f"Capability '{self.name}' has neither instance nor factory.",
                code="arc.capability.empty",
            )
        if self._building:
            raise CapabilityError(
                f"Circular capability dependency while building '{self.name}'.",
                code="arc.capability.cycle",
            )
        self._building = True
        try:
            self.instance = self.factory()
        finally:
            self._building = False
        return self.instance


class Capabilities:
    """Process-wide registry of provided capabilities."""

    def __init__(self) -> None:
        self._providers: dict[str, _Provider] = {}

    def provide(
        self,
        name: str,
        instance: Any = None,
        *,
        factory: Callable[[], Any] | None = None,
        version: str = "1.0.0",
        source: str = "",
        replace: bool = False,
    ) -> None:
        """Register a capability. Raises if already provided unless *replace*."""
        if instance is None and factory is None:
            raise CapabilityError(
                f"provide('{name}') needs an instance or a factory.",
                code="arc.capability.empty",
            )
        if name in self._providers and not replace:
            existing = self._providers[name].source
            raise CapabilityError(
                f"Capability '{name}' already provided by '{existing}'. "
                f"Pass replace=True to override.",
                code="arc.capability.duplicate",
            )
        self._providers[name] = _Provider(
            name=name, version=version, source=source, instance=instance, factory=factory
        )

    def require(self, name: str) -> Any:
        """Return the capability instance, building a factory on first use.

        Raises ``CapabilityError`` if no plugin provides *name*. Use for hard
        dependencies declared in ``Plugin.requires``.
        """
        provider = self._providers.get(name)
        if provider is None:
            raise CapabilityError(
                f"Required capability '{name}' is not provided by any plugin.",
                code="arc.capability.missing",
                detail={"available": sorted(self._providers)},
            )
        return provider.resolve()

    def get(self, name: str) -> Any | None:
        """Return the capability instance, or ``None`` if absent.

        The non-raising counterpart to ``require()``. Use for *optional*
        dependencies (``Plugin.requires_optional``) where the consumer falls
        back gracefully when no plugin provides the capability. A provider that
        exists but whose factory fails to build still raises — ``get`` only
        swallows *absence*, never a broken provider.
        """
        provider = self._providers.get(name)
        if provider is None:
            return None
        return provider.resolve()

    def has(self, name: str) -> bool:
        return name in self._providers

    def names(self) -> list[str]:
        return sorted(self._providers)

    def source_of(self, name: str) -> str:
        provider = self._providers.get(name)
        return provider.source if provider else ""