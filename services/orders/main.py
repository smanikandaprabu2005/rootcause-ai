"""Order orchestration: reserves stock, takes payment, notifies the customer."""
import os

from pydantic import BaseModel

from common.base import make_app

ctx = make_app("orders", use_db=True)
app = ctx.app

INVENTORY_URL = os.getenv("INVENTORY_URL", "http://inventory:8000")
PAYMENTS_URL = os.getenv("PAYMENTS_URL", "http://payments:8000")
NOTIFICATIONS_URL = os.getenv("NOTIFICATIONS_URL", "http://notifications:8000")


class OrderIn(BaseModel):
    item_id: int
    qty: int = 1


@app.post("/orders")
def create_order(order: OrderIn):
    # Short transactions on purpose: no connection is held across upstream calls.
    with ctx.db.conn() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO orders (item_id, qty, status) VALUES (%s, %s, 'pending') RETURNING id",
            (order.item_id, order.qty),
        )
        order_id = cur.fetchone()[0]

    reserved = ctx.upstream(
        "inventory", "POST", f"{INVENTORY_URL}/reserve", json={"item_id": order.item_id, "qty": order.qty}
    )
    amount = round(reserved["price"] * order.qty, 2)
    ctx.upstream("payments", "POST", f"{PAYMENTS_URL}/charge", json={"order_id": order_id, "amount": amount})

    with ctx.db.conn() as conn, conn.cursor() as cur:
        cur.execute("UPDATE orders SET status = 'confirmed' WHERE id = %s", (order_id,))

    try:  # notification is best-effort and must not fail the order
        ctx.upstream("notifications", "POST", f"{NOTIFICATIONS_URL}/notify", json={"order_id": order_id})
    except Exception:
        ctx.logger.warning(
            "order confirmed but notification failed",
            extra={"event": "notify_skipped", "order_id": order_id},
        )
    ctx.logger.info("order confirmed", extra={"event": "order_confirmed", "order_id": order_id})
    return {"order_id": order_id, "status": "confirmed", "amount": amount}
