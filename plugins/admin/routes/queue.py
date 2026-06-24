"""
plugins.admin.routes.queue
==========================
Queue management against the real redix ``queue.client`` (Redis Streams).

What redix actually exposes shapes this panel:
  • Jobs flow through Redis Streams + a consumer group. Once the worker picks a
    job up it is not individually enumerable, and there is NO cancel primitive.
  • The only enumerable per-job list is the DEAD-LETTER queue.
  • Global visibility is COUNTS: pending entries per priority stream + dead count.
  • A specific job's status is reachable by id via ``result(job_id)`` (TTL'd).

So the panel is: a status summary + a dead-letter table with Retry / Purge.

  GET  /api/v1/admin/queue/status              {streams:{high,default,low}, dead, available}
  GET  /api/v1/admin/queue/dead?limit=50       dead-letter envelopes (the listable jobs)
  POST /api/v1/admin/queue/dead/{job_id}/retry requeue one dead job
  POST /api/v1/admin/queue/dead/purge          clear the dead-letter queue
  GET  /api/v1/admin/queue/jobs/{job_id}       one job's status record (by id)
  GET  /api/v1/admin/queue/tasks               registered @relay.task names
  POST /api/v1/admin/queue/enqueue             enqueue {task, kwargs?, priority?}

When redix is absent, ``admin_ctx.queue`` is None: read routes return
``{"available": false}`` and write routes 400 — relay's inline fallback has no
durable jobs to manage.
"""

from __future__ import annotations

from plugins.relay import get, post, BadParam, NotFoundError

from plugins.admin import admin_ctx
from plugins.admin.guard import require_admin

_DEAD_LIST_CAP = 200
_VALID_PRIORITIES = ("high", "default", "low")


@get("/api/v1/admin/queue/status")
async def queue_status(ctx):
    require_admin(ctx)
    q = admin_ctx.queue
    if q is None:
        return {
            "available": False,
            "detail": "redix queue.client is not installed; the queue runs in "
                      "relay's inline fallback with no durable jobs to manage.",
        }
    st = await q.status()
    return {"available": True, **st}


@get("/api/v1/admin/queue/dead")
async def queue_dead(ctx):
    require_admin(ctx)
    q = admin_ctx.queue
    if q is None:
        return {"available": False, "jobs": []}
    try:
        limit = int(ctx.query.get("limit", 50))
    except (TypeError, ValueError):
        limit = 50
    limit = max(1, min(limit, _DEAD_LIST_CAP))
    jobs = await q.dead_list(limit=limit)
    return {"available": True, "jobs": jobs}


@post("/api/v1/admin/queue/dead/{job_id}/retry")
async def queue_dead_retry(ctx):
    require_admin(ctx)
    q = admin_ctx.queue
    if q is None:
        raise BadParam("redix queue.client is not available.")
    job_id = ctx.params["job_id"]
    ok = await q.dead_retry(job_id)
    if not ok:
        raise NotFoundError(f"No dead job with id {job_id!r}.")
    return {"job_id": job_id, "requeued": True}


@post("/api/v1/admin/queue/dead/purge")
async def queue_dead_purge(ctx):
    require_admin(ctx)
    q = admin_ctx.queue
    if q is None:
        raise BadParam("redix queue.client is not available.")
    purged = await q.dead_purge()
    return {"purged": purged}


@get("/api/v1/admin/queue/jobs/{job_id}")
async def queue_job(ctx):
    require_admin(ctx)
    q = admin_ctx.queue
    if q is None:
        return {"available": False}
    job_id = ctx.params["job_id"]
    rec = await q.result(job_id)
    if rec is None:
        raise NotFoundError(
            f"No job record for {job_id!r} (unknown id or result expired).")
    return {"available": True, "job_id": job_id, **rec}


@get("/api/v1/admin/queue/tasks")
async def queue_tasks(ctx):
    require_admin(ctx)
    from plugins.relay import relay as registrar
    return {"tasks": list(registrar.task_names())}


@post("/api/v1/admin/queue/enqueue")
async def queue_enqueue(ctx):
    require_admin(ctx)
    q = admin_ctx.queue
    if q is None:
        raise BadParam(
            "redix queue.client is not available; cannot enqueue a durable job.")
    body = ctx.data if isinstance(ctx.data, dict) else {}
    task = (body.get("task") or "").strip()
    if not task:
        raise BadParam("task is required.")
    kwargs = body.get("kwargs") or {}
    if not isinstance(kwargs, dict):
        raise BadParam("kwargs must be a JSON object.")
    priority = body.get("priority") or "default"
    if priority not in _VALID_PRIORITIES:
        raise BadParam(f"priority must be one of {_VALID_PRIORITIES}.")
    job_id = await q.enqueue(task, priority=priority, **kwargs)
    return {"job_id": job_id, "task": task, "priority": priority}
