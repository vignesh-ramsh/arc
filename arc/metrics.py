"""
arc.metrics
------------------
Cross-process telemetry aggregation — the write side any long-running ARC
process calls after arc.boot() (`start_exporter(role=...)`, mirrors
arc.events.install_process_bridge exactly: same registration idiom, same
"requires a running event loop and an active kernel" contract, same
deliberately-NOT-called-by-arc.boot()-itself posture — a one-off CLI
invocation shouldn't leave a background task or a metrics file behind
it), and the read side gateway's own `/metrics` HTTP route uses to answer
a scrape correctly regardless of which pre-forked worker happens to
receive it.

Mechanism: every process periodically overwrites ITS OWN snapshot at
.arc/runtime/metrics/<pid>.json — the identical per-pid-file idiom
arc.events.register_process already uses for .arc/runtime/processes/
<pid>.json (one file each, no shared lock needed since a process only
ever touches its own file, self-pruning on read via kill(pid, 0)).
Reading aggregates every live file together — this is what makes ONE
gateway worker's `/metrics` response reflect ALL workers (and every
lineup worker/scheduler also running start_exporter), not just itself.
A naive per-process endpoint would return different, jumping numbers
depending on which pre-forked worker happened to answer a given scrape;
this doesn't, by construction.

Collection itself (collect()) is duck-typed exactly like arc.health.
check(): a capability MAY expose `def metrics() -> dict` (sync — these
are meant to be cheap in-memory reads, no I/O, unlike health() which
often pings a real connection) and collect() calls whichever ones do,
isolating one capability's bad metrics() from losing every other
capability's numbers over it.

    import arc
    arc.boot()
    arc.metrics.start_exporter(role="gateway-worker")
    ...
    snapshots = arc.metrics.read_all(kernel.project_root)
    text = arc.metrics.format_prometheus(arc.metrics.aggregate(snapshots))
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import time
from pathlib import Path
from typing import Any

from . import _state
from .kernel import KernelError


class MetricsError(KernelError):
    pass


# --------------------------------------------------------------------------- #
# .arc/runtime/metrics/<pid>.json — one file per process, self-pruning
# --------------------------------------------------------------------------- #
def _metrics_dir(project_root: Path) -> Path:
    return project_root / ".arc" / "runtime" / "metrics"


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:  # alive, someone else's — shouldn't happen for .arc-local pids
        return True


def _write_snapshot(path: Path, role: str, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"pid": os.getpid(), "role": role, "written_at": time.time(), "data": data}
    # tmp-then-replace, not a direct write_text — a reader mid-scan of the
    # metrics/ directory must never see a torn/partial write from this
    # process's own periodic overwrite. Path.replace() is a single atomic
    # rename on the same filesystem, same pattern arc.settings' own
    # _write_toml() uses.
    tmp_path = path.with_name(path.name + f".tmp.{os.getpid()}")
    tmp_path.write_text(json.dumps(payload))
    tmp_path.replace(path)


def read_all(project_root: Path, *, prune: bool = True) -> list[dict]:
    """Every live process's most recent snapshot. `prune=True` (default)
    also deletes entries whose pid is gone — self-healing after a hard
    kill, same posture as arc.events.list_processes()."""
    directory = _metrics_dir(project_root)
    if not directory.is_dir():
        return []
    out: list[dict] = []
    for path in sorted(directory.glob("*.json")):
        try:
            info = json.loads(path.read_text())
            pid = int(info["pid"])
        except (ValueError, KeyError, json.JSONDecodeError, OSError):
            if prune:
                with contextlib.suppress(OSError):
                    path.unlink()
            continue
        if not _pid_alive(pid):
            if prune:
                with contextlib.suppress(OSError):
                    path.unlink()
            continue
        out.append(info)
    return out


# --------------------------------------------------------------------------- #
# Duck-typed collection — mirrors arc.health.check()
# --------------------------------------------------------------------------- #
def collect(kernel: Any) -> dict[str, dict]:
    """One dict per capability that exposes a sync `metrics() -> dict`.
    A capability without one is silently skipped, exactly like
    arc.health.check() skips a capability with no health()."""
    results: dict[str, dict] = {}
    for name, cap in kernel.capabilities().items():
        metrics_fn = getattr(cap.instance, "metrics", None)
        if not callable(metrics_fn):
            continue
        try:
            results[name] = metrics_fn()
        except Exception as exc:
            results[name] = {"error": f"{exc.__class__.__name__}: {exc}"}
    return results


# --------------------------------------------------------------------------- #
# The exporter — a background task each process type starts once, right
# alongside its own install_process_bridge() call.
# --------------------------------------------------------------------------- #
_exporter_task: asyncio.Task | None = None
_exporter_pidfile: Path | None = None


def start_exporter(*, role: str, interval_seconds: float = 5.0) -> None:
    """Claim this process as a metrics source: writes this process's own
    .arc/runtime/metrics/<pid>.json every `interval_seconds`, overwriting
    it each tick — a stale file left by a crash only ever lingers until
    the next read_all() prunes it. Requires a running event loop and an
    active kernel. Idempotent — a second call is a no-op."""
    global _exporter_task, _exporter_pidfile
    if _exporter_task is not None and not _exporter_task.done():
        return
    kernel = _state.get_kernel()
    if kernel is None:
        raise MetricsError("start_exporter() requires arc.boot() first.")
    if kernel.project_root is None:
        return  # e.g. an in-memory test kernel with no project on disk — nothing to write to

    path = _metrics_dir(kernel.project_root) / f"{os.getpid()}.json"
    _exporter_pidfile = path

    async def _loop() -> None:
        while True:
            try:
                _write_snapshot(path, role, collect(kernel))
            except Exception:
                pass  # a bad write this tick must never crash the exporter loop itself
            await asyncio.sleep(interval_seconds)

    _exporter_task = asyncio.get_running_loop().create_task(_loop())


async def stop_exporter() -> None:
    """Tear the exporter down: cancel the writer loop, remove this
    process's own snapshot file. Safe to call even if start never ran."""
    global _exporter_task, _exporter_pidfile
    task, _exporter_task = _exporter_task, None
    if task is not None:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    if _exporter_pidfile is not None:
        with contextlib.suppress(OSError):
            _exporter_pidfile.unlink(missing_ok=True)
        _exporter_pidfile = None


# --------------------------------------------------------------------------- #
# Aggregation + Prometheus text-exposition formatting
# --------------------------------------------------------------------------- #
def aggregate(snapshots: list[dict]) -> dict[str, dict]:
    """Sum every live snapshot's numeric fields together, per capability,
    per field — the merge that turns N processes' own private numbers
    into one system-wide picture. A bool is never summed (e.g. a
    capability's own `"ok": True` from health()-shaped data leaking in)
    and non-numeric values (strings, lists) are skipped rather than
    guessed at — this is deliberately generic across whatever any
    capability's metrics() happens to return, not hardcoded to gateway's
    own field names."""
    merged: dict[str, dict[str, Any]] = {}
    for snap in snapshots:
        data = snap.get("data") or {}
        if not isinstance(data, dict):
            continue
        for capability, fields in data.items():
            if not isinstance(fields, dict):
                continue
            bucket = merged.setdefault(capability, {})
            for key, value in fields.items():
                if isinstance(value, bool):
                    continue
                if isinstance(value, (int, float)):
                    bucket[key] = bucket.get(key, 0) + value
                elif isinstance(value, dict):
                    # one level of nested counters (e.g. RequestMetrics'
                    # by-status-class breakdown) — sum leaf values the same way
                    sub = bucket.setdefault(key, {})
                    for sub_key, sub_value in value.items():
                        if isinstance(sub_value, (int, float)) and not isinstance(sub_value, bool):
                            sub[sub_key] = sub.get(sub_key, 0) + sub_value
    return merged


def _metric_type(field: str) -> str:
    # Prometheus naming convention: a field ending in _total/_sum only
    # ever increases across a process's lifetime — everything else
    # (pool sizes, queue depths) can go up or down, i.e. a gauge.
    return "counter" if field.endswith(("_total", "_sum")) else "gauge"


def format_prometheus(aggregated: dict[str, dict]) -> str:
    """Hand-rolled text-exposition format (# HELP / # TYPE / name value) —
    no `prometheus_client` dependency; the format is simple enough that
    pulling in a library would be more machinery than the problem needs,
    same "no forced third-party dependency" posture as everywhere else
    in this project."""
    lines: list[str] = []
    for capability in sorted(aggregated):
        fields = aggregated[capability]
        for field in sorted(fields):
            value = fields[field]
            metric_name = f"arc_{capability}_{field}"
            metric_type = _metric_type(field)
            if isinstance(value, dict):
                lines.append(f"# TYPE {metric_name} {metric_type}")
                for label_value in sorted(value):
                    sub_value = value[label_value]
                    safe_label = str(label_value).replace("\\", "\\\\").replace('"', '\\"')
                    lines.append(f'{metric_name}{{bucket="{safe_label}"}} {sub_value}')
            else:
                lines.append(f"# TYPE {metric_name} {metric_type}")
                lines.append(f"{metric_name} {value}")
    return "\n".join(lines) + "\n"
