"""
Central place for reading environment/config. Everything here has a
sane default so the app runs out of the box with `uvicorn app.main:app`
even before you've created a .env file.
"""
import os
from dotenv import load_dotenv

load_dotenv()

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "").strip()
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./ipo.db")
REFRESH_INTERVAL_MINUTES = int(os.getenv("REFRESH_INTERVAL_MINUTES", "30"))
CACHE_TTL_MINUTES = int(os.getenv("CACHE_TTL_MINUTES", "20"))

CORS_ORIGINS = [
    origin.strip()
    for origin in os.getenv("CORS_ORIGINS", "*").split(",")
    if origin.strip()
]

LLM_ENABLED = bool(ANTHROPIC_API_KEY)
