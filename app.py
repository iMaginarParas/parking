"""
app.py — Wallet management, admin panel endpoints, and the final FastAPI
app that wires main.py + vendor.py together.

Run with:  uvicorn app:app --host 0.0.0.0 --port $PORT
"""

import os
import hmac
import hashlib

import razorpay
from fastapi import HTTPException, Depends
from pydantic import BaseModel
from datetime import datetime, timezone

from main import app, supabase, get_current_user
from vendor import vendor_router, _get_or_create_wallet, _credit_wallet

# Mount all vendor/session/spot routes
app.include_router(vendor_router)

# ─── RAZORPAY CLIENT ─────────────────────────────────────────────────────────

RAZORPAY_KEY_ID     = os.getenv("RAZORPAY_KEY_ID")
RAZORPAY_KEY_SECRET = os.getenv("RAZORPAY_KEY_SECRET")

if not RAZORPAY_KEY_ID or not RAZORPAY_KEY_SECRET:
    raise ValueError("RAZORPAY_KEY_ID or RAZORPAY_KEY_SECRET missing in environment variables.")

rzp_client = razorpay.Client(auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET))

# ─── MODELS ──────────────────────────────────────────────────────────────────

class CreateOrderRequest(BaseModel):
    amount: float           # ₹ amount user wants to top up

class WalletTopup(BaseModel):
    razorpay_order_id:   str
    razorpay_payment_id: str
    razorpay_signature:  str
    amount:              float   # must match the order amount — double-checked server-side

class AdminApprove(BaseModel):
    spot_id: str
    approve: bool

# ─── WALLET ENDPOINTS ────────────────────────────────────────────────────────

@app.get("/wallet/")
def get_wallet(current_user=Depends(get_current_user)):
    """Returns the logged-in user's wallet balance."""
    wallet = _get_or_create_wallet(str(current_user.id))
    return {
        "status":     "success",
        "balance":    wallet["balance"],
        "updated_at": wallet["updated_at"],
    }


@app.post("/wallet/create-order")
def create_razorpay_order(body: CreateOrderRequest, current_user=Depends(get_current_user)):
    """
    Step 1 of top-up flow.
    Flutter calls this first → we create a Razorpay order and return the
    order_id + key_id so the Flutter SDK can open the payment sheet.
    """
    if body.amount < 10:
        raise HTTPException(status_code=400, detail="Minimum top-up is ₹10.")
    if body.amount > 10_000:
        raise HTTPException(status_code=400, detail="Single top-up cannot exceed ₹10,000.")

    amount_paise = int(body.amount * 100)   # Razorpay works in paise

    try:
        order = rzp_client.order.create({
            "amount":   amount_paise,
            "currency": "INR",
            "receipt":  f"wallet_{str(current_user.id)[:8]}_{int(datetime.now().timestamp())}",
            "notes": {
                "user_id": str(current_user.id),
                "purpose": "wallet_topup",
            },
        })
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Razorpay order creation failed: {str(e)}")

    return {
        "status":   "success",
        "order_id": order["id"],
        "amount":   body.amount,
        "currency": "INR",
        "key_id":   RAZORPAY_KEY_ID,   # Flutter needs this to open the SDK
    }


@app.post("/wallet/topup")
def topup_wallet(body: WalletTopup, current_user=Depends(get_current_user)):
    """
    Step 2 of top-up flow.
    Flutter calls this AFTER the Razorpay payment sheet succeeds,
    passing the three values Razorpay returns on success.

    We:
      1. Verify the HMAC-SHA256 signature  → proves payment is genuine
      2. Fetch the payment from Razorpay   → confirms captured + exact amount
      3. Credit the wallet only if both checks pass
    """

    # ── 1. Signature verification ─────────────────────────────────────────────
    # Razorpay signs: order_id + "|" + payment_id  with your key secret
    expected_signature = hmac.new(
        RAZORPAY_KEY_SECRET.encode("utf-8"),
        f"{body.razorpay_order_id}|{body.razorpay_payment_id}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(expected_signature, body.razorpay_signature):
        raise HTTPException(
            status_code=400,
            detail="Payment signature verification failed. Do not retry.",
        )

    # ── 2. Fetch payment from Razorpay and confirm status + amount ────────────
    try:
        payment = rzp_client.payment.fetch(body.razorpay_payment_id)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Could not fetch payment details: {str(e)}")

    if payment.get("status") != "captured":
        raise HTTPException(
            status_code=400,
            detail=f"Payment not captured (status: {payment.get('status')}). Contact support.",
        )

    # Convert paise → ₹ and compare to what the client claims
    paid_inr = payment["amount"] / 100
    if abs(paid_inr - body.amount) > 0.5:       # allow 50p rounding tolerance
        raise HTTPException(
            status_code=400,
            detail=f"Amount mismatch: paid ₹{paid_inr}, claimed ₹{body.amount}.",
        )

    # ── 3. Idempotency guard — prevent double-credit if retried ──────────────
    already = (
        supabase.table("wallet_transactions")
        .select("id")
        .eq("note", f"Wallet top-up (Razorpay: {body.razorpay_payment_id})")
        .execute()
    )
    if already.data:
        raise HTTPException(status_code=409, detail="This payment has already been credited.")

    # ── 4. Credit wallet ──────────────────────────────────────────────────────
    new_balance = _credit_wallet(
        str(current_user.id),
        paid_inr,
        f"Wallet top-up (Razorpay: {body.razorpay_payment_id})",
    )

    return {
        "status":      "success",
        "message":     f"₹{paid_inr:.2f} added to your wallet.",
        "new_balance": new_balance,
        "payment_id":  body.razorpay_payment_id,
    }


@app.get("/wallet/transactions")
def get_transactions(current_user=Depends(get_current_user)):
    """Returns the last 100 wallet transactions for the logged-in user."""
    res = (
        supabase.table("wallet_transactions")
        .select("*")
        .eq("user_id", str(current_user.id))
        .order("created_at", desc=True)
        .limit(100)
        .execute()
    )
    return res.data


# ─── ADMIN ENDPOINTS ─────────────────────────────────────────────────────────

def _require_admin(current_user=Depends(get_current_user)):
    """Dependency: raises 403 if the user is not an admin."""
    meta     = getattr(current_user, "user_metadata", {}) or {}
    app_meta = getattr(current_user, "app_metadata",  {}) or {}
    if meta.get("role") != "admin" and app_meta.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin access required.")
    return current_user


@app.get("/admin/vendors/pending")
def admin_pending_vendors(current_user=Depends(_require_admin)):
    res = supabase.table("vendors").select("*").eq("is_approved", False).execute()
    return res.data


@app.get("/admin/vendors/all")
def admin_all_vendors(current_user=Depends(_require_admin)):
    res = supabase.table("vendors").select("*").order("created_at", desc=True).execute()
    return res.data


@app.post("/admin/vendor/approve")
def admin_approve_vendor(body: AdminApprove, current_user=Depends(_require_admin)):
    res = supabase.table("vendors").update({
        "is_approved": body.approve,
        "is_active":   body.approve,
    }).eq("id", body.spot_id).execute()
    if not res.data:
        raise HTTPException(status_code=404, detail="Vendor not found.")
    action = "approved" if body.approve else "rejected"
    return {"status": "success", "message": f"Vendor {action}."}


@app.get("/admin/sessions")
def admin_all_sessions(current_user=Depends(_require_admin)):
    res = (
        supabase.table("parking_sessions")
        .select("*, vendors(business_name), users:auth.users(phone)")
        .order("check_in_at", desc=True)
        .limit(200)
        .execute()
    )
    return res.data


@app.get("/admin/stats")
def admin_stats(current_user=Depends(_require_admin)):
    total_vendors    = supabase.table("vendors").select("id", count="exact").execute()
    approved_vendors = supabase.table("vendors").select("id", count="exact").eq("is_approved", True).execute()
    total_sessions   = supabase.table("parking_sessions").select("id", count="exact").execute()
    active_sessions  = supabase.table("parking_sessions").select("id", count="exact").eq("status", "active").execute()

    revenue_res   = supabase.table("parking_sessions").select("amount_charged").eq("status", "completed").execute()
    total_revenue = sum(r["amount_charged"] or 0 for r in revenue_res.data)

    return {
        "total_vendors":     total_vendors.count,
        "approved_vendors":  approved_vendors.count,
        "total_sessions":    total_sessions.count,
        "active_sessions":   active_sessions.count,
        "total_revenue_inr": round(total_revenue, 2),
        "generated_at":      datetime.now(timezone.utc).isoformat(),
    }
