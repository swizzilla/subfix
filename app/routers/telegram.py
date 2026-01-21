from fastapi import APIRouter, Request, BackgroundTasks, Depends
from sqlalchemy.orm import Session
import httpx
from app.database import get_db
from app.services.conversation import ConversationManager
from app.config import TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, ALLOWED_TELEGRAM_CHAT_IDS, TEMP_DIR, CREDENTIALS_DIR, SERVER_BASE_URL
from app.database import Account
from app.services.video import process_uploaded_audio, cleanup_temp_files
from app.services.youtube import upload_video, get_authorization_url
from app.config import GOOGLE_CLIENT_ID
from app.services.telethon_client import mtproto_client

router = APIRouter()


async def send_telegram_message(chat_id: str, message: str):
    """Sends a message back to Telegram"""
    # Check if this is an allowed chat ID before sending a message
    if str(chat_id) not in ALLOWED_TELEGRAM_CHAT_IDS:
        # Don't send messages to unauthorized users
        return
    
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    async with httpx.AsyncClient() as client:
        await client.post(url, json={
            "chat_id": chat_id,
            "text": message,
            "parse_mode": "HTML"
        })


async def download_media_from_telegram(file_url: str) -> str:
    """Download media from Telegram and save to temp directory"""
    import uuid
    async with httpx.AsyncClient() as client:
        response = await client.get(file_url)
        response.raise_for_status()

        content_type = response.headers.get("content-type", "")
        ext = ".jpg" if "jpeg" in content_type or "jpg" in content_type else ".png" if "png" in content_type else ".jpg"

        filename = f"thumb_{uuid.uuid4().hex[:8]}{ext}"
        filepath = TEMP_DIR / filename

        with open(filepath, "wb") as f:
            f.write(response.content)

        return str(filepath)


async def download_audio_from_telegram(file_url: str, file_extension: str = ".mp3") -> str:
    """Download audio from Telegram and save to temp directory"""
    import uuid
    async with httpx.AsyncClient() as client:
        response = await client.get(file_url)
        response.raise_for_status()

        filename = f"audio_{uuid.uuid4().hex[:8]}{file_extension}"
        filepath = TEMP_DIR / filename

        with open(filepath, "wb") as f:
            f.write(response.content)

        return str(filepath)


async def process_and_upload_async(chat_id: str, db: Session):
    """Async version of the background task to process and upload video"""
    # Verify this is an allowed chat ID before processing
    if str(chat_id) not in ALLOWED_TELEGRAM_CHAT_IDS:
        return  # Don't process for unauthorized users

    try:
        manager = ConversationManager(db, chat_id)
        data = manager.get_upload_data()

        if not data["account"]:
            await send_telegram_message(chat_id, "Error: No account selected.")
            manager.reset()
            return

        account = data["account"]
        thumbnail_path = data.get("thumbnail_path")

        # Process uploaded audio file (this is now the only option)
        if data.get("audio_path"):
            await send_telegram_message(chat_id, "Processing uploaded audio...")
            
            video_path = process_uploaded_audio(
                audio_path=data["audio_path"],
                thumbnail_path=thumbnail_path if thumbnail_path and not thumbnail_path.startswith("http") else None,
            )

            await send_telegram_message(chat_id, "Uploading to YouTube...")

            result = upload_video(
                credentials_path=account.credentials_path,
                video_path=video_path,
                title=data["title"],
                description=data["description"],
                privacy=data["privacy"],
                thumbnail_path=thumbnail_path if thumbnail_path and not thumbnail_path.startswith("http") else None,
            )

            await send_telegram_message(chat_id, f"Done!\n{result['video_url']}")

            cleanup_temp_files(video_path)
            # Clean up audio file if it was an uploaded one
            if data.get("audio_path"):
                cleanup_temp_files(data["audio_path"])
            if thumbnail_path and not thumbnail_path.startswith("http"):
                cleanup_temp_files(thumbnail_path)

            manager.mark_complete()

    except Exception as e:
        await send_telegram_message(chat_id, f"Error: {str(e)}")
        manager = ConversationManager(db, chat_id)
        manager.reset()


def process_and_upload_telegram(chat_id: str, db: Session):
    """Background task to process and upload video"""
    import asyncio
    # Run the async version in an event loop
    asyncio.run(process_and_upload_async(chat_id, db))


def create_account_and_get_auth_url(db: Session, account_name: str, chat_id: str) -> str:
    """Create account in DB and return OAuth URL"""
    # Verify this is an allowed chat ID before creating an account
    if str(chat_id) not in ALLOWED_TELEGRAM_CHAT_IDS:
        return ""  # Don't allow unauthorized users to create accounts
        
    credentials_path = CREDENTIALS_DIR / f"{account_name}_credentials.pickle"

    account = Account(
        name=account_name,
        credentials_path=str(credentials_path),
    )
    db.add(account)
    db.commit()
    db.refresh(account)

    # State format: account_id:chat_id (to send confirmation after OAuth)
    state = f"{account.id}:{chat_id}"
    return get_authorization_url(state=state)


@router.post("/webhook")
async def telegram_webhook(request: Request, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    data = await request.json()
    
    # Extract message info from Telegram JSON
    if "message" not in data:
        return {"status": "ok"}

    chat_id = str(data["message"]["chat"]["id"])
    text = data["message"].get("text", "")
    
    # Safety: Only allow specific Chat IDs from the list
    # Check if the user is authorized
    if chat_id not in ALLOWED_TELEGRAM_CHAT_IDS:
        # For debugging purposes, we can send a simple unauthorized response
        # but per our security model, unauthorized users shouldn't receive this
        print(f"Unauthorized access attempt from chat_id: {chat_id}")
        print(f"Configured allowed_chat_ids: {ALLOWED_TELEGRAM_CHAT_IDS}")
        return {"status": "unauthorized"}

    # Handle MTProto authentication codes/passwords via Bot API messages
    if text and mtproto_client.waiting_for_code:
        # Check if the message looks like a code (digits only, 5-6 chars)
        clean_text = text.strip().replace(" ", "").replace("-", "")
        if clean_text.isdigit() and 4 <= len(clean_text) <= 8:
            mtproto_client.submit_code(clean_text)
            await send_telegram_message(chat_id, "✅ Code received! Authenticating...")
            return {"status": "ok"}

    if text and mtproto_client.waiting_for_password:
        # Any text message while waiting for password is treated as the password
        mtproto_client.submit_password(text.strip())
        await send_telegram_message(chat_id, "✅ Password received! Authenticating...")
        return {"status": "ok"}

    # Check Google OAuth is configured
    if not GOOGLE_CLIENT_ID:
        await send_telegram_message(chat_id, "Server not configured. Set GOOGLE_CLIENT_ID in .env")
        return {"status": "ok"}

    manager = ConversationManager(db, str(chat_id))

    # Handle different types of media (photos, audio, documents)
    media_path = None
    audio_path = None
    
    # Handle photos
    photo_objects = data["message"].get("photo", [])
    if photo_objects:
        # Get the largest photo (last in array)
        photo_obj = photo_objects[-1]
        file_id = photo_obj["file_id"]
        
        # Get file URL from Telegram API
        file_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getFile?file_id={file_id}"
        async with httpx.AsyncClient() as client:
            file_response = await client.get(file_url)
            file_data = file_response.json()
            
            if file_response.status_code == 200 and "result" in file_data:
                file_path = file_data["result"]["file_path"]
                full_file_url = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file_path}"
                
                try:
                    media_path = await download_media_from_telegram(full_file_url)
                except Exception:
                    await send_telegram_message(chat_id, "Failed to download image. Try again.")
                    return {"status": "ok"}

    # Handle audio files (voice messages and audio files)
    audio_objects = []
    if "voice" in data["message"]:
        audio_objects.append(("voice", data["message"]["voice"]))
    if "audio" in data["message"]:
        audio_objects.append(("audio", data["message"]["audio"]))
    if "document" in data["message"]:
        doc = data["message"]["document"]
        # Check if document is an audio file by checking the mime type
        doc_mime = doc.get("mime_type", "")
        # More comprehensive check for audio MIME types
        is_audio_mime = any(mime_check in doc_mime.lower() for mime_check in ['audio/', 'video/', 'octet-stream'])
        # Additionally check by file extension if MIME type doesn't clearly indicate audio
        doc_filename = doc.get("file_name", "").lower()
        is_audio_ext = any(doc_filename.endswith(ext) for ext in ['.mp3', '.m4a', '.wav', '.flac', '.aac', '.ogg', '.opus', '.wma', '.m4p', '.mp2', '.mpa', '.mpc', '.ape', '.aiff', '.au', '.m3u', '.m4b', '.oga', '.wv', '.tta'])
        
        if is_audio_mime or is_audio_ext:
            audio_objects.append(("document", doc))

    for audio_type, audio_obj in audio_objects:
        file_id = audio_obj["file_id"]
        
        # Get file URL from Telegram API
        file_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getFile?file_id={file_id}"
        async with httpx.AsyncClient() as client:
            file_response = await client.get(file_url)
            file_data = file_response.json()
            
            if file_response.status_code == 200 and "result" in file_data:
                file_path = file_data["result"]["file_path"]
                full_file_url = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file_path}"
                
                # Determine file extension based on type
                if audio_type == "voice":
                    ext = ".ogg"
                elif audio_type == "audio":
                    ext = "." + audio_obj.get("file_name", "").split('.')[-1] if audio_obj.get("file_name") else ".mp3"
                elif audio_type == "document":
                    ext = "." + audio_obj.get("file_name", "").split('.')[-1] if audio_obj.get("file_name") else ".mp3"
                else:
                    ext = ".mp3"
                    
                try:
                    audio_path = await download_audio_from_telegram(full_file_url, ext)
                except Exception:
                    await send_telegram_message(chat_id, "Failed to download audio. Try again.")
                    return {"status": "ok"}

    # Preserve case for title/description
    state = manager.conversation.state
    original_text = text.strip()

    if state == "awaiting_title":
        manager.set_title(original_text)
    if state == "awaiting_description":
        manager.set_description(original_text)

    # Process message
    reply = manager.process_message(text, media_path, audio_path)

    # Handle special actions
    if isinstance(reply, dict):
        if reply.get("action") == "create_account":
            auth_url = create_account_and_get_auth_url(db, reply["account_name"], chat_id)
            if auth_url:  # Only proceed if the user is authorized
                manager.reset()
                await send_telegram_message(chat_id, f"Click to authorize:\n{auth_url}")
            return {"status": "ok"}

    # Start upload if processing
    if manager.conversation.state == "processing":
        background_tasks.add_task(process_and_upload_telegram, chat_id, db)

    # Send the reply back
    await send_telegram_message(chat_id, reply)
    return {"status": "ok"}


@router.get("/webhook")
async def telegram_webhook_verify():
    return {"status": "ok"}