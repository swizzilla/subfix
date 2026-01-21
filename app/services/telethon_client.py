# Fix the telethon_client.py file
from telethon import TelegramClient, events
from telethon.errors import SessionPasswordNeededError
from app.config import (
    TELEGRAM_API_ID,
    TELEGRAM_API_HASH,
    TELEGRAM_PHONE_NUMBER,
    ALLOWED_TELEGRAM_CHAT_IDS,
    TELEGRAM_TOKEN,
)
from app.database import get_db
from app.services.conversation import ConversationManager
from app.services.video import process_uploaded_audio, cleanup_temp_files
from app.services.youtube import upload_video
import tempfile
import os
import logging
import asyncio
import httpx

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class TelegramMTProtoClient:
    def __init__(self):
        self.api_id = TELEGRAM_API_ID
        self.api_hash = TELEGRAM_API_HASH
        self.phone_number = TELEGRAM_PHONE_NUMBER
        self.allowed_chat_ids = ALLOWED_TELEGRAM_CHAT_IDS
        self.client = TelegramClient('dawah_session', self.api_id, self.api_hash)
        self.is_running = False

        # Authentication state
        self.pending_code = None
        self.pending_password = None
        self.auth_code_event = asyncio.Event()
        self.auth_password_event = asyncio.Event()
        self.waiting_for_code = False
        self.waiting_for_password = False

    async def _send_bot_api_message(self, message: str):
        """Send a message via Bot API to all allowed chat IDs"""
        if not TELEGRAM_TOKEN:
            logger.warning("No TELEGRAM_TOKEN configured, cannot send Bot API message")
            return

        async with httpx.AsyncClient() as client:
            for chat_id in self.allowed_chat_ids:
                try:
                    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
                    await client.post(url, json={
                        "chat_id": chat_id,
                        "text": message,
                        "parse_mode": "HTML"
                    })
                    logger.info(f"Sent Bot API message to {chat_id}")
                except Exception as e:
                    logger.error(f"Failed to send Bot API message to {chat_id}: {e}")

    async def _code_callback(self):
        """Callback for Telethon to get the verification code"""
        self.waiting_for_code = True
        self.auth_code_event.clear()

        # Notify user via Bot API
        await self._send_bot_api_message(
            "🔐 <b>Telegram Authentication Required</b>\n\n"
            "A verification code has been sent to your Telegram app.\n\n"
            "Please reply with the code here, or visit:\n"
            "<code>/auth/code?code=YOUR_CODE</code>"
        )

        logger.info("Waiting for verification code... (send via Telegram or /auth/code endpoint)")

        # Wait for the code to be submitted
        await self.auth_code_event.wait()

        self.waiting_for_code = False
        code = self.pending_code
        self.pending_code = None
        return code

    async def _password_callback(self):
        """Callback for Telethon to get 2FA password"""
        self.waiting_for_password = True
        self.auth_password_event.clear()

        # Notify user via Bot API
        await self._send_bot_api_message(
            "🔐 <b>Two-Factor Authentication Required</b>\n\n"
            "Your account has 2FA enabled.\n\n"
            "Please reply with your 2FA password here, or visit:\n"
            "<code>/auth/password?password=YOUR_PASSWORD</code>"
        )

        logger.info("Waiting for 2FA password... (send via Telegram or /auth/password endpoint)")

        # Wait for the password to be submitted
        await self.auth_password_event.wait()

        self.waiting_for_password = False
        password = self.pending_password
        self.pending_password = None
        return password

    def submit_code(self, code: str):
        """Submit the verification code (called from API endpoint or message handler)"""
        self.pending_code = code.strip()
        self.auth_code_event.set()
        logger.info("Verification code received")

    def submit_password(self, password: str):
        """Submit the 2FA password (called from API endpoint or message handler)"""
        self.pending_password = password
        self.auth_password_event.set()
        logger.info("2FA password received")

    async def start_client(self):
        """Start the Telethon client and handle authorization"""
        await self.client.start(
            phone=self.phone_number,
            code_callback=self._code_callback,
            password=self._password_callback
        )

        if not await self.client.is_user_authorized():
            raise Exception("User is not authorized. Please authenticate manually.")

        logger.info("Telethon client started successfully")
        
        # Add event handler for incoming messages only (incoming=True prevents bot from processing its own outgoing messages)
        @self.client.on(events.NewMessage(incoming=True))
        async def handler(event):
            await self.handle_incoming_message(event)
            
        # Mark as running after successful start
        self.is_running = True

    async def handle_incoming_message(self, event):
        """Handle incoming messages from Telegram"""
        # Get the actual sender's ID
        sender_id = event.sender_id
        
        # Check if the sender is authorized
        if str(sender_id) not in self.allowed_chat_ids:
            logger.warning(f"Unauthorized access attempt from user ID: {sender_id}, allowed: {self.allowed_chat_ids}")
            # Send a message to the user letting them know they're not authorized
            try:
                await event.reply("You are not authorized to use this bot.")
            except Exception as e:
                logger.error(f"Could not send unauthorized message: {str(e)}")
            return  # Exit early if unauthorized

        # Check if this is a message from the user themselves to avoid loops
        me = await self.client.get_me()
        if sender_id == me.id:
            return  # Ignore messages from the user themselves

        # Create a database session to work with the conversation
        db_gen = get_db()
        db = next(db_gen)
        
        try:
            # Create conversation manager for this user
            manager = ConversationManager(db, str(sender_id))
            
            # Process media if present
            media_path = None
            audio_path = None
            
            if event.message.media:
                # Download the media file based on its type
                if hasattr(event.message.media, 'document'):
                    # This is a document, check if it's an audio file
                    doc = event.message.media.document
                    # Check the attributes to determine if it's an audio file
                    mime_type = getattr(doc, 'mime_type', '').lower()
                    
                    # Check if it's an audio file by looking at mime type
                    is_audio = any(audio_type in mime_type for audio_type in ['audio/', 'video/']) or \
                               any(getattr(doc, 'mime_type', '').endswith(ext) for ext in [
                                   'mp3', 'm4a', 'wav', 'flac', 'aac', 'ogg', 'opus', 
                                   'wma', 'm4p', 'mp2', 'mpa', 'mpc', 'ape', 'aiff', 
                                   'au', 'm3u', 'm4b', 'oga', 'wv', 'tta'
                               ])
                    
                    if is_audio:
                        # Download audio file
                        audio_filename = f"audio_{event.message.date.timestamp()}_{event.message.id}.tmp"
                        audio_path = await event.message.download_media(
                            file=os.path.join(tempfile.gettempdir(), audio_filename)
                        )
                        print(f"Downloaded audio file to: {audio_path}")
                        # Send confirmation that audio file was received
                        await event.reply("Audio file received successfully! Processing...")
                        
                    # Check if it's an image for thumbnails
                    is_image = any(img_type in mime_type for img_type in ['image/']) or \
                               any(getattr(doc, 'mime_type', '').endswith(ext) for ext in ['jpg', 'jpeg', 'png', 'gif', 'bmp'])
                    
                    if is_image and not is_audio:
                        # Download image file (for thumbnails)
                        image_filename = f"thumb_{event.message.date.timestamp()}_{event.message.id}.tmp"
                        media_path = await event.message.download_media(
                            file=os.path.join(tempfile.gettempdir(), image_filename)
                        )
                        print(f"Downloaded image file to: {media_path}")
            
            # Process the message with the conversation manager
            message_text = event.message.text or ""
            reply = manager.process_message(message_text, media_path, audio_path)
            
            # Handle special actions from the conversation manager
            if isinstance(reply, dict):
                # Currently we don't have special actions in the audio-only version
                # but keeping this for future compatibility
                pass
            else:
                # Send the reply back to the user
                # Add check to avoid sending messages to ourselves
                if sender_id != me.id:
                    await event.reply(reply)
                    
                    # If the conversation is in processing state, start the background task
                    if manager.conversation.state == "processing":
                        # Start processing in the background
                        import asyncio
                        loop = asyncio.get_event_loop()
                        loop.create_task(self.process_and_upload_async(str(sender_id), db))
                        
        except Exception as e:
            logger.error(f"Error processing message: {str(e)}")
            await event.reply(f"Error: {str(e)}")
        finally:
            db.close()

    async def send_message(self, chat_id, message):
        """Send a message to a specific chat"""
        # Check if chat_id is in the allowed list
        if str(chat_id) not in self.allowed_chat_ids:
            logger.warning(f"Attempt to send message to unauthorized chat ID: {chat_id}, allowed: {self.allowed_chat_ids}")
            return

        try:
            await self.client.send_message(int(chat_id), message)
            logger.info(f"Message sent to {chat_id}: {message[:50]}...")
        except Exception as e:
            logger.error(f"Failed to send message to {chat_id}: {str(e)}")

    async def process_and_upload_async(self, chat_id, db):
        """Process and upload the video asynchronously"""
        try:
            from app.database import get_db
            from app.services.conversation import ConversationManager
            from app.services.video import process_uploaded_audio
            from app.services.youtube import upload_video
            
            # Create a fresh DB session for this background task
            local_db_gen = get_db()
            local_db = next(local_db_gen)
            
            try:
                manager = ConversationManager(local_db, chat_id)
                data = manager.get_upload_data()

                if not data["account"]:
                    await self.send_message(chat_id, "Error: No account selected.")
                    manager.reset()
                    return

                account = data["account"]
                thumbnail_path = data.get("thumbnail_path")

                # Process uploaded audio file (this is now the only option)
                if data.get("audio_path"):
                    await self.send_message(chat_id, "Processing uploaded audio...")
                    
                    video_path = process_uploaded_audio(
                        audio_path=data["audio_path"],
                        thumbnail_path=thumbnail_path if thumbnail_path and not thumbnail_path.startswith("http") else None,
                    )

                    await self.send_message(chat_id, "Uploading to YouTube...")

                    result = upload_video(
                        credentials_path=account.credentials_path,
                        video_path=video_path,
                        title=data["title"],
                        description=data["description"],
                        privacy=data["privacy"],
                        thumbnail_path=thumbnail_path if thumbnail_path and not thumbnail_path.startswith("http") else None,
                    )

                    await self.send_message(chat_id, f"Done!\n{result['video_url']}")

                    # Cleanup temp files
                    cleanup_temp_files(video_path)
                    # Clean up audio file if it was an uploaded one
                    if data.get("audio_path"):
                        cleanup_temp_files(data["audio_path"])
                    if thumbnail_path and not thumbnail_path.startswith("http"):
                        cleanup_temp_files(thumbnail_path)

                    manager.mark_complete()

            finally:
                local_db.close()
                
        except Exception as e:
            error_msg = f"Error processing upload: {str(e)}"
            print(error_msg)
            await self.send_message(chat_id, error_msg)
            
            # Reset the conversation on error
            local_db_gen = get_db()
            local_db = next(local_db_gen)
            try:
                manager = ConversationManager(local_db, chat_id)
                manager.reset()
            finally:
                local_db.close()

# Initialize the client instance
mtproto_client = TelegramMTProtoClient()