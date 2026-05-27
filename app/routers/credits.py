from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from typing import Optional
import razorpay, hmac, hashlib
from app.config import get_supabase, settings
from app.dependencies import get_current_user
from app.routers.auth import _get_balance

router = APIRouter()

# ── Request models ────────────────────────────────────────────────────────────

class CreateOrderRequest(BaseModel):
    package_id: str
    currency: str = "INR"

class VerifyPaymentRequest(BaseModel):
    razorpay_order_id: str
    razorpay_payment_id: str
    razorpay_signature: str

# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("/balance")
async def get_balance(user=Depends(get_current_user)):
    db = get_supabase()
    return {"credits": _get_balance(db, user.id), "user_id": user.id}


@router.get("/packages")
async def get_packages():
    """Public — no auth needed."""
    return {"packages": settings.CREDIT_PACKAGES}


@router.post("/order")
async def create_order(req: CreateOrderRequest, user=Depends(get_current_user)):
    """
    Step 1 of checkout:
    Creates a Razorpay order and records it as 'pending' in DB.
    Returns order_id + razorpay_key to the frontend.
    KEY_SECRET never leaves the server.
    """
    pkg = next((p for p in settings.CREDIT_PACKAGES if p["id"] == req.package_id), None)
    if not pkg:
        raise HTTPException(400, "Invalid package ID")

    amount = pkg["inr"] if req.currency == "INR" else pkg["usd"]
    if amount < 100:
        raise HTTPException(400, "Amount must be at least 100 paise")

    try:
        client = razorpay.Client(auth=(settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET))
        order = client.order.create({
            "amount": amount,
            "currency": req.currency,
            "receipt": f"rcpt_{user.id[:8]}_{pkg['id']}",
            "notes": {
                "user_id": user.id,
                "package_id": pkg["id"],
                "credits": str(pkg["credits"]),
            }
        })
    except razorpay.errors.BadRequestError as e:
        raise HTTPException(400, f"Razorpay error: {str(e)}")
    except Exception as e:
        raise HTTPException(500, f"Could not create order: {str(e)}")

    # Log as pending — credits only land after verify-payment confirms signature
    db = get_supabase()
    db.table("credit_orders").insert({
        "user_id": user.id,
        "razorpay_order_id": order["id"],
        "package_id": pkg["id"],
        "credits": pkg["credits"],
        "amount": amount,
        "currency": req.currency,
        "status": "pending",
    }).execute()

    return {
        "order_id": order["id"],
        "amount": amount,
        "currency": req.currency,
        "credits": pkg["credits"],
        "package_label": pkg["label"],
        "razorpay_key": settings.RAZORPAY_KEY_ID,   # public key only
    }


@router.post("/verify-payment")
async def verify_payment(req: VerifyPaymentRequest, user=Depends(get_current_user)):
    """
    Step 3 of checkout:
    Verifies the Razorpay payment signature using HMAC-SHA256.
    This is the fraud-prevention step — credits only land if signature matches.
    Algorithm: HMAC-SHA256(order_id + "|" + payment_id, KEY_SECRET)
    """
    # 1. Verify signature
    msg = f"{req.razorpay_order_id}|{req.razorpay_payment_id}".encode()
    expected = hmac.new(
        settings.RAZORPAY_KEY_SECRET.encode(),
        msg,
        hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(expected, req.razorpay_signature):
        raise HTTPException(400, "Payment signature mismatch — payment not verified")

    db = get_supabase()

    # 2. Find the pending order (belongs to this user)
    order_res = db.table("credit_orders") \
        .select("*") \
        .eq("razorpay_order_id", req.razorpay_order_id) \
        .eq("user_id", user.id) \
        .eq("status", "pending") \
        .maybe_single().execute()

    if not order_res.data:
        # Already processed (idempotency) or not found
        balance = _get_balance(db, user.id)
        return {"ok": True, "credits_added": 0, "credits_balance": balance, "already_processed": True}

    order = order_res.data
    credits = order["credits"]

    # 3. Add credits to ledger
    db.table("credit_ledger").insert({
        "user_id": user.id,
        "delta": credits,
        "reason": "purchase",
        "meta": {
            "package_id": order["package_id"],
            "razorpay_order_id": req.razorpay_order_id,
            "razorpay_payment_id": req.razorpay_payment_id,
            "amount": order["amount"],
            "currency": order["currency"],
        }
    }).execute()

    # 4. Mark order paid
    db.table("credit_orders") \
        .update({
            "status": "paid",
            "razorpay_payment_id": req.razorpay_payment_id,
        }) \
        .eq("razorpay_order_id", req.razorpay_order_id).execute()

    new_balance = _get_balance(db, user.id)
    print(f"[Payment] Verified: +{credits} credits → user {user.id}, balance now {new_balance}")

    return {
        "ok": True,
        "credits_added": credits,
        "credits_balance": new_balance,
    }


@router.get("/history")
async def get_history(user=Depends(get_current_user)):
    db = get_supabase()
    res = db.table("credit_ledger") \
        .select("*") \
        .eq("user_id", user.id) \
        .order("created_at", desc=True) \
        .limit(50) \
        .execute()
    return {
        "balance": _get_balance(db, user.id),
        "transactions": res.data or []
    }
