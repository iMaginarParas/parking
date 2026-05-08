"""
vendor.py — Vendor registration, parking spot management, session tracking (QR scan-in/out), wallet.
Mount this router into main.py with:  app.include_router(vendor_router)
"""

import os
import uuid
import qrcode
import io
import base64
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Depends, UploadFile, File, Form
from fastapi.responses import JSONResponse
from pydantic import BaseModel

# Re-use the supabase client + auth helper from main
from main import supabase, get_current_user, get_address_from_google

vendor_router = APIRouter()

SUPABASE_BUCKET = os.getenv("SUPABASE_STORAGE_BUCKET", "vendor-photos")
HOURLY_RATE_DEFAULT = 30.0          # ₹ per hour — override per spot

# ─── MODELS ──────────────────────────────────────────────────────────────────

class VendorRegister(BaseModel):
    business_name: str
    owner_name: str
    phone: str
    address: str
    city: str
    state: str
    pincode: str
    latitude: float
    longitude: float
    total_slots: int = 1
    hourly_rate: float = HOURLY_RATE_DEFAULT
    description: str | None = None

class SpotStatusUpdate(BaseModel):
    is_active: bool

class WalletTopup(BaseModel):
    amount: float               # ₹ to add

# ─── SUPABASE TABLES EXPECTED ─────────────────────────────────────────────────
# vendors            : id, user_id, business_name, owner_name, phone, address,
#                      city, state, pincode, latitude, longitude, total_slots,
#                      available_slots, hourly_rate, description, photo_url,
#                      qr_code_b64, is_active, is_approved, created_at
#
# parking_sessions   : id, spot_id, user_id, vehicle_number, check_in_at,
#                      check_out_at, duration_minutes, amount_charged, status
#
# wallets            : id, user_id, balance, updated_at
#
# wallet_transactions: id, user_id, type(credit/debit), amount, note, created_at
# ─────────────────────────────────────────────────────────────────────────────

# ─── HELPERS ─────────────────────────────────────────────────────────────────

def _generate_qr(data: str) -> str:
    """Return a base-64 PNG QR code for the given data string."""
    img = qrcode.make(data)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()

def _get_or_create_wallet(user_id: str) -> dict:
    res = supabase.table("wallets").select("*").eq("user_id", user_id).execute()
    if res.data:
        return res.data[0]
    # create with ₹0 balance
    new = supabase.table("wallets").insert({
        "user_id": user_id,
        "balance": 0.0,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }).execute()
    return new.data[0]

def _debit_wallet(user_id: str, amount: float, note: str):
    wallet = _get_or_create_wallet(user_id)
    if wallet["balance"] < amount:
        raise HTTPException(status_code=402, detail="Insufficient wallet balance.")
    new_balance = round(wallet["balance"] - amount, 2)
    supabase.table("wallets").update({
        "balance": new_balance,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }).eq("user_id", user_id).execute()
    supabase.table("wallet_transactions").insert({
        "user_id": user_id,
        "type": "debit",
        "amount": amount,
        "note": note,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }).execute()
    return new_balance

def _credit_wallet(user_id: str, amount: float, note: str):
    wallet = _get_or_create_wallet(user_id)
    new_balance = round(wallet["balance"] + amount, 2)
    supabase.table("wallets").update({
        "balance": new_balance,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }).eq("user_id", user_id).execute()
    supabase.table("wallet_transactions").insert({
        "user_id": user_id,
        "type": "credit",
        "amount": amount,
        "note": note,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }).execute()
    return new_balance

# ─── VENDOR REGISTRATION ─────────────────────────────────────────────────────

@vendor_router.post("/vendor/register")
async def register_vendor(
    business_name: str = Form(...),
    owner_name: str = Form(...),
    phone: str = Form(...),
    address: str = Form(...),
    city: str = Form(...),
    state: str = Form(...),
    pincode: str = Form(...),
    latitude: float = Form(...),
    longitude: float = Form(...),
    total_slots: int = Form(1),
    hourly_rate: float = Form(HOURLY_RATE_DEFAULT),
    description: str = Form(""),
    photo: UploadFile = File(None),
    current_user=Depends(get_current_user),
):
    """
    Register a new parking vendor (from the website form).
    On success a unique QR code is generated and returned.
    """
    # Check if this user already has a vendor profile
    existing = supabase.table("vendors").select("id").eq("user_id", str(current_user.id)).execute()
    if existing.data:
        raise HTTPException(status_code=409, detail="Vendor profile already exists for this account.")

    spot_id = str(uuid.uuid4())
    photo_url = None

    # Upload photo to Supabase Storage if provided
    if photo:
        file_bytes = await photo.read()
        ext = photo.filename.split(".")[-1] if photo.filename else "jpg"
        path = f"{spot_id}/cover.{ext}"
        supabase.storage.from_(SUPABASE_BUCKET).upload(path, file_bytes, {"content-type": photo.content_type or "image/jpeg"})
        photo_url = supabase.storage.from_(SUPABASE_BUCKET).get_public_url(path)

    # QR payload: spot id used for scan-in / scan-out
    qr_payload = f"pocketparking://spot/{spot_id}"
    qr_b64 = _generate_qr(qr_payload)

    resolved_address = get_address_from_google(latitude, longitude) if not address else address

    vendor_row = {
        "id": spot_id,
        "user_id": str(current_user.id),
        "business_name": business_name,
        "owner_name": owner_name,
        "phone": phone,
        "address": resolved_address,
        "city": city,
        "state": state,
        "pincode": pincode,
        "latitude": latitude,
        "longitude": longitude,
        "total_slots": total_slots,
        "available_slots": total_slots,
        "hourly_rate": hourly_rate,
        "description": description,
        "photo_url": photo_url,
        "qr_code_b64": qr_b64,
        "is_active": True,
        "is_approved": False,       # admin must approve
        "created_at": datetime.now(timezone.utc).isoformat(),
    }

    res = supabase.table("vendors").insert(vendor_row).execute()
    if not res.data:
        raise HTTPException(status_code=500, detail="Failed to register vendor.")

    return {
        "status": "success",
        "message": "Vendor registered! Awaiting admin approval.",
        "spot_id": spot_id,
        "qr_code_b64": qr_b64,
        "qr_payload": qr_payload,
    }

@vendor_router.get("/vendor/me")
def get_my_vendor_profile(current_user=Depends(get_current_user)):
    """Returns the vendor profile of the currently logged-in vendor."""
    res = supabase.table("vendors").select("*").eq("user_id", str(current_user.id)).execute()
    if not res.data:
        raise HTTPException(status_code=404, detail="No vendor profile found.")
    return res.data[0]

@vendor_router.patch("/vendor/toggle")
def toggle_vendor_spot(body: SpotStatusUpdate, current_user=Depends(get_current_user)):
    """Vendor can mark their spot active/inactive (e.g. fully booked, closed today)."""
    res = supabase.table("vendors").update({"is_active": body.is_active}).eq("user_id", str(current_user.id)).execute()
    if not res.data:
        raise HTTPException(status_code=404, detail="Vendor spot not found.")
    return {"status": "success", "is_active": body.is_active}

# ─── PUBLIC SPOTS ────────────────────────────────────────────────────────────

@vendor_router.get("/spots/")
def get_all_spots():
    """All approved + active vendor spots — shown on the app map."""
    res = supabase.table("vendors").select(
        "id, business_name, owner_name, address, city, latitude, longitude, "
        "total_slots, available_slots, hourly_rate, photo_url, qr_code_b64"
    ).eq("is_active", True).eq("is_approved", True).execute()
    return res.data

@vendor_router.get("/spots/{spot_id}")
def get_spot(spot_id: str):
    """Single spot detail — called when app user taps a marker."""
    res = supabase.table("vendors").select("*").eq("id", spot_id).execute()
    if not res.data:
        raise HTTPException(status_code=404, detail="Spot not found.")
    return res.data[0]

# ─── QR SCAN — CHECK IN ──────────────────────────────────────────────────────

@vendor_router.post("/session/checkin/{spot_id}")
def checkin(spot_id: str, current_user=Depends(get_current_user)):
    """
    Called when a driver scans the QR code on arrival.
    Creates a parking session record and decrements available_slots.
    """
    # Get the spot
    spot_res = supabase.table("vendors").select("*").eq("id", spot_id).execute()
    if not spot_res.data:
        raise HTTPException(status_code=404, detail="Parking spot not found.")
    spot = spot_res.data[0]

    if not spot["is_active"] or not spot["is_approved"]:
        raise HTTPException(status_code=400, detail="This spot is currently unavailable.")
    if spot["available_slots"] <= 0:
        raise HTTPException(status_code=400, detail="No slots available right now.")

    # Make sure this user doesn't already have an open session here
    open_session = (
        supabase.table("parking_sessions")
        .select("id")
        .eq("user_id", str(current_user.id))
        .eq("spot_id", spot_id)
        .eq("status", "active")
        .execute()
    )
    if open_session.data:
        raise HTTPException(status_code=409, detail="You already have an active session at this spot.")

    session_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()

    supabase.table("parking_sessions").insert({
        "id": session_id,
        "spot_id": spot_id,
        "user_id": str(current_user.id),
        "check_in_at": now,
        "check_out_at": None,
        "duration_minutes": None,
        "amount_charged": None,
        "status": "active",
    }).execute()

    # Decrement available slots
    supabase.table("vendors").update({
        "available_slots": spot["available_slots"] - 1
    }).eq("id", spot_id).execute()

    return {
        "status": "success",
        "message": f"Checked in to {spot['business_name']}. Safe parking!",
        "session_id": session_id,
        "check_in_at": now,
        "hourly_rate": spot["hourly_rate"],
    }

# ─── QR SCAN — CHECK OUT ─────────────────────────────────────────────────────

@vendor_router.post("/session/checkout/{spot_id}")
def checkout(spot_id: str, current_user=Depends(get_current_user)):
    """
    Called when a driver scans the QR code on exit.
    Calculates duration, charges the wallet, closes the session.
    """
    spot_res = supabase.table("vendors").select("*").eq("id", spot_id).execute()
    if not spot_res.data:
        raise HTTPException(status_code=404, detail="Parking spot not found.")
    spot = spot_res.data[0]

    # Find the active session
    session_res = (
        supabase.table("parking_sessions")
        .select("*")
        .eq("user_id", str(current_user.id))
        .eq("spot_id", spot_id)
        .eq("status", "active")
        .execute()
    )
    if not session_res.data:
        raise HTTPException(status_code=404, detail="No active session found at this spot.")
    session = session_res.data[0]

    check_in_dt = datetime.fromisoformat(session["check_in_at"])
    check_out_dt = datetime.now(timezone.utc)
    duration_minutes = int((check_out_dt - check_in_dt).total_seconds() / 60)
    duration_hours = duration_minutes / 60
    amount = round(spot["hourly_rate"] * duration_hours, 2)
    if amount < 5:
        amount = 5.0        # minimum charge ₹5

    # Deduct from user wallet
    _debit_wallet(str(current_user.id), amount, f"Parking at {spot['business_name']} ({duration_minutes} min)")

    # Credit vendor wallet
    vendor_cut = round(amount * 0.85, 2)   # platform takes 15%
    _credit_wallet(spot["user_id"], vendor_cut, f"Parking income ({duration_minutes} min)")

    # Close session
    supabase.table("parking_sessions").update({
        "check_out_at": check_out_dt.isoformat(),
        "duration_minutes": duration_minutes,
        "amount_charged": amount,
        "status": "completed",
    }).eq("id", session["id"]).execute()

    # Restore slot
    supabase.table("vendors").update({
        "available_slots": spot["available_slots"] + 1
    }).eq("id", spot_id).execute()

    return {
        "status": "success",
        "message": "Checked out successfully!",
        "duration_minutes": duration_minutes,
        "amount_charged": amount,
        "vendor_received": vendor_cut,
    }

@vendor_router.get("/session/active")
def get_active_session(current_user=Depends(get_current_user)):
    """Returns the user's currently active parking session (if any)."""
    res = (
        supabase.table("parking_sessions")
        .select("*, vendors(business_name, address, hourly_rate)")
        .eq("user_id", str(current_user.id))
        .eq("status", "active")
        .execute()
    )
    return res.data[0] if res.data else None

@vendor_router.get("/session/history")
def get_session_history(current_user=Depends(get_current_user)):
    """Returns past parking sessions for the logged-in user."""
    res = (
        supabase.table("parking_sessions")
        .select("*, vendors(business_name, address)")
        .eq("user_id", str(current_user.id))
        .eq("status", "completed")
        .order("check_out_at", desc=True)
        .limit(50)
        .execute()
    )
    return res.data