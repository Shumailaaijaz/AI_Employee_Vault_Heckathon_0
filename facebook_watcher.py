"""Facebook + Instagram Watcher — Monitors both platforms via the Graph API.

Uses the official Facebook Graph API (facebook-sdk) for both Facebook
Pages and Instagram Business accounts. A single watcher handles both
because Instagram is accessed through the same Graph API using the same
Page Access Token.

What gets monitored:
  Facebook:
    - New comments on your Page posts
    - New unread conversations (Page Inbox / Messenger)
  Instagram:
    - New comments on your Business account posts
    - @mentions of your Instagram handle in other users' posts

Output files:
  FACEBOOK_COMMENT_*.md   → new comment on your Page post
  FACEBOOK_MESSAGE_*.md   → new Messenger conversation / DM
  INSTAGRAM_COMMENT_*.md  → new comment on your IG post
  INSTAGRAM_MENTION_*.md  → @mention of your IG handle

Auth setup (one-time):
  1. Create an app at developers.facebook.com
     → Add "Facebook Login" + "Pages API" + "Instagram Graph API" products
  2. Generate a long-lived Page Access Token:
     a. Log in at developers.facebook.com/tools/explorer/
     b. Select your app → select your Page → request these permissions:
        pages_read_engagement, pages_read_user_content,
        pages_messaging, instagram_basic,
        instagram_manage_comments, instagram_manage_insights
     c. Generate → exchange for a long-lived token (60-day expiry):
        GET /oauth/access_token?grant_type=fb_exchange_token
            &client_id=APP_ID&client_secret=APP_SECRET
            &fb_exchange_token=SHORT_TOKEN
  3. Get your Page ID:   GET /me?fields=id,name&access_token=TOKEN
  4. Get your Instagram Business User ID:
        GET /{page-id}?fields=instagram_business_account&access_token=TOKEN
  5. Add all values to .env

Token refresh:
  Long-lived tokens last 60 days. Set a calendar reminder to refresh.
  The watcher will log a warning when it receives an OAuthException (code 190).

Environment variables:
    VAULT_PATH                  Path to Obsidian vault (default: /mnt/d/...)
    FACEBOOK_PAGE_ACCESS_TOKEN  Long-lived Page Access Token
    FACEBOOK_PAGE_ID            Numeric Page ID (e.g. "123456789")
    INSTAGRAM_USER_ID           Numeric IG Business User ID (e.g. "987654321")
    FACEBOOK_CHECK_INTERVAL     Poll interval in seconds (default: 180)

Usage:
    uv run python facebook_watcher.py
    FACEBOOK_CHECK_INTERVAL=300 uv run python facebook_watcher.py
"""

import os
import re
from pathlib import Path
from datetime import datetime, timezone

import facebook

from base_watcher import BaseWatcher
from error_recovery import RetryConfig

# ── Config ────────────────────────────────────────────────────────────────────

VAULT_PATH     = os.getenv("VAULT_PATH", "/mnt/d/AI_Employee_Vault/AI_Employee_Vault")
CHECK_INTERVAL = int(os.getenv("FACEBOOK_CHECK_INTERVAL", "180"))  # seconds

PAGE_ACCESS_TOKEN = os.getenv("FACEBOOK_PAGE_ACCESS_TOKEN", "")
PAGE_ID           = os.getenv("FACEBOOK_PAGE_ID", "")
IG_USER_ID        = os.getenv("INSTAGRAM_USER_ID", "")

# Graph API version (update when Facebook deprecates older versions)
GRAPH_VERSION = "v19.0"

# How many feed posts to scan for new comments per cycle
POSTS_TO_SCAN = 5
# Max comments per post to retrieve
COMMENTS_PER_POST = 10
# Max Messenger conversations to check
CONVERSATIONS_TO_CHECK = 10
# Max Instagram media posts to scan for new comments
IG_POSTS_TO_SCAN = 5

# Keywords that flag a Facebook/Instagram item for action
ACTION_KEYWORDS: list[str] = [
    "urgent",
    "help",
    "complaint",
    "problem",
    "issue",
    "order",
    "invoice",
    "payment",
    "refund",
    "question",
    "hire",
    "collab",
    "partnership",
    "pricing",
    "review",
    "feedback",
    "spam",
]

# Facebook user names whose content is ALWAYS flagged
VIP_SENDERS: list[str] = [
    # Add names exactly as they appear in Facebook, e.g.:
    # "Jane Smith",
]


class FacebookInstagramWatcher(BaseWatcher):
    """
    Monitors Facebook Page comments + Messenger, and Instagram comments + mentions.

    A single watcher handles both platforms because Instagram Business accounts
    are accessed through the same Facebook Graph API endpoint and Page Access Token.
    """

    def __init__(
        self,
        vault_path: str,
        page_access_token: str = PAGE_ACCESS_TOKEN,
        page_id: str = PAGE_ID,
        ig_user_id: str = IG_USER_ID,
    ):
        super().__init__(vault_path, check_interval=CHECK_INTERVAL)

        self._token     = page_access_token
        self._page_id   = page_id
        self._ig_user_id = ig_user_id

        # Graph API client — re-created on token refresh
        self.graph: facebook.GraphAPI | None = None

        # Dedup: set of comment/message IDs already processed
        self.processed_ids: set[str] = set()

        self._connect()

    # ── Connection ────────────────────────────────────────────────────────────

    def _connect(self):
        """Initialise (or re-initialise) the Graph API client."""
        if not self._token:
            self.logger.warning(
                "FACEBOOK_PAGE_ACCESS_TOKEN not set. "
                "Set it in .env to enable Facebook + Instagram monitoring."
            )
            return

        self.graph = facebook.GraphAPI(
            access_token=self._token,
            version=GRAPH_VERSION,
            timeout=30,
        )
        self.logger.info(
            f"Facebook Graph API {GRAPH_VERSION} connected "
            f"(page_id={self._page_id or 'not set'}, "
            f"ig_user_id={self._ig_user_id or 'not set'})"
        )

    _FATAL_FB_CODES = {190}          # token expired / revoked — never retry
    _RATE_LIMIT_CODES = {4, 17, 32, 613}  # API throttle — retry after backoff

    def _handle_api_error(self, exc: facebook.GraphAPIError, context: str) -> bool:
        """
        Log a Graph API error and decide whether to retry.
        Returns True if the error is likely transient (caller should retry later).
        Returns False if it is fatal for this cycle (caller should skip).
        """
        code = getattr(exc, "code", None)
        if code == 190:
            self.logger.error(
                f"[{context}] Facebook token expired (OAuthException 190). "
                "Regenerate FACEBOOK_PAGE_ACCESS_TOKEN in .env and restart."
            )
            self.recovery.alert(
                level="ERROR",
                message="Facebook Page Access Token expired (OAuthException 190).",
                operation=context,
                action=(
                    "1. Go to developers.facebook.com/tools/explorer/\n"
                    "2. Generate a new long-lived Page Access Token.\n"
                    "3. Update FACEBOOK_PAGE_ACCESS_TOKEN in .env and restart watcher."
                ),
            )
            return False
        elif code in (4, 17, 32, 613):
            self.logger.warning(f"[{context}] Graph API rate limit ({code}) — skipping cycle")
            return True
        else:
            self.logger.error(f"[{context}] GraphAPIError {code}: {exc}")
            return True

    def _graph_call(self, endpoint: str, context: str, **kwargs) -> dict | None:
        """
        Call self.graph.get_object() with exponential-backoff retry.

        Fatal errors (token expired, code 190) are not retried; they write a
        human alert and return None immediately.  Rate-limit and transient
        errors are retried up to 3 times.
        """
        if not self.graph:
            return None

        def _call():
            try:
                return self.graph.get_object(endpoint, **kwargs)
            except facebook.GraphAPIError as exc:
                code = getattr(exc, "code", None)
                if code in self._FATAL_FB_CODES:
                    self._handle_api_error(exc, context)
                    return None   # signal caller to skip — not retried
                raise             # re-raise transient errors for retry loop

        return self.recovery.retry(
            _call,
            config   = RetryConfig(max_attempts=3, base_delay=10, max_delay=90),
            label    = f"FB.{context}",
            fallback = None,
        )

    # ── Facebook: Page post comments ──────────────────────────────────────────

    def _check_facebook_comments(self) -> list[dict]:
        """
        Fetch recent comments on the Page's posts.
        Only comments matching ACTION_KEYWORDS or from VIP_SENDERS are returned.
        """
        if not self.graph or not self._page_id:
            return []

        flagged: list[dict] = []
        feed = self._graph_call(
            f"{self._page_id}/feed", "fb_comments",
            fields=(
                f"id,message,created_time,"
                f"comments.limit({COMMENTS_PER_POST}){{id,from,message,created_time}}"
            ),
            limit=POSTS_TO_SCAN,
        )
        if not feed:
            return []

        for post in feed.get("data", []):
            post_id      = post.get("id", "")
            post_snippet = (post.get("message") or "")[:80]

            for comment in post.get("comments", {}).get("data", []):
                comment_id = comment.get("id", "")
                if comment_id in self.processed_ids:
                    continue

                from_info    = comment.get("from") or {}
                commenter    = from_info.get("name", "Unknown")
                comment_text = comment.get("message", "")
                created      = comment.get("created_time", "")

                is_vip = any(vip.lower() in commenter.lower() for vip in VIP_SENDERS)
                text_lower = comment_text.lower()
                matched    = [kw for kw in ACTION_KEYWORDS if kw in text_lower]

                if is_vip or matched:
                    flagged.append({
                        "id":               comment_id,
                        "platform":         "facebook",
                        "type":             "comment",
                        "author":           commenter,
                        "text":             comment_text,
                        "parent_id":        post_id,
                        "parent_snippet":   post_snippet,
                        "created_at":       created,
                        "matched_keywords": matched,
                        "is_vip":           is_vip,
                        "priority":         "critical" if is_vip else "high",
                        "url":              f"https://www.facebook.com/{post_id}",
                    })

        return flagged

    # ── Facebook: Page Messenger conversations ────────────────────────────────

    def _check_facebook_messages(self) -> list[dict]:
        """
        Fetch unread Messenger conversations from the Page inbox.
        All unread messages are flagged — DMs always need attention.
        """
        if not self.graph or not self._page_id:
            return []

        flagged: list[dict] = []
        inbox = self._graph_call(
            f"{self._page_id}/conversations", "fb_messages",
            fields="id,snippet,unread_count,updated_time,participants",
            limit=CONVERSATIONS_TO_CHECK,
        )
        if not inbox:
            return []

        for convo in inbox.get("data", []):
            unread = convo.get("unread_count", 0)
            if unread == 0:
                continue

            convo_id = convo.get("id", "")
            if convo_id in self.processed_ids:
                continue

            snippet  = convo.get("snippet", "")
            updated  = convo.get("updated_time", "")

            # Extract the non-Page participant as the sender
            participants = convo.get("participants", {}).get("data", [])
            sender = next(
                (p.get("name", "Unknown") for p in participants if p.get("id") != self._page_id),
                "Unknown",
            )

            is_vip = any(vip.lower() in sender.lower() for vip in VIP_SENDERS)
            matched = [kw for kw in ACTION_KEYWORDS if kw in snippet.lower()]

            flagged.append({
                "id":               convo_id,
                "platform":         "facebook",
                "type":             "message",
                "author":           sender,
                "text":             snippet,
                "parent_id":        "",
                "parent_snippet":   "",
                "created_at":       updated,
                "matched_keywords": matched,
                "is_vip":           is_vip,
                "priority":         "critical" if is_vip else "high",
                "url":              f"https://www.facebook.com/messages/t/{convo_id}",
            })

        return flagged

    # ── Instagram: Post comments ──────────────────────────────────────────────

    def _check_instagram_comments(self) -> list[dict]:
        """
        Fetch recent comments on your Instagram Business posts.
        Only comments matching ACTION_KEYWORDS or from VIP users are flagged.
        """
        if not self.graph or not self._ig_user_id:
            return []

        flagged: list[dict] = []
        media_list = self._graph_call(
            f"{self._ig_user_id}/media", "ig_comments_media",
            fields="id,caption,timestamp,comments_count",
            limit=IG_POSTS_TO_SCAN,
        )
        if not media_list:
            return []

        for media in media_list.get("data", []):
            media_id      = media.get("id", "")
            comment_count = media.get("comments_count", 0)
            caption       = (media.get("caption") or "")[:80]

            if comment_count == 0:
                continue

            comments_resp = self._graph_call(
                f"{media_id}/comments", f"ig_comments/{media_id}",
                fields="id,username,text,timestamp",
                limit=COMMENTS_PER_POST,
            )
            if not comments_resp:
                continue

            for comment in comments_resp.get("data", []):
                comment_id   = comment.get("id", "")
                if comment_id in self.processed_ids:
                    continue

                username     = comment.get("username", "unknown")
                comment_text = comment.get("text", "")
                timestamp    = comment.get("timestamp", "")

                is_vip = any(vip.lower() in username.lower() for vip in VIP_SENDERS)
                matched = [kw for kw in ACTION_KEYWORDS if kw in comment_text.lower()]

                if is_vip or matched:
                    flagged.append({
                        "id":               comment_id,
                        "platform":         "instagram",
                        "type":             "comment",
                        "author":           f"@{username}",
                        "text":             comment_text,
                        "parent_id":        media_id,
                        "parent_snippet":   caption,
                        "created_at":       timestamp,
                        "matched_keywords": matched,
                        "is_vip":           is_vip,
                        "priority":         "critical" if is_vip else "high",
                        "url":              f"https://www.instagram.com/p/{media_id}/",
                    })

        return flagged

    # ── Instagram: @mentions ──────────────────────────────────────────────────

    def _check_instagram_mentions(self) -> list[dict]:
        """
        Fetch posts where your Instagram Business account was @mentioned.
        Uses the IG Mentions API (requires instagram_manage_comments scope).
        """
        if not self.graph or not self._ig_user_id:
            return []

        flagged: list[dict] = []
        # Code 100/200 = permission not granted for /tags — skip silently
        def _fetch_mentions():
            try:
                return self.graph.get_object(
                    f"{self._ig_user_id}/tags",
                    fields="id,caption,media_type,timestamp,username",
                    limit=10,
                )
            except facebook.GraphAPIError as exc:
                code = getattr(exc, "code", None)
                if code in (100, 200):
                    self.logger.debug("IG @mentions endpoint unavailable (permission not granted)")
                    return None
                raise   # re-raise for retry

        mentions_resp = self.recovery.retry(
            _fetch_mentions,
            config   = RetryConfig(max_attempts=2, base_delay=10, max_delay=60),
            label    = "FB.ig_mentions",
            fallback = None,
        )
        if not mentions_resp:
            return []

        for mention in mentions_resp.get("data", []):
            mention_id = mention.get("id", "")
            if mention_id in self.processed_ids:
                continue

            username  = mention.get("username", "unknown")
            caption   = mention.get("caption", "")
            timestamp = mention.get("timestamp", "")

            flagged.append({
                "id":               mention_id,
                "platform":         "instagram",
                "type":             "mention",
                "author":           f"@{username}",
                "text":             caption,
                "parent_id":        "",
                "parent_snippet":   "",
                "created_at":       timestamp,
                "matched_keywords": [],
                "is_vip":           False,
                "priority":         "medium",
                "url":              f"https://www.instagram.com/p/{mention_id}/",
            })

        return flagged

    # ── BaseWatcher interface ─────────────────────────────────────────────────

    def check_for_updates(self) -> list[dict]:
        """Check Facebook and Instagram for new actionable items."""
        items: list[dict] = []
        items.extend(self._check_facebook_comments())
        items.extend(self._check_facebook_messages())
        items.extend(self._check_instagram_comments())
        items.extend(self._check_instagram_mentions())
        return items

    def create_action_file(self, item: dict) -> Path:
        """
        Write a FACEBOOK_*.md or INSTAGRAM_*.md action file to /Needs_Action.
        Prefix is chosen by item['platform'].
        """
        if item["id"] in self.processed_ids:
            return Path()

        now      = datetime.now(timezone.utc)
        platform = item["platform"].upper()    # FACEBOOK | INSTAGRAM
        type_tag = item["type"].upper()        # COMMENT | MESSAGE | MENTION
        safe_author = re.sub(r"[^\w-]", "_", item["author"])[:40].strip("_")
        filename = (
            f"{platform}_{type_tag}_{safe_author}"
            f"_{now.strftime('%Y-%m-%d_%H%M%S')}.md"
        )
        filepath = self.needs_action / filename

        keywords_str = (
            ", ".join(item["matched_keywords"])
            if item["matched_keywords"]
            else "VIP contact" if item["is_vip"] else "direct message / mention"
        )

        # Parent post context block (only for comments)
        parent_block = ""
        if item.get("parent_snippet"):
            parent_block = (
                f"\n## Original Post\n"
                f"> {item['parent_snippet']}\n"
                f"Post ID: `{item['parent_id']}`\n"
            )

        safe_text = item["text"].replace('"', "'")

        content = f"""---
type: {item['platform']}_{item['type']}
platform: {item['platform']}
from: "{item['author']}"
preview: "{safe_text[:200]}"
post_url: "{item['url']}"
matched_keywords: [{keywords_str}]
is_vip: {str(item['is_vip']).lower()}
priority: {item['priority']}
platform_created_at: "{item['created_at']}"
detected: {now.isoformat()}
status: pending
---

# {platform} {type_tag}: {item['author']}

## Message
> {item['text']}

{parent_block}
## Context
- **Platform**: {item['platform'].capitalize()}
- **Type**: {item['type'].capitalize()}
- **Priority**: {item['priority'].upper()}
- **Triggered by**: {keywords_str}
- **Link**: {item['url']}

## Suggested Actions
- [ ] Review the full {item['type']} at [{item['url']}]({item['url']})
- [ ] Draft a response (write to /Pending_Approval if business-critical)
- [ ] Move to /Done when handled
"""
        filepath.write_text(content, encoding="utf-8")
        self.processed_ids.add(item["id"])

        self.logger.info(
            f"Action created: {filename} "
            f"({item['platform']} {item['type']} from {item['author']}, "
            f"keywords: {keywords_str})"
        )
        self.log_action(
            f"{item['platform']}_{item['type']}_flagged",
            {
                "platform":    item["platform"],
                "type":        item["type"],
                "author":      item["author"],
                "preview":     item["text"][:100],
                "keywords":    item["matched_keywords"],
                "is_vip":      item["is_vip"],
                "url":         item["url"],
                "action_file": str(filepath),
            },
        )
        return filepath


# ── Entry Point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    watcher = FacebookInstagramWatcher(vault_path=VAULT_PATH)
    watcher.run()
