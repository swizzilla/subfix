from contextlib import asynccontextmanager
from fastapi import FastAPI
import httpx
import asyncio

from app.config import DEBUG, TELEGRAM_TOKEN, SERVER_BASE_URL
from app.database import init_db
from app.routers import oauth
from app.routers.mtproto_telegram import router as mtproto_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize database and setup MTProto client on startup"""
    init_db()
    
    # Setup MTProto client instead of registering webhook for bot API
    try:
        from app.services.telethon_client import mtproto_client
        # Starting the MTProto client
        await mtproto_client.start_client()
        print("✅ MTProto client initialized successfully")
    except Exception as e:
        print(f"❌ Failed to initialize MTProto client: {str(e)}")
    
    yield


app = FastAPI(
    title="YouTube Audio to YouTube Uploader",
    description="Upload YouTube videos from audio files via Telegram MTProto",
    version="1.0.0",
    lifespan=lifespan,
    debug=DEBUG,
)

# Routers
app.include_router(oauth.router, prefix="/oauth", tags=["OAuth"])
app.include_router(mtproto_router, prefix="/mtproto", tags=["MTProto Telegram"])

@app.get("/")
async def root():
    return {"status": "running", "message": "MTProto client active. You can now interact via Telegram."}

@app.get("/health")
async def health():
    return {"status": "ok"}