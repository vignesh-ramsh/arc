"""
arc.runtime
------------------
`arc.boot()` — the runtime counterpart to the CLI tooling. The CLI mutates the
project (install/build/enable); boot() only ever reads it.

Boot contract (Architecture §3.6):
  * runs at EVERY process start,
  * read-only — never mutates plugins.lock, settings, or anything on disk,
  * fully offline — no git, no PyPI,
  * safe to run on every replica simultaneously,
  * idempotent within a process — a second boot() is a no-op returning the
    same Kernel; pass force=True to rebuild (tests, embedding).

Sequence:
  1. locate the project root: explicit argument > $ARC_PROJECT_ROOT > walk up
     from cwd looking for .arc/arc.toml;
  2. read .arc/plugins.lock (source of truth for what is enabled) and the
     installed `arc.plugins` entry points;
  3. resolve a BootPlan (arc.resolver — pure; the same function powers
     `arc doctor` as a dry run);
  4. bind this project's SettingsManager onto the Kernel, making the
     module-level arc.settings API live (§3.5);
  5. call each plugin's register(kernel) in topological order — the active
     kernel is published *before* execution, so a plugin may use
     arc.<earlier_capability> and arc.settings inside register();
  6. every exported capability becomes reachable as arc.<name> via the
     package's PEP 562 __getattr__.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any, Iterable

from . import _state
from .kernel import Kernel, KernelError
from .registry import load_lock
from .resolver import PluginSpec, discover_entry_points, resolve
from .settings import SettingsManager


class BootError(KernelError):
    """arc.boot() could not start the process."""


_boot_lock = threading.Lock()


def find_project_root(start: Path | None = None) -> Path | None:
    """
    Locate the ARC project root. $ARC_PROJECT_ROOT wins when set (and is
    validated loudly — explicit configuration that is wrong should never be
    silently ignored); otherwise walk up from `start`/cwd looking for
    .arc/arc.toml. Returns None when no project is found.
    """
    env_root = os.environ.get("ARC_PROJECT_ROOT")
    if env_root:
        root = Path(env_root).expanduser().resolve()
        if not (root / ".arc" / "arc.toml").exists():
            raise BootError(
                f"$ARC_PROJECT_ROOT is set to '{root}', but no .arc/arc.toml "
                f"exists there — fix or unset the variable."
            )
        return root

    current = (start or Path.cwd()).resolve()
    for candidate in (current, *current.parents):
        if (candidate / ".arc" / "arc.toml").exists():
            return candidate
    return None


def boot(
    project_root: str | Path | None = None,
    *,
    force: bool = False,
    entry_points: Iterable[Any] | None = None,
) -> Kernel:
    """
    Boot the ARC runtime for this process and return the Kernel.

    `entry_points` overrides installed-entry-point discovery — for tests and
    embedding; production callers never pass it.
    """
    with _boot_lock:
        existing = _state.get_kernel()
        if existing is not None:
            if not force:
                return existing
            _state.set_kernel(None)

        if project_root is not None:
            root = Path(project_root).expanduser().resolve()
            if not (root / ".arc" / "arc.toml").exists():
                raise BootError(
                    f"project_root was given as '{root}', but no .arc/arc.toml "
                    f"exists there — is this really an ARC project? "
                    f"Run `arc init` first."
                )
        else:
            root = find_project_root()
            if root is None:
                raise BootError(
                    "Not inside an ARC project — no .arc/arc.toml found in the "
                    "current directory or any parent. Run from within a project, "
                    "pass boot(project_root=...), or set $ARC_PROJECT_ROOT."
                )

        lock_doc = load_lock(root / ".arc" / "plugins.lock")
        eps = tuple(entry_points) if entry_points is not None else discover_entry_points()
        plan = resolve(root, lock_doc=lock_doc, entry_points=eps)

        settings_manager = SettingsManager(root / ".arc")
        kernel = Kernel(project_root=root, settings=settings_manager, plan=plan)

        # Before anything else, including the plugin registration loop below
        # — so any logging.getLogger(...) call a plugin's own register()
        # makes is already captured. (kernel.advise() itself is a separate,
        # existing mechanism — Python's warnings module, not logging — and
        # is untouched by this.) See arc.log's own docstring for why root-
        # logger configuration lives here rather than in each plugin/CLI
        # entrypoint separately.
        from . import log as _log

        _log.configure(kernel)

        for warning in plan.warnings:
            kernel.advise(warning)
        if settings_manager.secrets_provider() == "local_file":
            # §3.5: fully supported tier for dev/self-hosted — advisory, never an error.
            kernel.advise(
                "local-file secrets provider in use (.arc/arc.secrets encrypted "
                "with .arc/arc.mkey) — a supported tier for dev and self-hosted "
                "deployments. For managed environments, consider a keyring/KMS "
                "provider via [secrets].provider in .arc/arc.toml. Advisory only; "
                "startup is never blocked."
            )

        # Publish BEFORE executing registers: topological order means a plugin
        # may legitimately use arc.<earlier_capability> and arc.settings inside
        # its own register(). Any failure below tears this back down.
        _state.set_kernel(kernel)
        try:
            for spec in plan.load_order:
                register = _load_register(spec)
                kernel._begin_plugin(spec)
                try:
                    register(kernel)
                except KernelError:
                    raise
                except Exception as exc:
                    raise BootError(
                        f"plugin '{spec.name}' raised during register(): "
                        f"{exc.__class__.__name__}: {exc}"
                    ) from exc
                kernel._finish_plugin(spec)
        except BaseException:
            _state.set_kernel(None)
            raise
        return kernel


def shutdown() -> None:
    """
    Clear the process-wide kernel so the next boot() starts fresh. Primarily
    for tests and embedding. No lifecycle/teardown hooks are invoked — that
    surface belongs to the future arc.health/lifecycle design, not here.
    """
    _state.set_kernel(None)


def _load_register(spec: PluginSpec) -> Any:
    """Import the plugin's entry point and return its register(kernel) callable."""
    ep = spec.entry_point
    if ep is None:  # defensive: resolve() always attaches one for enabled specs
        raise BootError(
            f"plugin '{spec.name}' has no entry point attached — internal "
            f"resolution error."
        )
    try:
        target = ep.load()
    except Exception as exc:
        raise BootError(
            f"could not import plugin '{spec.name}' "
            f"(entry point '{getattr(ep, 'value', '?')}'): "
            f"{exc.__class__.__name__}: {exc}"
        ) from exc
    if callable(target):
        return target
    register = getattr(target, "register", None)
    if callable(register):
        return register
    raise BootError(
        f"the entry point for plugin '{spec.name}' must reference a callable "
        f"register(kernel) function (e.g. \"{spec.name} = 'yourpkg:register'\") "
        f"or a module defining one — got {type(target).__name__}."
    )