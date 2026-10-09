"""Customer notifications. Non-critical: callers treat failures as best-effort."""
import random
import time

from pydantic import BaseModel

from common.base import make_app

ctx = make_app("notifications")
app = ctx.app


class Notify(BaseModel):
    order_id: int


@app.post("/notify")
def notify(body: Notify):
    time.sleep(random.uniform(0.003, 0.01))
    ctx.logger.info("notification sent", extra={"event": "notified", "order_id": body.order_id})
    return {"order_id": body.order_id, "sent": True}
