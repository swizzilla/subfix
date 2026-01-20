import re
from sqlalchemy.orm import Session

from app.database import Conversation, Account


class ConversationManager:
    """
    Manages WhatsApp conversation state for audio-only processing.

    States:
    - idle: Waiting for command
    - awaiting_audio: Waiting for audio file upload
    - awaiting_account: Waiting for account selection
    - awaiting_title: Waiting for video title
    - awaiting_description: Waiting for description
    - awaiting_thumbnail: Waiting for thumbnail
    - awaiting_privacy: Waiting for privacy selection
    - processing: Video is being processed
    - adding_account: Waiting for account name to add
    - removing_account: Waiting for account selection to remove
    """

    def __init__(self, db: Session, phone_number: str):
        self.db = db
        self.phone_number = phone_number
        self.conversation = self._get_or_create_conversation()

    def _get_or_create_conversation(self) -> Conversation:
        conv = (
            self.db.query(Conversation)
            .filter(Conversation.phone_number == self.phone_number)
            .first()
        )
        if not conv:
            conv = Conversation(phone_number=self.phone_number, state="idle")
            self.db.add(conv)
            self.db.commit()
            self.db.refresh(conv)
        return conv

    def reset(self):
        """Reset conversation to idle state"""
        self.conversation.state = "idle"
        self.conversation.youtube_url = None
        # Using hasattr to handle potentially old conversations without audio_path
        if hasattr(self.conversation, 'audio_path'):
            self.conversation.audio_path = None
        self.conversation.account_id = None
        self.conversation.title = None
        self.conversation.description = None
        self.conversation.thumbnail_path = None
        self.conversation.privacy = "public"
        self.db.commit()

    def get_accounts_list(self) -> list[Account]:
        return self.db.query(Account).all()

    def process_message(self, message: str, media_url: str | None = None, audio_path: str | None = None) -> str | dict:
        """
        Process incoming message and return response.
        Returns string for normal response, or dict with special actions.
        """
        message_lower = message.strip().lower()
        state = self.conversation.state

        # Global commands
        if message_lower in ["cancel", "stop", "quit"]:
            self.reset()
            return "Cancelled. Send 'upload' to start."

        if message_lower in ["help", "?"]:
            return self._help_message()

        if message_lower == "accounts":
            return self._list_accounts()

        # Handle audio file if one was sent, regardless of current state
        if audio_path:
            # If we receive an audio file, immediately switch to audio processing
            self.conversation.audio_path = audio_path
            self.conversation.youtube_url = None  # Clear any YouTube URL
            
            if state in ["idle"]:
                # If we're at the beginning, go straight to account selection
                accounts = self.get_accounts_list()
                if len(accounts) == 1:
                    # Auto-select if only one account
                    self.conversation.account_id = accounts[0].id
                    self.conversation.state = "awaiting_title"
                    self.db.commit()
                    return f"Using {accounts[0].name}. Enter video title:"

                self.conversation.state = "awaiting_account"
                self.db.commit()
                account_list = "\n".join([f"{i+1}. {a.name}" for i, a in enumerate(accounts)])
                return f"Choose account:\n{account_list}\n\nReply with number:"
            elif state == "awaiting_audio":
                # We're specifically waiting for audio
                accounts = self.get_accounts_list()
                if len(accounts) == 1:
                    # Auto-select if only one account
                    self.conversation.account_id = accounts[0].id
                    self.conversation.state = "awaiting_title"
                    self.db.commit()
                    return f"Using {accounts[0].name}. Enter video title:"

                self.conversation.state = "awaiting_account"
                self.db.commit()
                account_list = "\n".join([f"{i+1}. {a.name}" for i, a in enumerate(accounts)])
                return f"Choose account:\n{account_list}\n\nReply with number:"

        # Account management commands (from idle state)
        if state == "idle":
            if message_lower in ["add", "add account"]:
                self.conversation.state = "adding_account"
                self.db.commit()
                return "Enter a name for this account (e.g. MusicChannel):"

            if message_lower in ["remove", "remove account", "delete", "delete account"]:
                accounts = self.get_accounts_list()
                if not accounts:
                    return "No accounts to remove."
                self.conversation.state = "removing_account"
                self.db.commit()
                account_list = "\n".join([f"{i+1}. {a.name}" for i, a in enumerate(accounts)])
                return f"Which account to remove?\n{account_list}\n\nReply with number:"

            if message_lower in ["upload", "start", "new"]:
                accounts = self.get_accounts_list()
                if not accounts:
                    return "No accounts yet. Send 'add' to add an account first."
                self.conversation.state = "awaiting_audio"
                self.db.commit()
                return "Send an audio file directly (.mp3, .m4a, .wav, .flac, .aac, .ogg, .opus, .wma, .m4p, .mp2, .mpa, .mpc, .ape, .aiff, .au, .m3u, .m4b, .oga, .wv, .tta)."

            return "Commands:\n• upload - Start upload\n• add - Add account\n• remove - Remove account\n• accounts - List accounts"

        # Adding account
        if state == "adding_account":
            return self._handle_adding_account(message.strip())

        # Removing account
        if state == "removing_account":
            return self._handle_removing_account(message_lower)

        # Upload flow - only audio now
        if state == "awaiting_audio":
            # Allow "add" command even while waiting for audio
            if message_lower in ["add", "add account"]:
                self.conversation.state = "adding_account"
                self.db.commit()
                return "Enter a name for this account (e.g. MusicChannel):"
            if audio_path:
                self.conversation.audio_path = audio_path
                return self._handle_audio_upload()
            else:
                return "Please send an audio file. (Or type 'cancel' to go back, 'add' to add account)"
        elif state == "awaiting_account":
            return self._handle_awaiting_account(message_lower)
        elif state == "awaiting_title":
            return self._handle_awaiting_title(message)
        elif state == "awaiting_description":
            return self._handle_awaiting_description(message)
        elif state == "awaiting_thumbnail":
            return self._handle_awaiting_thumbnail(message_lower, media_url)
        elif state == "awaiting_privacy":
            return self._handle_awaiting_privacy(message_lower)
        elif state == "processing":
            return "Still processing... Please wait."

        self.reset()
        return "Something went wrong. Send 'upload' to start."

    def _help_message(self) -> str:
        return (
            "Commands:\n"
            "• upload - Start new upload\n"
            "• add - Add account\n"
            "• remove - Remove account\n"
            "• accounts - List accounts\n"
            "• cancel - Cancel current action"
        )

    def _list_accounts(self) -> str:
        accounts = self.get_accounts_list()
        if not accounts:
            return "No accounts. Send 'add' to add one."
        return "Your accounts:\n" + "\n".join([f"• {a.name}" for a in accounts])

    def _handle_adding_account(self, account_name: str) -> dict:
        """Returns dict with auth_url to send to user"""
        if not account_name:
            return "Please enter a valid account name."

        # Check if exists
        existing = self.db.query(Account).filter(Account.name == account_name).first()
        if existing:
            self.reset()
            return f"Account '{account_name}' already exists."

        # Return special dict - the router will handle creating account and auth URL
        return {
            "action": "create_account",
            "account_name": account_name,
        }

    def _handle_removing_account(self, message: str) -> str:
        accounts = self.get_accounts_list()

        try:
            choice = int(message)
            if 1 <= choice <= len(accounts):
                account = accounts[choice - 1]
                name = account.name

                # Delete credentials file
                from pathlib import Path
                if account.credentials_path and Path(account.credentials_path).exists():
                    Path(account.credentials_path).unlink()

                self.db.delete(account)
                self.db.commit()
                self.reset()
                return f"Removed '{name}'."
            else:
                return f"Enter a number between 1 and {len(accounts)}."
        except ValueError:
            return "Enter the number of the account to remove."

    def _handle_audio_upload(self) -> str:
        """Handles when an audio file has been received"""
        # Make sure we clear any YouTube URL that might have been set earlier
        self.conversation.youtube_url = None
        
        accounts = self.get_accounts_list()
        if len(accounts) == 1:
            # Auto-select if only one account
            self.conversation.account_id = accounts[0].id
            self.conversation.state = "awaiting_title"
            self.db.commit()
            return f"Using {accounts[0].name}. Enter video title:"

        self.conversation.state = "awaiting_account"
        self.db.commit()
        account_list = "\n".join([f"{i+1}. {a.name}" for i, a in enumerate(accounts)])
        return f"Choose account:\n{account_list}\n\nReply with number:"

    def _handle_awaiting_account(self, message: str) -> str:
        accounts = self.get_accounts_list()

        try:
            choice = int(message)
            if 1 <= choice <= len(accounts):
                self.conversation.account_id = accounts[choice - 1].id
                self.conversation.state = "awaiting_title"
                self.db.commit()
                return f"Using {accounts[choice - 1].name}. Enter video title:"
            return f"Enter 1-{len(accounts)}."
        except ValueError:
            return "Enter the account number."

    def _handle_awaiting_title(self, message: str) -> str:
        self.conversation.state = "awaiting_description"
        self.db.commit()
        return "Description? (or 'skip'):"

    def _handle_awaiting_description(self, message: str) -> str:
        self.conversation.state = "awaiting_thumbnail"
        self.db.commit()
        return "Send thumbnail image:"

    def _handle_awaiting_thumbnail(self, message: str, media_url: str | None) -> str:
        if media_url:
            self.conversation.thumbnail_path = media_url
        elif message in ["auto", "default", "original", "skip"]:
            self.conversation.thumbnail_path = None
        else:
            return "Send an image or reply 'auto'."

        self.conversation.state = "awaiting_privacy"
        self.db.commit()
        return "Privacy? (public / unlisted / private):"

    def _handle_awaiting_privacy(self, message: str) -> str:
        privacy_map = {
            "public": "public", "1": "public",
            "unlisted": "unlisted", "2": "unlisted",
            "private": "private", "3": "private",
        }

        if message not in privacy_map:
            return "Reply: public, unlisted, or private"

        self.conversation.privacy = privacy_map[message]
        self.conversation.state = "processing"
        self.db.commit()
        return "Processing... This may take a few minutes."

    def set_title(self, title: str):
        self.conversation.title = title
        self.db.commit()

    def set_description(self, description: str):
        if description.lower() not in ["skip", "none", "-"]:
            self.conversation.description = description
        else:
            self.conversation.description = ""
        self.db.commit()

    def get_upload_data(self) -> dict:
        account = self.db.query(Account).filter(Account.id == self.conversation.account_id).first()
        return {
            "youtube_url": self.conversation.youtube_url,
            "audio_path": getattr(self.conversation, 'audio_path', None),  # Handle if attribute doesn't exist
            "account": account,
            "title": self.conversation.title,
            "description": self.conversation.description or "",
            "thumbnail_path": self.conversation.thumbnail_path,
            "privacy": self.conversation.privacy,
        }

    def mark_complete(self):
        self.reset()

    def set_state(self, state: str):
        self.conversation.state = state
        self.db.commit()