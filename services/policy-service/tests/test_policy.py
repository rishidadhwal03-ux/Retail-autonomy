from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def evaluate(categories, discount):
    return client.post(
        "/evaluate", json={"categories": categories, "discount_percent": discount}
    ).json()["decision"]


def test_health():
    assert client.get("/health").json() == {"status": "ok"}


def test_phone_small_discount_auto_approved():
    assert evaluate(["phone"], 5) == "auto_approved"


def test_phone_medium_discount_needs_owner():
    assert evaluate(["phone"], 10) == "needs_approval"


def test_phone_excessive_discount_rejected():
    assert evaluate(["phone"], 20) == "rejected"


def test_accessory_allows_bigger_discount():
    assert evaluate(["accessory"], 15) == "auto_approved"


def test_mixed_cart_uses_strictest_rule():
    # accessory alone would auto-approve 10%, but the phone's limit (5%) applies
    assert evaluate(["phone", "accessory"], 10) == "needs_approval"


def test_unknown_category_uses_default_rule():
    assert evaluate(["laptop"], 3) == "auto_approved"
    assert evaluate(["laptop"], 4) == "needs_approval"


def test_invalid_discount_rejected_by_validation():
    resp = client.post(
        "/evaluate", json={"categories": ["phone"], "discount_percent": 150}
    )
    assert resp.status_code == 422