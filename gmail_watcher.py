# gmail_watcher.py
# Watches Gmail for unread important emails and creates actionable .md files in Obsidian vault
# Part of Personal AI Employee Hackathon 0

import os
import pickle
import base64
from pathlib import Path
from datetime import datetime
from googleapiclient.discovery import build
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from base_watcher import BaseWatcher
from error_recovery import RetryConfig

# === CONFIG ===
VAULT_PATH = Path("/mnt/d/AI_Employee_Vault/AI_Employee_Vault")  # ← Change if your vault is elsewhere
CREDENTIALS_PATH = Path.home() / "secure" / "gmail-credentials.json"  # Where you saved credentials.json
TOKEN_PATH = Path.home() / "secure" / "gmail-token.pickle"  # Auto-created after first auth
SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]  # Read + modify (for marking read later)
CHECK_INTERVAL = 120  # seconds


class GmailWatcher(BaseWatcher):
    def __init__(self, vault_path: str, credentials_path: str):
        super().__init__(vault_path, check_interval=CHECK_INTERVAL)
        self.credentials_path = Path(credentials_path)
        self.token_path = TOKEN_PATH
        self.service = None
        self.processed_ids = set()
        self._authenticate()

    def _authenticate(self):
        """Authenticate with Gmail API using OAuth2"""
        creds = None

        # Load existing token if available
        if self.token_path.exists():
            with open(self.token_path, "rb") as token:
                creds = pickle.load(token)

        # If no valid credentials → run OAuth flow
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_secrets_file(
                    self.credentials_path, SCOPES
                )
                creds = flow.run_local_server(port=0)

            # Save credentials for next run
            with open(self.token_path, "wb") as token:
                pickle.dump(creds, token)

        self.service = build("gmail", "v1", credentials=creds)

    def check_for_updates(self) -> list:
        """Find unread important emails not yet processed."""
        def _fetch():
            results = (
                self.service.users()
                .messages()
                .list(userId="me", q="is:unread is:important -in:sent")
                .execute()
            )
            messages = results.get("messages", [])
            return [m for m in messages if m["id"] not in self.processed_ids]

        return self.recovery.retry(
            _fetch,
            config  = RetryConfig(max_attempts=3, base_delay=10, max_delay=60),
            label   = "Gmail.list",
            fallback= [],
        )

    def create_action_file(self, message) -> Path:
        """Fetch email details and create .md action file."""
        def _fetch_msg():
            return (
                self.service.users()
                .messages()
                .get(userId="me", id=message["id"], format="full")
                .execute()
            )

        try:
            msg = self.recovery.retry(
                _fetch_msg,
                config  = RetryConfig(max_attempts=3, base_delay=5, max_delay=30),
                label   = "Gmail.get",
                fallback= None,
            )
            if msg is None:
                return Path()

            # Extract headers
            headers = {h["name"]: h["value"] for h in msg["payload"]["headers"]}
            subject = headers.get("Subject", "(No Subject)")
            from_ = headers.get("From", "Unknown")
            date_str = headers.get("Date", datetime.now().isoformat())

            # Get snippet or full body (snippet is usually enough)
            snippet = msg.get("snippet", "")

            # Optional: decode full body if needed (simplified here)
            content = f"""---
type: email
from: {from_}
subject: {subject}
received: {date_str}
priority: high
status: pending
message_id: {message['id']}
---

## Email Content
{snippet}

## Suggested Actions
- [ ] Reply to sender
- [ ] Forward to relevant person
- [ ] Archive after processing
- [ ] Flag as urgent

**Note**: Move this file to /Done/ after handling.
"""

            # Create unique filename
            safe_subject = "".join(c for c in subject if c.isalnum() or c in " -_").strip()
            filename = f"EMAIL_{message['id']}_{safe_subject[:50]}.md"
            filepath = self.needs_action / filename

            filepath.write_text(content)
            self.processed_ids.add(message["id"])

            self.logger.info(f"Created action file: {filepath}")
            return filepath

        except Exception as exc:
            self.logger.error("Failed to process message %s: %s", message["id"], exc)
            return Path()

    def run(self):
        self.logger.info("Starting GmailWatcher")
        super().run()


if __name__ == "__main__":
    # You still need base_watcher.py in the same folder
    watcher = GmailWatcher(
        vault_path=VAULT_PATH,
        credentials_path=CREDENTIALS_PATH,
    )
    watcher.run()
    