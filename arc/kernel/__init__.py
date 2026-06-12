"""
arc.kernel
=========
The Arc micro-kernel. Domain-free: it knows how to discover, resolve, wire,
and run plugins — and nothing about databases or HTTP.

Public API::

    from arc.kernel import Arc, Plugin, Runtime, CheckResult
    from arc.kernel import Capabilities, ExtensionRegistry, Points
"""

from arc.kernel.capability import Capabilities
from arc.kernel.config import ArcConfig, load_config
from arc.kernel.context import RequestContext, UserContext, get_request, get_user
from arc.kernel.contracts import CheckResult, CheckStatus
from arc.kernel.exceptions import ArcError
from arc.kernel.orchestrator import Arc
from arc.kernel.plugin import Plugin
from arc.kernel.registry import ExtensionRegistry, Points
from arc.kernel.runtime import Runtime

__all__ = [
    "Arc",
    "Plugin",
    "Runtime",
    "CheckResult",
    "CheckStatus",
    "Capabilities",
    "ExtensionRegistry",
    "Points",
    "ArcConfig",
    "load_config",
    "ArcError",
    "RequestContext",
    "UserContext",
    "get_request",
    "get_user",
]
