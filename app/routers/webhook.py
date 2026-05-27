from fastapi import APIRouter, HTTPException, Request, Header
import hmac, hashlib, json
from app.config import get_supabase, settings

router = APIRouter()

@router.post("/razorpay")
async def razorpay_webhook(request: Request, x_razorpay_signature: str = Header(...)):
    body = await request.body()

    # Verify signature
    expected = hmac.new(
        settings.RAZORPAY_WEBHOOK_SECRET.encode(),
        body,
        hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(expected, x_razorpay_signature):
        raise HTTPException(status_code=400, detail="Invalid signature")

    payload = json.loads(body)
    event = payload.get("event")

    if event == "payment.captured":
        payment = payload["payload"]["payment"]["entity"]
        order_id = payment.get("order_id")
        payment_id = payment.get("id")

        db = get_supabase()

        # Get pending order
        order_res = db.table("credit_orders") \
            .select("*") \
            .eq("razorpay_order_id", order_id) \
            .eq("status", "pending") \
            .maybe_single().execute()

        if not order_res.data:
            # Already processed or not found — idempotency
            return {"status": "ok"}

        order = order_res.data
        user_id = order["user_id"]
        credits = order["credits"]

        # Add credits to ledger
        db.table("credit_ledger").insert({
            "user_id": user_id,
            "delta": credits,
            "reason": "purchase",
            "meta": {
                "package_id": order["package_id"],
                "razorpay_order_id": order_id,
                "razorpay_payment_id": payment_id,
                "amount": order["amount"],
                "currency": order["currency"],
            }
        }).execute()

        # Mark order as paid
        db.table("credit_orders") \
            .update({"status": "paid", "razorpay_payment_id": payment_id}) \
            .eq("razorpay_order_id", order_id).execute()

        print(f"[Webhook] Added {credits} credits to user {user_id}")

    elif event == "payment.failed":
        payment = payload["payload"]["payment"]["entity"]
        order_id = payment.get("order_id")
        db = get_supabase()
        db.table("credit_orders") \
            .update({"status": "failed"}) \
            .eq("razorpay_order_id", order_id).execute()

    return {"status": "ok"}
