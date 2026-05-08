"""
vendor.py — Full vendor lifecycle:
  • Registration  (matches every field in vendor.html)
  • Public /spots/ endpoint  (returns everything the Flutter card grid needs)
  • Single spot detail  /spots/{id}
  • QR check-in / check-out  (vehicle-type aware billing)
  • Session history
  • Wallet helpers (shared with app.py)

Mount with:  app.include_router(vendor_router)

─── SUPABASE TABLES REQUIRED ────────────────────────────────────────────────
vendors:
  id uuid PK, user_id uuid FK auth.users,
  full_name text, business_name text, owner_name text, phone text,
  city text, state text, pincode text,
  address text, latitude float8, longitude float8,
  capacity_cars int, capacity_bikes int, capacity_other text,
  total_slots int, available_slots int,
  rate_bike_first_hour float8, rate_bike_after_first_hour float8,
  rate_car_first_hour  float8, rate_car_after_first_hour  float8,
  other_vehicle_rates  jsonb,          -- [{type, first_hour, after_hour}]
  checking_charge_from_customer bool,
  checking_charge_from_vendor   bool,
  allow_access_other_device     bool,
  device_number text, device_notification_allowed bool,
  photo_url text,                      -- cover/selfie URL in Supabase Storage
  location_photo_urls jsonb,           -- [url, url, …] location pictures
  id_proof_url text,
  qr_code_b64 text,                   -- base64 PNG (shown on vendor QR screen)
  is_active bool DEFAULT true,
  is_approved bool DEFAULT false,
  digital_signature text,
  created_at timestamptz DEFAULT now()

parking_sessions:
  id uuid PK, spot_id uuid FK vendors,
  user_id uuid FK auth.users,
  vehicle_type text,                   -- 'bike' | 'car' | custom
  check_in_at timestamptz, check_out_at timestamptz,
  duration_minutes int, amount_charged float8,
  status text                          -- 'active' | 'completed'

wallets:
  id uuid PK, user_id uuid FK auth.users,
  balance float8 DEFAULT 0, updated_at timestamptz

wallet_transactions:
  id uuid PK, user_id uuid FK auth.users,
  type text,                           -- 'credit' | 'debit'
  amount float8, note text, created_at timestamptz
─────────────────────────────────────────────────────────────────────────────
"""

import os
import uuid
import json
import qrcode
import io
import base64
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Depends, UploadFile, File, Form
from pydantic import BaseModel

from main import supabase, get_current_user, get_address_from_google

vendor_router = APIRouter()

SUPABASE_BUCKET  = os.getenv("SUPABASE_STORAGE_BUCKET", "vendor-photos")
PLATFORM_FEE_PCT = 0.15   # platform keeps 15 %, vendor gets 85 %
MIN_CHARGE       = 5.0    # ₹ minimum per session

# ─── MODELS ──────────────────────────────────────────────────────────────────

class SpotStatusUpdate(BaseModel):
    is_active: bool

class CheckinRequest(BaseModel):
    vehicle_type: str = "car"   # 'bike' | 'car' | any custom label

# ─── WALLET HELPERS (also imported by app.py) ─────────────────────────────────

def _get_or_create_wallet(user_id: str) -> dict:
    res = supabase.table("wallets").select("*").eq("user_id", user_id).execute()
    if res.data:
        return res.data[0]
    new = supabase.table("wallets").insert({
        "user_id": user_id,
        "balance": 0.0,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }).execute()
    return new.data[0]

def _debit_wallet(user_id: str, amount: float, note: str) -> float:
    wallet = _get_or_create_wallet(user_id)
    if wallet["balance"] < amount:
        raise HTTPException(status_code=402, detail="Insufficient wallet balance.")
    new_bal = round(wallet["balance"] - amount, 2)
    supabase.table("wallets").update({
        "balance": new_bal,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }).eq("user_id", user_id).execute()
    supabase.table("wallet_transactions").insert({
        "user_id": user_id, "type": "debit",
        "amount": amount, "note": note,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }).execute()
    return new_bal

def _credit_wallet(user_id: str, amount: float, note: str) -> float:
    wallet = _get_or_create_wallet(user_id)
    new_bal = round(wallet["balance"] + amount, 2)
    supabase.table("wallets").update({
        "balance": new_bal,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }).eq("user_id", user_id).execute()
    supabase.table("wallet_transactions").insert({
        "user_id": user_id, "type": "credit",
        "amount": amount, "note": note,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }).execute()
    return new_bal

# ─── QR HELPER ───────────────────────────────────────────────────────────────

def _generate_qr_b64(data: str) -> str:
    img = qrcode.make(data)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()

# ─── STORAGE HELPER ──────────────────────────────────────────────────────────

async def _upload_file(file: UploadFile, path: str) -> str:
    """Upload a file to Supabase Storage and return its public URL."""
    data = await file.read()
    ct   = file.content_type or "image/jpeg"
    supabase.storage.from_(SUPABASE_BUCKET).upload(path, data, {"content-type": ct})
    return supabase.storage.from_(SUPABASE_BUCKET).get_public_url(path)

# ─── BILLING HELPER ──────────────────────────────────────────────────────────

def _calculate_charge(spot: dict, vehicle_type: str, duration_minutes: int) -> float:
    """
    Rate logic:
      • First-hour rate  → charged flat for the first 60 min (or part thereof)
      • After-1st-hour rate → charged per hour for every minute beyond 60
      • If duration ≤ 60 min → just the first-hour rate
      • Minimum: MIN_CHARGE (₹5)

    vehicle_type: 'bike' | 'car' | any custom label stored in other_vehicle_rates
    """
    vt = vehicle_type.lower()

    if vt == "bike":
        first_hr  = float(spot.get("rate_bike_first_hour") or 0)
        after_hr  = float(spot.get("rate_bike_after_first_hour") or 0)
    elif vt == "car":
        first_hr  = float(spot.get("rate_car_first_hour") or 0)
        after_hr  = float(spot.get("rate_car_after_first_hour") or 0)
    else:
        # Look in other_vehicle_rates jsonb
        others = spot.get("other_vehicle_rates") or []
        if isinstance(others, str):
            try: others = json.loads(others)
            except: others = []
        matched = next((o for o in others if o.get("type", "").lower() == vt), None)
        if matched:
            first_hr = float(matched.get("first_hour") or 0)
            after_hr = float(matched.get("after_hour") or 0)
        else:
            # Fallback to car rates
            first_hr = float(spot.get("rate_car_first_hour") or 0)
            after_hr = float(spot.get("rate_car_after_first_hour") or 0)

    if duration_minutes <= 60:
        amount = first_hr
    else:
        extra_hours = (duration_minutes - 60) / 60
        amount = first_hr + (after_hr * extra_hours)

    # Platform ₹1 check-in fee (who bears it is recorded but billing is same either way)
    amount = round(amount, 2)
    return max(amount, MIN_CHARGE)

# ═══════════════════════════════════════════════════════════════════════════════
# VENDOR REGISTRATION  (receives the full vendor.html form)
# ═══════════════════════════════════════════════════════════════════════════════

@vendor_router.post("/vendor/register")
async def register_vendor(
    # ── Personal ──────────────────────────────────────────────────────────────
    full_name:       str   = Form(...),
    contact_number:  str   = Form(...),
    business_name:   str   = Form(...),
    city:            str   = Form(...),
    state:           str   = Form(...),
    pincode:         str   = Form(...),
    location_address: str  = Form(...),
    latitude:        Optional[float] = Form(None),
    longitude:       Optional[float] = Form(None),
    digital_signature: str = Form(...),

    # ── Capacity (optional) ───────────────────────────────────────────────────
    capacity_cars:   Optional[int]  = Form(None),
    capacity_bikes:  Optional[int]  = Form(None),
    capacity_other:  Optional[str]  = Form(None),

    # ── Secondary device (optional) ───────────────────────────────────────────
    allow_access_other_device:   str = Form("false"),
    device_number:               Optional[str] = Form(None),
    device_notification_allowed: str = Form("true"),

    # ── Pricing ───────────────────────────────────────────────────────────────
    rate_bike_first_hour:       float = Form(...),
    rate_bike_after_first_hour: float = Form(...),
    rate_car_first_hour:        float = Form(...),
    rate_car_after_first_hour:  float = Form(...),

    # ── Platform fee choice ───────────────────────────────────────────────────
    checking_charge_from_customer: str = Form("false"),
    checking_charge_from_vendor:   str = Form("false"),

    # ── Agreements ────────────────────────────────────────────────────────────
    consent_agreement: str = Form("Agreed"),
    terms_agreement:   str = Form("Agreed"),
    otp_verified:      str = Form("true"),

    # ── Files ─────────────────────────────────────────────────────────────────
    profile_photo:      UploadFile = File(None),
    id_proof:           UploadFile = File(None),
    location_pictures:  list[UploadFile] = File(default=[]),

    current_user=Depends(get_current_user),
):
    """
    Called by vendor.html on submit.
    Saves every field, uploads all photos, generates a unique QR code,
    and stores the vendor as pending (is_approved=False).
    """
    # Reject duplicate registrations
    existing = supabase.table("vendors").select("id").eq("user_id", str(current_user.id)).execute()
    if existing.data:
        raise HTTPException(status_code=409, detail="A vendor profile already exists for this account.")

    spot_id    = str(uuid.uuid4())
    now        = datetime.now(timezone.utc).isoformat()
    photo_url  = None
    id_url     = None
    loc_urls   = []

    # ── Upload profile photo ──────────────────────────────────────────────────
    if profile_photo and profile_photo.filename:
        ext = profile_photo.filename.rsplit(".", 1)[-1] if "." in profile_photo.filename else "jpg"
        photo_url = await _upload_file(profile_photo, f"{spot_id}/profile.{ext}")

    # ── Upload ID proof ───────────────────────────────────────────────────────
    if id_proof and id_proof.filename:
        ext = id_proof.filename.rsplit(".", 1)[-1] if "." in id_proof.filename else "jpg"
        id_url = await _upload_file(id_proof, f"{spot_id}/id_proof.{ext}")

    # ── Upload location pictures ──────────────────────────────────────────────
    for i, pic in enumerate(location_pictures or []):
        if pic and pic.filename:
            ext = pic.filename.rsplit(".", 1)[-1] if "." in pic.filename else "jpg"
            url = await _upload_file(pic, f"{spot_id}/location_{i}.{ext}")
            loc_urls.append(url)

    # ── Geocode address if co-ords missing ────────────────────────────────────
    resolved_address = location_address
    if latitude and longitude and (not location_address or location_address == ""):
        resolved_address = get_address_from_google(latitude, longitude)

    # ── Derive slot count from capacity ──────────────────────────────────────
    total_slots = (capacity_cars or 0) + (capacity_bikes or 0)
    if total_slots == 0:
        total_slots = 1   # default

    # ── QR code ──────────────────────────────────────────────────────────────
    qr_payload = f"pocketparking://spot/{spot_id}"
    qr_b64     = _generate_qr_b64(qr_payload)

    # ── Build row ─────────────────────────────────────────────────────────────
    vendor_row = {
        "id":             spot_id,
        "user_id":        str(current_user.id),

        # Personal / business
        "full_name":      full_name.strip(),
        "business_name":  business_name.strip(),
        "owner_name":     full_name.strip(),   # alias — same field
        "phone":          contact_number.strip(),
        "city":           city.strip(),
        "state":          state.strip(),
        "pincode":        pincode.strip(),
        "address":        resolved_address,
        "latitude":       latitude,
        "longitude":      longitude,

        # Capacity
        "capacity_cars":   capacity_cars,
        "capacity_bikes":  capacity_bikes,
        "capacity_other":  capacity_other,
        "total_slots":     total_slots,
        "available_slots": total_slots,

        # Pricing
        "rate_bike_first_hour":       rate_bike_first_hour,
        "rate_bike_after_first_hour": rate_bike_after_first_hour,
        "rate_car_first_hour":        rate_car_first_hour,
        "rate_car_after_first_hour":  rate_car_after_first_hour,

        # Platform fee
        "checking_charge_from_customer": checking_charge_from_customer.lower() == "true",
        "checking_charge_from_vendor":   checking_charge_from_vendor.lower()   == "true",

        # Secondary device
        "allow_access_other_device":    allow_access_other_device.lower() == "true",
        "device_number":                device_number,
        "device_notification_allowed":  device_notification_allowed.lower() == "true",

        # Media
        "photo_url":           photo_url,
        "location_photo_urls": loc_urls,    # jsonb array
        "id_proof_url":        id_url,

        # QR
        "qr_code_b64": qr_b64,

        # Status
        "is_active":   True,
        "is_approved": False,

        # Agreements
        "digital_signature": digital_signature.strip(),
        "created_at":        now,
    }

    res = supabase.table("vendors").insert(vendor_row).execute()
    if not res.data:
        raise HTTPException(status_code=500, detail="Failed to save vendor. Please try again.")

    return {
        "success":     True,
        "status":      "pending_approval",
        "vendor_id":   spot_id,
        "message":     "Registration submitted! Our team will review and approve your spot within 24 hours.",
        "qr_code_b64": qr_b64,
        "qr_payload":  qr_payload,
    }

# ── Vendor can submit other vehicle rates separately after registration ────────

@vendor_router.post("/vendor/vehicle-rates")
def add_vehicle_rates(
    rates: list[dict],   # [{"type": "Auto", "first_hour": 30, "after_hour": 15}, …]
    current_user=Depends(get_current_user),
):
    """Add / replace custom vehicle type rates for the vendor's spot."""
    res = supabase.table("vendors").update({
        "other_vehicle_rates": rates
    }).eq("user_id", str(current_user.id)).execute()
    if not res.data:
        raise HTTPException(status_code=404, detail="Vendor profile not found.")
    return {"status": "success", "rates_saved": len(rates)}

@vendor_router.get("/vendor/me")
def get_my_vendor_profile(current_user=Depends(get_current_user)):
    """Full vendor profile for the logged-in vendor (used in the QR screen)."""
    res = supabase.table("vendors").select("*").eq("user_id", str(current_user.id)).execute()
    if not res.data:
        raise HTTPException(status_code=404, detail="No vendor profile found.")
    return res.data[0]

@vendor_router.patch("/vendor/toggle")
def toggle_spot(body: SpotStatusUpdate, current_user=Depends(get_current_user)):
    """Vendor marks spot open/closed (e.g. on holiday)."""
    res = supabase.table("vendors").update({"is_active": body.is_active}).eq("user_id", str(current_user.id)).execute()
    if not res.data:
        raise HTTPException(status_code=404, detail="Vendor spot not found.")
    return {"status": "success", "is_active": body.is_active}

# ═══════════════════════════════════════════════════════════════════════════════
# PUBLIC SPOTS  — what the Flutter app map + card grid shows
# ═══════════════════════════════════════════════════════════════════════════════

@vendor_router.get("/spots/")
def get_all_spots():
    """
    Returns all approved + active spots.
    Includes every field the Flutter card grid and spot detail screen needs:
      name, address, city, state, rates, capacity, photos, availability, QR.
    """
    res = supabase.table("vendors").select(
        "id, business_name, owner_name, phone, "
        "address, city, state, pincode, latitude, longitude, "
        "capacity_cars, capacity_bikes, capacity_other, "
        "total_slots, available_slots, "
        "rate_bike_first_hour, rate_bike_after_first_hour, "
        "rate_car_first_hour,  rate_car_after_first_hour, "
        "other_vehicle_rates, "
        "checking_charge_from_customer, checking_charge_from_vendor, "
        "photo_url, location_photo_urls, "
        "is_active, created_at"
        # NOTE: qr_code_b64 is intentionally excluded from the public list
        # — it is only returned in /spots/{id} after the user navigates there
    ).eq("is_active", True).eq("is_approved", True).execute()
    return res.data

@vendor_router.get("/spots/{spot_id}")
def get_spot_detail(spot_id: str):
    """
    Full detail for a single spot — shown when user taps a card.
    Includes the QR code b64 so the app can display it as a fallback
    (vendor's own QR screen is the primary source).
    """
    res = supabase.table("vendors").select("*").eq("id", spot_id).execute()
    if not res.data:
        raise HTTPException(status_code=404, detail="Spot not found.")
    spot = res.data[0]
    # Strip sensitive internal fields before returning
    spot.pop("user_id", None)
    spot.pop("id_proof_url", None)
    spot.pop("device_number", None)
    return spot

# ═══════════════════════════════════════════════════════════════════════════════
# QR CHECK-IN
# ═══════════════════════════════════════════════════════════════════════════════

@vendor_router.post("/session/checkin/{spot_id}")
def checkin(spot_id: str, body: CheckinRequest, current_user=Depends(get_current_user)):
    """
    Called when a driver scans the QR code on arrival.
    vehicle_type: 'bike' | 'car' | custom label (must match a rate in the spot)
    """
    spot_res = supabase.table("vendors").select("*").eq("id", spot_id).execute()
    if not spot_res.data:
        raise HTTPException(status_code=404, detail="Parking spot not found.")
    spot = spot_res.data[0]

    if not spot["is_active"] or not spot["is_approved"]:
        raise HTTPException(status_code=400, detail="This spot is currently unavailable.")
    if spot["available_slots"] <= 0:
        raise HTTPException(status_code=400, detail="No slots available right now. Try another spot.")

    # Prevent duplicate active session at same spot
    open_sess = (
        supabase.table("parking_sessions")
        .select("id")
        .eq("user_id", str(current_user.id))
        .eq("spot_id", spot_id)
        .eq("status", "active")
        .execute()
    )
    if open_sess.data:
        raise HTTPException(status_code=409, detail="You already have an active session here. Scan again to check out.")

    session_id = str(uuid.uuid4())
    now        = datetime.now(timezone.utc).isoformat()

    supabase.table("parking_sessions").insert({
        "id":           session_id,
        "spot_id":      spot_id,
        "user_id":      str(current_user.id),
        "vehicle_type": body.vehicle_type,
        "check_in_at":  now,
        "status":       "active",
    }).execute()

    supabase.table("vendors").update({
        "available_slots": spot["available_slots"] - 1
    }).eq("id", spot_id).execute()

    # Compute expected first-hour rate to show user upfront
    first_hr = _calculate_charge(spot, body.vehicle_type, 60)

    return {
        "status":        "success",
        "message":       f"Checked in to {spot['business_name']}. Safe parking! 🅿️",
        "session_id":    session_id,
        "check_in_at":   now,
        "vehicle_type":  body.vehicle_type,
        "spot_name":     spot["business_name"],
        "spot_address":  spot["address"],
        "first_hr_rate": first_hr,
        "note":          "Scan the QR again when you leave to check out and pay.",
    }

# ═══════════════════════════════════════════════════════════════════════════════
# QR CHECK-OUT
# ═══════════════════════════════════════════════════════════════════════════════

@vendor_router.post("/session/checkout/{spot_id}")
def checkout(spot_id: str, current_user=Depends(get_current_user)):
    """
    Called when a driver scans the QR code on exit.
    Auto-detects vehicle type from the active session.
    Charges wallet using per-vehicle-type rates.
    """
    spot_res = supabase.table("vendors").select("*").eq("id", spot_id).execute()
    if not spot_res.data:
        raise HTTPException(status_code=404, detail="Parking spot not found.")
    spot = spot_res.data[0]

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

    check_in_dt      = datetime.fromisoformat(session["check_in_at"].replace("Z", "+00:00"))
    check_out_dt     = datetime.now(timezone.utc)
    duration_minutes = max(1, int((check_out_dt - check_in_dt).total_seconds() / 60))
    vehicle_type     = session.get("vehicle_type") or "car"

    amount      = _calculate_charge(spot, vehicle_type, duration_minutes)
    vendor_cut  = round(amount * (1 - PLATFORM_FEE_PCT), 2)

    # Debit driver
    _debit_wallet(
        str(current_user.id), amount,
        f"Parking at {spot['business_name']} — {vehicle_type} ({duration_minutes} min)"
    )

    # Credit vendor
    _credit_wallet(
        spot["user_id"], vendor_cut,
        f"Parking income — {vehicle_type} ({duration_minutes} min)"
    )

    # Close session
    supabase.table("parking_sessions").update({
        "check_out_at":    check_out_dt.isoformat(),
        "duration_minutes": duration_minutes,
        "amount_charged":   amount,
        "status":           "completed",
    }).eq("id", session["id"]).execute()

    # Restore slot
    supabase.table("vendors").update({
        "available_slots": spot["available_slots"] + 1
    }).eq("id", spot_id).execute()

    return {
        "status":          "success",
        "message":         "Checked out successfully! Thanks for parking with us. 👋",
        "duration_minutes": duration_minutes,
        "vehicle_type":    vehicle_type,
        "amount_charged":  amount,
        "vendor_received": vendor_cut,
        "platform_fee":    round(amount - vendor_cut, 2),
    }

# ═══════════════════════════════════════════════════════════════════════════════
# SESSIONS
# ═══════════════════════════════════════════════════════════════════════════════

@vendor_router.get("/session/active")
def get_active_session(current_user=Depends(get_current_user)):
    """
    Returns the user's currently active parking session.
    The Flutter QR scanner calls this first to decide checkin vs checkout.
    """
    res = (
        supabase.table("parking_sessions")
        .select(
            "id, spot_id, vehicle_type, check_in_at, status, "
            "vendors(business_name, address, city, "
            "rate_bike_first_hour, rate_bike_after_first_hour, "
            "rate_car_first_hour,  rate_car_after_first_hour)"
        )
        .eq("user_id", str(current_user.id))
        .eq("status", "active")
        .execute()
    )
    return res.data[0] if res.data else None

@vendor_router.get("/session/history")
def get_session_history(current_user=Depends(get_current_user)):
    """Last 50 completed sessions for the logged-in user."""
    res = (
        supabase.table("parking_sessions")
        .select("*, vendors(business_name, address, city)")
        .eq("user_id", str(current_user.id))
        .eq("status", "completed")
        .order("check_out_at", desc=True)
        .limit(50)
        .execute()
    )
    return res.data