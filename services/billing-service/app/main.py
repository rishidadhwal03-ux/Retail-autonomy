import os
import json
from pathlib import Path
import uuid
from datetime import datetime, timezone

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

POLICY_URL = os.getenv("POLICY_URL", "http://localhost:8001")
NOTIFY_URL = os.getenv("NOTIFY_URL", "http://localhost:8003")
app = FastAPI(title="Billing Service")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # prototype only; restrict to the dashboard origin in production
    allow_methods=["*"],
    allow_headers=["*"],
)

# File-backed store on a Docker volume so sales survive restarts and redeploys.
# At scale this would be replaced by a managed database such as DynamoDB.
DATA_FILE = Path(os.getenv("DATA_FILE", "/data/sales.json"))
DATA_FILE.parent.mkdir(parents=True, exist_ok=True)


def load_sales() -> dict[str, dict]:
    if DATA_FILE.exists():
        try:
            return json.loads(DATA_FILE.read_text())
        except json.JSONDecodeError:
            return {}
    return {}


def save_sales() -> None:
    tmp = DATA_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(SALES))
    tmp.replace(DATA_FILE)  # atomic swap so a crash can't leave a half-written file


SALES: dict[str, dict] = load_sales()

STATUS_MAP = {
    "auto_approved": "completed",
    "needs_approval": "pending_approval",
    "rejected": "rejected",
}


class Item(BaseModel):
    name: str
    category: str
    price: float = Field(gt=0)
    qty: int = Field(ge=1)


class SaleRequest(BaseModel):
    shop_id: str
    outlet_id: str
    employee: str
    items: list[Item] = Field(min_length=1)
    discount_percent: float = Field(default=0, ge=0, le=100)


class Decision(BaseModel):
    approved: bool
    decided_by: str = "owner"


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/sales", status_code=201)
def create_sale(req: SaleRequest):
    try:
        resp = httpx.post(
            f"{POLICY_URL}/evaluate",
            json={
                "categories": [i.category for i in req.items],
                "discount_percent": req.discount_percent,
            },
            timeout=5,
        )
        resp.raise_for_status()
    except httpx.HTTPError:
        raise HTTPException(status_code=503, detail="Policy service unavailable")

    verdict = resp.json()
    subtotal = round(sum(i.price * i.qty for i in req.items), 2)
    discount_amount = round(subtotal * req.discount_percent / 100, 2)

    sale = {
        "sale_id": str(uuid.uuid4()),
        "shop_id": req.shop_id,
        "outlet_id": req.outlet_id,
        "employee": req.employee,
        "items": [i.model_dump() for i in req.items],
        "subtotal": subtotal,
        "discount_percent": req.discount_percent,
        "discount_amount": discount_amount,
        "total": round(subtotal - discount_amount, 2),
        "status": STATUS_MAP[verdict["decision"]],
        "policy_reason": verdict["reason"],
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    SALES[sale["sale_id"]] = sale
    save_sales()

    if sale["status"] == "pending_approval":
        try:
            httpx.post(
                f"{NOTIFY_URL}/notify/approval",
                json={
                    "sale_id": sale["sale_id"],
                    "employee": sale["employee"],
                    "items": ", ".join(f'{i["qty"]} x {i["name"]}' for i in sale["items"]),
                    "subtotal": sale["subtotal"],
                    "discount_percent": sale["discount_percent"],
                    "total": sale["total"],
                    "reason": sale["policy_reason"],
                },
                timeout=5,
            )
        except httpx.HTTPError:
            pass  # the sale stays pending even if the alert fails
    return sale


@app.get("/sales")
def list_sales(shop_id: str | None = None, status: str | None = None):
    sales = list(SALES.values())
    if shop_id:
        sales = [s for s in sales if s["shop_id"] == shop_id]
    if status:
        sales = [s for s in sales if s["status"] == status]
    return sales


@app.get("/sales/{sale_id}")
def get_sale(sale_id: str):
    if sale_id not in SALES:
        raise HTTPException(status_code=404, detail="Sale not found")
    return SALES[sale_id]


@app.post("/sales/{sale_id}/decision")
def decide_sale(sale_id: str, decision: Decision):
    sale = SALES.get(sale_id)
    if not sale:
        raise HTTPException(status_code=404, detail="Sale not found")
    if sale["status"] != "pending_approval":
        raise HTTPException(status_code=409, detail="Sale is not awaiting approval")
    sale["status"] = "completed" if decision.approved else "rejected"
    sale["decided_by"] = decision.decided_by
    save_sales()
    return sale