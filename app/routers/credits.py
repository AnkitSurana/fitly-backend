from fastapi import APIRouter, HTTPException, Depends
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
import razorpay, hmac, hashlib, logging
from app.config import get_supabase, settings
from app.dependencies import get_current_user
from app.routers.auth import _get_balance

router = APIRouter()
logger = logging.getLogger("fitly.credits")

class CreateOrderRequest(BaseModel):
    package_id: str
    currency: str = "INR"

class VerifyPaymentRequest(BaseModel):
    razorpay_order_id: str
    razorpay_payment_id: str
    razorpay_signature: str

@router.get("/balance")
async def get_balance(user=Depends(get_current_user)):
    db = get_supabase()
    return {"credits": _get_balance(db, user.id), "user_id": user.id}

@router.get("/packages")
async def get_packages():
    return {"packages": settings.CREDIT_PACKAGES}

@router.post("/create-payment-link")
async def create_payment_link(req: CreateOrderRequest, user=Depends(get_current_user)):
    pkg = next((p for p in settings.CREDIT_PACKAGES if p["id"] == req.package_id), None)
    if not pkg:
        raise HTTPException(400, "Invalid package ID")

    amount = pkg["inr"] if req.currency == "INR" else pkg["usd"]

    try:
        client = razorpay.Client(auth=(settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET))

        payload = {
            "amount": amount,
            "currency": req.currency,
            "accept_partial": False,
            "description": f"Fitly — {pkg['credits']} analysis credits",
            "notes": {
                "user_id": user.id,
                "package_id": pkg["id"],
                "credits": str(pkg["credits"]),
            },
            "notify": {"sms": False, "email": False},
            "reminder_enable": False,
            "callback_url": f"{settings.APP_URL}/credits/payment-callback",
            "callback_method": "get",
        }

        logger.info(f"Creating payment link: pkg={pkg['id']} amount={amount} user={user.id}")
        link = client.payment_link.create(payload)
        logger.info(f"Payment link created: {link.get('id')} url={link.get('short_url')}")

    except razorpay.errors.BadRequestError as e:
        logger.error(f"Razorpay BadRequest: {e}")
        raise HTTPException(400, f"Razorpay error: {str(e)}")
    except Exception as e:
        logger.error(f"Payment link creation failed: {e}")
        raise HTTPException(500, f"Could not create payment link: {str(e)}")

    # Store as pending order
    db = get_supabase()
    try:
        db.table("credit_orders").insert({
            "user_id": user.id,
            "razorpay_order_id": link["id"],
            "package_id": pkg["id"],
            "credits": pkg["credits"],
            "amount": amount,
            "currency": req.currency,
            "status": "pending",
        }).execute()
    except Exception as e:
        logger.error(f"DB insert failed (non-fatal): {e}")
        # Don't fail the request — link was created, user can still pay

    return {
        "ok": True,
        "payment_url": link["short_url"],
        "payment_link_id": link["id"],
        "credits": pkg["credits"],
        "amount": amount,
        "package_label": pkg["label"],
    }

@router.get("/payment-callback")
async def payment_callback(
    razorpay_payment_id: str = "",
    razorpay_payment_link_id: str = "",
    razorpay_payment_link_reference_id: str = "",
    razorpay_payment_link_status: str = "",
    razorpay_signature: str = "",
):
    logger.info(f"Payment callback: status={razorpay_payment_link_status} link={razorpay_payment_link_id}")

    if razorpay_payment_link_status != "paid":
        return HTMLResponse(content=_result_page(False, "Payment was not completed."))

    # Verify HMAC signature
    # Correct Razorpay Payment Link signature: link_id | ref_id | status | payment_id
    msg = f"{razorpay_payment_link_id}|{razorpay_payment_link_reference_id}|{razorpay_payment_link_status}|{razorpay_payment_id}".encode()
    expected = hmac.new(
        settings.RAZORPAY_KEY_SECRET.encode(),
        msg, hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(expected, razorpay_signature):
        logger.error(f"Signature mismatch: expected={expected} got={razorpay_signature}")
        return HTMLResponse(content=_result_page(False, "Payment verification failed. Contact support."))

    db = get_supabase()

    # Find pending order
    order_res = db.table("credit_orders") \
        .select("*") \
        .eq("razorpay_order_id", razorpay_payment_link_id) \
        .maybe_single().execute()

    if not order_res.data:
        return HTMLResponse(content=_result_page(False, "Order not found. Contact support with ID: " + razorpay_payment_link_id))

    order = order_res.data

    if order["status"] == "paid":
        return HTMLResponse(content=_result_page(True, f"{order['credits']} credits already added!", order["credits"]))

    # Add credits
    db.table("credit_ledger").insert({
        "user_id": order["user_id"],
        "delta": order["credits"],
        "reason": "purchase",
        "meta": {
            "package_id": order["package_id"],
            "razorpay_payment_link_id": razorpay_payment_link_id,
            "razorpay_payment_id": razorpay_payment_id,
        }
    }).execute()

    db.table("credit_orders") \
        .update({"status": "paid", "razorpay_payment_id": razorpay_payment_id}) \
        .eq("razorpay_order_id", razorpay_payment_link_id).execute()

    logger.info(f"Credits added: +{order['credits']} → user {order['user_id']}")
    return HTMLResponse(content=_result_page(True, f"{order['credits']} credits added! Return to LinkedIn.", order["credits"]))


def _result_page(success: bool, message: str, credits: int = 0) -> str:
    color = "#1e8e3e" if success else "#d93025"
    bg    = "#e6f4ea" if success else "#fce8e6"
    icon  = "✓" if success else "✕"
    return f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>Fitly Payment</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:'Google Sans',system-ui,sans-serif;background:linear-gradient(135deg,#e8f0fe,#f8f9fa);min-height:100vh;display:flex;align-items:center;justify-content:center}}
.card{{background:#fff;border-radius:20px;padding:40px 32px;box-shadow:0 8px 32px rgba(0,0,0,.12);max-width:360px;width:90%;text-align:center}}
.icon{{width:64px;height:64px;border-radius:50%;background:{bg};color:{color};font-size:28px;font-weight:700;display:flex;align-items:center;justify-content:center;margin:0 auto 16px}}
h2{{font-size:20px;color:#202124;margin-bottom:8px}}
p{{font-size:14px;color:#5f6368;line-height:1.6;margin-bottom:20px}}
.btn{{background:#1a73e8;color:#fff;border:none;border-radius:20px;font-size:14px;font-weight:600;padding:12px 28px;cursor:pointer;font-family:inherit}}
</style></head>
<body><div class="card">
<div class="icon">{icon}</div>
<h2>{"Payment Complete!" if success else "Payment Failed"}</h2>
<p>{message}</p>
<button class="btn" onclick="window.close()">{"Close & Return" if success else "Try Again"}</button>
</div>
{"<script>setTimeout(()=>window.close(),4000)</script>" if success else ""}
</body></html>"""


@router.post("/order")
async def create_order(req: CreateOrderRequest, user=Depends(get_current_user)):
    return await create_payment_link(req, user)

@router.post("/verify-payment")
async def verify_payment(req: VerifyPaymentRequest, user=Depends(get_current_user)):
    db = get_supabase()
    msg = f"{req.razorpay_order_id}|{req.razorpay_payment_id}".encode()
    expected = hmac.new(settings.RAZORPAY_KEY_SECRET.encode(), msg, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, req.razorpay_signature):
        raise HTTPException(400, "Signature mismatch")
    order_res = db.table("credit_orders").select("*") \
        .eq("razorpay_order_id", req.razorpay_order_id) \
        .eq("user_id", user.id).eq("status", "pending").maybe_single().execute()
    if not order_res.data:
        return {"ok": True, "credits_added": 0, "credits_balance": _get_balance(db, user.id)}
    order = order_res.data
    db.table("credit_ledger").insert({
        "user_id": user.id, "delta": order["credits"], "reason": "purchase", "meta": {}
    }).execute()
    db.table("credit_orders").update({"status": "paid", "razorpay_payment_id": req.razorpay_payment_id}) \
        .eq("razorpay_order_id", req.razorpay_order_id).execute()
    return {"ok": True, "credits_added": order["credits"], "credits_balance": _get_balance(db, user.id)}

@router.get("/history")
async def get_history(user=Depends(get_current_user)):
    db = get_supabase()
    res = db.table("credit_ledger").select("*").eq("user_id", user.id) \
        .order("created_at", desc=True).limit(50).execute()
    return {"balance": _get_balance(db, user.id), "transactions": res.data or []}
