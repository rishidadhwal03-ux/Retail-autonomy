import json
from pathlib import Path

from fastapi import FastAPI
from pydantic import BaseModel, Field

RULES_PATH = Path(__file__).parent / "rules.json"
app = FastAPI(title="Policy Service")


def load_rules() -> dict:
    return json.loads(RULES_PATH.read_text())


class EvaluateRequest(BaseModel):
    categories: list[str] = Field(min_length=1)
    discount_percent: float = Field(ge=0, le=100)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/rules")
def get_rules():
    return load_rules()


@app.post("/evaluate")
def evaluate(req: EvaluateRequest):
    rules = load_rules()
    limits = [
        rules["categories"].get(c.lower(), rules["default"]) for c in req.categories
    ]
    # The strictest rule across all items in the sale applies
    auto_max = min(l["auto_approve_max"] for l in limits)
    hard_max = min(l["hard_max"] for l in limits)

    if req.discount_percent <= auto_max:
        decision, reason = "auto_approved", f"Within auto-approve limit of {auto_max}%"
    elif req.discount_percent <= hard_max:
        decision, reason = "needs_approval", f"Above {auto_max}%, owner approval required"
    else:
        decision, reason = "rejected", f"Exceeds maximum allowed discount of {hard_max}%"

    return {"decision": decision, "reason": reason}