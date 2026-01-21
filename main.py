import requests
from fastapi import FastAPI, Depends, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy import create_engine, Column, Integer, Float, String, Boolean
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, Session

# --- CONFIGURATION ---
# Replace this with your actual Google Maps API Key
GOOGLE_MAPS_API_KEY = "YOUR_GOOGLE_MAPS_API_KEY"
DATABASE_URL = "sqlite:///./parking.db"

# --- DATABASE SETUP ---
# creating the database connection
engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

# This defines what our database table looks like
class ParkingSpotDB(Base):
    __tablename__ = "parking_spots"

    id = Column(Integer, primary_key=True, index=True)
    latitude = Column(Float, nullable=False)
    longitude = Column(Float, nullable=False)
    address = Column(String, nullable=True)
    owner_name = Column(String, default="Anonymous")
    is_active = Column(Boolean, default=True)

# Create the tables in the database
Base.metadata.create_all(bind=engine)

# --- PYDANTIC MODELS (Data Validation) ---
# This is the data we expect from the frontend
class ParkingSpotCreate(BaseModel):
    latitude: float
    longitude: float
    owner_name: str | None = "Anonymous"

# This is the data we send back to the user
class ParkingSpotResponse(BaseModel):
    id: int
    latitude: float
    longitude: float
    address: str | None
    message: str

# --- FASTAPI APP SETUP ---
app = FastAPI()

# !!! CRITICAL: CORS MIDDLEWARE !!!
# This allows your frontend (running on a different port/domain) to talk to this backend.
# In production, replace ["*"] with your specific frontend domain (e.g., ["http://localhost:3000"])
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], 
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- DEPENDENCIES ---
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# --- HELPER FUNCTIONS ---
def get_address_from_google(lat: float, lng: float) -> str:
    """
    Reverse Geocoding: Converts lat/lng to a readable address using Google API.
    """
    if GOOGLE_MAPS_API_KEY == "YOUR_GOOGLE_MAPS_API_KEY":
        return "Address lookup disabled (API Key missing)"

    url = f"https://maps.googleapis.com/maps/api/geocode/json?latlng={lat},{lng}&key={GOOGLE_MAPS_API_KEY}"
    
    try:
        response = requests.get(url)
        data = response.json()
        
        if data.get('status') == 'OK':
            # Return the first (most accurate) address found
            return data['results'][0]['formatted_address']
        else:
            print(f"Google API Error: {data.get('status')}")
            return "Address not found"
            
    except Exception as e:
        print(f"Connection Error: {e}")
        return "Error retrieving address"

# --- API ENDPOINTS ---

@app.post("/mark-spot/", response_model=ParkingSpotResponse)
def mark_parking_spot(spot: ParkingSpotCreate, db: Session = Depends(get_db)):
    """
    Receives coordinates, fetches the address from Google, and saves the spot.
    """
    # 1. Get readable address
    readable_address = get_address_from_google(spot.latitude, spot.longitude)
    
    # 2. Prepare database object
    new_spot = ParkingSpotDB(
        latitude=spot.latitude,
        longitude=spot.longitude,
        address=readable_address,
        owner_name=spot.owner_name
    )
    
    # 3. Save to DB
    db.add(new_spot)
    db.commit()
    db.refresh(new_spot)
    
    return {
        "id": new_spot.id,
        "latitude": new_spot.latitude,
        "longitude": new_spot.longitude,
        "address": new_spot.address,
        "message": "Garden successfully marked as parking spot!"
    }

@app.get("/spots/")
def get_all_spots(db: Session = Depends(get_db)):
    """
    Returns a list of all registered parking spots.
    """
    return db.query(ParkingSpotDB).all()

# --- FOR TESTING CONNECTION ---
@app.get("/")
def read_root():
    return {"message": "Parking Backend is running!"}