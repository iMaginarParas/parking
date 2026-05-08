"""
app.py — Wallet management, admin panel endpoints, and the final FastAPI
app that wires main.py + vendor.py together.

Run with:  uvicorn app:app --host 0.0.0.0 --port $PORT
"""

from fastapi import HTTPException, Depends
from pydantic import BaseModel
from datetime import datetime, timezone

from main import app, supabase, get_current_user
from vendor import vendor_router, _get_or_create_wallet, _credit_wallet

# Mount all vendor/session/spot routes
app.include_router(vendor_router)

# ─── MODELS ──────────────────────────────────────────────────────────────────

class WalletTopup(BaseModel):
    amount: float           # ₹ amount to add (payment gateway handled client-side)
    transaction_ref: str    # ref from payment gateway (Razorpay/Stripe order id)

class AdminApprove(BaseModel):
    spot_id: str
    approve: bool           # True = approve, False = reject

# ─── WALLET ENDPOINTS ────────────────────────────────────────────────────────

@app.get("/wallet/")
def get_wallet(current_user=Depends(get_current_user)):
    """Returns the logged-in user's wallet balance."""
    wallet = _get_or_create_wallet(str(current_user.id))
    return {
        "status": "success",
        "balance": wallet["balance"],
        "updated_at": wallet["updated_at"],
    }

@app.post("/wallet/topup")
def topup_wallet(body: WalletTopup, current_user=Depends(get_current_user)):
    """
    Credit the user's wallet after a successful payment.
    The client (Flutter app) handles the actual payment (Razorpay/Stripe)
    and sends the verified transaction_ref here.
    In production: verify the payment signature before crediting.
    """
    if body.amount <= 0:
        raise HTTPException(status_code=400, detail="Amount must be positive.")
    if body.amount > 10000:
        raise HTTPException(status_code=400, detail="Single top-up cannot exceed ₹10,000.")

    # TODO: verify body.transaction_ref with Razorpay/Stripe webhook before crediting
    new_balance = _credit_wallet(
        str(current_user.id),
        body.amount,
        f"Wallet top-up (ref: {body.transaction_ref})",
    )
    return {
        "status": "success",
        "message": f"₹{body.amount} added to your wallet.",
        "new_balance": new_balance,
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
# These require an admin JWT. In Supabase, set a custom claim `role = admin`
# in the user's JWT via a database trigger or Supabase Edge Function.

def _require_admin(current_user=Depends(get_current_user)):
    """Dependency: raises 403 if the user is not an admin."""
    meta = getattr(current_user, "user_metadata", {}) or {}
    app_meta = getattr(current_user, "app_metadata", {}) or {}
    if meta.get("role") != "admin" and app_meta.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin access required.")
    return current_user

@app.get("/admin/vendors/pending")
def admin_pending_vendors(current_user=Depends(_require_admin)):
    """List all vendors waiting for approval."""
    res = supabase.table("vendors").select("*").eq("is_approved", False).execute()
    return res.data

@app.get("/admin/vendors/all")
def admin_all_vendors(current_user=Depends(_require_admin)):
    """List every vendor (approved + pending)."""
    res = supabase.table("vendors").select("*").order("created_at", desc=True).execute()
    return res.data

@app.post("/admin/vendor/approve")
def admin_approve_vendor(body: AdminApprove, current_user=Depends(_require_admin)):
    """Approve or reject a vendor spot."""
    res = supabase.table("vendors").update({
        "is_approved": body.approve,
        "is_active": body.approve,
    }).eq("id", body.spot_id).execute()
    if not res.data:
        raise HTTPException(status_code=404, detail="Vendor not found.")
    action = "approved" if body.approve else "rejected"
    return {"status": "success", "message": f"Vendor {action}."}

@app.get("/admin/sessions")
def admin_all_sessions(current_user=Depends(_require_admin)):
    """All parking sessions (active + completed)."""
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
    """High-level platform stats."""
    total_vendors = supabase.table("vendors").select("id", count="exact").execute()
    approved_vendors = supabase.table("vendors").select("id", count="exact").eq("is_approved", True).execute()
    total_sessions = supabase.table("parking_sessions").select("id", count="exact").execute()
    active_sessions = supabase.table("parking_sessions").select("id", count="exact").eq("status", "active").execute()

    # Total revenue = sum of amount_charged
    revenue_res = supabase.table("parking_sessions").select("amount_charged").eq("status", "completed").execute()
    total_revenue = sum(r["amount_charged"] or 0 for r in revenue_res.data)

    return {
        "total_vendors": total_vendors.count,
        "approved_vendors": approved_vendors.count,
        "total_sessions": total_sessions.count,
        "active_sessions": active_sessions.count,
        "total_revenue_inr": round(total_revenue, 2),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }