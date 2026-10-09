"""Payment capture (simulated processor + a Postgres ledger)."""
import random
import time

from pydantic import BaseModel

from common.base import make_app

ctx = make_app("payments", use_db=True)
app = ctx.app


class Charge(BaseModel):
    order_id: int
    amount: float


@app.post("/charge")
def charge(body: Charge):
    time.sleep(random.uniform(0.005, 0.02))  # simulated processor round-trip
    with ctx.db.conn() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO payments (order_id, amount, status) VALUES (%s, %s, 'captured') RETURNING id",
            (body.order_id, body.amount),
        )
        payment_id = cur.fetchone()[0]
    ctx.logger.info(
        "payment captured", extra={"fields": {"event": "payment_captured", "order_id": body.order_id}}
    )
    return {"payment_id": payment_id, "status": "captured"}
