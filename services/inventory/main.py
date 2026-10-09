"""Stock levels and reservations backed by Postgres."""
from fastapi import HTTPException
from pydantic import BaseModel

from common.base import make_app

ctx = make_app("inventory", use_db=True)
app = ctx.app


class Reserve(BaseModel):
    item_id: int
    qty: int = 1


@app.get("/items")
def items():
    with ctx.db.conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT id, name, price, stock FROM items ORDER BY id")
        return [
            {"id": r[0], "name": r[1], "price": float(r[2]), "stock": r[3]} for r in cur.fetchall()
        ]


@app.post("/reserve")
def reserve(body: Reserve):
    with ctx.db.conn() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE items SET stock = stock - %s WHERE id = %s AND stock >= %s RETURNING price",
            (body.qty, body.item_id, body.qty),
        )
        row = cur.fetchone()
    if row is None:
        ctx.logger.warning(
            "reservation rejected: item missing or out of stock",
            extra={"fields": {"event": "reserve_rejected", "item_id": body.item_id}},
        )
        raise HTTPException(status_code=409, detail="out of stock")
    return {"item_id": body.item_id, "qty": body.qty, "price": float(row[0])}
