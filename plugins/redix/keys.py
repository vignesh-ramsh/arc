"""
plugins.redix.keys
==================
Key namespacing. Each capability gets its own prefix so cache keys, queue stream
names, and scheduler lock/history keys never collide on a shared Redis instance
(and never collide with another app pointed at the same Redis).

A ``KeyBuilder`` is cheap; each capability holds one built from its configured
prefix.
"""

from __future__ import annotations


class KeyBuilder:
    """Builds prefixed, colon-delimited Redis keys.

    ``KeyBuilder("arc:cache").build("hrms:employee:42")`` → ``arc:cache:hrms:employee:42``
    """

    def __init__(self, prefix: str) -> None:
        self._prefix = prefix.rstrip(":")

    @property
    def prefix(self) -> str:
        return self._prefix

    def build(self, *parts: str) -> str:
        tail = ":".join(str(p).strip(":") for p in parts if p != "")
        return f"{self._prefix}:{tail}" if tail else self._prefix

    def match(self, pattern_tail: str = "*") -> str:
        """A SCAN/KEYS match pattern under this prefix."""
        return f"{self._prefix}:{pattern_tail}"