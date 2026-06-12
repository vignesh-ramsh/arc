"""Arc API plugin — auto-CRUD + custom routes."""
from arc.plugins.api.plugin import ApiPlugin
from arc.plugins.api.resource import Resource
from arc.plugins.api.custom import api

__all__ = ["ApiPlugin", "Resource", "api"]
