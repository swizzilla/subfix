from fastapi import APIRouter, BackgroundTasks, Depends
from sqlalchemy.orm import Session
import asyncio
from app.database import get_db
from app.services.conversation import ConversationManager
from app.services.telethon_client import mtproto_client
from app.config import ALLOWED_TELEGRAM_CHAT_IDS, TEMP_DIR, CREDENTIALS_DIR
from app.database import Account
from app.services.video import process_uploaded_audio
from app.services.youtube import upload_video
from app.config import GOOGLE_CLIENT_ID

router = APIRouter()

async def process_and_upload_async(chat_id: str, db: Session):
    """Async version of the background task to process and upload video"""
    # Verify this is the allowed chat ID before processing
    # Verify this is an allowed chat ID before processing
    if str(chat_id) not in ALLOWED_TELEGRAM_CHAT_IDS:
        return  # Don't process for unauthorized users

    try:
        manager = ConversationManager(db, chat_id)
        data = manager.get_upload_data()

        if not data["account"]:
            await mtproto_client.send_message(chat_id, "Error: No account selected.")
            manager.reset()
            return

        account = data["account"]
        thumbnail_path = data.get("thumbnail_path")

        # Process uploaded audio file (this is now the only option)
        if data.get("audio_path"):
            await mtproto_client.send_message(chat_id, "Processing uploaded audio...")
            
            video_path = process_uploaded_audio(
                audio_path=data["audio_path"],
                thumbnail_path=thumbnail_path if thumbnail_path and not thumbnail_path.startswith("http") else None,
            )

            await mtproto_client.send_message(chat_id, "Uploading to YouTube...")

            result = upload_video(
                credentials_path=account.credentials_path,
                video_path=video_path,
                title=data["title"],
                description=data["description"],
                privacy=data["privacy"],
                thumbnail_path=thumbnail_path if thumbnail_path and not thumbnail_path.startswith("http") else None,
            )

            await mtproto_client.send_message(chat_id, f"Done!\n{result['video_url']}")

            # Cleanup temp files (you'd need to implement this function)
            from app.services.video import cleanup_temp_files
            cleanup_temp_files(video_path)
            # Clean up audio file if it was an uploaded one
            if data.get("audio_path"):
                cleanup_temp_files(data["audio_path"])
            if thumbnail_path and not thumbnail_path.startswith("http"):
                cleanup_temp_files(thumbnail_path)

            manager.mark_complete()

    except Exception as e:
        await mtproto_client.send_message(chat_id, f"Error: {str(e)}")
        manager = ConversationManager(db, chat_id)
        manager.reset()


def process_and_upload_telegram(chat_id: str, db: Session):
    """Background task to process and upload video"""
    import asyncio
    # Run the async version in an event loop
    asyncio.run(process_and_upload_async(chat_id, db))


@router.get("/start_mtproto")
async def start_mtproto():
    """Endpoint to manually start the MTProto client if needed"""
    try:
        await mtproto_client.start_client()
        return {"status": "MTProto client started successfully"}
    except Exception as e:
        return {"error": str(e)}


@router.get("/auth/code")
async def submit_auth_code(code: str):
    """Submit the Telegram verification code"""
    if not mtproto_client.waiting_for_code:
        return {"error": "Not waiting for a verification code"}

    mtproto_client.submit_code(code)
    return {"status": "Code submitted successfully. Authentication in progress..."}


@router.get("/auth/password")
async def submit_auth_password(password: str):
    """Submit the 2FA password"""
    if not mtproto_client.waiting_for_password:
        return {"error": "Not waiting for a 2FA password"}

    mtproto_client.submit_password(password)
    return {"status": "Password submitted successfully. Authentication in progress..."}


@router.get("/auth/status")
async def auth_status():
    """Check the current authentication status"""
    return {
        "is_running": mtproto_client.is_running,
        "waiting_for_code": mtproto_client.waiting_for_code,
        "waiting_for_password": mtproto_client.waiting_for_password,
    }