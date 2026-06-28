"""YouTube Watcher - Monitors YouTube channel for comments and engagement.

Watches for:
  - New comments on your videos
  - Replies to your comments
  - Channel mentions (if accessible)
  - New subscribers milestones

Creates action files in /Needs_Action for Claude to process.

Usage:
    uv run python youtube_watcher.py

Prerequisites:
    1. Enable YouTube Data API v3 in Google Cloud Console
    2. Add YouTube scopes to OAuth credentials
    3. Run auth to get tokens with YouTube access
"""

import os
import pickle
from pathlib import Path
from datetime import datetime, timezone
from googleapiclient.discovery import build
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request

from base_watcher import BaseWatcher
from error_recovery import RetryConfig

# === CONFIG ===
VAULT_PATH = Path(os.getenv("VAULT_PATH", "/mnt/d/AI_Employee_Vault/AI_Employee_Vault"))
CREDENTIALS_PATH = Path.home() / "secure" / "gmail-credentials.json"  # Reuse same credentials
TOKEN_PATH = Path.home() / "secure" / "youtube-token.pickle"
SCOPES = [
    "https://www.googleapis.com/auth/youtube.readonly",
    "https://www.googleapis.com/auth/youtube.force-ssl",  # For comment replies
]
CHECK_INTERVAL = 300  # 5 minutes


class YouTubeWatcher(BaseWatcher):
    """Watch YouTube channel for new comments and engagement."""

    def __init__(self, vault_path: str, credentials_path: Path, token_path: Path):
        super().__init__(vault_path, check_interval=CHECK_INTERVAL)
        self.credentials_path = credentials_path
        self.token_path = token_path
        self.youtube = None
        self.channel_id = None
        self.seen_comments: set[str] = set()
        self._authenticate()

    def _authenticate(self):
        """Authenticate with YouTube API using OAuth2."""
        creds = None

        # Load existing token
        if self.token_path.exists():
            with open(self.token_path, "rb") as f:
                creds = pickle.load(f)

        # Refresh or get new credentials
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                self.logger.info("Refreshing YouTube token...")
                creds.refresh(Request())
            else:
                self.logger.info("Starting YouTube OAuth flow...")
                flow = InstalledAppFlow.from_client_secrets_file(
                    str(self.credentials_path), SCOPES
                )
                creds = flow.run_local_server(port=3458)

            # Save token
            self.token_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.token_path, "wb") as f:
                pickle.dump(creds, f)
            self.logger.info("YouTube token saved.")

        self.youtube = build("youtube", "v3", credentials=creds)
        self._get_channel_id()

    def _get_channel_id(self):
        """Get the authenticated user's channel ID."""
        def _fetch():
            return self.youtube.channels().list(part="snippet", mine=True).execute()

        response = self.recovery.retry(
            _fetch,
            config=RetryConfig(max_attempts=3, base_delay=5, max_delay=30),
            label="YouTube.channels.list",
            fallback=None,
        )
        if not response:
            self.logger.error("Failed to get channel ID after retries")
            return
        if response.get("items"):
            self.channel_id = response["items"][0]["id"]
            channel_name = response["items"][0]["snippet"]["title"]
            self.logger.info(f"Watching channel: {channel_name} ({self.channel_id})")
        else:
            self.logger.warning("No YouTube channel found for this account")

    def check_for_updates(self) -> list:
        """Check for new comments on channel videos."""
        if not self.youtube or not self.channel_id:
            return []

        new_items = []

        # Fetch recent videos with retry
        def _fetch_videos():
            return self.youtube.search().list(
                part="snippet", channelId=self.channel_id,
                type="video", order="date", maxResults=5,
            ).execute()

        videos_response = self.recovery.retry(
            _fetch_videos,
            config=RetryConfig(max_attempts=3, base_delay=10, max_delay=60),
            label="YouTube.search.list",
            fallback=None,
        )
        if not videos_response:
            return []

        video_ids = [item["id"]["videoId"] for item in videos_response.get("items", [])]

        # Get comments for each video with retry
        for video_id in video_ids:
            def _fetch_comments(vid=video_id):
                return self.youtube.commentThreads().list(
                    part="snippet,replies", videoId=vid,
                    maxResults=10, order="time",
                ).execute()

            response = self.recovery.retry(
                _fetch_comments,
                config=RetryConfig(max_attempts=2, base_delay=5, max_delay=30),
                label=f"YouTube.commentThreads/{video_id[:8]}",
                fallback=None,
            )
            if not response:
                continue

            for item in response.get("items", []):
                comment_id = item["id"]
                if comment_id in self.seen_comments:
                    continue

                self.seen_comments.add(comment_id)
                snippet = item["snippet"]["topLevelComment"]["snippet"]

                new_items.append({
                    "type":           "youtube_comment",
                    "comment_id":     comment_id,
                    "video_id":       snippet.get("videoId", ""),
                    "author":         snippet.get("authorDisplayName", "Unknown"),
                    "author_channel": snippet.get("authorChannelId", {}).get("value", ""),
                    "text":           snippet.get("textDisplay", ""),
                    "published_at":   snippet.get("publishedAt", ""),
                    "like_count":     snippet.get("likeCount", 0),
                    "reply_count":    item["snippet"].get("totalReplyCount", 0),
                })

        return new_items

    def create_action_file(self, item) -> Path:
        """Create action file for YouTube comment."""
        now = datetime.now(timezone.utc)
        timestamp = now.strftime("%Y-%m-%d")

        # Clean author name for filename
        safe_author = "".join(
            c for c in item["author"] if c.isalnum() or c in " -_"
        ).strip()[:20]

        filename = f"YOUTUBE_{safe_author}_{item['comment_id'][:8]}_{timestamp}.md"
        filepath = self.needs_action / filename

        # Determine urgency based on keywords
        text_lower = item["text"].lower()
        urgent_keywords = ["help", "problem", "issue", "broken", "fix", "urgent", "asap"]
        is_urgent = any(kw in text_lower for kw in urgent_keywords)

        # Check if it's a question
        is_question = "?" in item["text"]

        priority = "urgent" if is_urgent else ("high" if is_question else "normal")

        content = f"""---
type: youtube_comment
comment_id: {item['comment_id']}
video_id: {item['video_id']}
author: {item['author']}
author_channel: {item['author_channel']}
published_at: {item['published_at']}
like_count: {item['like_count']}
reply_count: {item['reply_count']}
priority: {priority}
detected: {now.isoformat()}
status: pending
---

# YouTube Comment from {item['author']}

## Comment
{item['text']}

## Video
https://youtube.com/watch?v={item['video_id']}

## Stats
- Likes: {item['like_count']}
- Replies: {item['reply_count']}
- Posted: {item['published_at']}

## Priority
{"🔴 URGENT - Contains help/problem keywords" if is_urgent else "🟡 Question - Consider responding" if is_question else "🟢 Normal"}

## Suggested Actions
- [ ] Read and understand the comment
- [ ] Draft a helpful reply
- [ ] Use youtube_reply skill if responding
- [ ] Move to /Done when complete
"""

        filepath.write_text(content)
        self.logger.info(f"Created action file: {filename}")

        self.log_action("youtube_comment_detected", {
            "comment_id": item["comment_id"],
            "author": item["author"],
            "video_id": item["video_id"],
            "priority": priority,
            "action_file": filename,
        })

        return filepath


def main():
    """Run the YouTube watcher."""
    watcher = YouTubeWatcher(
        vault_path=str(VAULT_PATH),
        credentials_path=CREDENTIALS_PATH,
        token_path=TOKEN_PATH,
    )
    watcher.run()


if __name__ == "__main__":
    main()
