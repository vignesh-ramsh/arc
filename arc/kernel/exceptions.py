"""
arc.kernel.exceptions
======================
The Arc error hierarchy. Every failure the kernel can raise is a typed
subclass of :class:`ArcError` so callers can catch broadly or narrowly.

The kernel is domain-free, so these errors are *about orchestration*
(loading, resolving, wiring, lifecycle) — never about SQL or HTTP. Plugins
raise their own ``ArcError`` subclasses for their domains.
"""

from __future__ import annotations

from typing import Any


class ArcError(Exception):
    """Base class for all Arc errors."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "arc.error",
        detail: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.detail = detail or {}

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.message


# ── Configuration / project layout ─────────────────────────────────────
class ConfigError(ArcError):
    """arc.toml is missing, malformed, or fails validation."""


class LockFileError(ArcError):
    """arc.lock is missing or cannot be parsed."""


# ── Plugin loading & resolution ─────────────────────────────────────────
class PluginError(ArcError):
    """Base for plugin-related failures."""


class PluginLoadError(PluginError):
    """An entrypoint could not be imported or instantiated."""


class PluginNotFoundError(PluginError):
    """A named plugin is not present in arc.lock."""


class CapabilityError(ArcError):
    """A required capability was never provided, or provided twice."""


class ResolutionError(ArcError):
    """The plugin graph cannot be ordered (cycle or missing provider)."""


# ── Lifecycle ───────────────────────────────────────────────────────────
class StartupError(ArcError):
    """A plugin failed a startup check or raised during startup()."""


class ShutdownError(ArcError):
    """A plugin raised during shutdown()."""
