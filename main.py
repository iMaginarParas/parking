import os
import requests
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
from supabase import create_client, Client

load_dotenv()

GOOGLE_MAPS_API_KEY = os.getenv("GOOGLE_MAPS_API_KEY")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

if not SUPABASE_URL or not SUPABASE_KEY:
    raise ValueError("Supabase URL or Key is missing in .env file")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
bearer_scheme = HTTPBearer()

app = FastAPI(title="Pocket Parking API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─── MODELS ──────────────────────────────────────────────────────────────────

class SendOTPRequest(BaseModel):
    phone: str

class VerifyOTPRequest(BaseModel):
    phone: str
    token: str

class AuthResponse(BaseModel):
    status: str
    message: str
    access_token: str | None = None
    refresh_token: str | None = None
    user_id: str | None = None

# ─── HELPERS ─────────────────────────────────────────────────────────────────

def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme)):
    token = credentials.credentials
    try:
        user_response = supabase.auth.get_user(token)
        if not user_response or not user_response.user:
            raise HTTPException(status_code=401, detail="Invalid or expired token")
        return user_response.user
    except Exception as e:
        raise HTTPException(status_code=401, detail=f"Authentication failed: {str(e)}")

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

# ─── AUTH ENDPOINTS ──────────────────────────────────────────────────────────

@app.post("/auth/send-otp", response_model=AuthResponse)
def send_otp(body: SendOTPRequest):
    """Send a 6-digit OTP via Supabase + Twilio SMS."""
    try:
        supabase.auth.sign_in_with_otp({"phone": body.phone})
        return AuthResponse(status="success", message=f"OTP sent to {body.phone}.")
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to send OTP: {str(e)}")

@app.post("/auth/verify-otp", response_model=AuthResponse)
def verify_otp(body: VerifyOTPRequest):
    """Verify SMS OTP and return Supabase JWT tokens."""
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
            message="Phone verified. Logged in.",
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
    """Refresh an expired access_token using the refresh_token."""
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
    try:
        supabase.auth.sign_out()
        return {"status": "success", "message": "Logged out successfully"}
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Logout failed: {str(e)}")

@app.get("/auth/me")
def get_me(current_user=Depends(get_current_user)):
    return {
        "status": "success",
        "user_id": str(current_user.id),
        "phone": current_user.phone,
        "created_at": str(current_user.created_at),
    }

@app.get("/")
def read_root():
    return {"message": "Pocket Parking API is running."}