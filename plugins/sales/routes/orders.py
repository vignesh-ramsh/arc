"""
sales.routes.orders
===================
Hooks + CUSTOM routes for Order.

The resource (resources/order.json) already auto-generates the standard CRUD:
    GET    /api/v1/sales/orders            list   (filter/sort/paginate/?q=)
    GET    /api/v1/sales/orders/{id}       get one
    POST   /api/v1/sales/orders            save   (upsert by order_no)
    PATCH  /api/v1/sales/orders/{id}       update (existing only)
    DELETE /api/v1/sales/orders/{id}       soft delete

Anything below is what the declarative resource CANNOT express: business
transactions, bulk operations, custom shapes. Custom routes use ABSOLUTE paths
and must NOT duplicate a generated (method, path) — that is a DiscoveryError.
Passing table="Order" tags each route so `arc relay routes -t Order` finds it.
"""

from __future__ import annotations

import datetime as dt

from arc.kernel.logger import get_logger
from plugins.relay import arc, delete, get, hook, on_commit, patch, post

log = get_logger("sales.orders")

ROLES = ["Sales Rep", "Sales Admin"]


# ═════════════════════════════════════════════════════════════════════════════
# HOOKS  (table-scoped; fire for BOTH resource and custom writes to "Order")
# ═════════════════════════════════════════════════════════════════════════════

@hook("Order", "validate")
async def validate_order(doc):
    doc.require("order_no")
    doc.require("customer")
    doc.require("product")
    if (doc.get("quantity") or 0) <= 0:
        doc.fail("quantity must be positive.", field="quantity")


@hook("Order", ["before_insert", "before_update"])
async def normalise_order(doc):
    if no := doc.get("order_no"):
        doc.set("order_no", no.strip().upper())
    if (status := doc.get("status")):
        doc.set("status", status.strip().lower())


@hook("Order", "before_insert")
async def default_status(doc):
    if not doc.get("status"):
        doc.set("status", "draft")
    if not doc.get("placed_at"):
        doc.set("placed_at", dt.datetime.now(dt.timezone.utc).isoformat())


@hook("Order", "before_update")
async def lock_order_no(doc):
    # order_no is the business key — never allow it to change on update.
    if doc.changed("order_no"):
        doc.fail("order_no cannot be changed after creation.", field="order_no")


@hook("Order", "after_insert")
async def log_new_order(doc):
    log.info("sales.order_created", order_no=doc.get("order_no"), id=doc.get("id"))


@hook("Order", "on_change")          # POST-COMMIT, per doc, runs in the background
async def audit_order_change(doc):
    # e.g. write to an audit table / emit an event. Reads here see COMMITTED state.
    log.info("sales.order_changed", event=doc.event, order_no=doc.get("order_no"))


@on_commit                            # POST-COMMIT, per transaction (global)
async def after_any_commit(tx):
    # Fires once per committed transaction, regardless of table.
    pass


# ═════════════════════════════════════════════════════════════════════════════
# CUSTOM READ ROUTES  — arc.list / arc.get / arc.count / arc.exists / arc.aggregate / arc.query
# ═════════════════════════════════════════════════════════════════════════════

@get("/api/v1/sales/orders/recent", roles=ROLES, table="Order")
async def recent_orders(ctx):
    """List + count in one shape the resource can't return.
    arc.list(search=...) is the ?q= ILIKE-OR; arc.count is unfiltered total."""
    rows = await arc.list(
        "Order",
        fields=["order_no", "customer", "status", "order_date"],
        filters=[("status", "in", ["draft", "confirmed"])],
        order="-order_date",
        limit=20,
        search=(["order_no"], ctx.query.get("q", "")) if ctx.query.get("q") else None,
    )
    total = await arc.count("Order", {"status": "confirmed"})
    return {"recent": rows, "confirmed_total": total}


@get("/api/v1/sales/orders/by-no/{order_no}", roles=ROLES, table="Order")
async def order_by_business_key(ctx):
    """arc.get by the business key (the resource item route only does /{id})."""
    row = await arc.get("Order", {"order_no": ctx.params["order_no"].upper()})
    if row is None:
        from plugins.relay import NotFoundError
        raise NotFoundError("order not found")
    return row


@get("/api/v1/sales/customers/{id}/stats", roles=ROLES, table="Order")
async def customer_stats(ctx):
    """arc.exists, arc.aggregate, and a raw read-only arc.query join."""
    cid = ctx.params["id"]
    has_orders = await arc.exists("Order", {"customer": cid})
    revenue = await arc.aggregate("Order", fn="sum", field="unit_price",
                                  where={"customer": cid, "status": "confirmed"})
    # arc.query: read-only, parameterised; for joins/dashboards the doc API can't do.
    by_status = await arc.query(
        'SELECT status, COUNT(*) AS n FROM "Order" '
        'WHERE customer = :cid AND "_state" != 99 GROUP BY status',
        {"cid": cid},
    )
    return {"has_orders": has_orders, "revenue": revenue, "by_status": by_status}


# ═════════════════════════════════════════════════════════════════════════════
# CUSTOM WRITE ROUTES  — arc.save / arc.update / arc.save_many / arc.update_many / arc.tx
# ═════════════════════════════════════════════════════════════════════════════

@post("/api/v1/sales/orders/place", roles=ROLES, table="Order")
async def place_order(ctx):
    """A real transaction across two tables — the resource POST can't do this.
    arc.tx() groups everything into ONE commit; a raise rolls it ALL back."""
    data = ctx.data
    async with arc.tx():
        product = await arc.get("Product", {"id": data["product"]})
        if product is None:
            from plugins.relay import BadParam
            raise BadParam("unknown product")
        if product["stock_qty"] < data["quantity"]:
            from plugins.relay import ValidationError
            raise ValidationError("insufficient stock", field="quantity")

        # decrement stock (update existing only)
        await arc.update("Product", {"id": product["id"]},
                         {"stock_qty": product["stock_qty"] - data["quantity"]})

        # create the order (upsert by order_no; here it's a fresh insert)
        order = await arc.save("Order", {
            "order_no": data["order_no"],
            "customer": data["customer"],
            "product": data["product"],
            "quantity": data["quantity"],
            "unit_price": product["price"],
            "status": "confirmed",
            "order_date": data.get("order_date") or dt.date.today().isoformat(),
        }, match_on=["order_no"])
    return {"placed": order}


@post("/api/v1/sales/orders/bulk", roles=ROLES, table="Order")
async def bulk_import(ctx):
    """arc.save_many — per-row upserts. atomic=False returns {saved, errors} so a
    bad row doesn't sink the whole import. body: {"rows": [ {...}, {...} ]}"""
    rows = ctx.data.get("rows", [])
    result = await arc.save_many("Order", rows, match_on=["order_no"], atomic=False)
    return result


@patch("/api/v1/sales/orders/status", roles=ROLES, table="Order")
async def bulk_set_status(ctx):
    """arc.update_many — ONE filter, MANY rows. body: {"from":"draft","to":"confirmed"}"""
    n = await arc.update_many(
        "Order",
        {"status": ctx.data["from"]},
        {"status": ctx.data["to"]},
    )
    return {"updated": n}


@delete("/api/v1/sales/orders/cancel-drafts", roles=["Sales Admin"], table="Order")
async def cancel_drafts(ctx):
    """arc.rm_many — soft-delete (sets _state=99) every matching draft."""
    n = await arc.rm_many("Order", {"status": "draft"}, limit=500)
    return {"cancelled": n}