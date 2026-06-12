"""
arc.kernel.logger
=================
Structured logging via structlog. Named ``logger.py`` (never ``logging.py``)
so it can never shadow the stdlib ``logging`` module — a hard Arc rule.

Usage::

    from arc.kernel.logger import get_logger
    log = get_logger(__name__)
    log.info("plugin.loaded", name="db")
"""

from __future__ import annotations

import logging
import sys

try:
    import structlog

    _HAS_STRUCTLOG = True
except ImportError:  # pragma: no cover - structlog is a hard dep, but stay safe
    _HAS_STRUCTLOG = False


_configured = False


def configure_logging(
    *,
    level: str = "INFO",
    renderer: str = "console",
    include_timestamp: bool = True,
) -> None:
    """Configure process-wide logging. Idempotent."""
    global _configured
    if _configured:
        return

    log_level = getattr(logging, level.upper(), logging.INFO)
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=log_level)

    if not _HAS_STRUCTLOG:
        _configured = True
        return

    processors = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
    ]
    if include_timestamp:
        processors.append(structlog.processors.TimeStamper(fmt="iso"))
    if renderer == "json":
        processors.append(structlog.processors.JSONRenderer())
    else:
        processors.append(structlog.dev.ConsoleRenderer())

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )
    _configured = True


def get_logger(name: str = "arc"):
    """Return a structured logger, falling back to stdlib if needed."""
    if _HAS_STRUCTLOG:
        return structlog.get_logger(name)
    return logging.getLogger(name)


def reset_logging() -> None:
    """Test helper — allow reconfiguration."""
    global _configured
    _configured = False
