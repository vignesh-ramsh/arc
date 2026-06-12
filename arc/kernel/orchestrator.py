"""
arc.kernel.orchestrator
=======================
``Arc`` — the micro-kernel orchestrator. It owns no domain logic: no SQL, no
HTTP, no auth. It only discovers plugins, resolves their graph, wires them,
runs their lifecycle, and exposes whatever an http plugin built.

build() (synchronous, before any event loop):
    1. locate arc.lock, inject project root into sys.path
    2. load arc.toml + configure logging
    3. import + instantiate every plugin
    4. resolve order (capabilities first, load_order tiebreak)
    5. setup pass     — every plugin registers its capabilities
    6. contribute pass — every plugin adds routes/cli/schemas (caps now ready)
    7. if some plugin provided 'http.app', fetch it and wire the lifespan

run():     build, then serve the ASGI app with uvicorn (lifespan-driven).
run_headless(): build, then run the lifecycle directly (worker/CLI apps).
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from arc.kernel.capability import Capabilities
from arc.kernel.config import ArcConfig, load_config
from arc.kernel.lifecycle import LifecycleManager
from arc.kernel.loader import PluginLoader
from arc.kernel.logger import configure_logging, get_logger
from arc.kernel.registry import ExtensionRegistry
from arc.kernel.resolver import ResolvedGraph, resolve
from arc.kernel.runtime import Runtime

log = get_logger(__name__)

# Capability name an http host plugin provides if HTTP serving is wanted.
HTTP_APP_CAPABILITY = "http.app"


class Arc:
    def __init__(
        self,
        *,
        lock_path: Path | None = None,
        config_path: Path | None = None,
        plugins: list | None = None,  # explicit plugins (tests) skip arc.lock
    ) -> None:
        self._lock_path = lock_path
        self._config_path = config_path
        self._explicit_plugins = plugins

        self.config: ArcConfig | None = None
        self.capabilities = Capabilities()
        self.extensions = ExtensionRegistry()
        self.graph: ResolvedGraph | None = None
        self.lifecycle: LifecycleManager | None = None
        self.runtime: Runtime | None = None
        self._asgi = None
        self._built = False

    # ── Build ──────────────────────────────────────────────────────────
    def build(self):
        if self._built:
            return self._asgi

        if self._explicit_plugins is None:
            loader = PluginLoader(self._lock_path)
            project_root = loader.project_root
            config_path = self._config_path or (project_root / "arc.toml")
            plugins = loader.load_all()
        else:
            project_root = Path.cwd()
            config_path = self._config_path
            plugins = list(self._explicit_plugins)

        self.config = load_config(config_path if config_path and config_path.exists() else None)
        configure_logging(
            level=self.config.log.level,
            renderer=self.config.log.renderer,
            include_timestamp=self.config.log.include_timestamp,
        )
        log.info("arc.build", app=self.config.app.name, plugins=len(plugins))

        # Resolve the graph (capabilities first, load_order tiebreak).
        self.graph = resolve(plugins)
        log.info("arc.graph.resolved", order=self.graph.names)

        self.lifecycle = LifecycleManager(self.graph.order)
        self.runtime = Runtime(
            config=self.config,
            capabilities=self.capabilities,
            extensions=self.extensions,
            lifecycle=self.lifecycle,
        )

        # Pass 1: setup — register provided capabilities.
        for p in self.graph.order:
            p.setup(self.runtime.scoped(p.name))
            log.debug("arc.plugin.setup", plugin=p.name)

        # Pass 2: contribute — add extensions (all capabilities now exist).
        for p in self.graph.order:
            p.contribute(self.runtime.scoped(p.name))
            log.debug("arc.plugin.contribute", plugin=p.name)

        # If an http host plugin offered an ASGI app, fetch it and wire lifespan.
        if self.capabilities.has(HTTP_APP_CAPABILITY):
            self._asgi = self.capabilities.require(HTTP_APP_CAPABILITY)
            self._wire_lifespan(self._asgi)

        self._built = True
        log.info("arc.build.complete", has_http=self._asgi is not None)
        return self._asgi

    def _wire_lifespan(self, app) -> None:
        """Attach the kernel lifecycle to a Starlette app's lifespan."""
        manager = self.lifecycle
        assert manager is not None

        @asynccontextmanager
        async def lifespan(_app):
            log.info("arc.startup")
            await manager.startup()
            log.info("arc.ready", started=len(manager.started))
            try:
                yield
            finally:
                log.info("arc.shutdown")
                try:
                    await manager.shutdown()
                except Exception as exc:  # logged, never re-raised on shutdown
                    log.error("arc.shutdown.error", error=str(exc))
                log.info("arc.stopped")

        # Starlette exposes router.lifespan_context.
        if hasattr(app, "router"):
            app.router.lifespan_context = lifespan

    # ── Serve ──────────────────────────────────────────────────────────
    def run(self, host: str = "127.0.0.1", port: int = 8000) -> None:
        app = self.build()
        if app is None:
            raise RuntimeError(
                "No http plugin provided 'http.app'. "
                "Use run_headless() for worker/CLI-only apps."
            )
        import uvicorn

        uvicorn.run(app, host=host, port=port)

    async def run_headless(self) -> None:
        """Run the lifecycle without an HTTP server (workers, schedulers)."""
        self.build()
        assert self.lifecycle is not None
        await self.lifecycle.startup()
        try:
            import asyncio

            await asyncio.Event().wait()  # block until cancelled
        finally:
            await self.lifecycle.shutdown()
