import os
import requests
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
from supabase import create_client, Client

# --- CONFIGURATION ---
load_dotenv()

GOOGLE_MAPS_API_KEY = os.getenv("GOOGLE_MAPS_API_KEY")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

if not SUPABASE_URL or not SUPABASE_KEY:
    raise ValueError("Supabase URL or Key is missing in .env file")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# --- SECURITY ---
bearer_scheme = HTTPBearer()

# --- MODELS ---

# Auth
class SendOTPRequest(BaseModel):
    phone: str  # E.164 format, e.g. +919876543210

class VerifyOTPRequest(BaseModel):
    phone: str
    token: str  # 6-digit OTP

class AuthResponse(BaseModel):
    status: str
    message: str
    access_token: str | None = None
    refresh_token: str | None = None
    user_id: str | None = None

# Parking
class ParkingSpotCreate(BaseModel):
    latitude: float
    longitude: float
    owner_name: str | None = "Anonymous"

class ParkingSpotResponse(BaseModel):
    status: str
    data: dict
    message: str

# --- FASTAPI APP ---
app = FastAPI(title="Parking Backend with OTP Auth")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- HELPER: Verify JWT token from Supabase ---
def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme)):
    """
    Validates the Bearer token by calling Supabase's get_user().
    Attach this as a dependency to any protected endpoint.
    """
    token = credentials.credentials
    try:
        user_response = supabase.auth.get_user(token)
        if not user_response or not user_response.user:
            raise HTTPException(status_code=401, detail="Invalid or expired token")
        return user_response.user
    except Exception as e:
        raise HTTPException(status_code=401, detail=f"Authentication failed: {str(e)}")

# --- HELPER: Google Maps Geocoding ---
def get_address_from_google(lat: float, lng: float) -> str:
    if not GOOGLE_MAPS_API_KEY or "YOUR_" in GOOGLE_MAPS_API_KEY:
        return "Address lookup disabled"
    url = f"https://maps.googleapis.com/maps/api/geocode/json?latlng={lat},{lng}&key={GOOGLE_MAPS_API_KEY}"
    try:
        response = requests.get(url)
        data = response.json()
        if data.get("status") == "OK":
            return data["results"][0]["formatted_address"]
        return "Address not found"
    except:
        return "Error retrieving address"

# =============================================================================
# AUTH ENDPOINTS
# =============================================================================

@app.post("/auth/send-otp", response_model=AuthResponse)
def send_otp(body: SendOTPRequest):
    """
    Step 1 — Send a 6-digit OTP to the user's phone via Supabase + Twilio.

    Phone must be in E.164 format: +919876543210
    Supabase internally uses Twilio (configured in your Supabase dashboard)
    to deliver the SMS.
    """
    try:
        supabase.auth.sign_in_with_otp({
            "phone": body.phone,
        })
        return AuthResponse(
            status="success",
            message=f"OTP sent to {body.phone}. Please check your SMS."
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to send OTP: {str(e)}")


@app.post("/auth/verify-otp", response_model=AuthResponse)
def verify_otp(body: VerifyOTPRequest):
    """
    Step 2 — Verify the OTP the user received via SMS.

    On success, returns a Supabase JWT (access_token + refresh_token).
    Use the access_token as a Bearer token for all protected endpoints.
    """
    try:
        auth_response = supabase.auth.verify_otp({
            "phone": body.phone,
            "token": body.token,
            "type": "sms",
        })

        session = auth_response.session
        user = auth_response.user

        if not session or not user:
            raise HTTPException(status_code=401, detail="OTP verification failed")

        return AuthResponse(
            status="success",
            message="Phone verified successfully. You are now logged in.",
            access_token=session.access_token,
            refresh_token=session.refresh_token,
            user_id=str(user.id),
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"OTP verification failed: {str(e)}")


@app.post("/auth/refresh-token")
def refresh_token(body: dict):
    """
    Refresh an expired access_token using the refresh_token.
    Body: { "refresh_token": "<your_refresh_token>" }
    """
    try:
        refresh = body.get("refresh_token")
        if not refresh:
            raise HTTPException(status_code=400, detail="refresh_token is required")

        auth_response = supabase.auth.refresh_session(refresh)
        session = auth_response.session

        if not session:
            raise HTTPException(status_code=401, detail="Could not refresh session")

        return {
            "status": "success",
            "access_token": session.access_token,
            "refresh_token": session.refresh_token,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Token refresh failed: {str(e)}")


@app.post("/auth/logout")
def logout(credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme)):
    """
    Logs out the current user and invalidates their session on Supabase.
    Requires: Authorization: Bearer <access_token>
    """
    try:
        supabase.auth.sign_out()
        return {"status": "success", "message": "Logged out successfully"}
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Logout failed: {str(e)}")


@app.get("/auth/me")
def get_me(current_user=Depends(get_current_user)):
    """
    Returns the currently authenticated user's info.
    Requires: Authorization: Bearer <access_token>
    """
    return {
        "status": "success",
        "user_id": str(current_user.id),
        "phone": current_user.phone,
        "created_at": str(current_user.created_at),
    }

# =============================================================================
# PARKING ENDPOINTS (Protected)
# =============================================================================

@app.post("/mark-spot/", response_model=ParkingSpotResponse)
def mark_parking_spot(
    spot: ParkingSpotCreate,
    current_user=Depends(get_current_user)   # 🔒 Protected
):
    """
    Mark a new parking spot. Requires a valid Bearer token.
    The spot is automatically linked to the authenticated user.
    """
    readable_address = get_address_from_google(spot.latitude, spot.longitude)

    spot_data = {
        "latitude": spot.latitude,
        "longitude": spot.longitude,
        "address": readable_address,
        "owner_name": spot.owner_name,
        "is_active": True,
        "user_id": str(current_user.id),   # Link spot to the logged-in user
    }

    try:
        response = supabase.table("parking_spots").insert(spot_data).execute()

        if hasattr(response, "data") and len(response.data) > 0:
            return {
                "status": "success",
                "data": response.data[0],
                "message": "Parking spot successfully marked!",
            }
        else:
            raise HTTPException(status_code=500, detail="Supabase insert failed (no data returned)")

    except Exception as e:
        print(f"Supabase Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/spots/")
def get_all_spots():
    """
    Returns all active parking spots. Public endpoint — no auth required.
    """
    response = supabase.table("parking_spots").select("*").execute()
    return response.data


@app.get("/my-spots/")
def get_my_spots(current_user=Depends(get_current_user)):   # 🔒 Protected
    """
    Returns only the parking spots belonging to the logged-in user.
    """
    response = (
        supabase.table("parking_spots")
        .select("*")
        .eq("user_id", str(current_user.id))
        .execute()
    )
    return response.data


# --- ROOT ---
@app.get("/")
def read_root():
    return {"message": "Parking Backend with Supabase OTP Auth is running!"}