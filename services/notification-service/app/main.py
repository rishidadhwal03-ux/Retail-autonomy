import asyncio
import json
import os
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI
from pydantic import BaseModel

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
OWNER_CHAT_ID = int(os.environ["TELEGRAM_CHAT_ID"])
BILLING_URL = os.getenv("BILLING_URL", "http://billing-service:8002")
API = f"https://api.telegram.org/bot{TOKEN}"


async def handle_button(client: httpx.AsyncClient, cb: dict):
    # Only the owner's chat may approve or reject
    if cb["from"]["id"] != OWNER_CHAT_ID:
        await client.post(f"{API}/answerCallbackQuery",
                          json={"callback_query_id": cb["id"], "text": "Not authorised"})
        return

    action, sale_id = cb["data"].split(":", 1)
    approved = action == "approve"

    resp = await client.post(
        f"{BILLING_URL}/sales/{sale_id}/decision",
        json={"approved": approved, "decided_by": "owner"},
    )
    if resp.status_code == 200:
        outcome = "✅ APPROVED" if approved else "❌ REJECTED"
    elif resp.status_code == 409:
        outcome = "ℹ️ Already decided"
    else:
        outcome = "⚠️ Could not update sale"

    await client.post(f"{API}/answerCallbackQuery",
                      json={"callback_query_id": cb["id"], "text": outcome})
    msg = cb["message"]
    await client.post(f"{API}/editMessageText", json={
        "chat_id": msg["chat"]["id"],
        "message_id": msg["message_id"],
        "text": f"{msg['text']}\n\nDecision: {outcome}",
    })


async def send_summary(client: httpx.AsyncClient, chat_id: int):
    r = await client.get(f"{BILLING_URL}/sales")
    sales = r.json()
    done = [s for s in sales if s["status"] == "completed"]
    pending = [s for s in sales if s["status"] == "pending_approval"]
    rejected = [s for s in sales if s["status"] == "rejected"]
    text = (
        "📊 Shop summary\n\n"
        f"Completed sales: {len(done)} (₹{sum(s['total'] for s in done):,.2f})\n"
        f"Discount given: ₹{sum(s['discount_amount'] for s in done):,.2f}\n"
        f"Pending approvals: {len(pending)}\n"
        f"Rejected: {len(rejected)}"
    )
    if done:
        top = max(done, key=lambda s: s["discount_percent"])
        text += f"\n\nHighest discount: {top['discount_percent']}% by {top['employee']}"
    await client.post(f"{API}/sendMessage", json={"chat_id": chat_id, "text": text})


async def poll_telegram():
    offset = 0
    async with httpx.AsyncClient(timeout=40) as client:
        while True:
            try:
                r = await client.get(f"{API}/getUpdates", params={
                    "offset": offset,
                    "timeout": 25,
                    "allowed_updates": json.dumps(["callback_query", "message"]),
                })
                for update in r.json().get("result", []):
                    offset = update["update_id"] + 1
                    if "callback_query" in update:
                        await handle_button(client, update["callback_query"])
                    elif "message" in update:
                        msg = update["message"]
                        text = msg.get("text", "").strip().lower()
                        if msg["from"]["id"] == OWNER_CHAT_ID and text == "/summary":
                            await send_summary(client, msg["chat"]["id"])
            except Exception as e:
                print(f"poll error: {e}", flush=True)
                await asyncio.sleep(3)


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(poll_telegram())
    yield
    task.cancel()


app = FastAPI(title="Notification Service", lifespan=lifespan)


class ApprovalAlert(BaseModel):
    sale_id: str
    employee: str
    items: str
    subtotal: float
    discount_percent: float
    total: float
    reason: str


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/notify/approval")
async def notify_approval(a: ApprovalAlert):
    text = (
        "🔔 Approval needed\n\n"
        f"Employee: {a.employee}\n"
        f"Items: {a.items}\n"
        f"Subtotal: ₹{a.subtotal:,.2f}\n"
        f"Discount: {a.discount_percent}%\n"
        f"Total: ₹{a.total:,.2f}\n\n"
        f"Why: {a.reason}"
    )
    keyboard = {"inline_keyboard": [[
        {"text": "✅ Approve", "callback_data": f"approve:{a.sale_id}"},
        {"text": "❌ Reject", "callback_data": f"reject:{a.sale_id}"},
    ]]}
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.post(f"{API}/sendMessage",
                              json={"chat_id": OWNER_CHAT_ID, "text": text,
                                    "reply_markup": keyboard})
        r.raise_for_status()
    return {"sent": True}