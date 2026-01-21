import os
import requests
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from supabase import create_client, Client

# --- CONFIGURATION ---
load_dotenv()

# Load keys
GOOGLE_MAPS_API_KEY = os.getenv("GOOGLE_MAPS_API_KEY")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

# Check if keys are present
if not SUPABASE_URL or not SUPABASE_KEY:
    raise ValueError("Supabase URL or Key is missing in .env file")

# Initialize Supabase Client
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# --- MODELS ---
class ParkingSpotCreate(BaseModel):
    latitude: float
    longitude: float
    owner_name: str | None = "Anonymous"

class ParkingSpotResponse(BaseModel):
    status: str
    data: dict
    message: str

# --- FASTAPI APP ---
app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- HELPER: Google Maps ---
def get_address_from_google(lat: float, lng: float) -> str:
    if not GOOGLE_MAPS_API_KEY or "YOUR_" in GOOGLE_MAPS_API_KEY:
        return "Address lookup disabled"

    url = f"https://maps.googleapis.com/maps/api/geocode/json?latlng={lat},{lng}&key={GOOGLE_MAPS_API_KEY}"
    try:
        response = requests.get(url)
        data = response.json()
        if data.get('status') == 'OK':
            return data['results'][0]['formatted_address']
        return "Address not found"
    except:
        return "Error retrieving address"

# --- ENDPOINTS ---

@app.post("/mark-spot/", response_model=ParkingSpotResponse)
def mark_parking_spot(spot: ParkingSpotCreate):
    # 1. Get Address
    readable_address = get_address_from_google(spot.latitude, spot.longitude)

    # 2. Prepare Data for Supabase
    spot_data = {
        "latitude": spot.latitude,
        "longitude": spot.longitude,
        "address": readable_address,
        "owner_name": spot.owner_name,
        "is_active": True
    }

    # 3. Insert into Supabase
    try:
        # .insert() returns a response object. .execute() runs it.
        response = supabase.table("parking_spots").insert(spot_data).execute()
        
        # Check if we got data back (means success)
        if hasattr(response, 'data') and len(response.data) > 0:
            return {
                "status": "success",
                "data": response.data[0],
                "message": "Garden successfully marked as parking spot!"
            }
        else:
            raise HTTPException(status_code=500, detail="Supabase insert failed (no data returned)")

    except Exception as e:
        print(f"Supabase Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/spots/")
def get_all_spots():
    # Fetch all active spots
    response = supabase.table("parking_spots").select("*").execute()
    return response.data

@app.get("/")
def read_root():
    return {"message": "Parking Backend is running with Supabase Client!"}