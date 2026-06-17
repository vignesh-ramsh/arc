"""
hrms.routes.employees
=====================
Employee hooks and EXTRA custom routes.

The resource JSON (resources/employee.json) already generates:
  GET    /api/v1/hrms/employees          list
  POST   /api/v1/hrms/employees          save (upsert)
  GET    /api/v1/hrms/employees/{id}     get one
  DELETE /api/v1/hrms/employees/{id}     soft delete

Do NOT re-declare those here — any duplicate path+method triggers
a DiscoveryError at startup. This file only adds hooks and
endpoints that go beyond what the JSON resource covers.
"""
from __future__ import annotations

from arc.kernel.logger import get_logger
from plugins.relay import arc, delete, get, hook, on_commit, on_rollback, post, stream

log = get_logger("hrms.employees")


# ─────────────────────────────────────────────────────────────────────────────
# PRE-COMMIT HOOKS  (in-transaction, raise = rollback)
# ─────────────────────────────────────────────────────────────────────────────

@hook("Employee", "validate")
async def validate_required(doc):
    doc.require("employee_id")
    doc.require("employee_name")


@hook("Employee", "validate")
async def validate_unique_employee_id(doc):
    eid = (doc.get("employee_id") or "").strip()
    if not eid:
        return
    existing = await arc.get("Employee", {"employee_id": eid})
    if existing and existing["id"] != doc.get("id"):
        doc.fail(f"employee_id '{eid}' is already in use.", field="employee_id")


@hook("Employee", ["before_insert", "before_update"])
async def normalise_employee(doc):
    if name := doc.get("employee_name"):
        doc.set("employee_name", name.strip())
    if eid := doc.get("employee_id"):
        doc.set("employee_id", eid.strip().upper())
    if dept := doc.get("department"):
        doc.set("department", dept.strip())


@hook("Employee", "before_insert")
async def default_department(doc):
    if not doc.get("department"):
        doc.set("department", "General")


@hook("Employee", "before_update")
async def lock_employee_id(doc):
    if doc.changed("employee_id"):
        doc.fail("employee_id cannot be changed after creation.", field="employee_id")


@hook("Employee", "before_delete")
async def guard_active_leave(doc):
    count = await arc.count("Leave", {"employee": doc.get("id")})
    if count:
        doc.fail(f"Cannot delete: {count} active leave record(s) exist.")


# ─────────────────────────────────────────────────────────────────────────────
# POST-COMMIT HOOKS  (background — never block the response)
# ─────────────────────────────────────────────────────────────────────────────

@hook("Employee", "on_change")
async def log_employee_change(doc):
    if doc.event == "insert":
        log.info("hrms.employee.created",
                 id=doc.get("id"), employee_id=doc.get("employee_id"))
    elif doc.event == "update":
        log.info("hrms.employee.updated",
                 id=doc.get("id"), employee_id=doc.get("employee_id"))
    elif doc.event == "delete":
        log.info("hrms.employee.deleted",
                 id=doc.get("id"), deleted_by=doc.user)


# ─────────────────────────────────────────────────────────────────────────────
# TRANSACTION HOOKS  (once per arc.tx() boundary)
# ─────────────────────────────────────────────────────────────────────────────

@on_commit
async def batch_committed(tx):
    count = tx.get("batch_count", 0)
    if count:
        log.info("hrms.employee.batch_committed", count=count)


@on_rollback
async def batch_rolled_back(tx):
    log.warning("hrms.employee.batch_rolled_back",
                error=str(tx.error) if tx.error else "unknown")


# ─────────────────────────────────────────────────────────────────────────────
# EXTRA CUSTOM ROUTES
# Only paths NOT already covered by resources/employee.json go here.
# These include the base path (/api/v1/...) because relay strips it
# automatically before the internal Starlette router sees it.
# ─────────────────────────────────────────────────────────────────────────────

@post(route="/api/v1/hrms/employees/bulk", roles=["Guest"])
async def bulk_save_employees(ctx):
    """Insert or update many employees. Per-row errors are collected."""
    rows = ctx.data.get("rows", [])
    saved, errors = [], []
    for i, row in enumerate(rows):
        try:
            saved.append(await arc.save("Employee", row))
        except Exception as exc:
            errors.append({"index": i, "detail": str(exc), "row": row})
    from starlette.responses import JSONResponse
    return JSONResponse(
        {"saved": len(saved), "data": saved, "errors": errors},
        status_code=207 if errors else 200,
    )


@post(route="/api/v1/hrms/employees/bulk-transfer", roles=["HR Manager"])
async def bulk_department_transfer(ctx):
    """Move all employees from one department to another atomically."""
    from_dept = ctx.data.get("from_department")
    to_dept   = ctx.data.get("to_department")
    if not from_dept or not to_dept:
        from starlette.responses import JSONResponse
        return JSONResponse(
            {"error": {"source": "request", "code": "bad_request",
                       "message": "from_department and to_department are required.",
                       "status": 400}},
            status_code=400,
        )
    async with arc.tx() as tx:
        employees = await arc.list(
            "Employee",
            fields=["id", "employee_name"],
            filters={"department": from_dept},
        )
        for emp in employees:
            await arc.save("Employee", {"id": emp["id"], "department": to_dept})
            tx.collect("transferred_ids", emp["id"])
        tx.set("batch_count", len(employees))

    return {
        "transferred": len(tx.collected("transferred_ids")),
        "from": from_dept,
        "to": to_dept,
    }


@post(route="/api/v1/hrms/employees/bulk-delete", roles=["HR Manager"])
async def bulk_delete_employees(ctx):
    """Soft-delete by id list. Per-row errors collected."""
    ids = ctx.data.get("ids", [])
    deleted, errors = [], []
    for emp_id in ids:
        try:
            row = await arc.rm("Employee", {"id": emp_id})
            if row is None:
                errors.append({"id": emp_id, "detail": "not found"})
            else:
                deleted.append(emp_id)
        except Exception as exc:
            errors.append({"id": emp_id, "detail": str(exc)})
    from starlette.responses import JSONResponse
    return JSONResponse(
        {"deleted": len(deleted), "errors": errors},
        status_code=207 if errors else 200,
    )


@delete(route="/api/v1/hrms/departments/{dept}/employees", roles=["HR Manager"])
async def delete_department_employees(ctx):
    """Soft-delete ALL employees in a department. Hard cap: 1000."""
    count = await arc.rm_many(
        "Employee",
        {"department": ctx.params["dept"]},
        order="-created_at",
    )
    return {"deleted_count": count, "department": ctx.params["dept"]}


@stream(route="/api/v1/hrms/employees/export", roles=["HR Manager"])
async def export_employees(ctx):
    """NDJSON stream export. arc.query is raw SELECT — _state not auto-filtered."""
    dept_filter = ""
    params: dict = {}
    if dept := ctx.query.get("department"):
        dept_filter = 'AND e."department" = :dept'
        params["dept"] = dept

    rows = await arc.query(
        f"""
        SELECT e.id, e.employee_id, e.employee_name,
               e.department, e.date_of_joining
        FROM   "Employee" e
        WHERE  e."_state" != 99
               {dept_filter}
        ORDER  BY e.date_of_joining DESC
        """,
        params or None,
    )
    for row in rows:
        yield row