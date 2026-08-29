"""
arc.tracing
------------------
OpenTelemetry-backed distributed tracing. Unlike arc.metrics (Prometheus's
plain text-exposition format was simple enough to hand-roll and skip a
dependency), this uses the real `opentelemetry-api`/`opentelemetry-sdk`/
`opentelemetry-exporter-otlp-proto-http` packages — OTLP's wire format and
W3C Trace Context propagation are genuinely complex specs, and the only
way a span is useful at all is if a real trace backend (Jaeger, Tempo,
Honeycomb, Datadog — anything OTLP-speaking) can already read it; a
hand-rolled format would also need ARC to build its own viewer.

`request_id` (gateway.middleware.request_id_middleware) is NOT reused as
the OTel trace_id — it's already deeply embedded elsewhere (job logs,
access logs, the X-Request-ID response header, CallContext) and doesn't
share OTel's strict 128-bit format. A real trace_id/span_id runs
alongside it; `arc.request_id` is set as a span ATTRIBUTE on the root
span, so pivoting from a log line to a trace viewer means searching that
attribute, not unifying two ID schemes.

Process-lifecycle-bound, exactly like arc.events.install_process_bridge():
`start_exporter(role=...)`/`stop_exporter()` are called from the same
three call sites — gateway workers, lineup workers, the lineup scheduler
— never from arc.boot() itself, and deliberately never from a one-off CLI
invocation (`arc perform`/`arc console`/any other short-lived command).
OTel's BatchSpanProcessor batches spans in memory and flushes on its own
background thread; a process that exits quickly could lose its own spans
before they're ever flushed, and CLI tracing isn't worth an explicit
flush-on-exit story at every CLI entry point for this pass — same
reasoning install_process_bridge's own docstring already gives for
itself ("a one-off CLI invocation shouldn't leave a background task...
behind it"). `arc.tracing.span()` is still safe to call from CLI code —
it just cleanly no-ops there, since no CLI entry point ever calls
start_exporter().

Fully OFF (tracing_otlp_endpoint unset, the default) means no
TracerProvider, no exporter, no background thread is ever constructed —
not just "constructed but idle." Every framework instrumentation point
(gateway's tracing_middleware, relay's call wrap, pgdb's query hook)
checks get_tracer() first and does nothing else at all when it's None.

Every ARC-generated span name and custom attribute carries an `arc.`
prefix, distinguishing this project's own instrumentation from anything
else that might land in the same trace in a shared multi-service
backend. Standard OTel semantic-convention attributes (http.method,
db.statement, ...) are deliberately NOT prefixed, so existing OTel-aware
tooling that already knows those names keeps working unmodified.

    import arc
    arc.boot()
    arc.tracing.start_exporter(role="gateway-worker")  # long-running processes only
    with arc.tracing.span("hrms.payroll_calc", employee_id=123):
        ...
"""

from __future__ import annotations

import contextlib
from typing import Any, Iterator

from . import _state
from .kernel import KernelError
from .settings import SettingsError

OTLP_ENDPOINT_KEY = "tracing_otlp_endpoint"
SAMPLE_RATE_PERCENT_KEY = "tracing_sample_rate_percent"

#: The two labels context_labels()/use_context_labels() (relay/__init__.py)
#: add alongside their own CallContext ones, for a background job's span to
#: continue as a child of the request that enqueued it, even in a
#: different process, possibly after that request has already responded.
#: Owned here (not relay/ambient.py's _CTX_LABEL_* constants) since this is
#: purely a tracing concern, not part of CallContext's own "who/what
#: request" shape — relay just calls current_trace_labels()/continue_trace()
#: generically, the same "relay owns the wire format, doesn't need to know
#: what's inside" posture context_labels()'s own docstring already states.
_LABEL_TRACE_ID = "arc_trace_id"
_LABEL_PARENT_SPAN_ID = "arc_trace_parent_span_id"


class TracingError(KernelError):
    pass


_tracer_provider: Any = None  # opentelemetry.sdk.trace.TracerProvider | None
_tracer: Any = None  # opentelemetry.trace.Tracer | None


def declare(kernel: Any) -> None:
    """Called once, early in arc.boot() (mirrors arc.tz.declare(kernel) —
    runtime.py's own comment: "declared before the plugin loop... so every
    plugin's own register() can already call [it], regardless of load
    order") so every process, including one that never calls
    start_exporter(), can still read/list these settings."""
    kernel.settings.declare(
        OTLP_ENDPOINT_KEY,
        default="",
        doc="OTLP HTTP collector endpoint (e.g. http://localhost:4318/v1/traces). "
        "Empty/unset (default) means tracing is fully off — no TracerProvider, "
        "no exporter, no background thread is ever created, in any process.",
    )
    kernel.settings.declare(
        SAMPLE_RATE_PERCENT_KEY,
        type=int,
        default=100,
        doc="Percentage (0-100) of traces sampled once tracing is on at all. "
        "Applied once at the root span (gateway ingress); every child span "
        "in the same trace (relay call, DB query, background job) inherits "
        "that one decision — never independently re-sampled per span.",
    )


def validate_sample_rate(kernel: Any) -> None:
    """Eager 0-100 range check — same "fail at boot, not at tracer-
    construction time" reasoning as arc.tz.server_timezone()'s own eager
    IANA-name check (runtime.py). declare(type=int) above already
    guarantees this parses as an int; it does NOT enforce a range, so
    that part needs its own explicit check, called from the same place
    runtime.py already calls validate_declared()."""
    percent = kernel.settings.get(SAMPLE_RATE_PERCENT_KEY)
    if not 0 <= percent <= 100:
        raise SettingsError(
            f"'{SAMPLE_RATE_PERCENT_KEY}' must be between 0 and 100, got {percent}."
        )


def get_tracer() -> Any:
    """This process's active tracer, or None when tracing is off or this
    process never called start_exporter(). Every framework instrumentation
    point checks this first and does nothing else at all when it's None —
    the single source of truth for "is tracing active right now"."""
    return _tracer


def start_exporter(*, role: str) -> None:
    """Construct this process's TracerProvider + OTLP HTTP exporter +
    BatchSpanProcessor + TraceIdRatioBased sampler, gated entirely on
    tracing_otlp_endpoint being set. Idempotent — a second call is a
    no-op. Requires an active kernel (reads settings from it) but, unlike
    arc.metrics.start_exporter()/install_process_bridge(), needs NO
    running event loop — OTel's own export machinery uses its own
    background thread, not an asyncio task. Safe to call before OR after
    _open_all_capabilities() — pgdb's own query-logger hook checks
    get_tracer() fresh on every query rather than once at registration
    time, specifically so it never depends on this ordering."""
    global _tracer_provider, _tracer
    if _tracer is not None:
        return
    kernel = _state.get_kernel()
    if kernel is None:
        raise TracingError("start_exporter() requires arc.boot() first.")
    endpoint = kernel.settings.get(OTLP_ENDPOINT_KEY)
    if not endpoint:
        return  # tracing fully off — no objects constructed at all

    from opentelemetry import trace
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    from opentelemetry.sdk.trace.sampling import TraceIdRatioBased

    validate_sample_rate(kernel)
    percent = kernel.settings.get(SAMPLE_RATE_PERCENT_KEY)

    provider = TracerProvider(
        resource=Resource.create({"service.name": _project_name(kernel), "arc.role": role}),
        sampler=TraceIdRatioBased(percent / 100.0),
    )
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
    trace.set_tracer_provider(provider)
    _tracer_provider = provider
    _tracer = trace.get_tracer("arc")


def stop_exporter() -> None:
    """Flush and shut down this process's TracerProvider — blocking, but
    bounded (OTel's own shutdown() has an internal timeout), so pending
    batched spans get a real chance to export before process exit rather
    than being silently dropped. Safe to call even if start_exporter()
    never actually constructed one (tracing was off) or was never called
    at all."""
    global _tracer_provider, _tracer
    provider, _tracer_provider = _tracer_provider, None
    _tracer = None
    if provider is not None:
        provider.shutdown()


def _project_name(kernel: Any) -> str:
    try:
        import tomlkit

        toml_path = kernel.project_root / ".arc" / "arc.toml"
        doc = tomlkit.parse(toml_path.read_text())
        name = doc.get("project", {}).get("name")
        if name:
            return str(name)
    except Exception:  # noqa: BLE001 - a resource attribute must never break boot
        pass
    return "arc"


@contextlib.contextmanager
def span(name: str, **attributes: Any) -> Iterator[Any]:
    """`with arc.tracing.span("hrms.payroll_calc", employee_id=123):` — the
    plugin-facing primitive, safe to call from ANYWHERE including a
    process that never called start_exporter() (a CLI command, a test) —
    a clean no-op, no real span object, when get_tracer() is None. `name`
    gets the `arc.` prefix automatically if the caller didn't already
    include one, so a plugin author never needs to remember the
    convention by hand."""
    tracer = get_tracer()
    if tracer is None:
        yield None
        return
    full_name = name if name.startswith("arc.") else f"arc.{name}"
    with tracer.start_as_current_span(full_name, attributes=attributes) as otel_span:
        yield otel_span


# --------------------------------------------------------------------------- #
# Cross-process propagation — the receiving half relay/__init__.py's
# context_labels()/use_context_labels() delegate to, so a background job's
# own span continues as a child of the request that enqueued it.
# --------------------------------------------------------------------------- #
def current_trace_labels() -> dict[str, str]:
    """The current span's trace_id/span_id as flat string labels, for a
    durable queue that has to carry them to another PROCESS — mirrors
    relay's own context_labels() shape exactly (plain strings only,
    `{}` when there's no active span, so a caller can skip labelling
    entirely). Returns `{}` whenever tracing is off, same as an empty
    CallContext."""
    tracer = get_tracer()
    if tracer is None:
        return {}
    from opentelemetry import trace

    current_span = trace.get_current_span()
    ctx = current_span.get_span_context()
    if not ctx.is_valid:
        return {}
    return {
        _LABEL_TRACE_ID: format(ctx.trace_id, "032x"),
        _LABEL_PARENT_SPAN_ID: format(ctx.span_id, "016x"),
    }


@contextlib.contextmanager
def continue_trace(labels: Any) -> Iterator[None]:
    """Decode labels produced by current_trace_labels() and bind them as
    the active trace context for the duration of this block — the
    receiving half, called by a `lineup` worker around the job it's
    about to run (mirrors relay's own use_context_labels()). A clean
    no-op when tracing is off, `labels` carries no trace_id (a job
    enqueued before this existed, or a CLI-triggered one — see the
    CLI-exclusion note above), or the labels are malformed — a job's
    provenance metadata must never fail the job itself."""
    tracer = get_tracer()
    trace_id_hex = (labels or {}).get(_LABEL_TRACE_ID)
    span_id_hex = (labels or {}).get(_LABEL_PARENT_SPAN_ID)
    if tracer is None or not trace_id_hex or not span_id_hex:
        yield
        return
    try:
        from opentelemetry import context as otel_context
        from opentelemetry import trace
        from opentelemetry.trace import NonRecordingSpan, SpanContext, TraceFlags

        parent_ctx = SpanContext(
            trace_id=int(trace_id_hex, 16),
            span_id=int(span_id_hex, 16),
            is_remote=True,
            trace_flags=TraceFlags(TraceFlags.SAMPLED),
        )
        parent = trace.set_span_in_context(NonRecordingSpan(parent_ctx))
        token = otel_context.attach(parent)
    except Exception:  # noqa: BLE001 - malformed provenance must never fail the job
        yield
        return
    try:
        with span("relay.background_job"):
            yield
    finally:
        otel_context.detach(token)
