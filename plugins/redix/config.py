"""
plugins.redix.config
====================
Configuration for the redix plugin. URL resolution follows psqldb's precedent:
the ``REDIS_URL`` environment variable wins, then ``[plugins.redix] url`` in
arc.toml, then a localhost default.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass
class CacheCfg:
    default_ttl: int = 300
    key_prefix: str = "arc:cache"


@dataclass
class QueueCfg:
    key_prefix: str = "arc:queue"
    default_retries: int = 3
    result_ttl: int = 86400


@dataclass
class SchedulerCfg:
    key_prefix: str = "arc:sched"
    leader_id: str = "scheduler-primary"


@dataclass
class RedixConfig:
    url: str = "redis://localhost:6379/0"
    max_connections: int = 20
    cache: CacheCfg = field(default_factory=CacheCfg)
    queue: QueueCfg = field(default_factory=QueueCfg)
    scheduler: SchedulerCfg = field(default_factory=SchedulerCfg)

    @classmethod
    def from_mapping(cls, mapping: dict | None, env: dict | None = None) -> "RedixConfig":
        m = dict(mapping or {})
        env = env if env is not None else os.environ
        url = env.get("REDIS_URL") or m.get("url") or "redis://localhost:6379/0"

        cache_m = dict(m.get("cache") or {})
        queue_m = dict(m.get("queue") or {})
        sched_m = dict(m.get("scheduler") or {})

        return cls(
            url=url,
            max_connections=int(m.get("max_connections", 20)),
            cache=CacheCfg(
                default_ttl=int(cache_m.get("default_ttl", 300)),
                key_prefix=str(cache_m.get("key_prefix", "arc:cache")),
            ),
            queue=QueueCfg(
                key_prefix=str(queue_m.get("key_prefix", "arc:queue")),
                default_retries=int(queue_m.get("default_retries", 3)),
                result_ttl=int(queue_m.get("result_ttl", 86400)),
            ),
            scheduler=SchedulerCfg(
                key_prefix=str(sched_m.get("key_prefix", "arc:sched")),
                leader_id=str(sched_m.get("leader_id", "scheduler-primary")),
            ),
        )