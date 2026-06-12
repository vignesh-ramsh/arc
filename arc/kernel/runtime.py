"""
arc.kernel.runtime
=================
``Runtime`` is the single handle the kernel hands to every plugin during
``setup()`` and ``contribute()``. It bundles everything a plugin legitimately
needs to wire itself in:

    rt.config            full ArcConfig (read-only)
    rt.plugin_config     this plugin's [plugins.<name>] table
    rt.capabilities      provide()/require() services
    rt.extensions        contribute()/get() extension points
    rt.lifecycle         read handle (e.g. for a /health aggregator)
    rt.logger            a bound structured logger

Passing one object keeps the plugin protocol stable as the kernel grows.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from arc.kernel.capability import Capabilities
from arc.kernel.config import ArcConfig
from arc.kernel.registry import ExtensionRegistry

if TYPE_CHECKING:
    from arc.kernel.lifecycle import LifecycleManager


@dataclass
class Runtime:
    config: ArcConfig
    capabilities: Capabilities
    extensions: ExtensionRegistry
    lifecycle: "LifecycleManager"
    _current_plugin: str = ""

    @property
    def plugin_config(self) -> dict[str, Any]:
        return self.config.for_plugin(self._current_plugin)

    @property
    def logger(self):
        from arc.kernel.logger import get_logger

        return get_logger(f"arc.plugin.{self._current_plugin or 'kernel'}")

    def scoped(self, plugin_name: str) -> "Runtime":
        """Return a view bound to *plugin_name* (same shared containers)."""
        return Runtime(
            config=self.config,
            capabilities=self.capabilities,
            extensions=self.extensions,
            lifecycle=self.lifecycle,
            _current_plugin=plugin_name,
        )
