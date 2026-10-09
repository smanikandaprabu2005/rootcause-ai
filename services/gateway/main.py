"""Public entry point. Fans requests out to orders and inventory."""
import os

from pydantic import BaseModel

from common.base import make_app

ctx = make_app("gateway")
app = ctx.app

ORDERS_URL = os.getenv("ORDERS_URL", "http://orders:8000")
INVENTORY_URL = os.getenv("INVENTORY_URL", "http://inventory:8000")


class OrderIn(BaseModel):
    item_id: int
    qty: int = 1


@app.get("/products")
def products():
    return ctx.upstream("inventory", "GET", f"{INVENTORY_URL}/items")


@app.post("/orders")
def place_order(order: OrderIn):
    return ctx.upstream("orders", "POST", f"{ORDERS_URL}/orders", json=order.model_dump())
