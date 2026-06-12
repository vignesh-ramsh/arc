"""
arc.kernel.contracts
====================
``CheckResult`` — the value every plugin contract method returns.

Plugins answer four questions with these results:

    startup_check()   preconditions met to serve traffic?
    health_check()    runtime dependencies reachable?

The kernel aggregates them; it never inspects *why* a check failed, only
whether it passed, warned, or failed.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from arc.kernel.exceptions import StartupError


class CheckStatus(str, Enum):
    OK = "ok"
    WARN = "warn"
    FAIL = "fail"


@dataclass(frozen=True)
class CheckResult:
    status: CheckStatus
    message: str = ""

    @classmethod
    def ok(cls, message: str = "") -> "CheckResult":
        return cls(CheckStatus.OK, message)

    @classmethod
    def warn(cls, message: str) -> "CheckResult":
        return cls(CheckStatus.WARN, message)

    @classmethod
    def fail(cls, message: str) -> "CheckResult":
        return cls(CheckStatus.FAIL, message)

    @property
    def passed(self) -> bool:
        return self.status is not CheckStatus.FAIL

    @property
    def failed(self) -> bool:
        return self.status is CheckStatus.FAIL

    def raise_if_failed(self, *, plugin: str = "") -> None:
        if self.failed:
            prefix = f"[{plugin}] " if plugin else ""
            raise StartupError(
                f"{prefix}{self.message}", code="arc.check.failed"
            )
